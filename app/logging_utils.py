import json
import os
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
from typing import Any, Dict, List, Optional
from app.models import CandidateResult

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "requests.jsonl")

# Global logger instance
_logger = None

def setup_logging():
    global _logger
    if _logger is not None:
        return _logger

    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)

    _logger = logging.getLogger("request_logger")
    _logger.setLevel(logging.INFO)
    
    # TimedRotatingFileHandler: 
    # when='midnight' -> rotates every day at midnight
    # backupCount=3 -> keeps the last 3 days of logs
    handler = TimedRotatingFileHandler(
        LOG_FILE, 
        when="midnight", 
        interval=1, 
        backupCount=3,
        encoding="utf-8"
    )
    
    # We want raw JSONL, so we don't add standard logging formatters like %(asctime)s
    # because our log_request function already provides a timestamp in the JSON.
    _logger.addHandler(handler)
    return _logger

def log_request(
    virtual_model: str,
    winner: Optional[CandidateResult],
    all_results: List[CandidateResult],
    usage: Optional[Dict[str, Any]] = None,
    success: bool = True,
    error_message: Optional[str] = None
):
    logger = setup_logging()
    
    log_entry = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "virtual_model": virtual_model,
        "success": success,
        "winner": winner.candidate_name if winner else None,
        "winner_real_model": winner.real_model if winner else None,
        "latency_ms": winner.latency_ms if winner else None,
        "candidates_tried": [
            {
                "name": r.candidate_name,
                "model": r.real_model,
                "latency": r.latency_ms,
                "status": r.status_code,
                "ok": (not r.error and r.response is not None)
            } for r in all_results
        ],
        "usage": usage,
        "error": error_message
    }
    
    # Add detailed errors for failed candidates if any
    log_entry["candidate_errors"] = [
        {"candidate": r.candidate_name, "error": r.error, "status": r.status_code} 
        for r in all_results if r.error or not r.response
    ]
    
    # Add winner content preview (first 200 chars) for debugging
    if winner and winner.response:
        try:
            resp = winner.response.model_dump() if hasattr(winner.response, 'model_dump') else winner.response
            choice = resp.get("choices", [{}])[0] if isinstance(resp, dict) else {}
            msg = choice.get("message", {}) if isinstance(choice, dict) else {}
            content_preview = (msg.get("content") or "")[:200]
            if content_preview:
                log_entry["winner_content_preview"] = content_preview
        except Exception:
            pass
    
    # Log as a single line JSON
    logger.info(json.dumps(log_entry))
