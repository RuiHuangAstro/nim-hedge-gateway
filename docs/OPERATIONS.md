# Operational Guide

## Monitoring

### Ranking API
You can check which models are currently performing best:
```bash
curl http://127.0.0.1:8000/v1/hedge/ranking/nim-large -H "Authorization: Bearer local-test"
```
**Look for**:
- `score`: Higher is better.
- `p95_latency_ms`: Real-world speed.
- `success_rate`: Percentage of valid responses.

### Health State
View the raw counters for every candidate:
```bash
curl http://127.0.0.1:8000/v1/hedge/health -H "Authorization: Bearer local-test"
```

## Log Analysis
The gateway logs to `logs/requests.jsonl`. Each entry is a single JSON line.

`logs/`, `health_state.json`, and standalone `*.jsonl` analysis exports are runtime artifacts. Keep them out of commits unless you have manually redacted and intentionally published a small fixture.

### Key Fields to Watch:
- `winner`: Which candidate actually responded first.
- `latency_ms`: Total time user waited.
- `candidates_tried`: How many concurrent tasks were spawned.
- `candidate_errors`: Why specific backups failed (e.g., "504 Gateway Timeout").

## Hermes Integration
To use with the Hermes Agent:
1. Edit `~/.hermes/config.yaml`.
2. Add the `hedge` provider:
   ```yaml
   providers:
     hedge:
       base_url: http://127.0.0.1:8000/v1
       api_key: local-test
   ```
3. Add models to `fallback_providers` or set as default:
   ```yaml
   fallback_providers:
     - provider: hedge
       model: nim-large
   ```

## Troubleshooting

### "All candidates are currently in cooldown"
- **Cause**: All available keys for a pool hit 429 or the `max_concurrency_per_api` is too restrictive.
- **Fix**: Check `health_state.json` to see if keys are in cooldown. Lower concurrency if 429s are frequent.

### High Latency despite Hedging
- **Cause**: Primary models are returning very slowly but *successfully*, or the phase intervals are too long.
- **Fix**: Check P95 latency in the ranking API. Shorten `interval_seconds` in `config.yaml`.
