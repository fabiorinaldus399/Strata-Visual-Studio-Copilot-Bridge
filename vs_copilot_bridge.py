"""Bridge between Visual Studio 2026 (Copilot BYOK) and a local Strata server.

Strata's OpenAI-compatible surface is close to the spec but not byte-for-byte
compatible, which is enough to break Visual Studio's client:

  * GET /v1/models          -> "status" is an object, "created"/"owned_by" missing
                               => the model picker shows nothing
  * GET /v1/models/{id}     -> not implemented (404)
                               => "the model is not available or the id is invalid"
  * streaming chat          -> extra "timings" object on the last chunk and
                               non-standard "reasoning_content" deltas
  * HTTP/1.0 + Connection: close on every request

This proxy normalises all of that and logs every request, so a failing client can
be diagnosed from vs_bridge.log.  Stdlib only, binds to 127.0.0.1 only.

Usage:  python vs_copilot_bridge.py [--port 8081] [--upstream 8080]
                                    [--reasoning keep|drop|content]
                                    [--unknown-tools pass|drop] [--strip-tools]
                                    [--no-chunked] [--capture] [--debug]

Reading vs_bridge.log:
  ">> POST /v1/chat/completions ..."   request arrived.  No matching "<<" line
                                       means it is still in flight.
  "<< POST ... -> 200 3.4s tools=23 stream=True finish=stop events=226
     content=680ch reasoning=81ch tool_calls=[-] terminated=True"
     the turn as the model produced it; terminated=True means data: [DONE] was
     sent, "client-disconnected" means Visual Studio aborted the request.

Not caused by this bridge, but worth knowing when debugging: Visual Studio 2026
(18.10) sometimes never releases a finished turn -- the Stop button stays active
even though the agent runtime reported the turn idle.  See README.md, section
"Known issue", for the evidence and the workarounds.
"""

import argparse
import http.client
import json
import logging
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ------------------------------------------------------------------ settings
# Module level on purpose: they are parsed once from argv in main() and read by
# the SSE re-encoder while it works.  test_bridge_sse.py also sets them directly.
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 8080
UPSTREAM_TIMEOUT = 600        # generous: large prompts on CPU inference are slow
REASONING = "keep"            # keep | drop | content  (what to do with reasoning_content)
DROP_UNKNOWN = False          # drop tool calls for tools the client never offered
STRIP_TOOLS = False           # diagnostic: remove "tools" so the model can only answer in text
CHUNKED = True                # frame the SSE response with Transfer-Encoding: chunked
CAPTURE = False               # dump every request/response pair into capture/
DEBUG = False


# ------------------------------------------------------------------- routing
# Azure OpenAI slot: /openai/deployments/<deployment>/chat/completions
DEPLOY_PATH = re.compile(r"^/openai/deployments/([^/]+)/(chat/completions|completions|embeddings)$")
# Visual Studio validates a model id with GET /v1/models/{id} (OpenAI "retrieve
# model"), which Strata does not implement -> 404 -> "the model is not available
# or the id is invalid".  The bridge answers it itself.
MODEL_GET = re.compile(r"^/(?:v1/)?models/(.+)$")
DEPLOY_GET = re.compile(r"^/openai/(?:deployments|models)/([^/]+)$")
MODELS_LIST = ("/v1/models", "/models")
AZURE_MODELS_LIST = ("/openai/models", "/openai/deployments", "/deployments")

# Keys the OpenAI chat-completion delta and usage objects are allowed to have.
# Anything else (Strata's "timings", vendor extras) is dropped.
DELTA_FIELDS = ("role", "content", "name", "tool_calls", "function_call", "audio")
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens",
                "prompt_tokens_details", "completion_tokens_details")

log = logging.getLogger("vs-bridge")


# -------------------------------------------------------------- model listing
def canonical_models(payload):
    """Strata's model list -> strict OpenAI model list."""
    now = int(time.time())
    data = []
    for m in payload.get("data", []):
        entry = {"id": m["id"], "object": "model", "created": m.get("created", now),
                 "owned_by": "strata"}
        arch = m.get("architecture")
        if isinstance(arch, dict):
            entry["architecture"] = {"input_modalities": arch.get("input_modalities", ["text"]),
                                     "output_modalities": arch.get("output_modalities", ["text"])}
        data.append(entry)
    return {"object": "list", "data": data}


