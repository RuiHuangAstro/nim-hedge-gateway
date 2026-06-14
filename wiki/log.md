# Wiki Log

> Chronological record of all wiki actions.
> Format: `## [YYYY-MM-DD] action | subject`

## [2026-06-03] update | Response validation: repetition-loop detection + glm5 garbage-response analysis

- Added `_detect_repetition_loop()` to `app/validators.py`: rejects responses where the model entered a degeneration loop and kept repeating the same pattern (e.g. `adorns:0.20000, and:0.20000, and:0.20000...` or `3.3.3.3.3.3.3`) with `finish_reason="stop"`. Two detection modes: medium ngrams (5–59 chars) repeated ≥4×, or short non-space ngrams (2–4 chars) repeated ≥5× and covering >50% of content.
- Added `REPETITION_LOOP_REASON_PREFIX` sentinel; hedger now routes these into a new `repetition_loop` archive category and `repetition_loop` health event type (distinct from `invalid_response`).
- Diagnosed glm5 garbage-short responses: glm5 is producing ≤6-token plain-text responses (e.g. `飞燕回家`) that pass validation because content is non-empty and finish_reason is `"stop"`. ~120 occurrences in 3 days. Root cause: glm5 model quality collapse on certain prompt shapes. Minimum-token threshold fix pending design decision.
- Updated `concepts/nim-health-cooldown-system.md` to document the new `repetition_loop` event type and archive category.

## [2026-05-31] update | Token-bucket routing and 429 pre-request delay
- Rewrote concepts/nim-health-cooldown-system.md for the current no-hard-cooldown design
- Documented app/router.py key selection, optional token buckets, queue behavior, and /v1/hedge/key_stats
- Updated queries/how-to-configure-nim-proxy.md for current server and health schema
- Updated entities/nim-hedge-gateway.md and index.md to remove stale paid-fallback/cooldown wording
- Validation observed during update: `.venv/bin/python -m pytest -q` passed with 59 tests

## [2026-05-17] update | GitHub publication readiness
- Added queries/how-to-publish-to-github.md with public/private file checklist and first-push commands
- Updated index.md to include the GitHub publication page
- README and docs now describe example config, local-only files, and runtime artifacts
- Refreshed how-to-configure-nim-proxy.md for the current shared `server.api_key_envs` schema
- Updated entity/concept metadata and removed stale per-model NIM `api_key_env` examples

## [2026-05-08] create | NIM Proxy Wiki initialized
- Domain: NIM Hedge Gateway — local LLM hedging gateway for improving reliability and tail-latency
- Structure created with SCHEMA.md, index.md, log.md
- Created 4 wiki pages:
  - entities/nim-hedge-gateway.md — NIM Hedge Gateway 项目实体
  - concepts/nim-hedging-strategy.md — NIM 对冲策略概念
  - concepts/nim-health-cooldown-system.md — NIM 健康冷却系统概念
  - queries/how-to-configure-nim-proxy.md — NIM Proxy 配置指南
- Updated index.md: Total pages: 4
