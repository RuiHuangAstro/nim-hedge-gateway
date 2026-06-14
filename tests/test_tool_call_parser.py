import json

from app.tool_call_parser import (
    has_harmony_markers,
    parse_harmony_content,
    repair_response_dict,
)


def _wrap(content, tool_calls=None, finish_reason="stop"):
    return {
        "choices": [
            {
                "message": {"content": content, "tool_calls": tool_calls},
                "finish_reason": finish_reason,
            }
        ]
    }


def test_no_markers_is_noop():
    assert not has_harmony_markers("hello world")
    cleaned, tcs = parse_harmony_content("hello world")
    assert cleaned == "hello world"
    assert tcs == []
    resp = _wrap("hello world")
    report = repair_response_dict(resp)
    assert not report
    assert report.had_markers is False
    assert resp["choices"][0]["message"]["content"] == "hello world"


def test_single_tool_call_kimi_format():
    raw = (
        "<|tool_calls_section_begin|>"
        "<|tool_call_begin|>functions.Bash:0"
        "<|tool_call_argument_begin|>"
        '{"command": "ls"}'
        "<|tool_call_end|>"
        "<|tool_calls_section_end|>"
    )
    cleaned, tcs = parse_harmony_content(raw)
    assert cleaned == ""
    assert len(tcs) == 1
    assert tcs[0]["function"]["name"] == "Bash"
    assert json.loads(tcs[0]["function"]["arguments"]) == {"command": "ls"}
    assert tcs[0]["type"] == "function"
    assert tcs[0]["id"].startswith("call_")


def test_multiple_tool_calls():
    raw = (
        "<|tool_calls_section_begin|>"
        "<|tool_call_begin|>functions.Read:0"
        "<|tool_call_argument_begin|>"
        '{"path": "/a"}'
        "<|tool_call_end|>"
        "<|tool_call_begin|>functions.Read:1"
        "<|tool_call_argument_begin|>"
        '{"path": "/b"}'
        "<|tool_call_end|>"
        "<|tool_calls_section_end|>"
    )
    _, tcs = parse_harmony_content(raw)
    assert len(tcs) == 2
    assert [tc["function"]["name"] for tc in tcs] == ["Read", "Read"]
    assert json.loads(tcs[1]["function"]["arguments"]) == {"path": "/b"}


def test_mixed_text_and_tool_call_preserves_text():
    raw = (
        "I will list files now. "
        "<|tool_call_begin|>functions.Bash:0"
        "<|tool_call_argument_begin|>"
        '{"command": "ls"}'
        "<|tool_call_end|>"
    )
    cleaned, tcs = parse_harmony_content(raw)
    assert cleaned == "I will list files now."
    assert len(tcs) == 1


def test_repair_response_dict_mutates_in_place():
    raw = (
        "<|tool_call_begin|>functions.Bash:0"
        "<|tool_call_argument_begin|>"
        '{"command": "pwd"}'
        "<|tool_call_end|>"
    )
    resp = _wrap(raw)
    report = repair_response_dict(resp)
    assert report.changed is True
    assert report.had_markers is True
    assert report.parsed_calls == 1
    assert report.raw_content == raw
    msg = resp["choices"][0]["message"]
    assert msg["content"] is None
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["function"]["name"] == "Bash"
    assert resp["choices"][0]["finish_reason"] == "tool_calls"


def test_repair_preserves_existing_tool_calls():
    raw = (
        "<|tool_call_begin|>functions.Bash:0"
        "<|tool_call_argument_begin|>"
        '{"command": "ls"}'
        "<|tool_call_end|>"
    )
    existing = [{"id": "pre_1", "type": "function",
                 "function": {"name": "Other", "arguments": "{}"}}]
    resp = _wrap(raw, tool_calls=existing)
    assert repair_response_dict(resp).changed is True
    tcs = resp["choices"][0]["message"]["tool_calls"]
    assert len(tcs) == 2
    assert tcs[0]["id"] == "pre_1"
    assert tcs[1]["function"]["name"] == "Bash"


def test_markers_without_parseable_call_scrubs_tokens():
    # Markers present but no `functions.NAME:` prefix to extract — we should
    # still scrub the raw special tokens out of content.
    raw = "<|tool_calls_section_begin|>some-id{\"x\":1}<|tool_calls_section_end|>"
    resp = _wrap(raw)
    report = repair_response_dict(resp)
    assert report.changed is True
    assert report.had_markers is True
    assert report.parsed_calls == 0
    cleaned = resp["choices"][0]["message"]["content"]
    assert "<|tool_calls_section_begin|>" not in cleaned
    assert "<|tool_calls_section_end|>" not in cleaned
    assert resp["choices"][0]["message"]["tool_calls"] is None


