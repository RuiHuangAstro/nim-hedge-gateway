"""nim-fusion mode.

Spec:
  1. Fire all fusion_tier models (4 different large models) at once.
  2. Each model runs its own lane: if a model hasn't returned a valid answer
     yet, re-fire it every fusion_retry_interval_seconds (default 60s) — proper
     per-model hedging, so multiple attempts of one model can be in flight. A
     model that has already answered validly is never re-fired.
  3. Stop the whole collection as soon as fusion_min_valid (default 2) *distinct*
     models have answered validly; cancel everything still running.
  4. Select — not synthesize — among the valid answers with a *hedged judge*:
     fire judge calls to the same tier's models concurrently and accept the
     FIRST judge that returns a valid pick. No scoring rubric, no fusion of
     text; the judge just chooses which existing answer to return.

Returns (winner, all_results) matching hedged_completion so main.py is agnostic.
Every valid candidate keeps its full `response` in all_results, so the request
recorder captures the multi-model dataset for offline evaluation.
"""
import asyncio
import json
import random
import time
from typing import Dict, List, Optional, Tuple

from app.models import ChatCompletionRequest, ChatCompletionMessage, CandidateResult
from app.config import VirtualModelStrategy, RawModel, config
from app.hedger import call_with_dynamic_key, classify_error, _record_validation_failure
from app.validators import validate_openai_chat_completion
from app.health import health_store


def _build_judge_request(
    original: ChatCompletionRequest,
    fusion_tier: str,
    candidates: List[CandidateResult],
    label_to_idx: Dict[str, int],
) -> ChatCompletionRequest:
    """Render the task tail + labeled candidate answers into a judge prompt."""
    convo = []
    for m in original.messages[-6:]:
        text = m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False, default=str)
        convo.append(f"<{m.role}>\n{text}")
    task = "\n\n".join(convo)
    if len(task) > 6000:
        task = "...(truncated)...\n" + task[-6000:]

    blocks = []
    for label, idx in label_to_idx.items():
        cand = candidates[idx]
        try:
            msg = cand.response.model_dump()["choices"][0]["message"]
            ans = msg.get("content") or ""
            if msg.get("tool_calls"):
                ans += f"\n[tool_calls: {json.dumps(msg['tool_calls'], ensure_ascii=False)}]"
        except Exception:
            ans = ""
        if len(ans) > 4000:
            ans = ans[:4000] + "\n...(truncated)..."
        blocks.append(f"### Answer {label}\n{ans}")
    answers_text = "\n\n".join(blocks)

    system = (
        "You are an impartial selector. Given a task and several candidate "
        "answers, choose the single best answer by correctness, completeness, "
        "and helpfulness. Do not write your own answer. Respond with ONLY a "
        'JSON object: {"best": "<LETTER>"}.'
    )
    user = f"## Task\n{task}\n\n## Candidate answers\n{answers_text}\n\nReturn the best letter."

    return ChatCompletionRequest(
        model=fusion_tier,
        messages=[
            ChatCompletionMessage(role="system", content=system),
            ChatCompletionMessage(role="user", content=user),
        ],
        temperature=0.0,
        max_tokens=200,
    )


def _parse_pick(content: Optional[str], valid_labels: List[str]) -> Optional[str]:
    if not content:
        return None
    text = content.strip()
    try:
        obj = json.loads(text[text.index("{"): text.rindex("}") + 1])
        letter = str(obj.get("best", "")).strip().upper()[:1]
        if letter in valid_labels:
            return letter
    except Exception:
        pass
    for ch in text.upper():
        if ch in valid_labels:
            return ch
    return None


async def _judge_select(
    request: ChatCompletionRequest,
    strategy: VirtualModelStrategy,
    virtual_model: str,
    candidates: List[CandidateResult],
    deadline: float,
) -> CandidateResult:
    """Hedged selection: fire judges concurrently, accept the first valid pick.
    Falls back to the first (fastest) candidate if every judge fails."""
    # Shuffle labels once so a fixed answer order can't bias every judge.
    order = list(range(len(candidates)))
    random.shuffle(order)
    labels = [chr(ord("A") + i) for i in range(len(order))]
    label_to_idx = {lbl: order[i] for i, lbl in enumerate(labels)}

    judge_req = _build_judge_request(request, strategy.fusion_tier, candidates, label_to_idx)
    judge_models: List[RawModel] = config.tiers.get(strategy.fusion_tier, [])
    per_call = strategy.per_call_timeout_seconds or strategy.hard_timeout_seconds

    tasks = [
        asyncio.create_task(
            call_with_dynamic_key(
                jm, judge_req, strategy.hard_timeout_seconds, per_call,
                0.0, virtual_model, False,
            )
        )
        for jm in judge_models
    ]
    try:
        pending = set(tasks)
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED, timeout=remaining,
            )
            if not done:
                break
            for task in done:
                try:
                    result, _key = await task
                except Exception:
                    continue
                if result.error or result.response is None:
                    continue
                try:
                    content = result.response.model_dump()["choices"][0]["message"].get("content")
                except Exception:
                    content = None
                letter = _parse_pick(content, labels)
                if letter is not None:
                    return candidates[label_to_idx[letter]]
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()

    # Every judge failed/abstained — keep it simple: take the fastest answer.
    return min(candidates, key=lambda c: c.latency_ms)


