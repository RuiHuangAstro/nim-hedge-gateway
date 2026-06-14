---
title: NIM Health and Rate Control System
created: 2026-05-08
updated: 2026-06-03
type: concept
tags: [health, cooldown, scoring, monitoring, routing]
sources: [../../app/health.py, ../../app/router.py, ../../app/hedger.py, ../../app/main.py, ../../config.example.yaml]
confidence: high
---

# NIM Health and Rate Control System

This page keeps the historical `cooldown` filename, but the current system no longer uses hard API-key cooldowns. Rate pressure is now handled by token-bucket key routing plus a per-key 2^N pre-request delay after upstream 429s.

## Current Design

The current rate-control stack has three parts:

1. `app/router.py`: selects API keys with round-robin tie-breaking and optional per-key RPM token buckets.
2. `app/health.py`: records candidate health events and recent per-key 429 timestamps.
3. `app/hedger.py`: asks the router for a key, consumes a token, applies any pre-request delay, and calls the upstream model.

There is no `max_concurrency_per_api` gate, no `api_cooldown_until`, and no paid fallback path in the current hedger. Active request count is retained for observability and phase spacing only.

## Key Routing

`RouterState.select_key()` walks `server.api_key_envs` in round-robin order. A key is skipped only when:

- `server.rpm_limit_per_api > 0`
- that key's token bucket has less than 1 token
- `server.allow_best_effort_when_all_limited` is false

If every key is out of token budget, the hedger either waits with `RouterState.wait_for_key()` when `server.queue_when_limited` is enabled, or returns a synthetic 429 self-throttle result.

Relevant config:

```yaml
server:
  api_key_envs:
    - "NVIDIA_API_KEY_1"
    - "NVIDIA_API_KEY_2"
    - "NVIDIA_API_KEY_3"
  rpm_limit_per_api: 0
  burst_per_api: 0
  queue_when_limited: false
  max_queue_seconds: 20.0
  allow_best_effort_when_all_limited: false
```

`rpm_limit_per_api: 0` means unlimited token budget. `burst_per_api: 0` defaults the burst capacity to the RPM limit when RPM limiting is enabled.

## 429 Backoff

Upstream 429s do not remove a key from rotation. Instead, `HealthStore.mark_api_429()` stores the timestamp for that key. Before the next upstream request on that key, `HealthStore.get_api_pre_request_delay()` computes:

```python
delay_seconds = min(2 ** recent_429_count, max_seconds)
```

Only 429s inside `health.pre_request_delay.window_seconds` count. Quiet keys naturally decay back to zero delay once their old 429 timestamps fall out of the window.

Relevant config:

```yaml
health:
  pre_request_delay:
    enabled: true
    window_seconds: 300
    max_seconds: 256
  max_recent_events: 200
  persistence_file: "health_state.json"
```

The 429 timestamp window is in memory only. Restarting the proxy clears it. Candidate health state is still persisted in `health_state.json`.

## Health Scoring

Candidate scoring remains model/candidate oriented, not API-key oriented. Scores are stored by `(virtual_model, candidate_name)` and influence tier ordering when dynamic ranking is enabled.

Current score formula:

```python
score = (
    smooth_success_rate
    - 0.6 * timeout_rate
    - 0.4 * server_error_rate
    - 0.3 * latency_penalty
    - content_penalty
)
```

Important event semantics:

- `success`: valid response, full success credit.
- `success_inferred`: usable response, but tool name had to be inferred from schema; small content penalty.
- `content_unparsed`: harmony markers existed but no tool call was extractable; not a success and gets an extra penalty.
- `repetition_loop`: plain-text content detected as a model degeneration loop (same ngram repeated many times); archived under `repetition_loop` category. Treated as `invalid_response` for scoring.
- `invalid_response`: generic validation failure.
- `rate_limit`: upstream or synthetic 429; records a per-key 429 only when the failing call had a concrete API-key alias.
- `timeout` and `server_error`: penalize candidate score but do not cool a key.

## Response Validation

`app/validators.py` gates every candidate response before it can become the hedged winner. Checks run in order:

1. **Structure**: response object, non-empty `choices`, message present.
2. **Content or tool_calls**: at least one must be non-empty; empty content + no tool calls → reject.
3. **Tool call JSON**: if `tool_calls` present, each must have a name and valid JSON arguments.
4. **Harmony markers**: if plain-text content contains `<|tool_call_begin|>`-style tokens but the parser cannot extract any call, reject as `harmony_unparsed`.
5. **Truncation**: `finish_reason == "length"` → reject (response was cut off mid-generation).
6. **Repetition loop**: plain-text content (no harmony markers, finish_reason ≠ "length") is checked for degeneration loops. Two detection modes:
   - Medium ngrams (5–59 chars) that appear ≥4 times → reject.
   - Short non-space ngrams (2–4 chars) that appear ≥5 times **and** cover >50% of the content → reject.
   - Catches patterns like `adorns:0.20000, and:0.20000, and:0.20000…` or `3.3.3.3.3.3.3.3`.

Responses that fail steps 4 or 6 are archived (categories `harmony_unparsed` / `repetition_loop`) with the raw content for post-hoc inspection.

### Known Gap: glm5 Garbage-Short Responses

glm5 occasionally returns 3–6 tokens of nonsense Chinese text (e.g. `飞燕回家`) with `finish_reason="stop"`. These pass current validation because content is non-empty and not truncated. A minimum-token-threshold check would catch these but risks rejecting legitimately short answers; the fix is pending a design decision.

## Observability

The current HTTP endpoints are:

```bash
curl http://127.0.0.1:8000/v1/hedge/key_stats \
  -H "Authorization: Bearer local-test"

curl http://127.0.0.1:8000/v1/hedge/health \
  -H "Authorization: Bearer local-test"

curl http://127.0.0.1:8000/v1/hedge/ranking/nim-large \
  -H "Authorization: Bearer local-test"
```

`/v1/hedge/key_stats` reports each configured key alias, active count, token-bucket remainder, RPM limit, recent 429 count, and current pre-request delay.

## Operational Notes

- If upstream 429s are frequent, tune `rpm_limit_per_api`, `burst_per_api`, and `health.pre_request_delay.max_seconds`; do not look for `cooldown_seconds`.
- If all buckets empty, enable `queue_when_limited` to wait briefly instead of returning a synthetic 429.
- If low latency matters more than strict RPM compliance, `allow_best_effort_when_all_limited: true` lets the router pick a key even when its bucket is empty.
- Paid fallback has been removed from the current config and tests; any local `paid_fallback` block is now stale.

## Related Pages

- [[nim-hedge-gateway]] — project entity and API surface
- [[nim-hedging-strategy]] — hedged phase execution and fallback tiers
- [[how-to-configure-nim-proxy]] — current config schema
