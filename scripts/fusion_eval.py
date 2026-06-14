#!/usr/bin/env python3
"""Offline fusion evaluation — Step 1 of the fusion plan.

Answers one question on *real captured traffic*: does selecting the best of N
diverse models per request beat always using the single best model?

Input: logs/fusion_dataset.jsonl produced by app/request_recorder.py — each line
is {request, candidates:[{name, real_model, valid, response, ...}]}. nim-fusion
traffic naturally yields several answers per prompt, which is what this needs.

Judge: a held-out strongest NIM model (NOT one of the fused candidates, to
avoid self-preference). For each record with >=2 valid candidate answers, the
judge picks the single best answer. We then measure:

  * win concentration — how often each model is picked. If one model wins almost
    always, fusion ~= that single model and is NOT worth the latency/complexity.
    If wins are spread across models per-prompt, fusion has real headroom.
  * best-single baseline — the model picked most often overall.
  * fusion uplift ceiling — fraction of prompts where the per-prompt winner is
    NOT the overall best-single model (the share of traffic where fusion could
    actually improve on just always calling the best single model).

Note on circularity: the judge is held out of the candidate set, so it never
scores its own output. The "uplift ceiling" assumes a perfect judge; the
deployed nim-fusion judge is itself hedged and imperfect, so real uplift <=
ceiling. A near-zero ceiling is therefore a strong signal NOT to ship fusion.

Usage:
  python scripts/fusion_eval.py \
      --dataset logs/fusion_dataset.jsonl \
      --judge-model nvidia/nemotron-3-ultra-550b-a55b \
      --judge-key-env NVIDIA_API_KEY_1 \
      --limit 200
"""
import argparse
import asyncio
import json
import os
import random
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

# Allow running from repo root without install.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import litellm  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv()
litellm.set_verbose = False

JUDGE_API_BASE = "https://integrate.api.nvidia.com/v1"
MAX_PROMPT_CHARS = 6000
MAX_ANSWER_CHARS = 4000


def _msg_text(m: Dict[str, Any]) -> str:
    content = m.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict)]
        return " ".join(parts)
    return ""


def render_prompt(request: Dict[str, Any]) -> str:
    """Render the conversation into a compact string for the judge. Keeps the
    tail of the conversation (where the actual task usually is) under a cap."""
    msgs = request.get("messages", [])
    rendered = []
    for m in msgs:
        role = m.get("role", "?")
        text = _msg_text(m)
        if m.get("tool_calls"):
            text += f" [tool_calls: {json.dumps(m['tool_calls'], ensure_ascii=False)[:500]}]"
        rendered.append(f"<{role}>\n{text}")
    full = "\n\n".join(rendered)
    if len(full) > MAX_PROMPT_CHARS:
        full = "...(truncated)...\n" + full[-MAX_PROMPT_CHARS:]
    return full