def _process_attempt(
    result: CandidateResult,
    used_key: Optional[str],
    request: ChatCompletionRequest,
    virtual_model: str,
    all_results: List[CandidateResult],
) -> bool:
    """Mark health/validation for one attempt and append it to all_results.
    Returns True iff this attempt is a valid answer."""
    all_results.append(result)
    if result.error:
        health_store.mark_failure(
            virtual_model, result.candidate_name,
            classify_error(result.status_code or 500, result.error or ""),
            result.latency_ms, result.error, api_alias=used_key,
        )
        return False
    # Fusion skips the repetition-loop check: the fan-out + judge already
    # filters degenerate answers, so we don't risk dropping a sole candidate.
    validation = validate_openai_chat_completion(
        result, tools_schema=request.tools, check_repetition=False,
    )
    if validation.ok:
        health_store.mark_success(
            virtual_model, result.candidate_name, result.latency_ms,
        )
        return True
    result.error = validation.reason
    _record_validation_failure(
        result, virtual_model, result.candidate_name,
        validation.reason, used_key=used_key,
    )
    return False


async def _model_lane(
    model_res: RawModel,
    request: ChatCompletionRequest,
    strategy: VirtualModelStrategy,
    virtual_model: str,
    deadline: float,
    all_results: List[CandidateResult],
) -> Optional[CandidateResult]:
    """One model's hedged lane: fire at t=0, then re-fire every
    fusion_retry_interval_seconds while no valid answer has come back. Multiple
    attempts may be in flight at once. Returns the first valid answer, or None
    if the deadline passes without one. Cancellation (the collection stopping)
    cleanly aborts any in-flight attempts via the finally block."""
    per_call = strategy.per_call_timeout_seconds or strategy.hard_timeout_seconds
    interval = max(1.0, strategy.fusion_retry_interval_seconds)
    inflight: set = set()
    next_fire = time.monotonic()
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_fire:
                inflight.add(asyncio.create_task(
                    call_with_dynamic_key(
                        model_res, request, strategy.hard_timeout_seconds,
                        per_call, 0.0, virtual_model, False,
                    )
                ))
                next_fire = now + interval

            wait_for = max(0.0, min(next_fire, deadline) - time.monotonic())
            if not inflight:
                await asyncio.sleep(wait_for)
                continue
            done, inflight = await asyncio.wait(
                inflight, timeout=wait_for, return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                try:
                    result, used_key = await task
                except Exception:
                    continue
                if _process_attempt(result, used_key, request, virtual_model, all_results):
                    return result
        return None
    finally:
        for t in inflight:
            if not t.done():
                t.cancel()


async def fusion_completion(
    request: ChatCompletionRequest,
    strategy: VirtualModelStrategy,
) -> Tuple[Optional[CandidateResult], List[CandidateResult]]:
    virtual_model = request.model
    deadline = time.monotonic() + strategy.hard_timeout_seconds
    models: List[RawModel] = config.tiers.get(strategy.fusion_tier, [])
    if not models:
        return None, []

    need = max(1, strategy.fusion_min_valid)
    all_results: List[CandidateResult] = []

    # One lane per distinct model. Each lane returns that model's first valid
    # answer (or None). The collection ends once `need` lanes have produced a
    # valid answer — i.e. `need` distinct models — then the rest are cancelled.
    lanes = [
        asyncio.create_task(
            _model_lane(m, request, strategy, virtual_model, deadline, all_results)
        )
        for m in models
    ]
    valid: List[CandidateResult] = []
    pending = set(lanes)
    try:
        while pending and len(valid) < need:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED, timeout=remaining,
            )
            if not done:
                break
            for lane in done:
                try:
                    res = await lane
                except Exception:
                    res = None
                if res is not None:
                    valid.append(res)
    finally:
        for lane in pending:
            if not lane.done():
                lane.cancel()

    if not valid:
        return None, all_results

    # Every valid answer reached the judge — mark them all as finalists so the
    # console shows fusion's "N in, 1 selected" shape, not a hedge cascade.
    for c in valid:
        c.is_finalist = True

    if len(valid) == 1:
        winner = valid[0]
    else:
        winner = await _judge_select(request, strategy, virtual_model, valid, deadline)

    winner.is_winner = True
    return winner, all_results
