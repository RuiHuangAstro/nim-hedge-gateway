import json
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel
from app.models import CandidateResult
from app.tool_call_parser import has_harmony_markers, parse_harmony_content

# Sentinel reason strings. Hedger checks these prefixes to pick archive category / event type.
HARMONY_UNPARSED_REASON_PREFIX = "harmony markers present, no tool call extractable"
REPETITION_LOOP_REASON_PREFIX = "repetition loop:"


def _detect_repetition_loop(content: str) -> Tuple[bool, Optional[str]]:
    """Return (True, reason) if content looks like a model repetition degeneration loop.

    Two detection modes:
      1. Medium ngrams (12-59 chars) repeated >=4 times AND covering >50% of the
         content (a true loop dominates the output; a few repeated markdown table
         separators or ASCII-chart rows do not).
      2. Short non-space ngrams (2-4 chars) repeated >=5 times AND covering >50%
         of the content length (catches patterns like "3.3.3.3.3" or "X,X,X,X,X").

    Ngrams that start or end with a space are skipped to avoid matching
    common words like "the " or " and" that appear naturally in prose.
    """
    if not content:
        return False, None
    for ngram_len in range(12, min(60, len(content) // 4 + 1)):
        for i in range(len(content) - ngram_len * 4 + 1):
            ngram = content[i:i + ngram_len]
            if ngram[0] == ' ' or ngram[-1] == ' ':
                continue
            if ngram[0] == ',':
                continue
            if not ngram.strip():
                continue
            count = 0
            pos = 0
            while True:
                pos = content.find(ngram, pos)
                if pos == -1:
                    break
                count += 1
                pos += ngram_len
            # A genuine degeneration loop dominates the output. Legit structured
            # content (markdown table separators, ASCII bar charts) repeats short
            # structural ngrams a handful of times but they cover only a few %
            # of the response — so require coverage like Mode 2, not just count.
            coverage = count * ngram_len / len(content)
            if count >= 4 and coverage > 0.5:
                return True, f"repetition loop: {ngram[:20]!r} repeated {count}x ({coverage:.0%} of content)"
    for ngram_len in range(2, 5):
        for i in range(len(content) - ngram_len * 5 + 1):
            ngram = content[i:i + ngram_len]
            if not ngram.strip() or ' ' in ngram:
                continue
            count = 0
            pos = 0
            while True:
                pos = content.find(ngram, pos)
                if pos == -1:
                    break
                count += 1
                pos += ngram_len
            if count >= 5 and count * ngram_len / len(content) > 0.5:
                return True, f"repetition loop: {ngram!r} repeated {count}x ({count * ngram_len / len(content):.0%} of content)"

    # Mode 3: token-space leak — every token separated by double spaces, causing
    # space_ratio > ~45%. Also require one non-space character dominates the
    # stripped content (confirms degeneration rather than just sparse formatting).
    if len(content) >= 20:
        space_ratio = content.count(' ') / len(content)
        if space_ratio >= 0.45:
            stripped = content.replace(' ', '')
            if stripped:
                top_char, top_count = Counter(stripped).most_common(1)[0]
                if top_count / len(stripped) > 0.25:
                    return True, f"token-space leak: space_ratio={space_ratio:.0%}, top_char={top_char!r} dominates non-space content"

    return False, None


class ValidationResult(BaseModel):
    ok: bool
    reason: Optional[str] = None

def validate_openai_chat_completion(
    result: CandidateResult,
    tools_schema: Optional[List[Dict[str, Any]]] = None,
) -> ValidationResult:
    if not result.response:
        return ValidationResult(ok=False, reason="No response object")

    try:
        # LiteLLM response object usually has choices
        choices = getattr(result.response, 'choices', [])
        if not choices:
            return ValidationResult(ok=False, reason="Empty choices")

        choice = choices[0]
        message = getattr(choice, 'message', None)
        if not message:
            return ValidationResult(ok=False, reason="No message in choice")

        content = getattr(message, 'content', None)
        tool_calls = getattr(message, 'tool_calls', None)

        # Must have either content or tool_calls
        has_content = content is not None and len(str(content).strip()) > 0
        has_tool_calls = tool_calls is not None and len(tool_calls) > 0

        if not has_content and not has_tool_calls:
            return ValidationResult(ok=False, reason="Empty content and no tool calls")

        # Validate tool calls if present
        if has_tool_calls:
            for tc in tool_calls:
                function = getattr(tc, 'function', None)
                if not function:
                    return ValidationResult(ok=False, reason="Tool call missing function")

                name = getattr(function, 'name', None)
                if not name:
                    return ValidationResult(ok=False, reason="Tool call missing function name")

                arguments = getattr(function, 'arguments', None)
                if arguments:
                    try:
                        json.loads(arguments)
                    except json.JSONDecodeError:
                        return ValidationResult(ok=False, reason=f"Invalid JSON in tool call arguments for {name}")

        # Reject responses where content carries harmony tool-call markers but
        # the parser can't extract a single call (with name inference if a
        # schema is provided). These responses are functionally empty —
        # hermes would just retry — so treat them as failures here and let
        # the hedger keep waiting for a healthier candidate.
        if has_content and not has_tool_calls and has_harmony_markers(str(content)):
            _, parsed = parse_harmony_content(str(content), tools_schema=tools_schema)
            if not parsed:
                sample = str(content)[:200]
                return ValidationResult(
                    ok=False,
                    reason=f"{HARMONY_UNPARSED_REASON_PREFIX} (sample={sample!r})",
                )

        finish_reason = getattr(choice, 'finish_reason', None)
        # Reject truncated responses only when tool calls are involved
        # (truncated tool calls are incomplete). Pure-text responses that
        # hit max_tokens are still useful (e.g. "This image is completely black").
        if finish_reason == "length":
            if has_tool_calls:
                return ValidationResult(ok=False, reason="Finish reason is 'length' with tool calls (truncated)")
            if has_content and has_harmony_markers(str(content)):
                return ValidationResult(ok=False, reason="Finish reason is 'length' with harmony markers (truncated)")

        # Detect repetition degeneration loops (e.g. "adorns:0.20000, and:0.20000, and:...")
        # Only check plain-text content; harmony-marker content is already handled above.
        if has_content and not has_tool_calls and not has_harmony_markers(str(content)):
            is_loop, loop_reason = _detect_repetition_loop(str(content))
            if is_loop:
                return ValidationResult(ok=False, reason=loop_reason)

        # Reject suspiciously fast micro-responses: very few completion tokens returned
        # almost instantly for a large prompt. Legitimate short answers to large prompts
        # still take several seconds; sub-4s with <=5 tokens on a >5k prompt is a model
        # collapse (e.g. kimi returning "odb", glm5 returning "飞燕回家").
        usage = getattr(result.response, 'usage', None)
        if usage is not None and result.latency_ms < 4000:
            ct = getattr(usage, 'completion_tokens', None)
            pt = getattr(usage, 'prompt_tokens', None)
            if not isinstance(ct, int) or not isinstance(pt, int):
                ct = pt = None
            if ct is not None and ct <= 5 and pt > 5000:
                return ValidationResult(
                    ok=False,
                    reason=f"suspiciously fast micro-response: {ct} completion tokens in {result.latency_ms}ms for {pt} prompt tokens",
                )

        return ValidationResult(ok=True)

    except Exception as e:
        return ValidationResult(ok=False, reason=f"Validation error: {str(e)}")
