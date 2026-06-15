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


def _validate_content(content: str):
    resp = MockResponse(choices=[MockChoice(message=MockMessage(content=content))])
    result = CandidateResult(candidate_name="test", real_model="test", response=resp, latency_ms=100)
    return validate_openai_chat_completion(result)


def test_repetition_loop_real_degeneration_rejected():
    # A genuine loop dominates the output -> must be rejected.
    content = "Here is the answer.\n" + ("the same broken line\n" * 60)
    validation = _validate_content(content)
    assert validation.ok is False
    assert "repetition loop" in (validation.reason or "")


def test_repetition_markdown_tables_pass():
    # Legit report with several markdown tables: separators repeat a handful of
    # times but cover ~nothing of the content. Must NOT be flagged (regression:
    # these were cascading whole requests through the pool).
    content = (
        "# RST v3 Deep Optimization - Final Report\n\n"
        "## Summary\nThe optimization completed successfully with strong results "
        "across every regime we tested, and the bull family remained dominant.\n\n"
    )
    for section in ("Bull", "Bear", "Sideways", "Combined"):
        content += (
            f"## {section} regime\n\n"
            "| metric | value | delta |\n"
            "|--------|-------|-------|\n"
            "| sharpe | 1.23  | +0.05 |\n"
            "| return | 0.18  | +0.02 |\n\n"
            "Detailed discussion of the findings for this regime goes here with "
            "enough prose to make the table separators a tiny fraction of text.\n\n"
        )
    validation = _validate_content(content)
    assert validation.ok is True, validation.reason


def test_repetition_check_disabled_passes_loop():
    # nim-fusion calls with check_repetition=False: a genuine loop must pass
    # (the judge filters it), while still being rejected under the default.
    content = "Here is the answer.\n" + ("the same broken line\n" * 60)
    resp = MockResponse(choices=[MockChoice(message=MockMessage(content=content))])
    result = CandidateResult(candidate_name="test", real_model="test", response=resp, latency_ms=100)
    assert validate_openai_chat_completion(result).ok is False
    assert validate_openai_chat_completion(result, check_repetition=False).ok is True


def test_repetition_ascii_chart_passes():
    # ASCII bar chart: ')  0.0h ░░░' style rows repeat per day but are a small
    # fraction of the message. Must NOT be flagged.
    content = (
        "📱⚠️ 手机使用超标！\n\n今天 4:00-15:00: 2h 3min\n7天同时段均值: 46min\n超出: 1h 16min\n\n"
        "📉 近7天同时段对比：\n"
        "  今天(Sun)   2.0h ███████████████ ⬆️\n"
        "  06/13(Sat)   1.2h █████░░░░░░░░░░\n"
        "  06/12(Fri)   0.0h ░░░░░░░░░░░░░░░\n"
        "  06/11(Thu)   0.8h ███░░░░░░░░░░░░\n"
        "  06/10(Wed)   0.0h ░░░░░░░░░░░░░░░\n"
        "  06/09(Tue)   0.0h ░░░░░░░░░░░░░░░\n"
        "  06/08(Mon)   1.7h ███████░░░░░░░░\n"
        "  06/07(Sun)   0.0h ░░░░░░░░░░░░░░░\n\n"
        "💡 建议放下手机，专注工作 🎯"
    )
    validation = _validate_content(content)
    assert validation.ok is True, validation.reason
