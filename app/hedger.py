import asyncio
import re
import time
import math
import os
from typing import List, Optional, Tuple, Dict, Any
from app.models import ChatCompletionRequest, CandidateResult, ChatCompletionMessage
from app.config import VirtualModelStrategy, RawModel, config
from app.providers import call_litellm_candidate
from app.validators import validate_openai_chat_completion, HARMONY_UNPARSED_REASON_PREFIX, REPETITION_LOOP_REASON_PREFIX
from app.health import health_store
from app.response_archive import archive as archive_response
from app.router import router_state

async def call_with_dynamic_key(
    model_res: RawModel,
    request: ChatCompletionRequest,
    timeout_seconds: float,
    per_call_timeout: float,
    delay: float,
    virtual_model: str,
    is_degraded: bool
) -> Tuple[CandidateResult, Optional[str]]:
    if delay > 0:
        await asyncio.sleep(delay)

    best_key = router_state.select_key()

    if best_key is None and config.server.queue_when_limited and config.server.max_queue_seconds > 0:
        best_key = await router_state.wait_for_key(config.server.max_queue_seconds)

    if best_key is None:
        key_status = ", ".join(
            f"{k}=active:{health_store.get_active_count(k)},tokens:{router_state.tokens_available(k):.1f}"
            for k in config.server.api_key_envs
        )
        print(f"\n[!] SELF-THROTTLE: No API key has rpm token budget left. ({key_status})")
        return (CandidateResult(
            candidate_name=model_res.name,
            real_model=model_res.model,
            response=None,
            latency_ms=0,
            error="Self-throttle: all keys out of rpm token budget",
            status_code=429,
            degraded=is_degraded
        ), None)

    router_state.consume_token(best_key)

    pre_delay = health_store.get_api_pre_request_delay(best_key)
    if pre_delay > 0:
        print(f"[~] PRE-REQUEST DELAY {pre_delay:.0f}s on {best_key} (429 backoff)")
        await asyncio.sleep(pre_delay)

    model_with_key = model_res.model_copy()
    model_with_key.api_key_env = best_key

    call_timeout = per_call_timeout if per_call_timeout > 0 else timeout_seconds

    health_store.increment_active(best_key)
    try:
        result = await call_litellm_candidate(model_with_key, request, call_timeout)
        result.degraded = is_degraded
        return (result, best_key)
    finally:
        health_store.decrement_active(best_key)

def classify_error(status_code: int, error_msg: str = "") -> str:
    if status_code == 429:
        if not error_msg.startswith("Self-throttle:"):
            print(f"\n[!] WARNING: Upstream 429 Rate Limit hit. (will back off via 2^N pre-request delay)\n")
        return "rate_limit"
    if status_code >= 500: return "server_error"
    if status_code == 504: return "timeout"
    return "invalid_response"


