---
title: How to Configure NIM Proxy
created: 2026-05-08
updated: 2026-05-31
type: query
tags: [configuration, setup, github-ready, routing]
sources: [../../docs/CONFIGURATION.md, ../../config.example.yaml, ../../app/router.py, ../../app/health.py]
confidence: high
---

# How to Configure NIM Proxy

NIM Hedge Gateway 的配置分成两类：

- 公开模板：`.env.example`、`config.example.yaml`，可以提交到 GitHub。
- 本机配置：`.env`、`config.yaml`，包含真实 key 或个人路由策略，不能提交。

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
cp config.example.yaml config.yaml
```

编辑 `.env`：

```bash
NVIDIA_API_KEY_1=<your-nvidia-key-1>
NVIDIA_API_KEY_2=<your-nvidia-key-2>
NVIDIA_API_KEY_3=<your-nvidia-key-3>
```

启动：

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

或：

```bash
./proxy.sh start
./proxy.sh status
./proxy.sh stop
```

## 当前 Config Schema

### server

`server.api_key_envs` 是共享 NIM key 池。当前版本不是在每个 tier model 下单独写 `api_key_env`。

```yaml
server:
  host: "127.0.0.1"
  port: 8000
  request_api_key: "local-test"
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

Rate-control fields:

- `rpm_limit_per_api`: proactive token-bucket RPM cap per key. `0` means unlimited.
- `burst_per_api`: token-bucket capacity. `0` defaults to the RPM limit.
- `queue_when_limited`: when all token buckets are empty, wait before returning a synthetic 429.
- `max_queue_seconds`: maximum wait time for `queue_when_limited`.
- `allow_best_effort_when_all_limited`: select a key even with an empty bucket.

The old `max_concurrency_per_api` field is no longer part of the current schema.

### tiers

`tiers` 定义物理模型池。每个候选者需要 `name` 和 `model`；NIM key 从 `server.api_key_envs` 动态选择。

```yaml
tiers:
  large:
    - { name: "large-primary", model: "qwen/qwen3.5-397b-a17b" }
    - { name: "large-backup", model: "nvidia/nemotron-3-super-120b-a12b" }

  medium:
    - { name: "medium-primary", model: "qwen/qwen3.5-122b-a10b" }
    - { name: "medium-backup", model: "z-ai/glm4.7" }

  small:
    - { name: "small-primary", model: "qwen/qwen3.5-122b-a10b" }
    - { name: "small-backup", model: "openai/gpt-oss-120b" }
```

### virtual_models

`virtual_models` 是客户端能看到的模型名，例如 `nim-small`、`nim-medium`、`nim-large`。

```yaml
virtual_models:
  nim-large:
    description: "Large model pool with medium/small fallbacks."
    hard_timeout_seconds: 1500
    require_valid_response: true
    phases:
      - { tier: "large", start_seconds: 0, end_seconds: 360, interval_seconds: 40 }
      - { tier: "medium", start_seconds: 360, end_seconds: 900, interval_seconds: 50 }
      - { tier: "small", start_seconds: 900, end_seconds: 1500, interval_seconds: 60 }
```

参数含义：

- `hard_timeout_seconds`: 整个请求的最大等待时间。
- `phases`: 顺序执行的 fallback 阶段。
- `interval_seconds`: 阶段内多久启动一次新的候选请求。

The old `paid_fallback` block was removed from the current hedger. If it still appears in a local `config.yaml`, treat it as stale configuration rather than active behavior.

### health

`health` 控制 429 后的 per-key pre-request delay、候选者健康评分和健康状态持久化：

```yaml
health:
  pre_request_delay:
    enabled: true
    window_seconds: 300
    max_seconds: 256
  max_recent_events: 200
  persistence_file: "health_state.json"
```

429 不再触发硬冷却。当前规则是：同一个 key 在 `window_seconds` 内有 N 次 429，则下一次用这个 key 前 sleep `min(2^N, max_seconds)` 秒。

`health_state.json` 是运行时状态，不能提交到 GitHub。

### archive

`archive` 控制响应归档，用于调试工具调用或 validator 失败：

```yaml
archive:
  enabled: true
  file_path: "logs/response_archive.jsonl"
```

`logs/` 和 `*.jsonl` 默认都是本机运行产物，不能提交。

## OpenAI-Compatible Client

客户端配置：

- Base URL: `http://127.0.0.1:8000/v1`
- API key: `server.request_api_key` 的值，例如 `local-test`
- Model: `nim-small`、`nim-medium`、`nim-large`

测试：

```bash
curl http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer local-test"
```

查看 key 路由状态：

```bash
curl http://127.0.0.1:8000/v1/hedge/key_stats \
  -H "Authorization: Bearer local-test"
```

## GitHub Safety

可以提交：

- `.env.example`
- `config.example.yaml`
- `README.md`
- `docs/`
- `wiki/`
- `app/`
- `tests/`

不能提交：

- `.env`
- `config.yaml`
- `health_state.json`
- `logs/`
- `*.jsonl`
- `.venv/`
- `.agents/`
- `.codex/`
- `.claude/`

## Related Concepts

- [[nim-health-cooldown-system]] — current health scoring and rate-control model
- [[nim-hedging-strategy]] — phase and tier behavior
