"""Recover harmony-style tool calls that some upstreams (notably NVIDIA NIM
serving moonshotai/kimi-k2.x and similar vLLM-based deployments) leak into
`content` as raw special tokens instead of returning structured `tool_calls`.

We post-process the response dict so the OpenAI-compatible client sees proper
tool calls, instead of seeing tokens like `<|tool_call_begin|>...` rendered as
chat text.
"""
import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nim_proxy.tool_call_parser")


@dataclass
class RepairReport:
    changed: bool = False
    had_markers: bool = False
    parsed_calls: int = 0
    inferred_calls: int = 0
    raw_content: Optional[str] = None

    def __bool__(self) -> bool:
        return self.changed

# We've seen at least 5 harmony format variants from kimi-k2.6 in the wild:
#   A) <|tool_call_begin|>functions.NAME:0<|tool_call_argument_begin|>{json}<|tool_call_end|>
#   B) <|tool_call_begin|>chatcmpl-tool-<id><|tool_call_argument_begin|>{json}<|tool_call_end|>
#   C) <|tool_call_begin|>chatcmpl-tool-<id>{json}<|tool_call_end|>
#   D) <|tool_call_begin|>functions.NAME{json}<|tool_call_end|>
#   E) <|tool_calls_section_begin|>:call_<HEX>:0<|tool_call_argument_begin|>{json}<|tool_call_end|>
#      (no <|tool_call_begin|> at all — the section_begin marker is the only opener)
#
# The robust approach: split content on <|tool_call_end|> (always present
# when a call closes), then for each preceding chunk, find the latest
# opener marker (<|tool_call_begin|> or <|tool_calls_section_begin|>, or
# nothing) to identify the call body. Inside the body, split header vs.
# args either at <|tool_call_argument_begin|> if present, or at the first
# `{` if not. B/C/E need schema-inference to recover the function name.
_CALL_END = "<|tool_call_end|>"
_CALL_BEGIN = "<|tool_call_begin|>"
_SECTION_BEGIN = "<|tool_calls_section_begin|>"
_ARG_BEGIN = "<|tool_call_argument_begin|>"

_FUNCTION_PREFIX_RE = re.compile(r"functions\.([A-Za-z0-9_\-]+)(?::\d+)?")

# Internal tag attached to inferred calls so repair_response_dict can count
# them, then stripped before the dict is returned to upstream.
_INFERRED_TAG = "__nim_proxy_inferred__"

_SCRUB_TOKENS = (
    "<|tool_calls_section_begin|>",
    "<|tool_calls_section_end|>",
    "<|tool_call_begin|>",
    "<|tool_call_argument_begin|>",
    "<|tool_call_end|>",
)


def has_harmony_markers(content: str) -> bool:
    return bool(content) and any(t in content for t in _SCRUB_TOKENS)


def _new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"


def _make_tool_call(name: str, args: str, inferred: bool = False) -> Dict[str, Any]:
    # Keep arguments as a JSON string per OpenAI spec, even if upstream gave
    # us malformed JSON — downstream validator will catch malformed args.
    call: Dict[str, Any] = {
        "id": _new_call_id(),
        "type": "function",
        "function": {"name": name, "arguments": args},
    }
    if inferred:
        call[_INFERRED_TAG] = True
    return call


def _infer_tool_name_from_args(
    args_str: str,
    tools_schema: Optional[List[Dict[str, Any]]],
) -> Optional[str]:
    """Pick a tool name when the harmony prefix lacks `functions.NAME`.

    Rules, applied in order:
      1. Parse `args_str` as a JSON object, take its keys.
      2. Keep only tools whose `required` is a subset of those keys.
      3. When the tool declares `parameters.properties`, additionally require
         that every arg key is one of those properties — this filters out
         no-arg / unrelated tools (e.g. ExitPlanMode, TaskList) that would
         otherwise match every Bash-style call by virtue of empty `required`.
      4. If multiple tools still match, prefer the one whose `required` set is
         strictly larger (most specific match). If two tools tie on required
         size, give up and return None.
    """
    if not tools_schema:
        return None
    try:
        args_obj = json.loads(args_str)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(args_obj, dict):
        return None
    arg_keys = set(args_obj.keys())

    matches: List[Tuple[str, int]] = []  # (name, required_size)
    for tool in tools_schema:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        params = fn.get("parameters") or {}
        required = set(params.get("required") or [])
        if not required.issubset(arg_keys):
            continue
        properties = params.get("properties")
        if isinstance(properties, dict) and properties:
            if not arg_keys.issubset(set(properties.keys())):
                continue
        matches.append((name, len(required)))

    if not matches:
        return None
    matches.sort(key=lambda x: x[1], reverse=True)
    if len(matches) == 1 or matches[0][1] > matches[1][1]:
        return matches[0][0]
    return None


