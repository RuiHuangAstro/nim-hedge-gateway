# nim-hedge-gateway

Local OpenAI-compatible hedging gateway for NVIDIA NIM and other OpenAI-compatible providers. It sits between agent clients and several backend model pools, launches delayed backup requests when the primary path is slow or unhealthy, and returns the first valid response.

## Features

- OpenAI-compatible `/v1/models` and `/v1/chat/completions` endpoints.
- Phase-based delayed hedging across virtual models such as `nim-small`, `nim-medium`, and `nim-large`.
- Health scoring and cooldown for rate limits, timeouts, server errors, and malformed responses.
- Optional paid fallback when the free/NIM pool is exhausted.
- LiteLLM-backed provider calls.
- JSONL request logging and optional response archive for validator debugging.
- Optional bearer-token auth through `server.request_api_key`.

## Quick Start

```bash
git clone https://github.com/RuiHuangAstro/nim-hedge-gateway.git
cd nim-hedge-gateway

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
cp config.example.yaml config.yaml
```

Edit `.env` and replace the placeholder values with your real API keys. Edit `config.yaml` if you want different model tiers, timeouts, hedging intervals, or local auth token.

Start the server:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Or use the helper:

```bash
./proxy.sh start
./proxy.sh status
./proxy.sh stop
```

## Test Requests

List virtual models:

```bash
curl http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer local-test"
```

Send a non-streaming chat completion:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer local-test" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nim-small",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ],
    "stream": false
  }'
```

Streaming is currently not supported; use `stream: false`.

## Client Configuration

Use these settings in OpenClaw, Hermes, OpenCode, or any OpenAI-compatible client:

- Base URL: `http://127.0.0.1:8000/v1`
- API key: the value of `server.request_api_key` in `config.yaml` (`local-test` in the example)
- Model: one of the configured virtual models, for example `nim-small`, `nim-medium`, or `nim-large`

## Public vs Local Files

The repository is prepared for GitHub with examples in place of local secrets:

- Commit: `.env.example`, `config.example.yaml`, source code, tests, docs, and wiki pages.
- Do not commit: `.env`, `config.yaml`, `health_state.json`, `logs/`, `*.jsonl`, `.venv/`, `.agents/`, `.codex/`, `.claude/`.

If real keys were ever committed accidentally, rotate them with the provider before making the repository public.

## Documentation

- [docs/README.md](docs/README.md): detailed documentation index.
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md): config schema and tuning guide.
- [docs/OPERATIONS.md](docs/OPERATIONS.md): health checks, ranking, logs, and troubleshooting.
- [wiki/index.md](wiki/index.md): local wiki index.

## Development

Run tests:

```bash
pytest
```

## License

MIT
