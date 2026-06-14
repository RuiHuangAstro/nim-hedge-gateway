import pytest
from typing import Optional
from app.validators import validate_openai_chat_completion, HARMONY_UNPARSED_REASON_PREFIX
from app.models import CandidateResult
from pydantic import BaseModel

class MockMessage(BaseModel):
    content: Optional[str] = None
    tool_calls: Optional[list] = None

class MockChoice(BaseModel):
    message: MockMessage
    finish_reason: str = "stop"

class MockResponse(BaseModel):
    choices: list

def test_validate_content_ok():
    msg = MockMessage(content="Hello")
    choice = MockChoice(message=msg)
    resp = MockResponse(choices=[choice])
    
    result = CandidateResult(
        candidate_name="test",
        real_model="test",
        response=resp,
        latency_ms=100
    )
    
    validation = validate_openai_chat_completion(result)
    assert validation.ok is True

def test_validate_empty_content_invalid():
    msg = MockMessage(content="")
    choice = MockChoice(message=msg)
    resp = MockResponse(choices=[choice])
    
    result = CandidateResult(
        candidate_name="test",
        real_model="test",
        response=resp,
        latency_ms=100
    )
    
    validation = validate_openai_chat_completion(result)
    assert validation.ok is False
    assert "Empty content" in validation.reason

def test_validate_truncated_harmony_marker_is_invalid():
    """kimi-k2.6's broken pattern: opener + fake hash + ']', no `{`, no end.
    Validator must reject so hedger keeps trying other candidates."""
    raw = (
        "<|tool_calls_section_begin|>"
        "<|tool_call_begin|>chatcmpl-tool-b8a6c8d1g4h5i6j7k8l9m0:0]"
    )
    msg = MockMessage(content=raw)
    choice = MockChoice(message=msg)
    resp = MockResponse(choices=[choice])
    result = CandidateResult(
        candidate_name="kimi", real_model="moonshotai/kimi-k2.6",
        response=resp, latency_ms=100,
    )
    bash = {"type":"function","function":{"name":"Bash",
        "parameters":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}}}
    validation = validate_openai_chat_completion(result, tools_schema=[bash])
    assert validation.ok is False
    assert validation.reason.startswith(HARMONY_UNPARSED_REASON_PREFIX)


def test_validate_recoverable_harmony_passes():
    """A response with harmony markers that DO parse (with schema inference)
    should pass validation."""
    raw = (
        "<|tool_calls_section_begin|>"
        "<|tool_call_begin|>chatcmpl-tool-abc123"
        "<|tool_call_argument_begin|>"
        '{"command": "ls"}'
        "<|tool_call_end|>"
        "<|tool_calls_section_end|>"
    )
    msg = MockMessage(content=raw)
    choice = MockChoice(message=msg)
    resp = MockResponse(choices=[choice])
    result = CandidateResult(
        candidate_name="kimi", real_model="moonshotai/kimi-k2.6",
        response=resp, latency_ms=100,
    )
    bash = {"type":"function","function":{"name":"Bash",
        "parameters":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}}}
    validation = validate_openai_chat_completion(result, tools_schema=[bash])
    assert validation.ok is True


def test_validate_finish_reason_length_text_passes():
    # Pure text truncated by length is still usable — validator should pass it.
    msg = MockMessage(content="Hello")
    choice = MockChoice(message=msg, finish_reason="length")
    resp = MockResponse(choices=[choice])
    result = CandidateResult(candidate_name="test", real_model="test", response=resp, latency_ms=100)
    validation = validate_openai_chat_completion(result)
    assert validation.ok is True


def test_validate_finish_reason_length_with_tool_calls_invalid():
    # Truncated tool calls are incomplete and must be rejected.
    from unittest.mock import MagicMock
    tc = MagicMock()
    tc.function.name = "Bash"
    tc.function.arguments = '{"command": "ls"}'
    msg = MockMessage(content=None, tool_calls=[tc])
    choice = MockChoice(message=msg, finish_reason="length")
    resp = MockResponse(choices=[choice])
    result = CandidateResult(candidate_name="test", real_model="test", response=resp, latency_ms=100)
    validation = validate_openai_chat_completion(result)
    assert validation.ok is False
    assert "length" in validation.reason
