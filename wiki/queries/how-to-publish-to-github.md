# How to Publish NIM Hedge Gateway to GitHub

Use this checklist before pushing the project to a public GitHub repository.

## Files to Commit

- Source: `app/`, `tests/`, `requirements.txt`
- Public examples: `.env.example`, `config.example.yaml`
- Documentation: `README.md`, `docs/`, `wiki/`
- Utility scripts: `proxy.sh`, `command.sh`, `kill_proxy.sh`

## Files to Keep Local

- `.env`: real provider keys
- `config.yaml`: local routing and auth settings
- `health_state.json`: live cooldown and health counters
- `logs/`: request logs and response archives
- `*.jsonl`: local analysis exports
- `.venv/`, `.agents/`, `.codex/`, `.claude/`: local runtime/tool state

These paths are covered by `.gitignore`.

## First Push

```bash
git init
git add .
git status --short
git commit -m "Initial public release"
git branch -M main
git remote add origin git@github.com:RuiHuangAstro/nim-hedge-gateway.git
git push -u origin main
```

If you prefer HTTPS:

```bash
git remote add origin https://github.com/RuiHuangAstro/nim-hedge-gateway.git
git push -u origin main
```

## GitHub Account Notes

Create the empty repository under `RuiHuangAstro` first. Do not initialize it with a README, `.gitignore`, or license if this local project already has those files.

For HTTPS pushes, GitHub requires a personal access token instead of an account password. For SSH pushes, add your local public SSH key to GitHub first.

## Secret Safety

If real API keys were ever committed, rotate them before making the repository public. `.gitignore` prevents new accidental additions, but it does not remove secrets from existing Git history.
