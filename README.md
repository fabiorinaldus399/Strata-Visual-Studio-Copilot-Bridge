# Strata → Visual Studio 2026 Copilot bridge

A single-file, stdlib-only local proxy that lets **Visual Studio 2026's Copilot
"Bring Your Own Key" (BYOK)** talk to a **Strata** runtime running on the same
machine.

Visual Studio's Copilot client is strict about the OpenAI wire format. Strata's
`/v1` surface is *close* to the spec but not byte-for-byte compatible, and the
differences are enough to make the client refuse the provider outright. The
bridge sits in between, rewrites the responses into strict OpenAI shape, and
logs every exchange so a failing client can be diagnosed from a text file.

```
Visual Studio 2026  ──HTTP/1.1──►  vs_copilot_bridge.py  ──HTTP/1.1──►  Strata
  OpenAI/JS SDK          127.0.0.1:8081/v1                  127.0.0.1:8080/v1
```

The socket is bound to `127.0.0.1` only and no API key is enforced (the client
sends one, it is forwarded as-is). The bridge appends to `vs_bridge.log` next to
the script; request and response bodies are only written to disk with `--capture`.

---

## Why the bridge is needed

| What Strata does | What Visual Studio expects | Symptom without the bridge |
| --- | --- | --- |
| `GET /v1/models` returns `status` as an object, no `created`/`owned_by` | strict OpenAI model list | model picker shows nothing |
| `GET /v1/models/{id}` not implemented (404) | OpenAI "retrieve model", used to validate a newly added model | *"the model is not available or the id is invalid"* |
| last SSE chunk carries an extra `timings` object | only `id`/`object`/`created`/`model`/`choices`/`usage` | stream rejected / turn never ends |
| deltas carry non-standard `reasoning_content` | `delta.content` / `delta.tool_calls` | thinking text lost or mis-parsed |
| `HTTP/1.0` + `Connection: close` on every response | HTTP/1.1 with proper framing | `fetch`/undici errors on the stream |
| `tool_calls` may name a tool the client never offered, or carry malformed `arguments` JSON | indexed, executable tool calls | tool call never runs, agent loop stalls |

The bridge also accepts the Azure-style paths (`/openai/deployments/{id}/chat/completions`,
`/openai/models`, `/openai/deployments`, plus bare `/models` and `/deployments`), so the
**Azure** provider slot works too. The **Ollama** slot does **not** work: it probes
`/api/tags`, which Strata does not implement. Use the **OpenAI** provider slot in
practice — it is the one that maps cleanly onto `/v1`.

---

## Requirements

