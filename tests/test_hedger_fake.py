import pytest
import asyncio
from unittest.mock import patch, MagicMock
from app.hedger import hedged_completion
from app.models import ChatCompletionRequest, CandidateResult, ChatCompletionMessage
from app.config import VirtualModelStrategy, StrategyPhase, RawModel, config

@pytest.mark.asyncio
async def test_hedger_dynamic_strategy_wins():
    # Define a custom strategy for testing
    strategy = VirtualModelStrategy(
        description="test strategy",
        hard_timeout_seconds=10,
        phases=[
            StrategyPhase(tier="large", start_seconds=0, end_seconds=2, interval_seconds=1)
        ]
    )
    
    # Ensure our tiers have something for "large"
    config.tiers["large"] = [
        RawModel(name="ModelA", model="mA", api_key_env="K1"),
        RawModel(name="ModelB", model="mB", api_key_env="K2")
    ]
    
    request = ChatCompletionRequest(model="nim-test", messages=[ChatCompletionMessage(role="user", content="hi")])
    
    # Mock call_litellm_candidate
    async def mock_call(raw_model, req, timeout):
        if raw_model.name == "ModelA":
            await asyncio.sleep(0.5)
            return CandidateResult(candidate_name="ModelA", real_model="mA", response=None, latency_ms=500, error="slow")
        else:
            # Model B should win
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock(message=MagicMock(content="hello from B"))]
            mock_resp.model_dump.return_value = {"choices": [{"message": {"content": "hello from B"}}]}
            return CandidateResult(candidate_name="ModelB", real_model="mB", response=mock_resp, latency_ms=100)

    with patch("app.hedger.call_litellm_candidate", side_effect=mock_call):
        winner, all_results = await hedged_completion(request, strategy)
        assert winner.candidate_name == "ModelB"
        assert winner.response.choices[0].message.content == "hello from B"

@pytest.mark.asyncio
async def test_hedger_strategy_all_fail():
    strategy = VirtualModelStrategy(
        description="fail strategy",
        hard_timeout_seconds=1,
        phases=[StrategyPhase(tier="large", start_seconds=0, end_seconds=1, interval_seconds=1)]
    )
    
    request = ChatCompletionRequest(model="nim-test", messages=[ChatCompletionMessage(role="user", content="hi")])
    
    async def mock_call(raw_model, req, timeout):
        return CandidateResult(candidate_name="Failer", real_model="m", response=None, latency_ms=10, error="fail", status_code=500)

    with patch("app.hedger.call_litellm_candidate", side_effect=mock_call):
        # All candidates fail -> no winner. hedged_completion returns
        # (None, results); main.py turns that into a 502 HTTPException.
        winner, all_results = await hedged_completion(request, strategy)
        assert winner is None
        assert all(r.error for r in all_results)

@pytest.mark.asyncio
async def test_hedger_rejects_broken_harmony_and_waits_for_clean():
    """Critical: if the first responder produces harmony markers but no
    extractable tool call, the hedger must NOT declare it the winner — it
    should keep waiting for another candidate."""
    strategy = VirtualModelStrategy(
        description="harmony fail",
        hard_timeout_seconds=10,
        phases=[StrategyPhase(tier="large", start_seconds=0, end_seconds=2, interval_seconds=0.1)]
    )

    config.tiers["large"] = [
        RawModel(name="Broken", model="moonshotai/kimi-k2.6", api_key_env="K1"),
        RawModel(name="Clean", model="zhipu/glm-5.1", api_key_env="K1"),
    ]

    request = ChatCompletionRequest(
        model="nim-test",
        messages=[ChatCompletionMessage(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "Bash",
            "parameters": {"type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"]}}}],
    )

    async def mock_call(raw_model, req, timeout):
        if raw_model.name == "Broken":
            # Return immediately with the truncated-harmony pattern.
            broken_content = (
                "<|tool_calls_section_begin|>"
                "<|tool_call_begin|>chatcmpl-tool-fakehashg7h8i9:0]"
            )
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock(message=MagicMock(
                content=broken_content, tool_calls=None,
            ), finish_reason="stop")]
            mock_resp.model_dump.return_value = {
                "choices": [{"message": {"content": broken_content, "tool_calls": None},
                             "finish_reason": "stop"}]
            }
            return CandidateResult(candidate_name="Broken", real_model="moonshotai/kimi-k2.6",
                                   response=mock_resp, latency_ms=50)
        # Clean candidate takes a bit longer but returns a real tool call.
        await asyncio.sleep(0.3)
        clean_content = (
            "<|tool_calls_section_begin|>"
            "<|tool_call_begin|>functions.Bash:0"
            "<|tool_call_argument_begin|>"
            '{"command": "ls"}'
            "<|tool_call_end|>"
            "<|tool_calls_section_end|>"
        )
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(
            content=clean_content, tool_calls=None,
        ), finish_reason="stop")]
        mock_resp.model_dump.return_value = {
            "choices": [{"message": {"content": clean_content, "tool_calls": None},
                         "finish_reason": "stop"}]
        }
        return CandidateResult(candidate_name="Clean", real_model="zhipu/glm-5.1",
                               response=mock_resp, latency_ms=300)

    with patch("app.hedger.call_litellm_candidate", side_effect=mock_call):
        winner, all_results = await hedged_completion(request, strategy)
        assert winner.candidate_name == "Clean", (
            f"Broken candidate should not win. Winner was {winner.candidate_name}. "
            "If hedger declared the broken one a winner, hermes would see an empty response."
        )


@pytest.mark.asyncio
async def test_hedger_no_available_keys():
    strategy = VirtualModelStrategy(
        description="no keys strategy",
        hard_timeout_seconds=1,
        phases=[StrategyPhase(tier="large", start_seconds=0, end_seconds=1, interval_seconds=1)]
    )
    
    request = ChatCompletionRequest(model="nim-test", messages=[ChatCompletionMessage(role="user", content="hi")])
    
    # Empty out available keys to force best_key = None
    with patch("app.hedger.config.server.api_key_envs", []):
        # Every candidate self-throttles (no key budget) -> no winner.
        winner, all_results = await hedged_completion(request, strategy)
        assert winner is None
        assert all(r.error for r in all_results)
