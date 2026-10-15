"""Offline checks for the SSE re-encoder in vs_copilot_bridge.py.

No server needed: it feeds hand-written Strata-style SSE lines straight into
SseCanon and asserts the output is strict OpenAI.  Run from this folder:

    python test_bridge_sse.py
"""
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vs_copilot_bridge as b  # noqa: E402

b.log.addHandler(logging.NullHandler())   # the --unknown-tools check logs on purpose
b.log.propagate = False


def chunk(delta, finish=None, **extra):
    """One chat.completion.chunk as Strata would send it."""
    obj = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
           "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    obj.update(extra)
    return "data: " + json.dumps(obj)


# A whole turn, in the order Strata sends it: role, thinking, text, a tool call
# split over two deltas, a final chunk carrying usage plus Strata's "timings".
STREAM = [
    b': keep-alive',
    chunk({"role": "assistant", "content": ""}).encode(),
    chunk({"reasoning_content": "thinking"}).encode(),
    chunk({"content": "hello"}).encode(),
    chunk({"tool_calls": [{"index": 0, "id": "c", "type": "function",
                           "function": {"name": "calc"}}]}).encode(),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"a":1}'}}]},
          finish="tool_calls").encode(),
    chunk({}, finish="stop", usage={"prompt_tokens": 5, "completion_tokens": 7,
                                    "total_tokens": 12},
          timings={"prompt_ms": 1, "predicted_ms": 2, "draft_n": 3}).encode(),
    b'data: [DONE]',
]


def run(stream, offered=None):
    """Feed a stream through one canon + one turn; returns (lines, turn)."""
    canon, turn = b.SseCanon(offered), b.Turn()
    return [out for out in (canon.line(line, turn) for line in stream) if out], turn


# ---------------------------------------------------------------- reasoning mode
for mode in ("keep", "drop", "content"):
    b.REASONING = mode
    lines, _ = run(STREAM)
    body = b"".join(lines).decode()
    assert ("thinking" in body) == (mode != "drop"), f"reasoning={mode}"
    if mode == "content":
        assert '"content":"thinking"' in body, "reasoning not folded into content"
    else:
        assert '"reasoning_content":"thinking"' not in body or mode == "keep"

# ------------------------------------------------------------- strict OpenAI shape
b.REASONING = "keep"
lines, turn = run(STREAM, offered={"calc"})
body = b"".join(lines)
assert b"timings" not in body, "Strata timings leaked to the client"
assert body.endswith(b"data: [DONE]\n\n"), "stream not terminated"
assert any(l.startswith(b":") for l in lines), "keep-alive comments should pass through"

final = json.loads(lines[-2][6:].decode())
assert set(final) <= {"id", "object", "created", "model", "choices", "usage"}, set(final)
assert set(final["choices"][0]) == {"index", "delta", "finish_reason"}
assert set(final["usage"]) == {"prompt_tokens", "completion_tokens", "total_tokens"}, final["usage"]

# the two tool-call fragments must survive, each with the index the SDK needs
deltas = [json.loads(l[6:].decode())["choices"][0]["delta"]
          for l in lines if l.startswith(b"data: {")]
calls = [tc for d in deltas for tc in d.get("tool_calls") or []]
assert len(calls) == 2, calls
assert calls[0]["index"] == 0 and calls[0]["function"]["name"] == "calc"
assert calls[1]["index"] == 0 and calls[1]["function"]["arguments"] == '{"a":1}'

# --------------------------------------------------------------- turn accounting
summary = turn.summary()
assert "finish=stop" in summary and "content=5ch" in summary and "reasoning=8ch" in summary
assert "calc(7B)" in summary, summary
assert turn.problems({"calc"}) == [], turn.problems({"calc"})
print("turn summary:", summary)

# --------------------------------------------------- --unknown-tools drop + finish
b.DROP_UNKNOWN = True
lines, turn = run(STREAM, offered={"other_tool"})
body = b"".join(lines).decode()
assert "calc" not in body, "unknown tool call was not dropped"
assert '"finish_reason":"stop"' in body, "finish_reason should fall back to stop"
assert turn.problems({"other_tool"}), "the dropped call should be reported as a problem"
b.DROP_UNKNOWN = False

# ------------------------------------------------------------------ canon_sse_line
assert b.canon_sse_line(b'data: [DONE]') == b"data: [DONE]\n\n"
assert b.canon_sse_line(b'') is None
print("all SSE canon checks OK")
