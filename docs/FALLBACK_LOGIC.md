# Fallback Logic Design

## Overview

NIM Hedge Gateway implements a sophisticated multi-tier fallback system that maximizes availability while minimizing cost. The fallback logic is designed to handle three types of failures:

1. **Rate Limits (429)** - API key exhaustion
2. **Server Errors (500/504)** - Upstream instability
3. **Timeouts** - Tail latency issues

## Core Fallback Strategy

### Phase-Based Sequential Execution

The gateway executes fallbacks in **phases**, not parallel. Each phase represents a tier of models:

```
Phase 1 (Large Tier) → Phase 2 (Medium Tier) → Phase 3 (Small Tier) → Paid Fallback
```

**Why Sequential?**
- Prevents wasteful parallel calls to expensive models
- Allows early termination when a valid response is found
- Enables dynamic health-based ranking within each phase

### Within-Phase Hedging

Within each phase, multiple candidates fire on a **staggered schedule**:

```
T+0s:   Candidate A (best health score)
T+45s:  Candidate B (second best)
T+90s:  Candidate C (third best)
T+135s: Candidate A (round-robin back to best)
```

**Key Parameters:**
- `interval_seconds`: How often to launch a new backup request (default: 45s)
- `hard_timeout_seconds`: Maximum time to wait for any response (default: 1500s)

### Paid Fallback Trigger

The paid fallback is triggered **once per request** when:

1. **All NIM API keys are in 429 cooldown** AND
2. **A paid_fallback is configured** AND
3. **The paid fallback hasn't been tried yet in this request**

**Behavior:**
- If paid fallback wins → return immediately
- If paid fallback fails → fall through to the current phase (a key may have come out of cooldown)

## Fallback Decision Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    REQUEST ARRIVES FOR nim-large                             │
└─────────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
              ┌──────────────────────────────┐
              │  GENERATE EXECUTION PLAN      │
              │  (3 phases, 1500s timeline)  │
              └──────────────┬───────────────┘
                             │
                             ▼
              ┌──────────────────────────────┐
              │  PHASE 1: LARGE TIER          │
              │  (T+0 to T+360, every 45s)    │
              └──────────────┬───────────────┘
                             │
                    ┌────────┴────────┐
                    │                 │
                    ▼                 ▼
          ┌──────────────┐   ┌──────────────┐
          │ ALL NIM KEYS │   │ LAUNCH       │
          │ IN 429 COOL?  │   │ CANDIDATES   │
          └──────┬───────┘   └──────┬───────┘
                 │                 │
           ┌─────┴─────┐           │
           │           │           │
          YES         NO          │
           │           │           │
           ▼           │           │
  ┌──────────────┐     │           │
  │ TRY PAID     │     │           │
  │ FALLBACK     │     │           │
  │ (ONCE)       │     │           │
  └──────┬───────┘     │           │
         │             │           │
    ┌────┴────┐        │           │
    │         │        │           │
   WIN       FAIL      │           │
    │         │        │           │
    │         └────────┴───────────┘
    │                    │
    │                    ▼
    │         ┌──────────────────────┐
    │         │  WAIT FOR FIRST      │
    │         │  VALID RESPONSE      │
    │         └──────────┬───────────┘
    │                    │
    │           ┌────────┴────────┐
    │           │                 │
    │           ▼                 ▼
    │     ┌──────────┐      ┌──────────┐
    │     │ WINNER   │      │ TIMEOUT  │
    │     │ FOUND    │      │ REACHED  │
    │     └────┬─────┘      └────┬─────┘
    │          │                 │
    │          │                 │
    │          └────────┬────────┘
    │                   │
    │                   ▼
    │         ┌──────────────────────┐
    │         │  CANCEL REMAINING    │
    │         │  TASKS               │
    │         └──────────┬───────────┘
    │                   │
    │                   │
    └───────────────────┤
                        │
                        ▼
              ┌──────────────────────┐
              │  RETURN WINNER       │
              │  (or continue to     │
              │   next phase)         │
              └──────────┬───────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │  PHASE 2: MEDIUM TIER │
              │  (T+360 to T+900)     │
              └──────────┬───────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │  PHASE 3: SMALL TIER │
              │  (T+900 to T+1500)   │
              └──────────┬───────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │  ALL PHASES FAILED   │
              │  → RAISE ERROR       │
              └──────────────────────┘