- Visual Studio 2026 with **Copilot chat 18.11.1011 or later** (BYOK lives in the
  new **Agent (Preview)** mode; older builds can leave a finished turn stuck —
  see [Known issue](#known-issue-visual-studio-never-releases-the-turn)).
- A Strata runtime serving `http://127.0.0.1:8080/v1`.
- Python 3.8+ on `PATH`. No third-party packages.

## Quick start

1. Start Strata as usual.
2. Start the bridge:

   ```powershell
   python .\vs_copilot_bridge.py --port 8081 --upstream 8080
   ```

3. In Visual Studio: **Chat → model picker → Add a model**
   - Provider: **OpenAI**
   - Base URL: `http://127.0.0.1:8081/v1`
   - API key: anything, e.g. `strata`
   - Model ID: the exact `id` reported by `http://127.0.0.1:8081/v1/models`
   - Input tokens `128000`, output tokens `8192` (match your `n_ctx`)
4. Pick the model and use **Agent (Preview)**.

Stop the bridge with `Ctrl+C` in its window.

## Command-line options

| Option | Default | Meaning |
| --- | --- | --- |
| `--port N` | `8081` | local port to bind (on `127.0.0.1`) |
| `--upstream N` | `8080` | Strata port |
| `--reasoning keep\|drop\|content` | `keep` | pass `reasoning_content` through / remove it / fold it into `delta.content` |
| `--unknown-tools pass\|drop` | `pass` | `drop` removes tool calls for tools VS never offered (or with an empty name), and rewrites `finish_reason: tool_calls` → `stop` when nothing executable is left |
| `--strip-tools` | off | diagnostic: strip `tools`/`tool_choice` from the request so the model can only answer in text |
| `--no-chunked` | off | stream without `Transfer-Encoding: chunked` (for clients that dislike chunk framing) |
| `--capture` | off | write every request/response pair into `capture/` |
| `--debug` | off | also log the tool list VS offered and the last upstream SSE event |

---

## How it works

### Model discovery

- `GET /v1/models` → the upstream list is rebuilt as
  `{id, object: "model", created, owned_by: "strata", architecture:{input_modalities, output_modalities}}`.
- `GET /v1/models/{id}` (and `/openai/models/{id}`, `/openai/deployments/{id}`)
  → looked up by `id` **or any alias**, case-insensitive fallback. A miss
  returns a proper OpenAI error object (`model_not_found`) instead of a bare
  404, so VS shows a useful message and the bridge logs the available ids.

### Chat completions

The response body is re-encoded line by line, not passed through:

1. SSE comments (`: keep-alive`) are preserved.
2. Each `data:` payload is parsed; unknown `delta` keys are dropped, only
   `role`, `content`, `name`, `tool_calls`, `function_call`, `audio` survive.
3. `reasoning_content` is handled per `--reasoning`.
4. `tool_calls` deltas are re-indexed (`index` is always present), `function.arguments`
   always defaults to `""`, and with `--unknown-tools drop` calls VS cannot
   execute are removed.
5. Chunks whose delta is empty and whose `finish_reason` is `null` are dropped
   (Strata emits them; the OpenAI SDK treats them as noise).
6. `usage` is filtered to the standard fields; Strata's `timings` is dropped.
7. `data: [DONE]` is **always** emitted, even if upstream dies mid-stream, so
   the client never waits forever.
8. The body is framed with `Transfer-Encoding: chunked` and terminated with
   `0\r\n\r\n`.

While doing this, the bridge reconstructs the whole turn (`Turn`) so it can log
what the model actually produced and flag tool calls the client cannot run.

### Logging

Every request produces a start line and a summary line in `vs_bridge.log`
(next to the script) and on stdout:

```
21:56:51 INFO  >> POST /v1/chat/completions  req=233540B  ua='OpenAI/JS 5.20.1' auth=yes
21:57:30 INFO  << POST /v1/chat/completions -> 200  39.33s  tools=23 stream=True
                finish=tool_calls events=171 content=0ch reasoning=461ch
                tool_calls=[get_file(120B), get_file(120B)] terminated=True
```

- A `>>` with no matching `<<` means the request is still in flight (or the
  client hung up without the bridge noticing).
- `model-get=<id>` on a model request is VS validating a Model ID; `(not served)`
  means Strata does not know that id and the UI will reject it.
- `finish=stop tool_calls=[-]` means the model ended the turn in plain text.
- `terminated=False` means the stream never reached `[DONE]`.
- `client-disconnected` means VS aborted the request.
- `WARNING UNEXECUTABLE TOOL CALL -> #2 get_file: arguments not valid JSON`
  flags a tool call the harness will never be able to run.

`--capture` additionally writes `capture/<time>_req.json` (the decoded request)
and `capture/<time>_resp.json` (finish reason, usage, and the reassembled tool
calls).

---

## Helper scripts

| Script | What it does |
| --- | --- |
| `test_bridge_sse.py` | offline unit test of the SSE re-encoder, no network: `python test_bridge_sse.py` |
| `vs_turn_verdict.py` | reads VS's chat logs and reports, per session, whether each agent turn was released or left stuck: `python vs_turn_verdict.py` |

The Copilot CLI has its own BYOK mode, which makes it possible to exercise the
exact same agent harness without Visual Studio in the loop:

```powershell
$env:COPILOT_PROVIDER_BASE_URL = "http://127.0.0.1:8081/v1"
$env:COPILOT_PROVIDER_API_KEY  = "strata"
$env:COPILOT_MODEL             = "qwen3.8-flash-next-iq3_s"
& "C:\Program Files\Microsoft Visual Studio\18\Community\Common7\IDE\CopilotCli\copilot.exe" `
    -p "hello" --no-auto-update --output-format text
```

---

## Known issue: Visual Studio never releases the turn

**Requires Copilot chat 18.11.1011 or later.** Older builds — 18.10.1203, the one
shipped with Visual Studio 18.10 — can finish an Agent-mode turn, render the
answer, and still leave the **Stop** button active. Their log shows
`session.idle` delivered and then `CLI event delivery fence was not observed`,
meaning Visual Studio never closes its own CLI event channel; nothing on the
provider side is wrong.

Your build is on the first line of `%TEMP%\VSGitHubCopilotLogs\*.chat.log`
(`Copilot chat version …`), and `python vs_turn_verdict.py` reports per session
how many turns released.

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Model picker is empty | the bridge is not running, or the base URL is missing `/v1` |
| *"the model is not available or the id is invalid"* | the Model ID does not match an `id`/alias in `/v1/models`; the bridge logs `model 'x' is not served by Strata (available: [...])` |
| Stream errors in the client | try `--no-chunked`, and check `terminated=True` in the log |
| Model answers but never uses tools | check `tools=` in the log; `--strip-tools` must be off |
| Model invents tools / emits broken JSON arguments | run with `--unknown-tools drop`; the log flags them as `UNEXECUTABLE TOOL CALL` |
| Thinking text missing | `--reasoning content` folds it into the answer body |
| Nothing in the log at all | VS is not pointing at `http://127.0.0.1:8081/v1` |

## Limitations

- BYOK covers **Chat and Agent mode only**. Inline completions, commit
  messages, embeddings and repository indexing still go through the Copilot
  service.
- BYOK only works in **Agent (Preview)**; the older Ask/Agent BYOK path was
  removed.
- The bridge is a debugging proxy, not a hardened endpoint: no TLS, no
  authentication, single-user, `127.0.0.1` only.
- Upstream timeout is 600 s; very large prompts on CPU inference can approach it.
