# Configuration Guide

Start from the public template:

```bash
cp .env.example .env
cp config.example.yaml config.yaml
```

Keep `.env` and `config.yaml` local. They are ignored by Git because they may contain real keys, private model routing choices, or local auth tokens.

## `config.yaml` Structure

### 1. `server`

- `host`, `port`: bind address for the FastAPI service.
- `request_api_key`: optional bearer token required from clients.
- `max_concurrency_per_api`: maximum concurrent upstream calls per API key.
- `api_key_envs`: environment variable names used as the shared NIM key pool.

### 2. `tiers`

Define physical model groups. Each model needs a stable `name` and provider `model` path. NIM models use the shared `server.api_key_envs` pool.

```yaml
tiers:
  large:
    - name: "kimi"
      model: "moonshotai/kimi-k2.6"
```

### 3. `virtual_models`

Virtual models are the names exposed through `/v1/models` and `/v1/chat/completions`.

```yaml
virtual_models:
  nim-large:
    hard_timeout_seconds: 1500
    phases:
      - tier: "large"
        start_seconds: 0
        end_seconds: 360
        interval_seconds: 45
```

- `tier`: which resource group to use.
- `start_seconds`: when this phase begins.
- `end_seconds`: when this phase stops adding new requests.
- `interval_seconds`: how often to launch a new backup request within this phase.
- `paid_fallback`: optional non-NIM fallback used when NIM keys are in cooldown.

### 4. `health`

Controls cooldown behavior and health-state persistence. `health_state.json` is runtime state and should not be committed.

### 5. `archive`

Controls the optional response archive used for validator debugging. Archive files live under `logs/` by default and should not be committed.

### 6. `ranking`

Controls dynamic in-tier ordering by recent health score. Use `tier_overrides` when a tier should preserve config-file order.

## `.env`

Map your API key aliases to actual values:

```bash
NVIDIA_API_KEY_1=nvapi-...
NVIDIA_API_KEY_2=nvapi-...
NVIDIA_API_KEY_3=nvapi-...
DEEPSEEK_API_KEY=sk-...
```

## Advanced Tuning

- **Aggressive Hedging**: Lower `interval_seconds` to fight tail latency.
- **Strict Logic**: Create a virtual model with only one phase that stays on a single tier for the entire timeout.
- **Cost Control**: Remove `paid_fallback` blocks if you only want to use NIM-backed capacity.
