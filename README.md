# Overlord Bridge

Drive a home-rooted **"Overlord"** from Telegram on your phone. The Overlord's
**brain is swappable**: Claude (the Claude Agent SDK, the default) or OpenAI
Codex (the `codex app-server`), selected with one env flag.

The Overlord runs in `/home/user`, so it can dispatch agents into any
`~/Projects/*` or `~/Documents/*` folder. Any action that isn't pre-approved is
bounced to your phone as an **Allow / Deny** prompt, and an off-limits backstop
auto-denies obvious secrets — both apply regardless of which brain is active.

It's a slimmed-down descendant of the `Joshua` assistant: same threaded
`python-telegram-bot` handler + queue pump, with a pluggable brain behind a
common interface and a remote permission gate added.

## Files

| File | Purpose |
|------|---------|
| `bridge.py` | Main loop: queue pump → `Brain` → streamed replies; selects the brain from `OVERLORD_BRAIN` |
| `modules/brain.py` | `Brain` ABC + shared `PermissionGate` (off-limits backstop + Telegram Allow/Deny + timeout) |
| `modules/claude_brain.py` | `ClaudeBrain`: the Claude Agent SDK behind the interface |
| `modules/codex_brain.py` | `CodexBrain`: the Codex App Server (JSON-RPC over stdio or WebSocket) behind the interface |
| `modules/telegram_handler.py` | Threaded Telegram I/O (owner-gated, chunked, Allow/Deny buttons) |
| `modules/workers.py` | Dispatch-folder worker runner for Claude, Codex, and local-agent workers; Telegram completion push; worker report audit store |
| `modules/fetch_relay.py` | Localhost-only headless-Chromium fetch endpoint (`GET /fetch?url=`) for sites that domain-block WebFetch or bot-wall curl; standalone process, own service |
| `AGENTS.example.md` | Sanitized template of the canonical shared Overlord instruction file (role, dispatch rules, off-limits). Copy to `AGENTS.md`, edit for your machine, then symlink it as `~/.claude/CLAUDE.md` + `~/AGENTS.md` so Claude and Codex share one file. (Your real `AGENTS.md` stays local — it's gitignored.) |
| `overlord-bridge.service` | `systemd --user` unit |
| `overlord-fetch-relay.service` | `systemd --user` unit for `modules/fetch_relay.py` |
| `.env.example` | Config template |

## Choosing the brain

Set `OVERLORD_BRAIN` in `.env`:

- `OVERLORD_BRAIN=claude` *(default)* — the live, deployed behavior. Loads
  `~/.claude/CLAUDE.md` + `~/.claude/settings.json` deny rules as a hard floor.
- `OVERLORD_BRAIN=codex` — uses the Codex CLI's `app-server`. See
  [Codex setup](#codex-brain-setup) below.

Both brains share the same Telegram I/O, the `.session` persistence file, the
`/new` reset, and the `PermissionGate` (off-limits auto-deny + Allow/Deny +
timeout). Switching brains is just changing the flag and restarting the service.

## Setup

1. **Create a bot.** In Telegram, message [@BotFather](https://t.me/BotFather)
   → `/newbot` → copy the token. Use a *new* bot, separate from Joshua.
2. **Get your chat id.** Message [@userinfobot](https://t.me/userinfobot); it
   replies with your numeric id.
3. **Configure.**
   ```bash
   cd ~/Projects/overlord-bridge
   cp .env.example .env
   $EDITOR .env          # paste TELEGRAM_BOT_TOKEN and OWNER_CHAT_ID
   chmod 600 .env
   ```
4. **Dependencies** are already installed in `.venv`. To recreate:
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```

## Run

Foreground (for a first test):
```bash
.venv/bin/python bridge.py
```
You should get a `🟢 Overlord online` message in Telegram. Send it a task.

As a managed service:
```bash
cp overlord-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now overlord-bridge
systemctl --user status overlord-bridge
journalctl --user -u overlord-bridge -f      # logs
```

To keep it running while you're logged out (true "away from terminal"):
```bash
sudo loginctl enable-linger $USER
```

## Shared context: Telegram ⇄ terminal

The Overlord is **one conversation**, reachable from your phone *and* your
terminal — what you do "while mobile" is there when you sit down, and vice versa.

Both sides pin to the active brain's saved id (`.session.claude` or
`.session.codex`; cwd `/home/user`, so Claude sessions live in the normal
`~/.claude/projects/-home-user/` store):

- **Terminal:** run `overlord` (symlinked onto your PATH → `overlord-bridge/overlord`).
  It resumes the shared per-brain session so the terminal sees your Telegram
  history; if no session exists yet it mints one and records it so Telegram
  joins *that*.
  When the active brain is Codex and `OVERLORD_CODEX_APP_SERVER_PROXY=true`,
  the bridge and terminal attach to the same local app-server Unix socket. The
  env name is historical: the bridge starts `codex app-server --listen
  unix://PATH` and talks direct WebSocket to it, while the launcher resumes the
  pinned thread with `codex --remote unix://PATH resume <thread>`. Bridge-started
  Codex turns then use the same transport as the TUI instead of a private stdio
  app-server.
  It also mirrors Ben's shell `codex()` wrapper: if Kitty graphics are supported
  but not advertised via environment, it sets `KITTY_WINDOW_ID=1` before
  `codex`/`codex resume` so terminal pets and images render correctly.
- **Telegram:** before each reply the bridge checks whether the session's
  transcript changed on disk (a terminal turn) and reloads it, so your phone
  reflects terminal work too. (`ClaudeBrain._maybe_refresh`.)
- `/new` on either side resets the shared thread.

This works for **both brains** — the terminal `overlord` launcher reads
`OVERLORD_BRAIN` and resumes the matching pinned id:

| Brain | Pinned id file | Terminal resume | Bridge reload watches |
|-------|----------------|-----------------|------------------------|
| claude | `.session.claude` | `claude --resume <id>` | `~/.claude/projects/-home-user/<id>.jsonl` mtime |
| codex  | `.session.codex`  | `codex resume <id>`    | `~/.codex/sessions/**/rollout-*-<id>.jsonl` mtime |

Each brain keeps its **own** pinned thread, so switching brains doesn't mix a
Claude session id with a Codex thread id.

Caveat: drive one side at a time. They share a single transcript file, so typing
into the terminal and the phone *simultaneously* on the same session can interleave
writes. Hand off, don't co-pilot.

> Codex note: terminal Codex can't pin a brand-new id, so to link the two, send
> the bridge a message first (it creates + records the thread), then run `overlord`.

## Lazy-loaded capability tools

The repo includes a local Codex plugin at `plugins/overlord` so canonical
Overlord capabilities are exposed as searchable MCP tools instead of only prose
in `CAPABILITIES.md`. The repo-local marketplace lives at
`.agents/plugins/marketplace.json`.

Install or refresh the plugin:

```bash
codex plugin marketplace add /home/user/Projects/overlord-bridge
codex plugin add overlord@personal
```

The plugin currently exposes:

- `list_overlord_capabilities`
- `find_overlord_capabilities`
- `overlord_status`
- `send_overlord_email`
- `dispatch_overlord_worker`

Start a new Codex thread after installing or changing the plugin; existing
threads may not refresh lazy-loaded tool metadata.

### Companion plugins (separate projects)

The Overlord also drives two other plugins that live in their **own separate
repos**, not this one:

- **`voice-gateway`** — supervised AI phone calls through a PC-as-Bluetooth
  gateway (local Whisper STT, TTS, automatic DTMF).
- **`ytmusic-dj`** — an AI DJ for YouTube Music that resolves seed
  playlists/tracks to real videoIds and builds playlists.

They register with the bridge exactly like `overlord` does, but their source
isn't included here. **We may fold these projects into the repo (or publish them
alongside it) in the future.**

## Codex brain setup

To run the Overlord on OpenAI Codex instead of Claude:

1. **Install the Codex CLI** (not bundled here):
   ```bash
   npm i -g @openai/codex
   codex --version          # confirm it's on PATH
   ```
2. **Authenticate** — `codex app-server` reuses your Codex login:
   ```bash
   codex login              # ChatGPT or API-key auth; writes ~/.codex/auth.json
   ```
   The bridge fails with a clear error if the binary is missing; usage-limit /
   auth errors from Codex are relayed to your phone as `⚠️ …`.
3. **Flip the flag** in `.env`:
   ```ini
   OVERLORD_BRAIN=codex
   # optional, see .env.example for all knobs:
   # OVERLORD_CODEX_SANDBOX=read-only
   # OVERLORD_CODEX_APPROVAL_POLICY=on-request
   # OVERLORD_CODEX_MODEL=          # empty = codex default
   # OVERLORD_CODEX_BIN=codex
   # OVERLORD_CODEX_APP_SERVER_PROXY=false
   # OVERLORD_CODEX_APP_SERVER_SOCKET=/home/user/Projects/overlord-bridge/run/codex-app-server.sock
   # OVERLORD_CODEX_INSTRUCTIONS_FILE=~/Projects/overlord-bridge/AGENTS.md
   ```
4. **Restart** the service (or the foreground process):
   ```bash
   systemctl --user restart overlord-bridge
   ```

### Guardrails for Codex

Codex does **not** read `~/.claude/CLAUDE.md` or `~/.claude/settings.json`, so its
guardrails come from three places:

- **`AGENTS.md`** in this folder is the canonical shared Overlord instruction
  file, including the worker roster and dispatch-file protocol. Keep Claude and
  Codex pointed at this same file:
  ```bash
  ln -s ~/Projects/overlord-bridge/AGENTS.md ~/AGENTS.md
  ln -s ~/Projects/overlord-bridge/AGENTS.md ~/CLAUDE.md
  ln -s ~/Projects/overlord-bridge/AGENTS.md ~/.claude/CLAUDE.md
  ```
  The bridge also injects it into Codex app-server as `developerInstructions`
  on both `thread/start` and `thread/resume`, because the Overlord cwd is
  `/home/user` and Codex would not otherwise discover this repo's `AGENTS.md`.
- **Sandbox + approval policy** — set via `OVERLORD_CODEX_SANDBOX` (default
  `read-only`) and `OVERLORD_CODEX_APPROVAL_POLICY` (default `on-request`),
  passed to `thread/start` / `thread/resume`. The Overlord is rooted at
  `/home/user`, so read-only is the conservative default; write actions, such as
  dropping worker dispatch files, become Allow/Deny prompts on your phone.
- **`PermissionGate`** (shared with the Claude path) auto-denies off-limits
  paths/commands and routes everything else to the phone, timing out into a deny.

> Note: this does not write Codex's global config under `~/.codex`. If you want a
> persistent sandbox/approval default at the Codex level, configure
> `~/.codex/config.toml` yourself; the bridge sets these per-thread.

### How the Codex approval round-trip works

`codex app-server` speaks JSON-RPC 2.0 over stdio for the private bridge mode,
or over WebSocket when the shared Unix-socket mode is enabled. When the agent
needs approval it sends a **server-initiated request** that pauses the turn
until the bridge replies. The bridge maps that onto the same Telegram Allow/Deny
mechanism the Claude path uses:

| Codex server request | Telegram tap | Reply sent to Codex |
|----------------------|--------------|---------------------|
| `item/commandExecution/requestApproval` | ✅ Allow | `{ "decision": "accept" }` |
| `item/fileChange/requestApproval` | ⛔ Deny | `{ "decision": "decline" }` |
| (either) | no tap before `OVERLORD_PERMISSION_TIMEOUT` | auto-deny (off-limits) / declined |

Session resume uses the App Server's native threads: the `.session` file holds
the Codex `threadId`, replayed via `thread/resume` on startup (stale ids fall
back to a fresh `thread/start`). `/new` wipes it and starts a new thread.

## Worker Reports

Workers are dispatched by dropping JSON into `dispatch/`. When a worker finishes,
fails, times out, cannot start, or crashes, the bridge still sends the terminal
status to Telegram on its own. It also records bounded audit data locally:

- `worker_reports/events.jsonl` appends accepted/started/terminal events and
  rotates to `events.1.jsonl` after `WORKER_REPORT_EVENTS_MAX_BYTES`.
- `worker_reports/latest/<worker>.json` stores the latest event for quick
  standups.

Reports include the worker name, folder, brain, session, status, timestamps,
duration, exit code, result tail, and real Codex session id when the worker
prints one. The bridge does not store the original task text, caps tails, and
redacts common token/password forms.

Supported worker brains:

- `claude`: cloud Claude CLI worker, resumable via Claude session id.
- `codex`: cloud Codex CLI worker, resumable via Codex session id and optional
  per-dispatch `approval_policy` / `sandbox`.
- `local-agent`: local model agent worker, currently one-shot. Use this when the
  local model should behave like a small Codex/Claude-style agent: it can list
  files, read/write project files, run shell commands in the worker folder, see
  tool results, loop, validate, and commit. `files` is optional and only provides
  starting hints; the agent can inspect the project one file at a time. The first
  proven backend is `qwen3-coder-next` through on-demand `llama-server` at 256k
  context. Ollama-style aliases route through Ollama's OpenAI-compatible `/v1`
  endpoint when configured/running.

Example local agent dispatch:

```json
{
  "name": "LocalAgent",
  "folder": "/home/user/Projects/plexorcist",
  "task": "Inspect the project, make the focused fix, validate it, and commit locally.",
  "brain": "local-agent",
  "model": "qwen3-coder-next"
}
```

With `WORKER_REPORT_TO_THREAD=true` (default), terminal worker reports are also
appended to the active Overlord conversation when the active brain supports it.
Codex app-server uses `thread/inject_items` to append a tagged
`[BRIDGE/SYSTEM WORKER REPORT]` developer-role history item without starting a
model turn, so it does not create a reply loop or extra Telegram message. This
path is durable context for future status questions; current Codex UI does not
render injected items in an already-open live UI.

With `WORKER_REPORT_VISIBLE_TURN=true` (default `false`), the bridge also starts
a real tagged Codex app-server `turn/start` after the durable append. That turn
asks Codex to produce one terse assistant status message beginning
`[BRIDGE/SYSTEM WORKER STATUS]` in the active bridge Codex app-server thread.
For that status to appear live in an already-open Codex TUI, both the bridge and
the TUI must be attached to the shared Unix socket via
`OVERLORD_CODEX_APP_SERVER_PROXY=true` and `overlord`'s
`codex --remote unix://PATH resume <thread>` path. The working path was verified
with `SocketVisibleTest` on 2026-06-25: Telegram received the worker completion
and the Codex TUI showed the visible bridge status turn. Earlier
`thread/inject_items` and bridge-private stdio `turn/start` paths were durable
transcript/history only; they did not repaint an already-open separate TUI.
If a Codex turn is already active, the bridge first interrupts it with
`turn/interrupt` and waits for it to settle before starting the status turn.
If that active turn was started by another Codex client, the bridge infers the
current unfinished `turn_id` from the shared rollout JSONL before interrupting.
During that report turn, the Codex brain declines any approval or interactive
tool request without asking Telegram, and the prompt forbids tool use, worker
dispatch, approval, recursion, or follow-up work.

### CLI-side worker-report delivery (dual delivery, brain-neutral)

Codex's `thread/inject_items`/`turn/start` paths above work because the Codex
app-server is a single persistent process that both the bridge and a terminal
TUI attach to as clients — mutating its in-memory thread state repaints any
attached live UI. Claude Code has no equivalent: each `claude` invocation is
self-contained and only reads its session transcript at start/resume, so
there is no local socket for an external process to push content into an
already-running interactive `claude` session. (Claude Code's own "Remote
Control" feature does something similar, but only for the official claude.ai
web/mobile client over a cloud-mediated channel — not something this bridge
drives, and it's disabled outright under API-key auth.)

So the `overlord` launcher and bridge build the closest real substitute out of
tmux, which does own the pty of an interactive session:

- The launcher runs the interactive brain inside a tmux session named
  `overlord-<brain>` (pane 0 = the brain). On first launch it splits off a
  second, passive pane (pane 1) that runs `tail -F` on
  `worker_reports/cli_feed.log`. Re-running `overlord` (from any terminal)
  attaches/switches to the same live session rather than starting a new one.
- With `WORKER_REPORT_CLI_FEED=true` (default), the bridge appends every
  worker completion/failure/timeout to that feed file. The kernel's inotify
  wakes the `tail -F` reader the instant it's written — genuine push, not a
  poll loop — and this channel never touches the brain pane's input, so it
  cannot corrupt anything Ben is mid-typing. This is on by default and works
  regardless of which brain is active, since it operates at the tmux/pty
  level rather than through a brain-specific API.
- With `WORKER_REPORT_TERMINAL_INJECT=true` (default `false`, opt-in), the
  bridge additionally types the report into the live brain pane as a real
  turn via `tmux send-keys`, so the brain actually reads it in-context —
  mirroring what Codex's `thread/inject_items` achieves. Because there is no
  reliable external signal for "Ben is about to type," this is guarded by
  `modules/cli_notify.py:pane_input_is_idle`, which inspects the pane's
  prompt line via `tmux capture-pane -e`: Claude Code renders its empty-input
  placeholder hint with the ANSI dim attribute, while real typed text has no
  such wrapper, so the check can tell "empty prompt" from "Ben has something
  typed" and skips injection on any doubt (including if the pane can't be
  inspected at all).
- Both channels are independent of `WORKER_REPORT_TO_THREAD`/
  `WORKER_REPORT_VISIBLE_TURN` above (which are Codex-specific today; Claude's
  `record_event`/`record_visible_event` are unimplemented and always report
  unsupported) and never take `self._brain_lock`, so they can't stall on an
  in-flight turn.

## Security

- The bot answers **only** `OWNER_CHAT_ID`; every other sender is dropped.
- Secrets stay in `.env` (chmod 600, git-ignored) — never committed.
- The shared `PermissionGate` auto-denies off-limits secrets and asks for the
  rest, for **both** brains.
- Claude adds a hard floor via `~/.claude/settings.json` deny rules. Codex gets
  the Overlord rules from this repo's `AGENTS.md`, injected as app-server
  developer instructions, plus its sandbox + approval policy. See
  [Codex brain setup](#guardrails-for-codex).
- Telegram long-polling is outbound-only; no inbound ports are exposed.

## How it behaves

- Idle until you send a task. Say *"let's work on Plexorcist"* and the Overlord
  spawns an agent scoped to `~/Projects/plexorcist`, works, reports back, done.
- One persistent conversation, so it remembers context across messages.
- Anything outside the allowlist → you get **Allow / Deny** buttons. No answer
  within `OVERLORD_PERMISSION_TIMEOUT` (default 5 min) = auto-deny.
