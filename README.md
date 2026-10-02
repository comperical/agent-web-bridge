# agent-web-bridge

Drive a Claude Code agent from a browser tab instead of the terminal, through a
page the **agent designs itself** rather than a chat window.

The server runs `claude -p --input-format stream-json --output-format stream-json`
as a long-lived subprocess. It also acts as an MCP server that gives the agent
canvas tools. The browser shows:

- **the canvas:** a full-screen page the agent writes in HTML, CSS, and JS, running in a sandboxed iframe.
- **the control strip:** a trusted bar the agent can't touch, with an input box, Stop,
  a status light, a ticker showing the agent's latest text, canvas version history, and an error badge.
- **the transcript:** a drawer, closed by default, holding the full log of text and tool calls.

```
browser ──POST /send──────▶ Flask ──stdin (JSON)──▶ claude -p
browser ◀──SSE /events───── Flask ◀──stdout (JSON)── claude -p
canvas  ◀──/canvas/frame─── Flask ◀──POST /mcp────── claude -p  (render/patch/script/state)
```

## Run

```bash
pip install -r requirements.txt
python3 server.py --cwd ~/code/some-repo            # then open the printed URL
python3 server.py --cwd . -- --permission-mode acceptEdits --model sonnet
```

The canvas is designed as a **glance display**, e.g. a small panel at the edge of AR
glasses. After every render and update, the page checks itself: text cut off or
outside the viewport (including text hidden by `overflow: hidden`), text below
the minimum font size, and visible words over budget. Failures come back to the
agent as `GLANCE CHECK FAILED`. Tune the budget to your display:

```bash
python3 server.py --glance-min-font 16 --glance-max-words 40
```

Anything after `--` is passed through to `claude`.

- **Auth:** binds to `127.0.0.1` only. The printed URL carries a random token,
  which is new on every start. Open it once and a cookie keeps you signed in. Set
  `BRIDGE_TOKEN=...` for a token that stays the same across restarts. Treat the URL like a shell login.
- **Reload-safe:** events and every canvas version (with its patches) are held in
  memory and replayed to a new tab.
- **Stop:** sends SIGINT to the agent. The turn ends and the process exits. The next
  message respawns it with `--resume <session_id>`, so context is kept.

## Canvas tools (MCP, pre-approved)

| Tool                   | Effect                                                              |
|------------------------|---------------------------------------------------------------------|
| `mcp__canvas__render`  | **layout mode:** replace the page with a new HTML document and save it as a new version; reports its `data-slot`s |
| `mcp__canvas__update`  | **update mode:** small `{slot: value}` data updates to the current layout; rejects unknown slots |
| `mcp__canvas__patch`   | escape hatch: `inner/replace/append/prepend/text/attr/remove` ops on CSS selectors |
| `mcp__canvas__script`  | run JS in the current page                                           |
| `mcp__canvas__state`   | viewport, color scheme, version, recent errors and canvas messages  |

The agent lays out a view once per phase of a task, then streams small updates
into its slots. Regions marked `data-auto="status|activity|log"` are filled
automatically from the session. Pages can render slot data themselves with
`bridge.onUpdate(slot, fn)`.

`render`, `update` and `patch` wait briefly for the browser to confirm, then return any JS
errors or policy violations so the agent can fix them. Inside the page,
`window.bridge` provides `send(text)` (for buttons and forms) and
`onEvent(cb, {replay})` (live session events). The agent's instructions are in
`canvas/prompt.md`.

## Guardrails

| Risk | Mitigation |
|------|------------|
| Canvas reads the token or drives the bridge directly | The iframe is sandboxed (`allow-scripts allow-forms` only), so its origin is opaque and it has no cookie access. CSP `connect-src 'none'` blocks fetch, XHR, and WebSocket. |
| Exfiltration from the canvas | CSP is sent as an HTTP header, so the agent's HTML can't loosen it. No remote images or connections. Scripts load only from library paths on cdnjs (`/ajax/libs/`) and jsDelivr (`/npm/`), and fonts only from Google Fonts. The host page's `frame-src 'self'` blocks the canvas from navigating to another origin before any request is sent (verified in Chromium; Firefox and Safari not yet tested). The host reloads the canvas and reports the attempt to the agent. Residual risk: CDN request URLs can still carry data, but only to the CDN operator. |
| Exfiltration via markdown in the transcript | The host page's CSP allows images only from `'self'` and `data:`. |
| Canvas impersonates host UI or approval prompts | The canvas is confined to its iframe. The strip is drawn by the host and visually distinct. The agent is told not to imitate it or ask for secrets. |
| Popups, dialogs, top-window navigation | Blocked by the sandbox. |
| Canvas drives the agent on its own | `bridge.send` is limited server-side to 3 messages per 10s and 4000 chars. Messages are prefixed `[canvas]`, labeled "via canvas" in the transcript, and the agent is told never to treat them as authorization for destructive or outward-facing actions. |
| Runaway output | Each render is capped at 1 MB and each patch at 500 KB. |
| A bad page | The user can step back through every version with ◀ ▶ and return with `live`. |

## Permissions (current limitation)

Headless mode can't show approval prompts. Tools that would normally ask
(non-read-only Bash, edits in `default` mode, …) are **denied**, and the strip
shows a notice. For now, pick a policy up front:

```bash
python3 server.py -- --permission-mode acceptEdits
```

Next step: route approvals to the trusted strip with `--permission-prompt-tool`
pointing at a new tool on the same `/mcp` endpoint.

## Endpoints

| Method | Path             | Purpose                                              |
|--------|------------------|------------------------------------------------------|
| GET    | `/`              | host page (`public/index.html`)                      |
| GET    | `/events`        | SSE stream of all events (honors Last-Event-ID)      |
| POST   | `/send`          | `{"text": "...", "source"?: "canvas"}`: a user turn |
| POST   | `/interrupt`     | stop the current turn                                |
| GET    | `/canvas/frame`  | canvas document `?v=N`, with runtime and CSP          |
| POST   | `/canvas/report` | browser → server: load, patch, and error reports    |
| POST   | `/client`        | browser → server: viewport and color scheme          |
| POST   | `/mcp`           | MCP (JSON-RPC) endpoint used by the agent            |