def _record_validation_failure(
    result: CandidateResult,
    virtual_model: str,
    candidate_name: str,
    reason: Optional[str],
    used_key: Optional[str],
) -> None:
    """Mark health + archive when a candidate's response failed validation.

    Splits the harmony-unparsed sub-failure off into its own event_type and
    archive category so we can rank these candidates lower than generic
    invalid_response failures (kimi-k2.6's truncated harmony output is a
    persistent enough mode that we want explicit signal in the score).
    """
    is_harmony_unparsed = bool(reason) and reason.startswith(HARMONY_UNPARSED_REASON_PREFIX)
    is_repetition_loop = bool(reason) and reason.startswith(REPETITION_LOOP_REASON_PREFIX)
    if is_harmony_unparsed:
        event_type = "content_unparsed"
        archive_category = "harmony_unparsed"
    elif is_repetition_loop:
        event_type = "repetition_loop"
        archive_category = "repetition_loop"
    else:
        event_type = "invalid_response"
        archive_category = "validation_failed"

    health_store.mark_failure(
        virtual_model, candidate_name,
        event_type, result.latency_ms,
        reason or "Validation failed",
        api_alias=used_key,
    )
    try:
        failed_dict = result.response.model_dump() if result.response else None
    except Exception:
        failed_dict = None
    raw_content = None
    if failed_dict:
        try:
            raw_content = failed_dict["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError):
            raw_content = None
    archive_response(
        category=archive_category,
        virtual_model=virtual_model,
        candidate_name=candidate_name,
        real_model=result.real_model,
        response_dict=failed_dict if not (is_harmony_unparsed or is_repetition_loop) else None,
        raw_content=raw_content if (is_harmony_unparsed or is_repetition_loop) else None,
        extra={"reason": reason},
    )


async def hedged_completion(
    request: ChatCompletionRequest,
    strategy: VirtualModelStrategy,
) -> Tuple[Optional[CandidateResult], List[CandidateResult]]:
    """Run all phases as a single interleaved schedule.

    Each phase contributes slots with *global* delays (seconds from request
    start = phase.start_seconds + i * effective_interval). All slots across
    all phases are created upfront; they internally sleep until their fire
    time. This means phase 2 slots start firing at phase.start_seconds
    regardless of whether phase 1 candidates have completed — the phases are
    time windows, not sequential barriers.

    First slot (from any phase) that passes validation wins and cancels the
    rest. Slots from phase 0 have degraded=False; later phases have
    degraded=True.
    """
    virtual_model = request.model
    overall_deadline = time.monotonic() + strategy.hard_timeout_seconds

    api_count = len(config.server.api_key_envs)
    total_active = sum(health_store.get_active_count(k) for k in config.server.api_key_envs)

    # Build a flat plan: (model, global_delay, is_degraded) sorted by fire time.
    plan: List[Tuple[RawModel, float, bool]] = []
    for phase_idx, phase in enumerate(strategy.phases):
        tier_models = config.tiers.get(phase.tier, [])
        if not tier_models:
            continue

        if config.ranking.is_enabled_for_tier(phase.tier):
            sorted_tier_models = sorted(
                tier_models,
                key=lambda x: health_store.get_candidate_health(virtual_model, x.name, x.model).score,
                reverse=True,
            )
        else:
            sorted_tier_models = list(tier_models)

        phase_duration = phase.end_seconds - phase.start_seconds
        effective_interval = max(0.01, min(90.0, phase.interval_seconds - api_count + total_active))
        num_slots = max(1, math.ceil(phase_duration / effective_interval))
        is_degraded = (phase_idx > 0)

        for i in range(num_slots):
            global_delay = phase.start_seconds + i * effective_interval
            if global_delay >= strategy.hard_timeout_seconds:
                break
            model_to_use = sorted_tier_models[i % len(sorted_tier_models)]
            plan.append((model_to_use, global_delay, is_degraded))

    plan.sort(key=lambda x: x[1])

    tasks: List[asyncio.Task] = []
    task_to_index: Dict[asyncio.Task, int] = {}
    request_start = time.monotonic()

    for i, (model_res, delay, deg) in enumerate(plan):
        task = asyncio.create_task(
            call_with_dynamic_key(
                model_res, request, strategy.hard_timeout_seconds,
                strategy.per_call_timeout_seconds, delay, virtual_model, deg,
            )
        )
        tasks.append(task)
        task_to_index[task] = i

    done_results_map: Dict[int, CandidateResult] = {}
    winner: Optional[CandidateResult] = None
    winner_index = -1

    try:
        pending: set = set(tasks)
        while pending and winner is None:
            remaining = overall_deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED, timeout=remaining,
            )
            if not done:
                break
            for task in done:
                idx = task_to_index[task]
                try:
                    result, used_key = await task
                    done_results_map[idx] = result
                    if result.error:
                        health_store.mark_failure(
                            virtual_model, result.candidate_name,
                            classify_error(result.status_code or 500, result.error or ""),
                            result.latency_ms, result.error,
                            api_alias=used_key,
                        )
                        continue
                    validation = validate_openai_chat_completion(result, tools_schema=request.tools)
                    if validation.ok:
                        health_store.mark_success(
                            virtual_model, result.candidate_name, result.latency_ms,
                        )
                        winner = result
                        winner_index = idx
                        break
                    result.error = validation.reason
                    _record_validation_failure(
                        result, virtual_model, result.candidate_name,
                        validation.reason, used_key=used_key,
                    )
                except Exception as e:
                    print(f"Unexpected error in task: {e}")

        elapsed = time.monotonic() - request_start
        results: List[CandidateResult] = []
        for i, (model_res, delay, deg) in enumerate(plan):
            if delay > elapsed and i != winner_index:
                continue
            existing = done_results_map.get(i)
            if existing:
                if i == winner_index:
                    existing.is_winner = True
                results.append(existing)
            else:
                results.append(CandidateResult(
                    candidate_name=model_res.name,
                    real_model=model_res.model,
                    response=None,
                    latency_ms=int(max(0, elapsed - delay) * 1000),
                    error="Pending/Cancelled",
                    status_code=None,
                    degraded=deg,
                ))
        return winner, results
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
