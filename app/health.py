import time
import json
import os
import statistics
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field
from app.config import config

class HealthEvent(BaseModel):
    timestamp: float
    event_type: str
    latency_ms: int
    error: Optional[str] = None

class CandidateHealth(BaseModel):
    name: str
    virtual_model: str
    real_model: str
    
    success_count: int = 0
    failure_count: int = 0
    timeout_count: int = 0
    rate_limit_count: int = 0
    server_error_count: int = 0
    invalid_response_count: int = 0
    
    recent_events: List[HealthEvent] = Field(default_factory=list)
    cooldown_until: float = 0 # This now mainly tracks model-level issues
    score: float = 0.0

class HealthStore:
    def __init__(self):
        self.candidates: Dict[str, CandidateHealth] = {}
        # Active request count is tracked per API KEY alias (observability /
        # phase spacing only — there is no hard concurrency cap).
        self.active_requests_per_api: Dict[str, int] = {}
        # Sliding window of recent 429 timestamps per key. This is the sole
        # 429 signal: get_api_pre_request_delay turns N (429s in the window)
        # into a 2^N pre-request delay. No key is ever taken out of rotation.
        # Not persisted — a restart resets the window, which is fine.
        self.api_recent_429_ts: Dict[str, List[float]] = {}
        
        self.persistence_file = config.health.persistence_file
        self.load_state()

    def get_active_count(self, api_alias: str) -> int:
        return self.active_requests_per_api.get(api_alias, 0)

    def increment_active(self, api_alias: str):
        self.active_requests_per_api[api_alias] = self.get_active_count(api_alias) + 1

    def decrement_active(self, api_alias: str):
        count = self.get_active_count(api_alias)
        if count > 0:
            self.active_requests_per_api[api_alias] = count - 1

    def get_api_pre_request_delay(self, api_alias: str) -> float:
        """Return the extra sleep (seconds) to inject before sending a request
        to api_alias. delay = 2^N where N = 429s within the last window_seconds,
        capped at max_seconds. Returns 0 when the feature is disabled or N == 0."""
        cfg = config.health.pre_request_delay
        if not cfg.enabled:
            return 0.0
        now = time.time()
        window_start = now - cfg.window_seconds
        history = self.api_recent_429_ts.get(api_alias, [])
        n = sum(1 for t in history if t >= window_start)
        if n == 0:
            return 0.0
        return min(float(2 ** n), cfg.max_seconds)

    def mark_api_429(self, api_alias: str):
        """Record a 429 for this key. The timestamp feeds the sliding window
        that get_api_pre_request_delay reads to compute the 2^N back-off; the
        key stays in rotation regardless."""
        now = time.time()
        window_start = now - config.health.pre_request_delay.window_seconds
        history = [t for t in self.api_recent_429_ts.get(api_alias, []) if t >= window_start]
        history.append(now)
        self.api_recent_429_ts[api_alias] = history

    def _get_key(self, virtual_model: str, model_name: str) -> str:
        return f"{virtual_model}/{model_name}"

    def get_candidate_health(self, virtual_model: str, model_name: str, real_model_path: str) -> CandidateHealth:
        key = self._get_key(virtual_model, model_name)
        if key not in self.candidates:
            self.candidates[key] = CandidateHealth(
                name=model_name,
                virtual_model=virtual_model,
                real_model=real_model_path
            )
        return self.candidates[key]

    def mark_success(self, virtual_model: str, model_name: str, latency_ms: int):
        key = self._get_key(virtual_model, model_name)
        health = self.candidates.get(key)
        if not health: return
        health.success_count += 1
        self._add_event(health, HealthEvent(timestamp=time.time(), event_type="success", latency_ms=latency_ms))
        self._update_score(health)
        self.save_state()

    def mark_content_quality(self, virtual_model: str, model_name: str, quality: str):
        """Retroactively annotate the most recent event with content-quality info.

        Called from main.py after `repair_response_dict` has examined the
        winner's response. `quality` is one of:
          - "inferred"  → tool-call name had to be schema-inferred (signal of
                          fragile upstream formatting; small score penalty).
          - "unparsed"  → harmony markers present but no tool calls recoverable
                          (hermes effectively saw an empty response and will
                          retry; treated as not-a-success and penalized).

        "clean" is a no-op — the existing `success` event already represents
        a fully usable response.
        """
        new_type = {
            "inferred": "success_inferred",
            "unparsed": "success_unparsed",
        }.get(quality)
        if not new_type:
            return
        key = self._get_key(virtual_model, model_name)
        health = self.candidates.get(key)
        if not health or not health.recent_events:
            return
        last = health.recent_events[-1]
        # Only downgrade an event we just wrote as `success`. This guards
        # against a stale event when the call site is racy.
        if last.event_type != "success":
            return
        last.event_type = new_type
        self._update_score(health)
        self.save_state()

    def mark_failure(self, virtual_model: str, model_name: str, event_type: str, latency_ms: int, error: str, api_alias: Optional[str] = None):
        if event_type == "rate_limit" and api_alias:
            self.mark_api_429(api_alias)

        key = self._get_key(virtual_model, model_name)
        health = self.candidates.get(key)
        if not health: return
        
        health.failure_count += 1
        if event_type == "timeout": health.timeout_count += 1
        elif event_type == "server_error": health.server_error_count += 1
        
        self._add_event(health, HealthEvent(timestamp=time.time(), event_type=event_type, latency_ms=latency_ms, error=error))
        self._update_score(health)
        self.save_state()

    def _add_event(self, health: CandidateHealth, event: HealthEvent):
        health.recent_events.append(event)
        # Trim events outside the scoring window first, then cap by max count.
        cutoff = time.time() - config.health.score_window_seconds
        health.recent_events = [e for e in health.recent_events if e.timestamp >= cutoff]
        if len(health.recent_events) > config.health.max_recent_events:
            health.recent_events = health.recent_events[-config.health.max_recent_events:]

    def _update_score(self, health: CandidateHealth):
        cutoff = time.time() - config.health.score_window_seconds
        events = [e for e in health.recent_events if e.timestamp >= cutoff]
        if not events:
            health.score = 0.0
            return
        total = len(events)
        # Event-type semantics:
        #   `success`           — fully usable, full credit
        #   `success_inferred`  — usable but tool name had to be inferred;
        #                         counted as a success but mildly penalized
        #   `success_unparsed`  — legacy: post-hoc downgrade after winner returned;
        #                         no longer fired (the validator now rejects these),
        #                         but kept for backward compat with persisted state
        #   `content_unparsed`  — validator rejected: harmony markers present but
        #                         no tool call extractable. NOT a success; heavy
        #                         penalty to push the model down the ranking.
        #   `invalid_response`  — generic validation failure; not a success
        #   `timeout` / `server_error` — explicit penalties
        successes = sum(1 for e in events if e.event_type in ("success", "success_inferred"))
        inferred = sum(1 for e in events if e.event_type == "success_inferred")
        legacy_unparsed = sum(1 for e in events if e.event_type == "success_unparsed")
        content_unparsed = sum(1 for e in events if e.event_type == "content_unparsed")
        timeouts = sum(1 for e in events if e.event_type == "timeout")
        server_errors = sum(1 for e in events if e.event_type == "server_error")
        latencies = [e.latency_ms for e in events if e.event_type.startswith("success")]
        p95 = statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 2 else (latencies[0] if latencies else 60000)

        a, b = 3.0, 1.0
        smooth_success_rate = (successes + a) / (total + a + b)
        timeout_rate = (timeouts + 1.0) / (total + 10.0)
        server_error_rate = (server_errors + 1.0) / (total + 10.0)
        latency_penalty = min(p95 / 180000.0, 0.5)
        # content_unparsed is double-charged: it's already excluded from
        # successes (so smooth_success_rate drops), AND it gets an extra
        # penalty here. This is intentional — we want a candidate that
        # returns broken harmony every other request to drop visibly below
        # one with mixed timeout/5xx failures, because the broken-harmony
        # case wastes more of hermes's time (full RTT + retry).
        content_penalty = (
            0.8 * content_unparsed       # bumped from 0.4 (legacy) — louder signal
            + 0.7 * legacy_unparsed       # legacy event type, kept for back-compat
            + 0.1 * inferred              # bumped from 0.05 — nudge cleaner candidates
        ) / total

        health.score = (
            smooth_success_rate
            - 0.6 * timeout_rate
            - 0.4 * server_error_rate
            - 0.3 * latency_penalty
            - content_penalty
        )

    def get_ranking(self, virtual_model: str) -> List[Dict[str, Any]]:
        pool_candidates = [c for k, c in self.candidates.items() if c.virtual_model == virtual_model]
        sorted_candidates = sorted(pool_candidates, key=lambda x: x.score, reverse=True)
        ranking = []
        for c in sorted_candidates:
            latencies = [e.latency_ms for e in c.recent_events if e.event_type == "success"]
            p95 = statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 2 else (latencies[0] if latencies else 0)
            ranking.append({
                "model": c.name, "score": round(c.score, 3), "p95_ms": int(p95), "samples": len(c.recent_events)
            })
        return ranking

    def load_state(self):
        if os.path.exists(self.persistence_file):
            try:
                with open(self.persistence_file, "r") as f:
                    data = json.load(f)
                    for key, val in data.get("candidates", {}).items():
                        self.candidates[key] = CandidateHealth(**val)
            except Exception as e:
                print(f"Error loading health state: {e}")

    def save_state(self):
        try:
            data = {
                "candidates": {k: v.model_dump() for k, v in self.candidates.items()},
            }
            with open(self.persistence_file, "w") as f:
                json.dump(data, f)
        except Exception as e:
            print(f"Error saving health state: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Returns aggregate concurrency and error stats for console logging.
        
        Returns:
            {
                'total_active': int,
                'load_distribution': List[int], (sorted desc)
                'recent_429_count': int (last 5 minutes)
            }
        """
        all_keys = config.server.api_key_envs
        active_counts = [self.get_active_count(k) for k in all_keys]
        total_active = sum(active_counts)
        load_dist = sorted(active_counts, reverse=True)
        
        # Calculate 429s in last 5 minutes across all keys
        now = time.time()
        five_min_ago = now - 300
        total_429s = 0
        for history in self.api_recent_429_ts.values():
            total_429s += sum(1 for ts in history if ts >= five_min_ago)
            
        return {
            "total_active": total_active,
            "load_distribution": load_dist,
            "recent_429_count": total_429s
        }

health_store = HealthStore()
