"""Bounded archive of upstream responses that triggered tool-call parsing,
content scrubbing, or validation failures.

The point is not full audit logging — it's keeping enough history to spot
new harmony-style token formats that the parser does not yet handle, plus
recurring validation failures.

Storage is a size-rotated jsonl file (default ~100MB total = 10MB × 10).
"""
import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

from app.config import ArchiveConfig, config

_logger: Optional[logging.Logger] = None
_active_categories: set = set()
_initialized = False


def _setup() -> Optional[logging.Logger]:
    global _logger, _active_categories, _initialized
    if _initialized:
        return _logger

    _initialized = True
    cfg: ArchiveConfig = config.archive
    if not cfg.enabled:
        return None

    _active_categories = set(cfg.categories)

    log_dir = os.path.dirname(cfg.file_path) or "."
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    lg = logging.getLogger("nim_proxy.response_archive")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    # Clear any pre-existing handlers (e.g. when reloaded under tests)
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
    """Reload archive state — only used by the test suite."""
    global _logger, _active_categories, _initialized
    if _logger is not None:
        for h in list(_logger.handlers):
            try:
                h.close()
            except Exception:
                pass
            _logger.removeHandler(h)
    _logger = None
    _active_categories = set()
    _initialized = False


def _extract_message_fields(response_dict: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not response_dict:
        return {}
    try:
        choice = response_dict["choices"][0]
        msg = choice["message"]
        return {
            "content": msg.get("content"),
            "tool_calls": msg.get("tool_calls"),
            "finish_reason": choice.get("finish_reason"),
        }
    except (KeyError, IndexError, TypeError):
        return {"raw_response": response_dict}


def archive(
    category: str,
    *,
    virtual_model: Optional[str] = None,
    candidate_name: Optional[str] = None,
    real_model: Optional[str] = None,
    response_dict: Optional[Dict[str, Any]] = None,
    raw_content: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one record to the archive. Never raises — archiving must
    not break the request path."""
    lg = _setup()
    if lg is None:
        return
    if category not in _active_categories:
        return

    record: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "category": category,
        "virtual_model": virtual_model,
        "candidate_name": candidate_name,
        "real_model": real_model,
    }
    msg_fields = _extract_message_fields(response_dict)
    if msg_fields:
        record.update(msg_fields)
    if raw_content is not None:
        record["raw_content"] = raw_content
    if extra:
        record["extra"] = extra

    try:
        lg.info(json.dumps(record, ensure_ascii=False, default=str))
    except Exception:
        # Last-resort: never propagate logging errors.
        pass