def azure_models(payload):
    """Same list in the Azure OpenAI list-deployments shape."""
    return {"data": [{"id": m["id"], "modelName": m["id"], "status": "succeeded"}
                     for m in payload.get("data", [])]}


# --------------------------------------------------- turn accounting + SSE canon
class Turn:
    """What the model actually produced, reconstructed from the deltas.

    Purely for logging and for deciding whether a tool call is executable; it
    never changes what goes back to the client.
    """

    def __init__(self):
        self.finish = None
        self.calls = {}          # index -> {"name": str, "arguments": str}
        self.dropped = set()     # call indexes we removed from the stream
        self.content = 0
        self.reasoning = 0
        self.usage = None
        self.events = 0

    def record(self, obj):
        """Fold one decoded SSE chunk into the running picture of the turn."""
        self.events += 1
        if obj.get("usage"):
            self.usage = obj["usage"]
        for ch in obj.get("choices") or []:
            if ch.get("finish_reason"):
                self.finish = ch["finish_reason"]
            delta = ch.get("delta") or {}
            self.content += len(delta.get("content") or "")
            self.reasoning += len(delta.get("reasoning_content") or "")
            for tc in delta.get("tool_calls") or []:
                slot = self.calls.setdefault(tc.get("index", 0), {"name": "", "arguments": ""})
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]          # names/arguments arrive split
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    def problems(self, offered):
        """Tool calls the client cannot execute: empty name, bad JSON, unknown tool."""
        bad = []
        for idx, c in sorted(self.calls.items()):
            name = c["name"].strip()
            if not name:
                bad.append(f"#{idx} empty tool name")
                continue
            try:
                json.loads(c["arguments"] or "")
            except Exception:
                bad.append(f"#{idx} {name}: arguments not valid JSON")
            if offered and name not in offered:
                bad.append(f"#{idx} {name}: not in the tool list VS sent")
        return bad

    def summary(self):
        """One-line description used in the "<< ..." log line."""
        calls = ", ".join(f"{c['name'] or '?'}({len(c['arguments'])}B)"
                          for c in self.calls.values()) or "-"
        return (f"finish={self.finish} events={self.events} content={self.content}ch "
                f"reasoning={self.reasoning}ch tool_calls=[{calls}]")


class SseCanon:
    """Rewrites Strata SSE chunks into strict OpenAI shape, one request at a time.

    One instance per streamed response: it holds the tool names that response
    offered, which is what makes an "unknown tool" detectable.
    """

    def __init__(self, offered=None):
        self.offered = offered or set()

    def line(self, raw, turn):
        """raw = one SSE line (bytes).  Returns the line to forward, or None to drop it."""
        raw = raw.rstrip(b"\r")
        if not raw.strip():
            return None                                            # canon adds its own blank line
        if raw.startswith(b":"):                                   # comment / keep-alive
            return raw + b"\n"
        if not raw.startswith(b"data:"):
            return raw + b"\n"
        payload = raw[5:].strip()
        if payload == b"[DONE]":
            return b"data: [DONE]\n\n"
        try:
            obj = json.loads(payload)
        except Exception:
            return raw + b"\n"
        if not isinstance(obj, dict):
            return raw + b"\n"

        turn.record(obj)
        choices = []
        for ch in obj.get("choices") or [{}]:
            delta_in = ch.get("delta") or {}
            delta = {k: v for k, v in delta_in.items() if k in DELTA_FIELDS}

            reasoning = delta_in.get("reasoning_content")
            if reasoning:
                if REASONING == "content":
                    delta["content"] = reasoning                   # show thinking as the answer
                elif REASONING == "keep":
                    delta["reasoning_content"] = reasoning         # pass through untouched
                # "drop": leave it out of delta entirely

            if "tool_calls" in delta:
                keep = []
                for k, tc in enumerate(delta["tool_calls"]):
                    idx = tc.get("index", k)                       # index is mandatory for the SDK
                    name = (tc.get("function") or {}).get("name")
                    if DROP_UNKNOWN:
                        if idx in turn.dropped:
                            continue                               # later fragments of a dropped call
                        if not name or (self.offered and name not in self.offered):
                            log.warning("dropping tool call #%s %r: VS cannot execute it",
                                        idx, name or "")
                            turn.dropped.add(idx)
                            continue
                    tc = dict(tc)
                    tc["index"] = idx
                    fn = dict(tc.get("function") or {})
                    fn.setdefault("arguments", "")
                    tc["function"] = fn
                    keep.append(tc)
                if keep:
                    delta["tool_calls"] = keep
                else:
                    delta.pop("tool_calls")

            finish = ch.get("finish_reason")
            if finish == "tool_calls" and turn.dropped >= set(turn.calls):
                finish = "stop"                                    # nothing left to execute
            if not delta and finish is None:
                continue                                           # empty delta: noise
            choices.append({"index": ch.get("index", 0), "delta": delta, "finish_reason": finish})
        if not choices:
            return None

        out = {"id": obj.get("id", "chatcmpl-bridge"), "object": "chat.completion.chunk",
               "created": obj.get("created", int(time.time())), "model": obj.get("model", "")}
        out["choices"] = choices
        if obj.get("usage"):
            out["usage"] = {k: v for k, v in obj["usage"].items() if k in USAGE_FIELDS}
        return b"data: " + json.dumps(out, separators=(",", ":")).encode() + b"\n\n"