def test_fallback_format_no_arg_separator():
    raw = (
        "<|tool_call_begin|>functions.Bash:0"
        '{"command": "ls"}'
        "<|tool_call_end|>"
    )
    _, tcs = parse_harmony_content(raw)
    assert len(tcs) == 1
    assert tcs[0]["function"]["name"] == "Bash"
    assert json.loads(tcs[0]["function"]["arguments"]) == {"command": "ls"}


# --- Schema-based inference -------------------------------------------------
# Some kimi-k2.6 responses fill the harmony header with a tool-call id like
# `chatcmpl-tool-...` instead of `functions.NAME`. We recover the function
# name by matching the args' keys against the request's `tools` schema.

_BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "Bash",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "number"},
            },
            "required": ["command"],
        },
    },
}

_READ_TOOL = {
    "type": "function",
    "function": {
        "name": "Read",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["file_path"],
        },
    },
}


def _kimi_id_form(call_id: str, args_json: str) -> str:
    return (
        "<|tool_calls_section_begin|>"
        f"<|tool_call_begin|>{call_id}"
        "<|tool_call_argument_begin|>"
        f"{args_json}"
        "<|tool_call_end|>"
        "<|tool_calls_section_end|>"
    )


def test_infer_name_from_args_kimi_chatcmpl_id():
    # Real sample 2 from logs/response_archive.jsonl
    raw = _kimi_id_form(
        "chatcmpl-tool-4beb6d33f42a4b72",
        '{"command": "ls -la /home/huangrui/Data/XARTATOMS/XMM/ODF/0900170101/rpc/ | head -30"}',
    )
    resp = _wrap(raw)
    report = repair_response_dict(resp, tools_schema=[_BASH_TOOL, _READ_TOOL])
    assert report.parsed_calls == 1
    assert report.inferred_calls == 1
    tc = resp["choices"][0]["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "Bash"
    # Internal tag must not leak into the response.
    assert "__nim_proxy_inferred__" not in tc


def test_infer_name_falls_back_when_ambiguous():
    # Both tools have empty `required` — args of {} cannot disambiguate.
    empty_a = {"type": "function", "function": {"name": "A", "parameters": {"type": "object", "properties": {}, "required": []}}}
    empty_b = {"type": "function", "function": {"name": "B", "parameters": {"type": "object", "properties": {}, "required": []}}}
    raw = _kimi_id_form("chatcmpl-tool-xyz", "{}")
    resp = _wrap(raw)
    report = repair_response_dict(resp, tools_schema=[empty_a, empty_b])
    # Could match both → no inference, treated as unparsed.
    assert report.parsed_calls == 0
    assert report.inferred_calls == 0
    assert report.had_markers is True


def test_infer_skipped_when_no_schema_provided():
    # Without tools_schema we must preserve the old behavior: unparsed.
    raw = _kimi_id_form("chatcmpl-tool-xyz", '{"command": "ls"}')
    cleaned, tcs = parse_harmony_content(raw)
    assert tcs == []
    assert "<|tool_call_begin|>" not in cleaned


def test_infer_skipped_when_args_not_json():
    raw = _kimi_id_form("chatcmpl-tool-xyz", "not-json-at-all")
    resp = _wrap(raw)
    report = repair_response_dict(resp, tools_schema=[_BASH_TOOL])
    assert report.parsed_calls == 0
    assert report.inferred_calls == 0


def test_infer_picks_unique_match_with_optional_args():
    # Args include an optional Read parameter (`offset`); only Read's required
    # keys are a subset, so inference should still pick Read uniquely.
    raw = _kimi_id_form(
        "chatcmpl-tool-abc",
        '{"file_path": "/etc/hosts", "offset": 0}',
    )
    resp = _wrap(raw)
    report = repair_response_dict(resp, tools_schema=[_BASH_TOOL, _READ_TOOL])
    assert report.parsed_calls == 1
    assert report.inferred_calls == 1
    assert resp["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "Read"


def test_infer_filters_out_unrelated_tool_with_empty_required():
    # ExitPlanMode-style tool: no required keys, properties don't include
    # `command`. It must NOT compete with Bash for a `{"command": ...}` call.
    exit_plan = {
        "type": "function",
        "function": {
            "name": "ExitPlanMode",
            "parameters": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": []},
        },
    }
    raw = _kimi_id_form("chatcmpl-tool-xyz", '{"command": "ls"}')
    resp = _wrap(raw)
    report = repair_response_dict(resp, tools_schema=[_BASH_TOOL, exit_plan])
    assert report.parsed_calls == 1
    assert report.inferred_calls == 1
    assert resp["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "Bash"


def test_infer_tie_breaks_on_more_specific_required():
    # Two tools both satisfiable; the one with the bigger required set wins.
    edit = {
        "type": "function",
        "function": {
            "name": "Edit",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        },
    }
    raw = _kimi_id_form(
        "chatcmpl-tool-xyz",
        '{"file_path": "/a", "old_string": "x", "new_string": "y"}',
    )
    resp = _wrap(raw)
    report = repair_response_dict(resp, tools_schema=[_READ_TOOL, edit])
    assert report.parsed_calls == 1
    # Both Read (required=[file_path]) and Edit (required=[file_path, old, new])
    # match, but Edit's required is strictly larger → Edit wins.
    assert resp["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "Edit"


def test_infer_gives_up_on_equal_size_required_tie():
    # Two tools with identical-size `required` sets that both fit the args
    # → ambiguous, no inference.
    a = {"type": "function", "function": {"name": "A", "parameters": {"type": "object", "properties": {"x": {}, "y": {}}, "required": ["x"]}}}
    b = {"type": "function", "function": {"name": "B", "parameters": {"type": "object", "properties": {"x": {}, "y": {}}, "required": ["y"]}}}
    raw = _kimi_id_form("chatcmpl-tool-xyz", '{"x": 1, "y": 2}')
    resp = _wrap(raw)
    report = repair_response_dict(resp, tools_schema=[a, b])
    assert report.parsed_calls == 0
    assert report.inferred_calls == 0


def test_format_c_no_separator_with_id_prefix():
    # Real entry 4 sample — `<|tool_call_begin|>chatcmpl-tool-xxx{json}<|tool_call_end|>`
    # with no `<|tool_call_argument_begin|>` separator.
    raw = (
        "Some prefix text. "
        "<|tool_calls_section_begin|>"
        "<|tool_call_begin|>chatcmpl-tool-8a08c2282a7e8c5c"
        '{"command": "ls -la /tmp"}'
        "<|tool_call_end|>"
        "<|tool_calls_section_end|>"
    )
    resp = _wrap(raw)
    report = repair_response_dict(resp, tools_schema=[_BASH_TOOL])
    assert report.parsed_calls == 1
    assert report.inferred_calls == 1
    tc = resp["choices"][0]["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "Bash"
    # text prefix should remain
    assert resp["choices"][0]["message"]["content"] == "Some prefix text."


def test_format_e_section_begin_only_no_call_begin():
    """Real entry 242: <|tool_calls_section_begin|>:call_<HEX>:0<|tool_call_argument_begin|>{json}<|tool_call_end|>
    No <|tool_call_begin|> marker at all — section_begin doubles as the opener.
    """
    write_tool = {
        "type": "function",
        "function": {
            "name": "Write",
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string"}, "path": {"type": "string"}},
                "required": ["content", "path"],
            },
        },
    }
    raw = (
        "Some chain of thought text. "
        "<|tool_calls_section_begin|>"
        ":call_8e5e3e8e5f3242b8b325b325b325b325:0"
        "<|tool_call_argument_begin|>"
        '{"content": "hello", "path": "/tmp/x.md"}'
        "<|tool_call_end|>"
        "<|tool_calls_section_end|>"
    )
    resp = _wrap(raw)
    report = repair_response_dict(resp, tools_schema=[write_tool])
    assert report.parsed_calls == 1
    assert report.inferred_calls == 1
    tc = resp["choices"][0]["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "Write"
    # Leading text preserved (the body must only consume from section_begin).
    assert resp["choices"][0]["message"]["content"] == "Some chain of thought text."


def test_format_d_no_separator_with_functions_prefix():
    # No argument-begin separator, but prefix still says `functions.NAME`.
    raw = (
        "<|tool_call_begin|>functions.Bash:0"
        '{"command": "pwd"}'
        "<|tool_call_end|>"
    )
    _, tcs = parse_harmony_content(raw)
    assert len(tcs) == 1
    assert tcs[0]["function"]["name"] == "Bash"


def test_named_call_alongside_inferred_call():
    # One call has functions.NAME, another has chatcmpl-tool-id form → mixed.
    raw = (
        "<|tool_calls_section_begin|>"
        "<|tool_call_begin|>functions.Read:0"
        "<|tool_call_argument_begin|>"
        '{"file_path": "/a"}'
        "<|tool_call_end|>"
        "<|tool_call_begin|>chatcmpl-tool-deadbeef"
        "<|tool_call_argument_begin|>"
        '{"command": "ls"}'
        "<|tool_call_end|>"
        "<|tool_calls_section_end|>"
    )
    resp = _wrap(raw)
    report = repair_response_dict(resp, tools_schema=[_BASH_TOOL, _READ_TOOL])
    assert report.parsed_calls == 2
    assert report.inferred_calls == 1
    names = [tc["function"]["name"] for tc in resp["choices"][0]["message"]["tool_calls"]]
    assert names == ["Read", "Bash"]
