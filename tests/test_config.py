import pytest
from app.config import load_config, AppConfig

def test_load_config_new_structure():
    config = load_config("config.example.yaml")
    assert isinstance(config, AppConfig)
    assert "large" in config.tiers
    assert "nim-large" in config.virtual_models
    
    strategy = config.virtual_models["nim-large"]
    assert len(strategy.phases) >= 1
    assert strategy.phases[0].tier == "large"
    assert strategy.hard_timeout_seconds == 1500

def test_config_values():
    config = load_config("config.example.yaml")
    assert config.server.rpm_limit_per_api == 0
    assert config.health.max_recent_events == 200
    assert config.health.pre_request_delay.max_seconds == 256.0
