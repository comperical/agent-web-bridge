"""The headless `claude -p` process and the canvas state it draws on.

Agent knows nothing about HTTP: server.py exposes it to the browser and serves
its canvas tools over MCP. Construct one per bridge:

    agent = Agent("~/code/some-repo", ["--model", "sonnet"], port=4477)
"""

from __future__ import annotations

import atexit
import json
import os
import re
import secrets
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).parent
CANVAS_RUNTIME = (ROOT / "canvas" / "runtime.js").read_text()
CANVAS_PROMPT_TEMPLATE = (ROOT / "canvas" / "prompt.md").read_text()

# ---- canvas guardrails --------------------------------------------------------

MAX_HTML_BYTES = 1_000_000
MAX_PATCH_BYTES = 500_000
LARGE_UPDATE_BYTES = 1_500  # beyond this, suggest a new layout instead
CANVAS_SEND_LIMIT = (3, 10.0)  # at most 3 canvas messages per 10 seconds
CANVAS_SEND_MAX_CHARS = 4000

CANVAS_TOOLS = [
    {
        "name": "render",
        "description": (
            "LAYOUT MODE: replace the canvas with a new HTML document (a new version). Use when "
            "the task changes phase or topic. Mark regions you'll fill later with data-slot=\"name\". "
            "Returns the slots the browser found, plus any JS errors."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"html": {"type": "string", "description": "Complete HTML document"}},
            "required": ["html"],
        },
    },
    {
        "name": "update",
        "description": (
            "UPDATE MODE: send small data updates to named slots of the current layout, "
            "e.g. {\"status\": \"Running tests\", \"plan.2\": {\"state\": \"done\"}}. Values: string/number "
            "(text, or value of progress/meter/input), array (one item per element), null (clear), "
            "or {text, html, value, state, append, attr}. Pages may render slots themselves via "
            "bridge.onUpdate. Fails on unknown slots; change the layout with render instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "values": {
                    "type": "object",
                    "description": "slot name -> value",
                    "additionalProperties": True,
                }
            },
            "required": ["values"],
        },
    },
    {
        "name": "patch",
        "description": (
            "Low-level escape hatch: edit the current canvas by CSS selector without reloading it. "
            "Prefer update (slots) for progress and render for structural changes. "
            "Ops apply in order; <script> tags inside inserted html run."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ops": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS selector; every match is updated"},
                            "action": {
                                "type": "string",
                                "enum": ["inner", "replace", "append", "prepend", "remove", "text", "attr"],
                            },
                            "html": {"type": "string", "description": "for inner/replace/append/prepend"},
                            "text": {"type": "string", "description": "for text"},
                            "name": {"type": "string", "description": "attribute name, for attr"},
                            "value": {"type": "string", "description": "attribute value, for attr"},
                        },
                        "required": ["selector", "action"],
                    },
                }
            },
            "required": ["ops"],
        },
    },
    {
        "name": "script",
        "description": "Run JavaScript inside the current canvas (e.g. to update a chart's data).",
        "inputSchema": {
            "type": "object",
            "properties": {"js": {"type": "string"}},
            "required": ["js"],
        },
    },
    {
        "name": "state",
        "description": (
            "Describe the user's canvas: current version, viewport size, color scheme, "
            "connected browsers, recent JS errors and recent canvas messages."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class ToolError(Exception):
    pass


class RateLimited(Exception):
    pass


# ---- agent process ----------------------------------------------------------


class Agent:
    """One long-lived `claude -p` process speaking stream-json on stdin/stdout.

    `cwd` must be the root of a git repository (contain a .git directory);
    ValueError otherwise.
    `port` must be the port the bridge server listens on: the agent reaches its
    canvas tools at http://127.0.0.1:<port>/mcp. `token` guards both the browser
    and MCP endpoints; a random one is generated if omitted.
    """

    def __init__(
        self,
        cwd: str,
        extra_args: list[str] | None = None,
        *,
        port: int = 4477,
        token: str | None = None,
        glance_min_font: float = 14,
        glance_max_words: int = 60,
    ):
        cwd_path = Path(cwd).expanduser().resolve()
        if not (cwd_path / ".git").is_dir():
            raise ValueError(f"{cwd_path} is not a git repository (no .git directory)")
        self.cwd = str(cwd_path)
        self.extra_args = list(extra_args or [])
        self.port = port
        self.token = token or secrets.token_hex(16)
        self.glance_limits = {"minFont": glance_min_font, "maxWords": glance_max_words}
        self.canvas_prompt = (
            CANVAS_PROMPT_TEMPLATE
            .replace("{{MIN_FONT}}", f"{glance_min_font:g}")
            .replace("{{MAX_WORDS}}", str(glance_max_words))
        )
        self.events: list[dict] = []  # full session log, index == event id
        self.cond = threading.Condition()
        self.proc: subprocess.Popen | None = None
        self.stopping: subprocess.Popen | None = None  # interrupted, may still be exiting
        self.lock = threading.Lock()  # serializes process start/stop and stdin writes
        self.session_id: str | None = None
        self.busy = False
        self.mcp_config_path = self._write_mcp_config()

        # canvas state
        self.canvas: list[dict] = []  # versions: {"html": str, "patches": [ops, ...]}
        self.reports: dict[tuple, dict] = {}  # (version, "ready"|patch index) -> report
        self.canvas_errors: deque = deque(maxlen=20)
        self.glance: dict | None = None  # latest glance check from the browser
        self.canvas_sends: deque = deque(maxlen=20)
        self.client_info: dict = {}
        self.slots: dict[int, list[str]] = {}  # version -> slot names reported by the browser
        self.renderers: dict[int, list[str]] = {}  # version -> slots with a custom renderer
        self.browsers = 0

    def _write_mcp_config(self) -> str:
        # A 0600 temp file rather than argv, so the token isn't visible in `ps`.
        config = {
            "mcpServers": {
                "canvas": {
                    "type": "http",
                    "url": f"http://127.0.0.1:{self.port}/mcp",
                    "headers": {"X-Bridge-Token": self.token},
                }
            }
        }
        fd, path = tempfile.mkstemp(prefix="bridge-mcp-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(config, f)
        atexit.register(lambda: os.path.exists(path) and os.unlink(path))
        return path

    # -- event log --

    def emit(self, data: dict) -> None:
        with self.cond:
            self.events.append(data)
            self.cond.notify_all()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.emit({"type": "bridge", "subtype": "status", "busy": busy})

    # -- process management --

    def start(self) -> None:
        canvas_tools = ",".join(f"mcp__canvas__{t['name']}" for t in CANVAS_TOOLS)
        cmd = [
            "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--mcp-config", self.mcp_config_path,
            "--allowedTools", canvas_tools,
            "--append-system-prompt", self.canvas_prompt,
        ]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        cmd += self.extra_args

        proc = subprocess.Popen(
            cmd,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.proc = proc
        threading.Thread(target=self._read_stdout, args=(proc,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True).start()

    def _read_stdout(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self.emit({"type": "bridge", "subtype": "stdout", "text": line})
                continue
            if msg.get("session_id"):
                self.session_id = msg["session_id"]
            if msg.get("type") == "rate_limit_event":
                continue  # noise for the UI
            self.emit(msg)
            # Busy follows the stream: there's no 1:1 send->result mapping, because a
            # message sent mid-turn is merged into that turn (one result for both).
            # Output from an interrupted process must not touch a newer one's status.
            if self._is_current(proc):
                if msg.get("type") == "result":
                    self.set_busy(False)
                elif msg.get("type") in ("assistant", "user") and not self.busy:
                    self.set_busy(True)

        code = proc.wait()
        with self.lock:
            current = self._is_current(proc)
            if self.proc is proc:
                self.proc = None
        self.emit({"type": "bridge", "subtype": "exit", "code": code})
        if current:
            self.set_busy(False)

    def _is_current(self, proc: subprocess.Popen) -> bool:
        # An interrupted process (self.stopping) never counts: interrupt() already went idle.
        return self.proc is proc or (self.proc is None and self.stopping is not proc)

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        for line in proc.stderr:
            self.emit({"type": "bridge", "subtype": "stderr", "text": line.rstrip("\n")})

    # -- commands --

    def send(self, text: str, source: str = "user") -> None:
        with self.lock:
            self._send_locked(text, source)

    def _send_locked(self, text: str, source: str) -> None:
        if source == "canvas":
            now = time.monotonic()
            limit, window = CANVAS_SEND_LIMIT
            recent = [t for t in self.canvas_sends if now - t["at"] < window]
            if len(recent) >= limit:
                raise RateLimited()
            text = text[:CANVAS_SEND_MAX_CHARS]
            self.canvas_sends.append({"at": now, "text": text})
            content = f"[canvas] {text}"
        else:
            content = text

        self._ensure_running()
        self.emit({"type": "bridge", "subtype": "user_input", "text": text, "source": source})
        self.set_busy(True)
        line = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except OSError:
            # It died between the liveness check and the write: start fresh and retry once.
            self.proc = None
            self._ensure_running()
            self.proc.stdin.write(line)
            self.proc.stdin.flush()

    def _ensure_running(self) -> None:
        """Called with self.lock held."""
        if self.proc is not None and self.proc.poll() is None:
            return
        # Let an interrupted process finish exiting first, so two processes never
        # write the same session at once.
        old = self.stopping or self.proc
        if old is not None and old.poll() is None:
            try:
                old.wait(timeout=10)
            except subprocess.TimeoutExpired:
                old.kill()
                old.wait()
        self.stopping = None
        self.start()

    def interrupt(self) -> None:
        # Kill the turn; the next message respawns with --resume so context survives.
        with self.lock:
            if self.proc is not None:
                self.proc.send_signal(signal.SIGINT)
                self.stopping, self.proc = self.proc, None
        self.set_busy(False)

    def stop(self) -> None:
        for proc in (self.proc, self.stopping):
            if proc is not None and proc.poll() is None:
                proc.terminate()

    # -- canvas --

    def add_report(self, report: dict) -> None:
        version = report.get("version")
        if isinstance(report.get("glance"), dict):
            self.glance = {"version": version, **report["glance"]}
        if isinstance(report.get("slots"), list):
            self.slots[version] = [str(x) for x in report["slots"]]
            self.renderers[version] = [str(x) for x in report.get("renderers") or []]
        key = (version, "ready" if report.get("kind") == "ready" else report.get("index"))
        for err in report.get("errors") or []:
            self.canvas_errors.append({"version": version, "error": str(err)[:500]})
        with self.cond:
            self.reports.setdefault(key, report)
            self.cond.notify_all()

    def _wait_report(self, key: tuple, timeout: float) -> dict | None:
        with self.cond:
            self.cond.wait_for(lambda: key in self.reports, timeout=timeout)
            return self.reports.get(key)

    def _describe_report(self, report: dict | None) -> str:
        if report is None:
            if self.browsers == 0:
                return "No browser is connected, so nobody is looking at the canvas right now."
            return "The browser didn't confirm within the timeout (the tab may be in the background)."
        errors = report.get("errors") or []
        if not errors:
            text = "Browser applied it with no JS errors."
        else:
            text = "Browser reported JS errors:\n" + "\n".join(f"- {e}" for e in errors)
        return text + self._describe_glance(report.get("glance"))

    def _describe_glance(self, glance: dict | None) -> str:
        if not glance:
            return ""
        size = f"{glance.get('width')}x{glance.get('height')}"
        issues = glance.get("issues") or []
        if not issues:
            return f"\nGlance check passed ({glance.get('words')} words at {size})."
        return (
            f"\nGLANCE CHECK FAILED at {size}. The user can't read this at a glance:\n"
            + "\n".join(f"- {i}" for i in issues)
            + "\nFix this before doing anything else: cut content, enlarge text, or split it into steps or tabs."
        )

    def _viewport(self) -> str:
        info = self.client_info
        if not info:
            return ""
        return f" Viewport {info.get('width')}x{info.get('height')}, {info.get('scheme', 'light')} mode."

    def canvas_render(self, html: str) -> str:
        size = len(html.encode())
        if size > MAX_HTML_BYTES:
            raise ToolError(f"HTML is {size} bytes; the limit is {MAX_HTML_BYTES}.")
        with self.cond:
            self.canvas.append({"html": html, "patches": []})
            version = len(self.canvas)
        self.emit({"type": "bridge", "subtype": "canvas_render", "version": version})
        report = self._wait_report((version, "ready"), timeout=4)
        return (
            f"Rendered canvas v{version} ({size / 1024:.1f} KB).{self._viewport()} "
            + self._describe_slots(version, report)
            + self._describe_report(report)
        )

    def _describe_slots(self, version: int, report: dict | None) -> str:
        if report is None:
            return ""
        slots = self.slots.get(version) or []
        if not slots:
            return "No data-slot regions, so `update` can't target this layout. "
        return f"Slots for `update`: {', '.join(slots)}. "

    def canvas_update(self, values: dict) -> str:
        version = len(self.canvas)
        known = self.slots.get(version)
        if known is not None:
            renderers = self.renderers.get(version) or []

            # Valid: a known slot, or a sub-key of a slot with a custom renderer ("plan.3" -> "plan").
            def valid(key: str) -> bool:
                parts = key.split(".")
                return key in known or any(".".join(parts[:i]) in renderers for i in range(1, len(parts)))

            unknown = [k for k in values if not valid(k)]
            if unknown:
                raise ToolError(
                    f"Unknown slot(s) on canvas v{version}: {', '.join(unknown)}. "
                    f"Available: {', '.join(known) or '(none)'}. "
                    "If the view needs new regions, render a new layout."
                )
        result = self.canvas_patch([{"selector": "", "action": "update", "values": values}], label="update")
        size = len(json.dumps(values).encode())
        if size > LARGE_UPDATE_BYTES:
            result += (
                f" Note: this update was {size / 1024:.1f} KB. Updates should be small; "
                "if the content outgrew its slots, render a new layout."
            )
        return result

    def canvas_patch(self, ops: list[dict], label: str = "patch") -> str:
        size = len(json.dumps(ops).encode())
        if size > MAX_PATCH_BYTES:
            raise ToolError(f"Patch is {size} bytes; the limit is {MAX_PATCH_BYTES}.")
        with self.cond:
            if not self.canvas:
                raise ToolError("There is no canvas yet; call render first.")
            version = len(self.canvas)
            index = len(self.canvas[-1]["patches"])
            self.canvas[-1]["patches"].append(ops)
        self.emit({"type": "bridge", "subtype": "canvas_patch", "version": version, "index": index, "ops": ops})
        report = self._wait_report((version, index), timeout=3)
        return f"Applied {label} #{index} to canvas v{version}. " + self._describe_report(report)

    def canvas_state(self) -> str:
        state = {
            "version": len(self.canvas),
            "slots": self.slots.get(len(self.canvas)),
            "patches_on_current_version": len(self.canvas[-1]["patches"]) if self.canvas else 0,
            "connected_browsers": self.browsers,
            "viewport": self.client_info or None,
            "glance_limits": self.glance_limits,
            "last_glance_check": self.glance,
            "recent_js_errors": list(self.canvas_errors),
            "recent_canvas_messages": [s["text"] for s in self.canvas_sends],
        }
        return json.dumps(state, indent=2)

    def frame_html(self, version: int) -> str:
        """Agent HTML for a version, with the bridge runtime and patch replay injected."""
        entry = self.canvas[version - 1]
        # lineOffset lets the runtime report line numbers that match the agent's own HTML.
        boot = {
            "version": version,
            "patches": entry["patches"],
            "lineOffset": CANVAS_RUNTIME.count("\n") + 1,
            "glance": self.glance_limits,
        }
        # < keeps "</script>" inside patch html from closing the boot script.
        boot_json = json.dumps(boot).replace("<", "\\u003c")
        head = f"<script>window.__BRIDGE_BOOT={boot_json};\n{CANVAS_RUNTIME}</script>"
        html = entry["html"]
        doctype = re.match(r"\s*<!doctype[^>]*>", html, re.I)
        if doctype:
            return html[: doctype.end()] + head + html[doctype.end() :]
        return "<!doctype html>" + head + html
