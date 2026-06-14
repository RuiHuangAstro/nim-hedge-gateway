import os
import yaml
from typing import List, Dict, Optional, Any
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

class RankingConfig(BaseModel):
    enabled: bool = True
    window_size: int = 100
    min_samples_for_dynamic_order: int = 3
    # Per-tier overrides. Key = tier name, value = True (sort by score) or
    # False (use config-file order). Missing tiers fall back to `enabled`.
    tier_overrides: Dict[str, bool] = Field(default_factory=dict)

    def is_enabled_for_tier(self, tier: str) -> bool:
        return self.tier_overrides.get(tier, self.enabled)

class RawModel(BaseModel):
    name: str
    provider: str = "openai"
    api_base: str = "https://integrate.api.nvidia.com/v1"
    model: str
    api_key_env: Optional[str] = None # Now optional

class StrategyPhase(BaseModel):
    tier: str
    start_seconds: float
    end_seconds: float
    interval_seconds: float

class PaidFallback(BaseModel):
    """Paid (non-NIM) provider used when every NIM API key is in 429
    cooldown. Fired at most once per request, sequentially (never in parallel
    with NIM candidates), so a paid response cannot lose to a slower free one.
    Triggered at the start of any phase whose required NIM keys are all
    cooled — including the primary phase, so a request arriving when NIM is
    fully cooled goes straight to the paid endpoint.
    """
    name: str
    provider: str = "openai"
    api_base: str
    model: str
    api_key_env: str
    timeout_seconds: float = 300

class VirtualModelStrategy(BaseModel):
    description: str = ""
    hard_timeout_seconds: float = 1500
    # Per-call timeout for individual upstream LiteLLM calls. Prevents one
    # slow/hanging model from blocking the entire phase until hard_timeout_seconds.
    # Defaults to 300s; set to 0 to use hard_timeout_seconds instead.
    per_call_timeout_seconds: float = 300.0
    require_valid_response: bool = True
    # "hedge" (default): first valid response wins, latency-optimized.
    # "fusion": fan out the whole fusion_tier, wait for >= min_valid valid
    #   answers, then have a hedged judge select the best among them.
    mode: str = "hedge"
    phases: List[StrategyPhase] = Field(default_factory=list)
    paid_fallback: Optional[PaidFallback] = None
    # --- fusion mode only ---
    fusion_tier: str = "large"      # tier whose models are fanned out + reused as judges
    fusion_min_valid: int = 2       # stop once this many *distinct* models answer validly
    # Each model runs its own lane: fire at t=0, then re-fire every
    # fusion_retry_interval_seconds until that model returns a valid answer (or
    # the whole collection stops because fusion_min_valid distinct models are in).
    fusion_retry_interval_seconds: float = 60.0

class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    request_api_key: Optional[str] = None
    log_level: str = "info"
    api_key_envs: List[str] = Field(default_factory=lambda: ["NVIDIA_API_KEY_1"])
    # Token bucket rate limiting (0 = unlimited)
    rpm_limit_per_api: int = 0
    burst_per_api: int = 0
    # Queue waiting when all keys busy/rate-limited
    queue_when_limited: bool = False
    max_queue_seconds: float = 20.0
    allow_best_effort_when_all_limited: bool = False

class PreRequestDelayConfig(BaseModel):
    """Per-key pre-request delay based on 429 count within window_seconds.
    delay = 2^N seconds where N = number of 429s in the window, capped at
    max_seconds. Applied after key selection, before the upstream call fires.

    This is the *only* 429 throttle: there is no separate cooldown that takes
    a key out of rotation. A key that keeps getting 429'd simply waits longer
    (2^N) before each request, so the request rate self-regulates toward a
    mild, steady 429 frequency — quiet for window_seconds and N decays back to
    0, push harder again."""
    enabled: bool = True
    window_seconds: float = 300.0  # 5 minutes
    max_seconds: float = 256.0     # 2^8 = 256 s hard cap

class HealthConfig(BaseModel):
    pre_request_delay: PreRequestDelayConfig = Field(default_factory=PreRequestDelayConfig)
    max_recent_events: int = 200
    score_window_seconds: float = 1800.0  # only events within this window feed scoring
    persistence_file: str = "health_state.json"

class ArchiveConfig(BaseModel):
    enabled: bool = True
    file_path: str = "logs/response_archive.jsonl"
    max_bytes_per_file: int = 10 * 1024 * 1024  # 10MB
    backup_count: int = 10                       # ~100MB total
    # Which categories to record. Available:
    #   "harmony_repaired"   — markers detected and parsed into tool_calls
    #   "harmony_inferred"   — parsed, but function name was inferred from the
    #                          request's tools schema because upstream omitted
    #                          `functions.NAME` (e.g. kimi-k2.6 occasionally
    #                          fills the slot with the tool-call id instead)
    #   "harmony_unparsed"   — markers detected but format unrecognized
    #   "validation_failed"  — candidate response failed validation
    categories: List[str] = Field(default_factory=lambda: [
        "harmony_repaired", "harmony_inferred", "harmony_unparsed", "validation_failed"
    ])

class RecordConfig(BaseModel):
    """Record {full request, every candidate's full output, winner} for each
    served request, so the fusion dataset accumulates from real traffic
    (especially nim-fusion, whose fan-out naturally yields several answers per
    prompt). Consumed by scripts/fusion_eval.py.

    Unlike a shadow fan-out, this records only what the request *actually* ran —
    zero extra upstream calls. Size-rotated jsonl; total disk cap ≈
    max_bytes_per_file × (backup_count + 1). Default ≈ 20 GB.
    """
    enabled: bool = False
    file_path: str = "logs/fusion_dataset.jsonl"
    max_bytes_per_file: int = 2 * 1024 * 1024 * 1024   # 2 GB
    backup_count: int = 9                              # ~20 GB total
    # If non-empty, only record these virtual models (e.g. ["nim-fusion"]).
    only_virtual_models: List[str] = Field(default_factory=list)

class AppConfig(BaseModel):
    server: ServerConfig
    health: HealthConfig
    tiers: Dict[str, List[RawModel]]
    virtual_models: Dict[str, VirtualModelStrategy]
    ranking: RankingConfig = Field(default_factory=RankingConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)
    record: RecordConfig = Field(default_factory=RecordConfig)

def load_config(config_path: str = "config.yaml") -> AppConfig:
    if not os.path.exists(config_path):
        config_path = "config.example.yaml"
    
    with open(config_path, "r") as f:
        raw_config = yaml.safe_load(f)
    
    return AppConfig(**raw_config)

config = load_config()