def canon_sse_line(line):
    """Convenience wrapper (used by the offline test)."""
    return SseCanon().line(line, Turn())


# ---------------------------------------------------------------------- proxy
class Handler(BaseHTTPRequestHandler):
    """One instance per connection.  Every response ends with Connection: close,
    which keeps the proxy stateless and matches what the client expects.

    self.note carries what the request turned out to be (offered tools, model id,
    turn summary); the helpers fill it in and _handle logs it once at the end,
    including when the request fails half-way through.
    """

    protocol_version = "HTTP/1.1"
    server_version = "StrataVSBridge/1.0"

    # ---------------------------------------------------------------- plumbing
    def _upstream(self, method, path, body, headers):
        """Re-issue a request against Strata, dropping the hop-by-hop headers."""
        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=UPSTREAM_TIMEOUT)
        hdrs = {k: v for k, v in headers.items()
                if k.lower() not in ("host", "connection", "accept-encoding", "content-length")}
        hdrs["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"
        if body:
            hdrs["Content-Length"] = str(len(body))
        conn.request(method, path, body=body, headers=hdrs)
        return conn, conn.getresponse()

    def _upstream_models(self):
        """Strata's raw model list, decoded.  Shared by the three model endpoints."""
        conn, resp = self._upstream("GET", "/v1/models", b"", self.headers)
        try:
            return json.loads(resp.read() or b"{}")
        finally:
            conn.close()

    def _send(self, status, payload, ctype="application/json"):
        """Send one complete, Content-Length framed response and close.

        A client that disappeared while we were working is not worth a
        traceback: the answer simply cannot be delivered any more.
        """
        blob = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Connection", "close")
        try:
            self.end_headers()
            self.wfile.write(blob)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            log.info("client went away, the %s response was not delivered", status)
        self.close_connection = True

    def _emit(self, payload):
        """Write one piece of a streamed body, using HTTP/1.1 chunk framing when asked for."""
        if not payload:
            return
        if CHUNKED:
            self.wfile.write(b"%X\r\n%s\r\n" % (len(payload), payload))
        else:
            self.wfile.write(payload)
        self.wfile.flush()

    # ----------------------------------------------------------- entry points
    def do_GET(self):
        self._handle(b"")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._handle(self.rfile.read(length) if length else b"")

    # Any other verb is answered like a GET, so an unexpected one still gets a
    # logged, proxied answer instead of BaseHTTPRequestHandler's canned 501.
    do_DELETE = do_PUT = do_OPTIONS = do_HEAD = do_GET

    # ------------------------------------------------------------- dispatching
    def _handle(self, body):
        """Route one request; always log the one-line summary for it."""
        raw_path = self.path
        path, _, query = raw_path.partition("?")
        started = time.time()
        status, self.note = 500, ""

        log.info(">> %s %s  req=%dB  ua=%r auth=%s", self.command, raw_path, len(body),
                 (self.headers.get("User-Agent") or "")[:40],
                 "yes" if self.headers.get("Authorization") else "no")

        try:
            path, body = self._azure_deployment(path, body)

            if path in MODELS_LIST:                                # OpenAI model picker
                self._send(200, canonical_models(self._upstream_models()))
                status = 200
            elif path in AZURE_MODELS_LIST:                        # Azure model picker
                self._send(200, azure_models(self._upstream_models()))
                status = 200
            elif MODEL_GET.match(path) or DEPLOY_GET.match(path):  # model id validation
                status = self._model_get(path)
            else:                                                  # chat / completions / ...
                status = self._forward(path, query, body)

        except Exception as exc:                                   # noqa: BLE001
            log.warning("%s %s -> ERROR %s", self.command, raw_path, exc)
            self._send(502, {"error": {"message": f"bridge: {exc}"}})
            status = 502

        finally:
            log.info("<< %s %s -> %s  %.2fs  %s", self.command, raw_path, status,
                     time.time() - started, self.note)

    def _azure_deployment(self, path, body):
        """Map the Azure slot's /openai/deployments/<id>/<endpoint> onto /v1.

        The deployment name is the model, so it is injected into the body.
        Returns the originals, unchanged, when the path does not match.
        """
        m = DEPLOY_PATH.match(path)
        if not m:
            return path, body
        model, tail = m.group(1), m.group(2)
        if body:
            try:
                req = json.loads(body)
                req["model"] = model
                body = json.dumps(req).encode()
            except Exception:                                      # noqa: BLE001
                pass                                               # not JSON: forward as-is
        self.note = f"azure-deployment={model}"
        return f"/v1/{tail}", body

    def _model_get(self, path):
        """Answer GET /v1/models/{id}, which Strata does not implement.

        Visual Studio will not accept a provider until this call returns the
        model, so the lookup also accepts aliases and falls back to a
        case-insensitive id match.  A miss returns a proper OpenAI error object
        instead of a bare 404, and logs what Strata does serve.
        """
        want = (MODEL_GET.match(path) or DEPLOY_GET.match(path)).group(1)
        payload = self._upstream_models()
        found = next((d for d in payload.get("data", [])
                      if want in [d["id"], *(d.get("aliases") or [])]
                      or want.lower() == str(d["id"]).lower()), None)

        if found is None:
            log.warning("model %r is not served by Strata (available: %s)", want,
                        [d["id"] for d in payload.get("data", [])])
            self._send(404, {"error": {"message": f"The model '{want}' does not exist or you "
                                                  "do not have access to it.",
                                       "type": "invalid_request_error", "param": "model",
                                       "code": "model_not_found"}})
            self.note = f"model-get={want} (not served)"
            return 404

        if DEPLOY_GET.match(path):
            self._send(200, {"id": found["id"], "modelName": found["id"], "modelVersion": "1",
                             "owner": "strata", "status": "succeeded"})
        else:
            self._send(200, canonical_models({"data": [found]})["data"][0])
        self.note = f"model-get={want}"
        return 200

    # -------------------------------------------------- chat / completions path
    @staticmethod
    def _request_tools(body):
        """The decoded request and the set of tool names the client offered."""
        req_json, offered = None, set()
        if body:
            try:
                req_json = json.loads(body)
                offered = {t.get("function", {}).get("name") for t in req_json.get("tools") or []}
                offered.discard(None)
            except Exception:                                      # noqa: BLE001
                pass                                               # not JSON: forward as-is
        return req_json, offered

    def _forward(self, path, query, body):
        """Proxy everything that is not model discovery, streaming when upstream streams."""
        req_json, offered = self._request_tools(body)

        if STRIP_TOOLS and isinstance(req_json, dict) and req_json.get("tools"):
            log.info("strip-tools: dropping %d tool definitions, model can only answer in text",
                     len(req_json["tools"]))
            req_json.pop("tools", None)
            req_json.pop("tool_choice", None)
            body = json.dumps(req_json).encode()
            offered = set()

        if offered:
            self.note = f"tools={len(offered)} stream={bool(req_json and req_json.get('stream'))}"
        if DEBUG and offered:
            log.info("VS offered tools: %s", sorted(offered))

        fwd = f"{path}?{query}" if query else path
        conn, resp = self._upstream(self.command, fwd, body, self.headers)
        ctype = resp.getheader("Content-Type", "application/json")

        if "text/event-stream" in ctype:
            return self._stream_sse(conn, resp, offered, req_json)

        blob = resp.read()
        conn.close()
        self._send(resp.status, blob, ctype)
        return resp.status

    def _stream_sse(self, conn, resp, offered, req_json):
        """Re-encode Strata's SSE body, then report what the turn contained."""
        self.send_response(resp.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        if CHUNKED:
            self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        canon, turn = SseCanon(offered), Turn()
        buf, done, client_gone, last = b"", False, False, b""
        try:
            while not done:
                chunk = resp.read(2048)
                if not chunk:
                    break
                buf += chunk
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line, buf = buf[:nl], buf[nl + 1:]
                    if line.strip().startswith(b"data:"):
                        last = line.strip()
                    out = canon.line(line, turn)
                    if out is None:
                        continue
                    self._emit(out)
                    if out.startswith(b"data: [DONE]"):
                        done = True
                        break
            if not done:                                  # upstream died mid-stream
                out = canon.line(buf, turn)
                if out:
                    self._emit(out)
                self._emit(b"data: [DONE]\n\n")           # always terminate the turn
                done = True
            if CHUNKED:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            client_gone = True                            # VS pressed Stop or closed
        finally:
            conn.close()

        self.note = f"{self.note} {turn.summary()} terminated={done}".strip()
        for problem in turn.problems(offered):
            log.warning("UNEXECUTABLE TOOL CALL -> %s", problem)
        if client_gone:
            self.note += " client-disconnected"
        if DEBUG:
            log.info("last upstream event: %s", last[:400].decode("utf-8", "replace"))
        if CAPTURE:
            _dump(req_json, turn)
        return resp.status

    def log_message(self, fmt, *args):                    # keep it quiet
        pass


# -------------------------------------------------------------------- capture
def _dump(req_json, turn):
    """Write one request/response pair into capture/ (only with --capture)."""
    try:
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capture")
        os.makedirs(here, exist_ok=True)
        name = time.strftime("%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
        with open(os.path.join(here, name + "_req.json"), "w", encoding="utf-8") as fh:
            json.dump(req_json, fh, ensure_ascii=False, indent=1)
        reply = {"finish_reason": turn.finish, "usage": turn.usage,
                 "content_chars": turn.content, "reasoning_chars": turn.reasoning,
                 "tool_calls": [c for _, c in sorted(turn.calls.items())]}
        with open(os.path.join(here, name + "_resp.json"), "w", encoding="utf-8") as fh:
            json.dump(reply, fh, ensure_ascii=False, indent=1)
    except Exception as exc:                               # noqa: BLE001
        log.warning("capture failed: %s", exc)


# -------------------------------------------------------------- entry point
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--upstream", type=int, default=8080)
    ap.add_argument("--reasoning", choices=["keep", "drop", "content"], default="keep",
                    help="keep: pass reasoning_content through; drop: remove it; "
                         "content: fold it into delta.content")
    ap.add_argument("--unknown-tools", choices=["pass", "drop"], default="pass",
                    help="drop tool calls VS cannot execute (unknown or unnamed tool)")
    ap.add_argument("--strip-tools", action="store_true",
                    help="diagnostic: never give the model any tool, so every turn ends in text")
    ap.add_argument("--no-chunked", action="store_true",
                    help="stream without Transfer-Encoding: chunked")
    ap.add_argument("--capture", action="store_true",
                    help="write every request/response pair into capture/")
    ap.add_argument("--debug", action="store_true", help="log the offered tools and last SSE event")
    args = ap.parse_args()

    global UPSTREAM_PORT, REASONING, DROP_UNKNOWN, STRIP_TOOLS, CHUNKED, CAPTURE, DEBUG
    UPSTREAM_PORT, REASONING = args.upstream, args.reasoning
    DROP_UNKNOWN = args.unknown_tools == "drop"
    STRIP_TOOLS, CAPTURE, DEBUG = args.strip_tools, args.capture, args.debug
    CHUNKED = not args.no_chunked

    here = os.path.dirname(os.path.abspath(__file__))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(os.path.join(here, "vs_bridge.log"), encoding="utf-8")],
    )

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    log.info("VS Copilot bridge on http://127.0.0.1:%d/v1  ->  Strata http://127.0.0.1:%d"
             "  (reasoning=%s unknown-tools=%s strip-tools=%s chunked=%s capture=%s)",
             args.port, UPSTREAM_PORT, REASONING, "drop" if DROP_UNKNOWN else "pass",
             STRIP_TOOLS, CHUNKED, CAPTURE)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