```

## Timeline Walkthrough: nim-large Real-World Scenarios

Based on actual `config.yaml` (`nim-large` strategy: Large 0-360s/40s, Medium 360-900s/50s, Small 900-1500s/60s, paid fallback ds-pro-paid).

### Scenario 1: Phase 1 Win (typical case)

```
        Timeline                          Event
        ────────                          ─────

        T+0s   ┌─────────────────────┐
               │  ds-pro  [in-flight] │  ← Slot 1: best health score
               └──────────┬──────────┘
                          │ (waiting...)
                          │
        T+40s  ┌──────────┴──────────┐
               │  glm5    [in-flight] │  ← Slot 2: 40s interval, 2nd best
               └──────────┬──────────┘
                          │ (ds-pro still waiting...)
                          │
        T+80s  ┌──────────┴──────────┐
               │  kimi    [in-flight] │  ← Slot 3: 80s interval, 3rd best
               └──────────┬──────────┘
                          │
        T+82s  ┌──────────┴──────────┐
               │  ds-pro  [✗ 429]    │  ← ds-pro returns 429 (rate limit)
               └─────────────────────┘
                          │
        T+85s  ┌─────────────────────┐
               │  glm5    [✗ 502]    │  ← glm5 returns 502 (upstream error)
               └─────────────────────┘
                          │
        T+98s  ┌─────────────────────┐
               │  kimi    [✓ WINNER] │  ★ First valid response! Cancel rest
               └─────────────────────┘

        T+120s   ds-pro  [cancelled]  ← Slot 4 cancelled (winner found)
        T+160s   glm5    [cancelled]  ← Slot 5 cancelled

        ─────────────────────────────────────────────────
        Result: x-hedge-winner=kimi, x-hedge-degraded=false
        Latency: 98s (kimi won)
```

### Scenario 2: Multi-Phase Fallback

When all Phase 1 candidates fail:

```
        Timeline                          Event
        ────────                          ─────

  ┌── Phase 1: Large Tier (0-360s, interval=40s) ──────────────────────────┐
  │                                                                          │
  │  T+0s    ds-pro  [✗ 429]                                                │
  │  T+40s   glm5    [✗ 502]                                                │
  │  T+80s   kimi    [✗ 504]                                                │
  │  T+120s  ds-pro  [✗ 429]                                                │
  │  T+160s  glm5    [✗ 502]                                                │
  │  T+200s  kimi    [✗ timeout]                                            │
  │  T+240s  ds-pro  [✗ 429]                                                │
  │  T+280s  glm5    [✗ 502]                                                │
  │  T+320s  kimi    [✗ 504]                                                │
  │                                                                          │
  │  → Phase 1 exhausted (9 attempts, 0 successes)                          │
  └──────────────────────────────────────────────────────────────────────────┘
        │
        ▼
  ┌── Phase 2: Medium Tier (360-900s, interval=50s) ────────────────────────┐
  │                                                                          │
  │  T+360s  qwen-397   [✗ timeout]                                         │
  │  T+410s  glm4-7     [✗ 502]                                             │
  │  T+460s  ds-flash   [✓ WINNER]  ★                                       │
  │                                                                          │
  │  → Phase 2 won! Cancel remaining tasks                                   │
  └──────────────────────────────────────────────────────────────────────────┘

        ─────────────────────────────────────────────────
        Result: x-hedge-winner=ds-flash, x-hedge-degraded=true
        Latency: ~460s (Phase 2 win)
        Note: Response marked degraded (non-primary tier)
```

### Scenario 3: Paid Fallback Trigger

When all NIM API keys are in 429 cooldown:

```
        Timeline                          Event
        ────────                          ─────

  ┌── Phase 1: Large Tier ───────────────────────────────────────────────────┐
  │                                                                          │
  │  T+0s    ds-pro  [✗ 429]  → NVIDIA_API_KEY_1 cooldown (600s)             │
  │  T+40s   glm5    [✗ 429]  → NVIDIA_API_KEY_2 cooldown (600s)             │
  │  T+80s   kimi    [✗ 429]  → NVIDIA_API_KEY_3 cooldown (600s)             │
  │  T+120s  ds-pro  [✗ 429]  → NVIDIA_API_KEY_4 cooldown (600s)             │
  │                                                                          │
  │  → ALL NIM keys in cooldown! Trigger paid fallback                        │
  └──────────────────────────────────────────────────────────────────────────┘
        │
        ▼
  ┌── Paid Fallback ─────────────────────────────────────────────────────────┐
  │                                                                          │
  │  T+130s  ds-pro-paid  [in-flight...]                                     │
  │                                                                          │
  │  If paid fallback wins:                                                  │
  │    → Return immediately, x-hedge-degraded=true                           │
  │                                                                          │
  │  If paid fallback fails:                                                 │
  │    → Fall through to current phase (some keys may have left cooldown)    │
  │    → Continue Phase 1 or Phase 2                                         │
  └──────────────────────────────────────────────────────────────────────────┘