def render_answer(response: Optional[Dict[str, Any]]) -> str:
    if not response:
        return ""
    try:
        choice = response["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    text = msg.get("content") or ""
    if msg.get("tool_calls"):
        text += f"\n[tool_calls: {json.dumps(msg['tool_calls'], ensure_ascii=False)}]"
    if len(text) > MAX_ANSWER_CHARS:
        text = text[:MAX_ANSWER_CHARS] + "\n...(truncated)..."
    return text


def valid_candidates(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for c in record.get("candidates", []):
        if c.get("valid") and c.get("response"):
            out.append(c)
    return out


async def judge_pick(
    prompt: str,
    answers: List[Tuple[str, str]],  # (model_name, rendered_answer)
    judge_model: str,
    judge_key: str,
    timeout: float,
) -> Optional[str]:
    """Ask the judge to pick the single best answer. Answers are presented with
    shuffled anonymous labels (A, B, ...) to remove name/position bias. Returns
    the real model_name of the picked answer, or None on failure."""
    labeled = list(answers)
    random.shuffle(labeled)
    labels = [chr(ord("A") + i) for i in range(len(labeled))]
    label_to_model = {lbl: name for lbl, (name, _) in zip(labels, labeled)}

    blocks = []
    for lbl, (_, ans) in zip(labels, labeled):
        blocks.append(f"### Answer {lbl}\n{ans}")
    answers_text = "\n\n".join(blocks)

    system = (
        "You are an impartial evaluator. Given a task and several candidate "
        "answers, pick the single best answer by correctness, completeness, and "
        "helpfulness. Respond with ONLY a JSON object: "
        '{"best": "<LETTER>", "reason": "<short>"}.'
    )
    user = f"## Task\n{prompt}\n\n## Candidate answers\n{answers_text}\n\nPick the best."

    try:
        resp = await asyncio.wait_for(
            litellm.acompletion(
                model=f"openai/{judge_model}",
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
                max_tokens=200,
                api_base=JUDGE_API_BASE,
                api_key=judge_key,
                timeout=timeout,
            ),
            timeout=timeout,
        )
        text = resp["choices"][0]["message"]["content"] or ""
    except Exception as e:
        print(f"  [judge error] {e}", file=sys.stderr)
        return None

    # Extract the chosen letter.
    letter = None
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        obj = json.loads(text[start:end])
        letter = str(obj.get("best", "")).strip().upper()[:1]
    except Exception:
        for ch in text.upper():
            if ch in label_to_model:
                letter = ch
                break
    return label_to_model.get(letter)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="logs/fusion_dataset.jsonl")
    ap.add_argument("--judge-model", default="nvidia/nemotron-3-ultra-550b-a55b",
                    help="held-out strongest NIM model; must NOT be a fused candidate")
    ap.add_argument("--judge-key-env", default="NVIDIA_API_KEY_1")
    ap.add_argument("--limit", type=int, default=0, help="max records to judge (0 = all)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    judge_key = os.environ.get(args.judge_key_env)
    if not judge_key:
        print(f"ERROR: {args.judge_key_env} not set in environment/.env", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(args.dataset):
        print(f"ERROR: dataset not found: {args.dataset}\n"
              f"Enable `record` in config.yaml and send nim-fusion traffic first.",
              file=sys.stderr)
        sys.exit(1)

    records: List[Dict[str, Any]] = []
    with open(args.dataset, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue

    total = len(records)
    usable = [r for r in records if len(valid_candidates(r)) >= 2 and r.get("request", {}).get("messages")]
    # Warn if the judge model also appears as a candidate (self-preference risk).
    cand_models = set()
    for r in records:
        for c in r.get("candidates", []):
            cand_models.add(c.get("real_model"))
    if args.judge_model in cand_models:
        print(f"WARNING: judge model {args.judge_model} is also a fused candidate — "
              f"self-preference will bias results. Pick a held-out model.\n", file=sys.stderr)

    if args.limit:
        usable = usable[: args.limit]

    print(f"Dataset: {total} records | usable (>=2 valid answers): {len(usable)} | judging: {len(usable)}")
    print(f"Judge: {args.judge_model}\n")

    sem = asyncio.Semaphore(args.concurrency)
    win_counter: Counter = Counter()
    appear_counter: Counter = Counter()
    valid_counter: Counter = Counter()
    judged = 0
    judge_failures = 0

    # Track per-record winner to compute uplift ceiling after best-single is known.
    record_winners: List[Optional[str]] = []

    for r in records:
        for c in r.get("candidates", []):
            appear_counter[c.get("real_model")] += 1
            if c.get("valid"):
                valid_counter[c.get("real_model")] += 1

    async def judge_one(r: Dict[str, Any]) -> Optional[str]:
        async with sem:
            cands = valid_candidates(r)
            prompt = render_prompt(r["request"])
            answers = [(c["real_model"], render_answer(c["response"])) for c in cands]
            return await judge_pick(prompt, answers, args.judge_model, judge_key, args.timeout)

    results = await asyncio.gather(*[judge_one(r) for r in usable])
    for picked in results:
        judged += 1
        if picked is None:
            judge_failures += 1
            record_winners.append(None)
            continue
        win_counter[picked] += 1
        record_winners.append(picked)

    decided = [w for w in record_winners if w is not None]
    print("=" * 64)
    print("PER-MODEL (over all records)")
    print(f"  {'model':40} {'appear':>7} {'valid':>7} {'wins':>6} {'win%':>6}")
    all_models = sorted(appear_counter, key=lambda m: -win_counter[m])
    for m in all_models:
        wins = win_counter[m]
        winpct = (100.0 * wins / len(decided)) if decided else 0.0
        print(f"  {str(m):40} {appear_counter[m]:>7} {valid_counter[m]:>7} {wins:>6} {winpct:>5.1f}%")

    print("\n" + "=" * 64)
    print("VERDICT")
    if not decided:
        print("  No records could be judged. Need more captured data.")
        return
    best_single, best_wins = win_counter.most_common(1)[0]
    best_share = 100.0 * best_wins / len(decided)
    uplift = 100.0 * (len(decided) - best_wins) / len(decided)
    print(f"  judged: {len(usable)} | decided: {len(decided)} | judge failures: {judge_failures}")
    print(f"  best single model: {best_single}  (wins {best_share:.1f}% of decided prompts)")
    print(f"  fusion uplift ceiling: {uplift:.1f}% of prompts a non-best model won")
    print()
    if uplift < 10:
        print("  => LOW headroom. One model dominates; fusion likely NOT worth the")
        print("     extra latency + judge complexity. Prefer routing to the best single model.")
    elif uplift < 25:
        print("  => MODERATE headroom. Fusion may help on a meaningful minority of")
        print("     prompts; worth a careful A/B against best-single before committing.")
    else:
        print("  => HIGH headroom. Different models win different prompts — selection")
        print("     across diverse models has real quality value. Fusion is justified.")
    print("\n  Caveat: ceiling assumes a perfect judge. The deployed nim-fusion judge")
    print("  is hedged + imperfect, so realized uplift <= this ceiling.")


if __name__ == "__main__":
    asyncio.run(main())
