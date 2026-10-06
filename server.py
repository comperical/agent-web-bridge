#!/usr/bin/env python3
"""agent-web-bridge: run Claude Code headless and drive it from a browser.

    python3 server.py [--cwd DIR] [--port N] [-- <extra claude args>]

The agent communicates mainly through a *canvas*: a web page it designs with
MCP tools served from this process (/mcp). The canvas runs in a sandboxed,
network-less iframe; the host page around it (input, stop, status, transcript)
is trusted code the agent cannot touch.

Output events stream to the browser over SSE (/events); user turns arrive via
POST /send. Every event is kept in memory so a reloaded tab replays the whole
session.

Importing this module has no side effects. From another script:

    from agent import Agent
    from server import serve
    serve(Agent("~/code/some-repo", ["--model", "sonnet"], port=4477))
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file

from agent import CANVAS_TOOLS, Agent, RateLimited, ToolError

ROOT = Path(__file__).parent
INDEX_HTML = ROOT / "public" / "index.html"

# Library paths only (not jsDelivr's /gh/ etc.), to narrow what a URL can carry.
SCRIPT_CDNS = "https://cdnjs.cloudflare.com/ajax/libs/ https://cdn.jsdelivr.net/npm/"
# Sent as an HTTP header on every canvas document, so nothing the agent writes
# can loosen it. No network access at all beyond the CDN/font allowlist.
CANVAS_CSP = "; ".join([
    "default-src 'none'",
    f"script-src 'unsafe-inline' {SCRIPT_CDNS}",
    f"style-src 'unsafe-inline' https://fonts.googleapis.com {SCRIPT_CDNS}",
    f"font-src data: https://fonts.gstatic.com {SCRIPT_CDNS}",
    "img-src data: blob:",
    "media-src data: blob:",
    "connect-src 'none'",
    "form-action 'none'",
    "frame-src 'none'",
    "worker-src 'none'",
    "base-uri 'none'",
    "sandbox allow-scripts allow-forms",
])

COOKIE = "bridge_token"
UNAUTHORIZED_PAGE = """<!doctype html><meta charset="utf-8"><title>Agent Web Bridge</title>
<body style="font:15px/1.5 -apple-system,sans-serif;max-width:560px;margin:15vh auto;padding:0 16px">
<h2>Missing or stale access token</h2>
<p>Open the full URL that <code>server.py</code> printed when it started, including
<code>?token=&hellip;</code>. A new token is generated on every start unless
<code>BRIDGE_TOKEN</code> is set. After one visit with the token, a cookie keeps you signed in.</p>
</body>"""


def sse(event_id: int | None, data: dict) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}data: {json.dumps(data)}\n\n"


# ---- MCP (streamable HTTP, JSON responses only) ----------------------------


def call_tool(agent: Agent, name: str, arguments: dict) -> str:
    if name == "render":
        return agent.canvas_render(str(arguments.get("html", "")))
    if name == "update":
        values = arguments.get("values")
        if not isinstance(values, dict) or not values:
            raise ToolError("values must be a non-empty object of slot name -> value")
        return agent.canvas_update(values)
    if name == "patch":
        ops = arguments.get("ops")
        if not isinstance(ops, list) or not ops:
            raise ToolError("ops must be a non-empty array")
        return agent.canvas_patch(ops)
    if name == "script":
        return agent.canvas_patch([{"selector": "body", "action": "script", "js": str(arguments.get("js", ""))}])
    if name == "state":
        return agent.canvas_state()
    raise ToolError(f"unknown tool {name}")


def handle_rpc(agent: Agent, method: str, params: dict) -> dict:
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agent-web-bridge-canvas", "version": "0.1"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": CANVAS_TOOLS}
    if method == "tools/call":
        try:
            text = call_tool(agent, params.get("name", ""), params.get("arguments") or {})
            return {"content": [{"type": "text", "text": text}]}
        except ToolError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    raise LookupError(method)


# ---- http -------------------------------------------------------------------


def create_app(agent: Agent) -> Flask:
    """A Flask app serving the browser UI, the canvas, and MCP for `agent`."""
    app = Flask(__name__)

    @app.before_request
    def check_token():
        if request.path == "/favicon.ico":
            return "", 204
        supplied = (
            request.args.get("token")
            or request.headers.get("X-Bridge-Token")
            or request.cookies.get(COOKIE)
        )
        if not supplied or not secrets.compare_digest(supplied, agent.token):
            return UNAUTHORIZED_PAGE, 401

    @app.get("/")
    def index():
        resp = send_file(INDEX_HTML)
        resp.set_cookie(COOKIE, agent.token, httponly=True, samesite="Strict")
        return resp

    @app.get("/events")
    def events():
        start = int(request.headers.get("Last-Event-ID", -1)) + 1

        def stream():
            agent.browsers += 1
            try:
                yield sse(None, {"type": "bridge", "subtype": "hello", "busy": agent.busy, "cwd": agent.cwd})
                i = start
                while True:
                    with agent.cond:
                        # Wake periodically so we can send a keep-alive comment.
                        agent.cond.wait_for(lambda: len(agent.events) > i, timeout=25)
                        batch = agent.events[i:]
                    if not batch:
                        yield ": ping\n\n"
                        continue
                    for data in batch:
                        yield sse(i, data)
                        i += 1
            finally:
                agent.browsers -= 1

        return Response(
            stream(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/send")
    def send():
        body = request.get_json(silent=True) or {}
        text = body.get("text")
        source = "canvas" if body.get("source") == "canvas" else "user"
        if not isinstance(text, str) or not text.strip():
            abort(400)
        try:
            agent.send(text, source)
        except RateLimited:
            return "too many canvas messages; slow down\n", 429
        return "", 204

    @app.post("/interrupt")
    def interrupt():
        agent.interrupt()
        return "", 204

    # -- canvas --

    @app.get("/canvas/frame")
    def canvas_frame():
        version = request.args.get("v", type=int) or len(agent.canvas)
        if not 1 <= version <= len(agent.canvas):
            abort(404)
        return Response(
            agent.frame_html(version),
            mimetype="text/html",
            headers={
                "Content-Security-Policy": CANVAS_CSP,
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/canvas/report")
    def canvas_report():
        report = request.get_json(silent=True)
        if not isinstance(report, dict):
            abort(400)
        agent.add_report(report)
        return "", 204

    @app.post("/client")
    def client_info():
        info = request.get_json(silent=True) or {}
        agent.client_info = {k: info.get(k) for k in ("width", "height", "scheme")}
        return "", 204

    # -- MCP --

    @app.post("/mcp")
    def mcp():
        msg = request.get_json(silent=True)
        if not isinstance(msg, dict):
            return jsonify({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}), 400
        if "id" not in msg:
            return "", 202  # notification
        try:
            result = handle_rpc(agent, msg.get("method", ""), msg.get("params") or {})
            return jsonify({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        except LookupError:
            error = {"code": -32601, "message": f"method not found: {msg.get('method')}"}
            return jsonify({"jsonrpc": "2.0", "id": msg["id"], "error": error})

    @app.route("/mcp", methods=["GET", "DELETE"])
    def mcp_no_stream():
        return "", 405

    return app


def serve(agent: Agent) -> None:
    """Serve `agent` on 127.0.0.1:<agent.port> until interrupted, then stop its process."""
    print("agent-web-bridge")
    print(f"  cwd:         {agent.cwd}")
    print(f"  claude args: {' '.join(agent.extra_args) or '(none)'}")
    print(f"  open:        http://127.0.0.1:{agent.port}/?token={agent.token}")
    try:
        create_app(agent).run(host="127.0.0.1", port=agent.port, threaded=True)
    finally:
        agent.stop()


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if "--" in argv:
        split = argv.index("--")
        own_argv, extra_claude_args = argv[:split], argv[split + 1 :]
    else:
        own_argv, extra_claude_args = argv, []

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cwd", default=os.getcwd(), help="git repository the agent works in")
    parser.add_argument("--port", type=int, default=4477)
    parser.add_argument("--glance-min-font", type=float, default=14, help="smallest allowed canvas text, px")
    parser.add_argument("--glance-max-words", type=int, default=60, help="most visible words on the canvas")
    args = parser.parse_args(own_argv)

    try:
        agent = Agent(
            args.cwd,
            extra_claude_args,
            port=args.port,
            token=os.environ.get("BRIDGE_TOKEN"),
            glance_min_font=args.glance_min_font,
            glance_max_words=args.glance_max_words,
        )
    except ValueError as e:
        parser.error(str(e))
    serve(agent)


if __name__ == "__main__":
    main()
