# LightAIBox · Lightweight AI Toolbox

[简体中文](README.zh-CN.md) | English

A PySide6 desktop gateway that unifies multiple LLM API providers (OpenAI-compatible / Anthropic) behind a single local entry point, with smart scheduling, quota control, and call logging. Manage and reuse multiple model keys from one place.

![Python](https://img.shields.io/badge/Python-3.8+-blue) ![GUI](https://img.shields.io/badge/GUI-PySide6-green)

![LightAIBox main window](homepage.png)

## Features

- **Unified API proxy** — a single `chat` / `chat_stream` interface hides the OpenAI / Anthropic protocol differences. Call a specific model, or leave it unset to auto-select a provider by policy.
- **Claude Code ready** — the Anthropic-compatible endpoint fully passes through `tools` and multi-turn `tool_result`, with spec-compliant streaming `tool_use` events, so it can drive Claude Code's multi-step agent loop directly.
- **Built-in AI chat** — a WeChat-style chat tab right in the app: streaming replies, Markdown + offline MathJax-rendered LaTeX and mermaid diagrams, a per-reply **Raw / Render** toggle, collapsible **thinking** display, **image uploads for multimodal chats**, an optional **agent mode** that runs a LightAgents SuperAgent orchestration loop (intent routing + tool discovery + streaming) with built-in desktop tools, **sandboxed file generation** (docx / xlsx), **browser automation** (open pages / click / screenshot) and **gated SSH remote execution**, and time separators (see [Built-in Chat](#built-in-chat)).
- **Multiple providers** — add / edit / copy / delete providers (name, protocol, base URL, API key, model, multimodal flag), plus **JSON import / export** of the whole provider list for backup or sharing, with background connectivity testing that never blocks the UI.
- **Smart scheduling** — pick among available providers by policy (long-input-first / short-input-first), with automatic fallback on failure. Image requests route only to providers flagged as multimodal (a clear error instead of silently dropping images); mislabeled providers get the flag auto-revoked after repeated image failures.
- **Quota control** — limit by call count or token count; auto-disables a provider when it exceeds quota, resettable in one click.
- **Call logging & stats** — SQLite-persisted records of tokens, latency, speed, and status per call, filterable by date / provider, with aggregated statistics.
- **Floating Provider bar** — an independent, always-on-top, translucent panel floating at the bottom of the screen with a "Usage" header that lists every running provider and its remaining quota (model, usage / quota right-aligned) at a glance. It stays visible even when the main window is minimized to the tray; drag it anywhere, or use the "Lock/Locked" button (top-right) to stop it moving (turns green when locked) and the "Close" button to dismiss it. Hidden by default on every launch; toggle it from the corner of the tab bar, text follows the language switch.
- **Runs in the background** — closing the window minimizes it to the system tray while the unified API keeps serving. Single-click the tray icon to show / hide the window; right-click for show / quit.

## When to use Auto Mode

The `auto` model (adaptive scheduling) is especially recommended when:

1. **Your model API has a quota limit** — when one provider hits its token
   limit, LightAIBox automatically switches to the next available one, so your
   AI coding session never gets interrupted mid-flight.
2. **You're juggling multiple free/limited accounts** — providers are picked
   automatically by priority / quota / input length, so you can squeeze the
   most out of every free token. Cyber-beggar friendly. 🫙

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run
python -m app.main
```

The database is created automatically on first run in the user data directory
(`%APPDATA%/LightAIBox/lightbox.db` on Windows, `~/.local/share/LightAIBox/` on
Linux, `~/Library/Application Support/LightAIBox/` on macOS).

### Add a provider

Click **Add** in the provider list and fill in the name (unique), protocol type, base URL (the trailing `/v1` is optional), API key, model, and optionally tick **multimodal (images)**. Enable / disable is controlled only by the row button (the edit dialog never changes it). Then use the row buttons to edit / copy / delete / reset quota / test connectivity. Use **Import / Export** to replace or back up the whole provider list as JSON.

## Unified API (HTTP)

A local HTTP server exposes the gateway to external tools (curl / OpenAI SDK / Anthropic SDK / Claude Code) and **auto-starts with the app**, so it works out of the box on `127.0.0.1:8765` (the listen **address** and **port** are remembered across restarts and can be changed on the 统一 API page).

- OpenAI-compatible: `POST /v1/chat/completions`, `GET /v1/models`
- Anthropic-compatible: `POST /v1/messages`

> **LAN access**: on the 统一 API page, switch the **listen address** to `0.0.0.0` (all interfaces) to let other machines on your network reach it — the page then shows your current LAN IP to use in place of `127.0.0.1`. ⚠️ The server is currently **unauthenticated**: binding to `0.0.0.0` exposes a proxy that draws on your configured provider keys to anything that can reach the port, so only open it on a trusted network.

```bash
curl -s http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Hello"}]}'
```

### Use with Claude Code

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8765 \
ANTHROPIC_API_KEY=anything \
ANTHROPIC_MODEL=<model-name> \
claude
```

## Built-in Chat

The **对话 (Chat)** tab is a full in-app chat window modeled after WeChat and driven by the
same gateway, so it works with any configured provider (or `auto` scheduling):

- **WeChat-style message stream** — rounded-rectangle avatars ("AI" / "Me") in the app's
  indigo color, bubbles with directional tails pointing at the avatar, asymmetric corner
  radii, and centered time separators (shown for the first message and whenever two
  messages are more than 5 minutes apart).
- **Real-time streaming** — replies stream into a live bubble as tokens arrive; a **Stop**
  button cancels the current generation. **Smart scrolling**: while streaming, the view only
  auto-follows the bottom if you're already at the bottom — if you scroll up to read earlier
  content it stays put instead of being yanked back down by each token.
- **Markdown + LaTeX** — replies are rendered from Markdown, and LaTeX is typeset by the
  **locally bundled MathJax v3** (fully offline) using `\(...\)` / `\[...\]` (also
  `$...$` / `$$...$$`). Fenced blocks declared as `latex` / `tex` / `math` are rendered as
  centered display math (`%` comments stripped); other code fences stay styled code blocks.
- **Mermaid diagrams** — ` ```mermaid ` fences are rendered into SVG by the **locally
  bundled mermaid v10** (fully offline), themed to match the current dark / light UI.
- **Raw / Render toggle** — a link under each assistant reply flips it between the raw
  Markdown and the rendered view.
- **Thinking mode** — a "Thinking: on / off" toggle in the top bar (persisted). When on,
  the model's reasoning streams into a collapsible block above the answer, collapsed by
  default; applies to subsequent messages only.
- **Multimodal images** — the "🖼 Image" button next to the input picks local images
  (png / jpg / jpeg / gif / webp, multi-select), sent base64-encoded with the next
  message and shown as thumbnails in the bubble; `auto` scheduling routes image
  requests only to providers flagged multimodal.
- **Agent mode** — an "Agent: on / off" toggle in the top bar (persisted). When on, replies
  are produced by a **LightAgents SuperAgent orchestration** loop instead of a plain
  completion: intent routing → sub-agent dispatch → tool discovery → streaming synthesis.
  The model can call built-in read-only desktop tools, produce sandboxed files (docx / xlsx,
  each write confirmed), observe results, and keep reasoning until it can give a final
  answer. Tool activity (which tool ran, its arguments, success / failure) is rendered as
  status lines inside the assistant bubble, above the thinking block, and is preserved when
  the conversation is replayed. See [Agent Mode](#agent-mode).
- **Theme-aware** — bubble / avatar / time colors follow the dark / light theme; the chat
  canvas is transparent so theme switches apply instantly with no one-frame lag, and a thin
  (6px) scrollbar keeps it unobtrusive.

### Agent Mode

Toggling **Agent: on** switches the chat from a single streaming completion to a
**LightAgents SuperAgent** orchestration loop driven through the same local gateway:

1. The model receives the conversation plus JSON schemas of the available tools.
2. SuperAgent routes the intent, discovers and dispatches the relevant tools (or
   sub-agents), and streams each step in-band.
3. If a tool would help, it is **executed locally** and its observation is fed back to the
   model, which keeps reasoning (up to 5 steps) until it answers in natural language; the
   summary is streamed token-by-token into the bubble.

Key properties:

- **Protocol-aware** — the loop speaks both function-calling dialects: OpenAI
  `tool_calls` / `role: "tool"` messages and Anthropic `tool_use` / `tool_result` content
  blocks. The gateway picks a provider for the first step, then **locks onto it** so
  tool-call messages never cross protocols (which upstream APIs would reject). Under the
  hood a `GatewayLLM` duck-typed adapter (`app/gateway_llm.py`) presents the multi-provider
  gateway to LightAgents as a single model.
- **Read-only desktop tools** — `get_current_time` (local date / time / timezone / region,
  inferred offline from the local timezone), `calculator` (arithmetic expressions parsed
  through an AST whitelist — `__import__` / `exec` and friends are rejected), and
  `get_gateway_status` (configured providers, their models, usage / quota and speed — handy
  for "what models can I use right now?").
- **Sandboxed file generation** — `write_text` / `write_docx` / `write_xlsx` let the agent
  actually produce deliverables. Files are generated **structurally** with
  python-docx / openpyxl (no arbitrary script execution, no external CLI), written only
  inside a per-session sandbox under `Documents/LightAIBoxOutputs/<date>/<session>/`
  (`app/config.py` `OUTPUT_ROOT`), capped at 20 MB, and **every write is confirmed by a
  dialog** showing the path, type and size before anything hits disk
  (`app/approval.py` + `app/file_tools.py`). Produced files are surfaced as a `🔗 打开`
  link you can click to open in the system default app.
- **Weather / location** — `weather` (online) returns multi-day forecasts for a city (or you
  can just name a place and it resolves the city), and `get_current_location` reports your
  IP-derived location. Both are **on-demand**: they're not always in the model's tool schema,
  but are discovered via FindTools when you ask about the weather or where you are.
- **Browser automation (Playwright)** — the `browser` tool can genuinely **open a web page**,
  extract visible text, click / fill / scroll / press keys, run JS and take screenshots,
  driving your local Google Chrome. When the page needs login / a CAPTCHA, it tells you to
  finish it manually in the Chrome window and then keeps going. It's discovered on demand
  (ask to "open the browser / visit a web page / search on a site"); **state-changing
  actions** (`goto` / `click` / `fill` …) are gated behind a confirmation dialog showing the
  action and its parameters, while read-only grabs (text / screenshot) run freely. *Note: it
  opens Chrome headless by default (invisible) for extracting content; a visible window is
  intended for demo / screen-capture use.*
- **SSH remote commands (Python)** — with an approved remote host, the `ssh_exec` tool runs
  commands on a server over SSH. **Every execution pops a
  confirmation dialog showing the host and the command** before it runs; credentials come from
  the configured session, never from the model. Discovered on demand (ask to "run a command /
  connect to the server / deploy").
- **Fail-safe observations** — a tool error becomes an observation (`❌ …`) fed back to
  the model instead of aborting the loop; it can retry or explain.
- **Transparent UI** — each step streams in-band: tool calls and results appear as
  `🔧 ✓ calculator 2+3*4 = 14` status lines in the bubble, thinking segments in the
  collapsible block (if thinking mode is on), and the final answer as normal markdown.

Under the hood the orchestration is provided by the external LightAgents framework
(`../LightAgents`, SuperAgent + sub-agents + tool discovery via ToolCatalog / FindTools),
streamed through `app/agent_bridge.py`; the desktop tool registry lives in
`app/gateway_llm.py` + `app/agent_tools.py`, with the browser / SSH / weather tools coming
from LightAgents' built-in `browser_tool` / `ssh_exec_tool` / `weather_tool` (Playwright and
paramiko are optional deps — skipped if missing).

## Project Structure

```
app/
├── main.py          # entry point (init DB, auto-start unified API, launch window)
├── config.py        # config & constants
├── models.py        # domain models
├── client.py        # unified LLM client (OpenAI / Anthropic)
├── gateway.py       # gateway: scheduling + quota + logging
├── server.py        # unified API service (FastAPI + uvicorn)
├── db.py            # SQLite persistence
├── chat_session.py  # chat session: multi-turn history + display timestamps
├── agent_bridge.py  # agent mode: LightAgents SuperAgent orchestration, streamed over the gateway
├── gateway_llm.py   # GatewayLLM (gateway as a single LightAgents LLM) + StreamingSuperAgent + desktop registry
├── agent_tools.py   # built-in read-only desktop tools + JSON-schema export
├── approval.py      # cross-thread write-confirmation coordinator (proposal + per-write dialog)
├── file_tools.py    # sandboxed file generation (write_text / write_docx / write_xlsx)
└── ui/              # PySide6 UI
    ├── chat_page.py # Chat tab: WeChat-style bubbles + MathJax rendering + agent mode
    ├── providers_bar.py # floating always-on-top panel: header + running providers + quota (right-aligned model/usage, lock/close buttons)
    └── resources/chat/  # chat container HTML + bundled MathJax v3 + mermaid (offline)
```

## Packaging (build a distributable executable)

LightAIBox can be packaged into a standalone executable using PyInstaller. A
ready-to-use spec file is provided.

```bash
# Windows (Git Bash / MSYS)
bash build_windows.sh
# or directly:
python -m PyInstaller --clean --noconfirm lightaibox.spec
```

Output lands in `dist/LightAIBox/LightAIBox.exe` (onedir bundle — ship the whole
`dist/LightAIBox/` folder).

Notes:

- The build **must run on the target OS** — PyInstaller does not cross-compile.
  Build on Windows for a Windows `.exe`, on macOS for a `.app`, on Linux for an
  ELF binary.
- Read-only resources (`app/resources/styles/*.qss`) are bundled and resolved via
  `sys._MEIPASS` at runtime (`app/config.py`).
- Writable data (SQLite DB) goes to the per-user data directory, **not** the
  bundle — see Quick Start above.
- uvicorn / fastapi use dynamic imports, handled explicitly in `hiddenimports`.
- The exe is built with `console=False` (no terminal window).

## License

This project is released under the [MIT License](LICENSE). You are free to use,
modify, and distribute it, including for commercial purposes.

## Acknowledgements

Special thanks to **my wife** for the Alibaba Cloud account — one more source of
free model tokens to scrape by on. 💖
