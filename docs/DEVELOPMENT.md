# Development Guide

## Environment Setup
The project is verified to run on **Python 3.13.2** (Miniconda).

### Binary Location
`/home/huangrui/software/miniconda3/bin/python`

### Installation
```bash
/home/huangrui/software/miniconda3/bin/python -m pip install -r requirements.txt
```

## Running the Server
```bash
/home/huangrui/software/miniconda3/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Testing
The project uses `pytest` with `pytest-asyncio`.

### Running Tests
```bash
/home/huangrui/software/miniconda3/bin/python -m pytest
```

### Test Coverage
- **Config**: Validates Pydantic models and YAML loading.
- **Validator**: Ensures response integrity logic works (checks JSON tool calls, content presence).
- **Hedger**: Mocks API calls to verify first-wins logic, cancellation, and delay orchestration.

## Key Dependencies
- `fastapi` & `uvicorn`: API framework.
- `litellm`: Multi-provider LLM client.
- `pydantic`: Data validation and settings.
- `pyyaml`: Configuration parsing.
- `python-dotenv`: Secret management.