```

## Health-Based Ranking

Within each phase, candidates are ranked by their **real-time health score**:

```python
score = smooth_success_rate
        - 0.6 * timeout_rate
        - 0.4 * server_error_rate
        - 0.3 * latency_penalty
        - content_penalty
```

**Components:**
- `smooth_success_rate`: Bayesian-smoothed success rate (prevents volatility for new models)
- `timeout_rate`: Frequency of timeouts
- `server_error_rate`: Frequency of 500/504 errors
- `latency_penalty`: P95 latency penalty (capped at 0.5)
- `content_penalty`: Penalty for malformed responses (harmony unparsed, tool call inference)

**Ranking Strategy:**
- Round-robin assignment: Best → Second Best → Third Best → Best...
- Ensures all healthy models get a chance
- Prevents a single model from monopolizing slots

## Concurrency Protection

To prevent a single slow model from blocking the entire pipeline:

```python
if active_count >= max_concurrency_per_api:
    skip this candidate
    move to next in ranking
```

**Default limit:** 5 concurrent requests per API key

## Degradation Detection

The gateway flags responses as **degraded** when:

- The winning candidate comes from a phase where the `tier` is different from the first phase's tier
- A custom header `x-hedge-degraded: true` is attached to the response

**Use Case:** Clients can detect if they received a high-quality answer (from the primary tier) or a fallback answer (from a secondary tier).

## Error Classification

| Status Code | Classification | Action |
|-------------|----------------|--------|
| 429 | `rate_limit` | Put API key in cooldown (default: 300s) |
| 500 | `server_error` | Mark failure, no cooldown |
| 504 | `timeout` | Mark failure, no cooldown |
| Other | `invalid_response` | Mark failure, no cooldown |

**Cooldown Policy:**
- Only 429 rate limits trigger hard cooldowns
- 500/504 errors and timeouts do NOT trigger cooldowns (zero-cooldown persistence)
- This ensures maximum availability even during upstream instability

## Configuration Example (actual config.yaml)

```yaml
tiers:
  large:
    - { name: "ds-pro", model: "deepseek-ai/deepseek-v4-pro" }
    - { name: "glm5", model: "z-ai/glm-5.1" }
    - { name: "kimi", model: "moonshotai/kimi-k2.6" }
  medium:
    - { name: "qwen-397", model: "qwen/qwen3.5-397b-a17b" }
    - { name: "glm4-7", model: "z-ai/glm4.7" }
    - { name: "ds-flash", model: "deepseek-ai/deepseek-v4-flash" }
    - { name: "minimax-m2.7", model: "minimaxai/minimax-m2.7" }
  small:
    - { name: "qwen-122", model: "qwen/qwen3.5-122b-a10b" }
    - { name: "nemotron", model: "nvidia/nemotron-3-super-120b-a12b" }
    - { name: "gpt-oss", model: "openai/gpt-oss-120b" }

virtual_models:
  nim-large:
    hard_timeout_seconds: 1500
    phases:
      - { tier: "large", start_seconds: 0, end_seconds: 360, interval_seconds: 40 }
      - { tier: "medium", start_seconds: 360, end_seconds: 900, interval_seconds: 50 }
      - { tier: "small", start_seconds: 900, end_seconds: 1500, interval_seconds: 60 }
    paid_fallback:
      name: "ds-pro-paid"
      api_base: "https://api.deepseek.com/v1"
      model: "deepseek-v4-pro"
      api_key_env: "DEEPSEEK_API_KEY"
      timeout_seconds: 300
```

## Key Design Principles

1. **Progressive Hedging**: Aggressive hedging within a tier, seamless fallback to other tiers
2. **Zero-Cooldown Persistence**: Keep trying during 502/504 errors, only hard cooldown for 429s
3. **Just-In-Time Planning**: Generate unique execution plans per request based on real-time health
4. **First-Valid-Response Wins**: Cancel remaining tasks immediately when a winner is found
5. **Health-Based Ranking**: Use real-time scores to prioritize healthy models
