"""Record {request, every candidate's output, winner} per served request.

This is how the fusion dataset accumulates from real traffic: nim-fusion fans
out several diverse models per prompt, so its records naturally contain the
multiple answers that scripts/fusion_eval.py needs — no shadow fan-out required.

Records only what the request actually ran (zero extra upstream calls). Writes a
size-rotated jsonl file; never raises into the request path.
"""
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

from app.config import RecordConfig, config
from app.models import ChatCompletionRequest, CandidateResult
from app.validators import validate_openai_chat_completion

_logger: Optional[logging.Logger] = None
_initialized = False


def _setup() -> Optional[logging.Logger]:
    global _logger, _initialized
    if _initialized:
        return _logger

    _initialized = True
    cfg: RecordConfig = config.record
    if not cfg.enabled:
        return None

    log_dir = os.path.dirname(cfg.file_path) or "."
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    lg = logging.getLogger("nim_proxy.request_recorder")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    for h in list(lg.handlers):
        lg.removeHandler(h)
    handler = RotatingFileHandler(
        cfg.file_path,
        maxBytes=cfg.max_bytes_per_file,
        backupCount=cfg.backup_count,
        encoding="utf-8",
    )
    lg.addHandler(handler)
    _logger = lg
    return _logger


def reset_for_tests() -> None:
    global _logger, _initialized
    if _logger is not None:
        for h in list(_logger.handlers):
            try:
                h.close()
            except Exception:
                pass
            _logger.removeHandler(h)
    _logger = None
    _initialized = False


def _candidate_record(
    result: CandidateResult, request: ChatCompletionRequest
) -> Dict[str, Any]:
    response_dict: Optional[Dict[str, Any]] = None
    valid = False
    validation_reason: Optional[str] = None
    if result.response is not None and not result.error:
        try:
            response_dict = result.response.model_dump()
        except Exception:
            response_dict = None
        # Match fusion's runtime policy (repetition is filtered by the judge,
        # not rejected) so the recorded `valid` flag reflects what was accepted.
        validation = validate_openai_chat_completion(
            result, tools_schema=request.tools, check_repetition=False,
        )
        valid = validation.ok
        validation_reason = None if validation.ok else validation.reason

    return {
        "name": result.candidate_name,
        "real_model": result.real_model,
        "ok": bool(result.response is not None and not result.error),
        "valid": valid,
        "validation_reason": validation_reason,
        "is_winner": result.is_winner,
        "latency_ms": result.latency_ms,
        "status_code": result.status_code,
        "error": result.error,
        "response": response_dict,
    }


def record_request(
    request: ChatCompletionRequest,
    virtual_model: str,
    winner: Optional[CandidateResult],
    all_results: List[CandidateResult],
) -> None:
    """Append one record. No-op unless record.enabled. Never raises."""
    cfg = config.record
    if not cfg.enabled:
        return
    if cfg.only_virtual_models and virtual_model not in cfg.only_virtual_models:
        return
    lg = _setup()
    if lg is None:
        return

    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "id": uuid.uuid4().hex,
            "virtual_model": virtual_model,
            "winner": winner.candidate_name if winner else None,
            "fusion_judge_model": winner.fusion_judge_model if winner else None,
            "fusion_judge_analysis": winner.fusion_judge_analysis if winner else None,
            "request": request.model_dump(exclude_none=True),
            "candidates": [_candidate_record(r, request) for r in all_results],
        }
        lg.info(json.dumps(record, ensure_ascii=False, default=str))
    except Exception:
        pass