def _extract_call_body(chunk: str) -> Optional[Tuple[str, int]]:
    """Given a chunk of text that ends just before <|tool_call_end|>, locate
    the start of the actual tool-call body by walking backwards to the
    latest opener marker. Returns (body, opener_offset_in_chunk) — the
    offset is the index where the call begins (start of the opener token,
    or 0 if no opener found). Returns None when nothing usable is present.

    Walking backwards (rather than forwards) is critical because the chunk
    may contain text/chain-of-thought with stray `{` characters; we only
    want the body of the call closest to <|tool_call_end|>.
    """
    pos_call_begin = chunk.rfind(_CALL_BEGIN)
    pos_section_begin = chunk.rfind(_SECTION_BEGIN)
    if pos_call_begin >= 0 and pos_call_begin > pos_section_begin:
        return chunk[pos_call_begin + len(_CALL_BEGIN):], pos_call_begin
    if pos_section_begin >= 0:
        return chunk[pos_section_begin + len(_SECTION_BEGIN):], pos_section_begin
    return None


def _split_body(body: str) -> Optional[Tuple[str, str]]:
    """Split a tool-call body into (prefix, args). Prefer the explicit
    <|tool_call_argument_begin|> separator; fall back to the first `{`.
    Returns None when no JSON object is found at all.
    """
    if _ARG_BEGIN in body:
        prefix, _, args = body.partition(_ARG_BEGIN)
        prefix = prefix.strip()
        args = args.strip()
        if not args.startswith("{"):
            # Separator was there but args don't look like a JSON object —
            # let the brace-fallback below have a try.
            pass
        else:
            return prefix, args
    brace_idx = body.find("{")
    if brace_idx < 0:
        return None
    prefix = body[:brace_idx].replace(_ARG_BEGIN, "").strip()
    args = body[brace_idx:].strip()
    return prefix, args


def parse_harmony_content(
    content: str,
    tools_schema: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Extract tool calls from harmony-style markers in `content`.

    Returns (cleaned_content, parsed_tool_calls). When markers are absent the
    content is returned unchanged with an empty list.

    `tools_schema`, if provided, lets us recover the function name when the
    upstream filled the header slot with a tool-call id (chatcmpl-tool-...,
    :call_<HEX>:0, plain call_<HEX>, etc.) instead of `functions.NAME`.
    Inferred calls are tagged so the caller can report them separately.
    """
    if not has_harmony_markers(content):
        return content, []

    tool_calls: List[Dict[str, Any]] = []
    matched_any = False
    consumed_spans: List[Tuple[int, int]] = []  # (start, end) of each call

    cursor = 0
    while True:
        end_idx = content.find(_CALL_END, cursor)
        if end_idx < 0:
            break
        chunk = content[cursor:end_idx]
        extraction = _extract_call_body(chunk)
        span_end = end_idx + len(_CALL_END)
        if extraction is not None:
            body, opener_offset = extraction
            split = _split_body(body)
            if split is not None:
                matched_any = True
                prefix, args = split
                # Consume from the opener position (preserves any leading
                # chain-of-thought text), through the closing marker.
                span_start = cursor + opener_offset
                fm = _FUNCTION_PREFIX_RE.match(prefix)
                if fm:
                    tool_calls.append(_make_tool_call(fm.group(1), args))
                    consumed_spans.append((span_start, span_end))
                else:
                    inferred_name = _infer_tool_name_from_args(args, tools_schema)
                    if inferred_name:
                        tool_calls.append(_make_tool_call(inferred_name, args, inferred=True))
                        consumed_spans.append((span_start, span_end))
        cursor = span_end

    if consumed_spans:
        # Build cleaned text by stitching together the spans NOT consumed.
        pieces: List[str] = []
        last = 0
        for s, e in consumed_spans:
            pieces.append(content[last:s])
            last = e
        pieces.append(content[last:])
        cleaned = "".join(pieces)
    else:
        cleaned = content

    if not tool_calls:
        logger.warning(
            "harmony markers present but no tool call parsed; sample=%r",
            content[:500],
        )

    for tok in _SCRUB_TOKENS:
        cleaned = cleaned.replace(tok, "")
    cleaned = cleaned.strip()
    return cleaned, tool_calls


def repair_response_dict(
    response_dict: Dict[str, Any],
    tools_schema: Optional[List[Dict[str, Any]]] = None,
) -> RepairReport:
    """Mutate `response_dict` in place: lift harmony tool calls out of
    `choices[0].message.content` into `tool_calls`.

    Returns a RepairReport. The result is truthy if the dict was mutated,
    so existing call sites of the form `if repair_response_dict(d):` keep
    working. `tools_schema` enables function-name inference when the upstream
    omits `functions.NAME` from the harmony header (see parse_harmony_content).
    """
    report = RepairReport()
    try:
        choice = response_dict["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError):
        return report

    content = message.get("content") or ""
    if not has_harmony_markers(content):
        return report

    report.had_markers = True
    report.raw_content = content
    cleaned, parsed = parse_harmony_content(content, tools_schema=tools_schema)

    if not parsed:
        # No parseable calls, but still scrub stray markers so the user
        # doesn't see raw special tokens leak into chat.
        if cleaned != content:
            message["content"] = cleaned or None
            report.changed = True
        return report

    inferred_count = sum(1 for c in parsed if c.pop(_INFERRED_TAG, False))

    existing = message.get("tool_calls") or []
    message["content"] = cleaned or None
    message["tool_calls"] = list(existing) + parsed
    if choice.get("finish_reason") in (None, "stop"):
        choice["finish_reason"] = "tool_calls"
    report.parsed_calls = len(parsed)
    report.inferred_calls = inferred_count
    report.changed = True
    return report
