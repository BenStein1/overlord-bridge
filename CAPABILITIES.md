# Shared Capabilities

This file is the living catalog of things Claude and Codex should both know
about in the Overlord setup. Keep it current when a new helper, service, or
workflow lands.

## Current Capabilities

- `voice_gateway`: place and manage phone calls, send DTMF, listen to real
  people with local Whisper STT, and speak responses through Azure TTS or
  ElevenLabs. Live as MCP tools (`hfp_status`, `lookup_contact`, `place_call`)
  via the `voice-gateway` plugin in the `nova-local` marketplace (added
  2026-07-11) — previously the engine was CLI-only, so no brain could
  actually place a call even though it worked; now it's callable directly.
- `nova-local` plugin marketplace (`.claude-plugin/marketplace.json`, rooted
  in this repo): the landing spot for every future local MCP server. A raw
  `~/.claude.json` MCP entry only takes effect at session start (a restart
  per new tool — expensive against a 5-hour cap window); a plugin-provided
  MCP server picks up with `/reload-plugins` instead, no restart. Each plugin
  lives at `plugins/<name>/.claude-plugin/plugin.json` declaring its own
  `mcpServers` block. Install once, then `/reload-plugins` after future
  additions. Current plugins: `ytmusic-dj`, `voice-gateway`.
- `ytmusic-dj`: Nova's AI DJ for YouTube Music. Tools: `search_tracks`,
  `create_playlist`, `analyze_playlist`, `list_ai_playlists`,
  `get_ai_playlist`, `edit_ai_playlist`, `reorder_ai_playlist`,
  `delete_ai_playlist`, `whoami`. Resolves a seed playlist/track to real
  videoIds and creates/edits playlists in Ben's BENformation channel;
  edit/delete are guardrailed to playlists this tool itself created. Wired
  via the `nova-local` marketplace.
- Telegram phone bridge: one owner-gated Overlord conversation reachable from
  Telegram and the terminal, with shared session sync between them.
- `overlord` launcher: resumes the current shared Overlord thread in the
  terminal, matching the active brain.
- `overlord`: **one brain, two microphones — Telegram and the terminal are the same
  live conversation. This is the DEFAULT; no flag needed.** `overlord --no-shared`
  forces the old plain-resume path, and any unexpected condition falls back to it
  automatically. Runs Ben's pin through Claude Code's local
  background-job daemon (`claude --bg`, `claude attach <id>`) instead of a plain
  `claude --resume` (mechanism found 2026-07-13, see
  `research/one_brain_poc/NOTES.md`). Unlike plain `--resume` — where a second
  live process silently forks — a `--bg` job accepts MULTIPLE attached clients
  at once, mirroring input live, with a single unforked transcript.
  - Ben's half: `overlord` creates (or reuses) the job and attaches his terminal to
    it. He gets the **real Claude Code TUI** — statusline, slash
    commands, permission footer, plan mode — because `attach` is the native
    client, not a homegrown pipe. The job is tracked in `.session.claude.job`
    (`<job-id>:<live-session-id>`) so repeat runs reuse it. Note `claude --bg
    --resume` does NOT resume in place — it starts a NEW session id and freezes the
    old transcript — so the launcher re-pins to the job's live id. A running job is
    also findable from the pin alone via the roster, so a lost sidecar is survivable.
  - The bridge's half (`modules/shared_job.py`): when a live shared job exists,
    a Telegram turn is typed straight into it and the reply is read back from the
    session JSONL. So a message sent from bed lands in the conversation his
    terminal is showing, and is still there in the morning. No live shared job →
    the bridge silently uses the ordinary per-turn SDK path (Design B), so plain
    `overlord` behaves exactly as before.
  - Worker reports (`shared_job.notify`) are injected the same way, so a
    completion shows up in Telegram AND the live terminal automatically — no
    per-dispatch arming, no tmux launcher wrap, no Monitor polling.
  - **Guarded:** injection types into a real pty, so it refuses to type while Ben
    has a half-written message sitting at the prompt (its Enter would submit his
    sentence). Idleness is judged by rendering the attached frame through `pyte`
    and reading the bottom-most prompt line. A refusal degrades to Telegram-only
    delivery — never a mangled terminal, never a lost message.
  - **Permissions caveat:** an injected turn runs inside the job's own `claude`
    process, so its tool calls follow that process's permission settings and do
    NOT raise the bridge's Telegram Allow/Deny prompt (the gate still guards the
    SDK fall-back path). Consistent with Ben's `defaultMode: bypassPermissions`,
    but worth knowing before trusting it with something destructive.
  - Falls back to today's exact `claude --resume` on any unexpected condition
    (job creation fails, pin already held by a live interactive session, etc).
- `overlord status` (alias `overlord health`): one command that re-grounds a
  session on WHO it is (the Overlord) and runs a single bridge health sweep —
  active brain, service state, pinned session, Codex shared socket, and pending
  dispatch files. Launches no brain; safe to run any time. Use it instead of
  hand-running five separate checks.
- Brain swap: Overlord can run on Claude or Codex, selected by `OVERLORD_BRAIN`.
- Allow/Deny approvals: actions outside the pre-approved set are surfaced to
  Telegram with operator prompts and time out into denial.
- Worker dispatch from Telegram: the bridge can spawn named workers into
  project folders and report results back in the thread.
- `overlord-email`: sends mail through Gmail SMTP and supports file attachments
  with `--attach`. It reads `GMAIL_USER` and `GMAIL_APP_PASSWORD` from
  `overlord-bridge/.env`.
- `overlord` Codex plugin / MCP surface: exposes shared capabilities as
  lazy-loadable tools (`list_overlord_capabilities`,
  `find_overlord_capabilities`, `overlord_status`, `send_overlord_email`, and
  `dispatch_overlord_worker`). The source lives in `plugins/overlord`, with a
  repo-local marketplace at `.agents/plugins/marketplace.json`; reinstall with
  `codex plugin add overlord@personal` after plugin changes. Claude can use the
  same brain-neutral stdio MCP server by registering the command from
  `plugins/overlord/.mcp.json`. New Codex threads pick up the lazy-loaded tools.
- Worker dispatch: create a JSON file in `overlord-bridge/dispatch/` and the
  bridge will run a named worker in a project folder.
- Worker reports: terminal worker completions/failures/timeouts are pushed to
  Telegram, written to `worker_reports/events.jsonl` and
  `worker_reports/latest/<worker>.json`, and, for the active Codex Overlord
  brain, appended to the same app-server thread via `thread/inject_items`.
  `WORKER_REPORT_VISIBLE_TURN=true` can additionally start a tagged visible
  assistant status turn in the active bridge Codex app-server thread. Live Codex
  TUI visibility requires the shared Unix-socket path: bridge direct WebSocket to
  `codex app-server --listen unix://...` and terminal `codex --remote
  unix://... resume <thread>` through the `overlord` launcher.
- CLI-side worker-report surfacing (dual delivery: Telegram **and** the live
  terminal Overlord session, not a replacement for either). **The tmux launcher
  wrap was removed from `overlord` on 2026-07-05** — Ben rejected it
  (2026-07-01) and it now just execs `claude` directly, no `overlord-<brain>`
  tmux session, no split tail pane. Historical context: the launcher used to
  run the brain in tmux pane 0 with pane 1 tailing
  `worker_reports/cli_feed.log`, plus an opt-in `WORKER_REPORT_TERMINAL_INJECT`
  path that `tmux send-keys`-typed reports into the live brain pane. That whole
  approach is gone from the launcher. The bridge-side feed append
  (`WORKER_REPORT_CLI_FEED`, `modules/cli_notify.py`) still exists and is
  harmless, but nothing consumes it via tmux anymore.
  The actual live practice: the interactive Claude Overlord session self-arms a
  `Monitor`/background watch on `worker_reports/events.jsonl` (seeded from the
  current line count, NOT `tail -F -n0` — this box's `tail` is uutils
  coreutils, which doesn't honor `-n0` and dumps the whole backlog) and relays
  each terminal-status line inline as a plain human-readable turn, no raw JSON.
  Needs zero bridge changes; it's a per-session runtime practice, not
  infrastructure. Reporting template Ben set: `TIMESTAMP / Worker: <name> /
  Completed task: <task> / Result: <result>` — verify the actual artifact
  before filling in Result, a worker's own final line is often just "Done."
- Trusted Codex workers can be launched unattended per project with
  `"approval_policy":"never"` and `"sandbox":"workspace-write"` in the
  dispatch JSON. **Codex is intermittent-but-credited (updated 2026-07-15), not
  retired** — it's polled by the engine gas gauge and dispatchable as a fallback
  *when the gauge says it's up* (see the "Engine gas gauge" entry; kill-switch
  `OVERLORD_ENGINE_CODEX_DISABLED`). Old roster codex sessions are still dead —
  re-hire with a fresh uuid, don't resume the old id.
- Local agent workers can be launched with `"brain":"local-agent"` and a model
  alias: `"qwen3-coder-next"` (-> `openai/Qwen3-Coder-Next`, on-demand local
  `llama.cpp`/`llama-server`) or `"qwen2.5-coder-14b"` (->
  `ollama_chat/qwen2.5-coder:14b`). This is the local-model Codex/Claude-style
  worker path: the bridge gives the model tools to list files, read/write
  project files, run commands in the worker folder, feed results back, continue
  looping, validate, and commit. `files` is optional and only a starting hint —
  the local agent can inspect the project itself and loop one file at a time
  without a preselected list. The bridge pins a fixed context window by default
  (`OVERLORD_WORKER_LOCAL_AGENT_CONTEXT_TOKENS`, default `256000`, retry floor
  `OVERLORD_WORKER_LOCAL_AGENT_MIN_CONTEXT_TOKENS`, default `2048`) with
  model-specific hard caps after alias resolution; `qwen3-coder-next` is capped
  at `256000`. For the llama.cpp-backed alias the cap becomes the per-job
  `llama-server --ctx-size`; the bridge starts that server only for the worker
  run and stops it after, using `--no-mmap`, `--cache-ram 0`, q8 KV cache, and
  configurable MoE CPU offload. For the Ollama alias the cap becomes `num_ctx`,
  retrying once at a smaller cap if Ollama reports insufficient memory. Local
  workers are currently one-shot, not persistent resumable sessions. Treat this
  model as rough but free: a last-resort fit for small, contained tasks, and
  sometimes acceptable for a narrow large-single-file edit because the 256k
  context gives it enough room to function. Do not hand it broad, ambiguous, or
  high-judgment work.
- NVIDIA NIM cloud workers can be launched with `"brain":"nvidia"` (aliases
  `nim`, `nvidia-nim`): the same tool-loop harness as `local-agent`, pointed at
  NVIDIA's free OpenAI-compatible endpoint (`integrate.api.nvidia.com/v1`) —
  big frontier-adjacent models with huge contexts and zero local VRAM cost.
  Requires `OVERLORD_WORKER_NVIDIA_API_KEY` in `.env`. Aliases live in
  `modules/workers.py` `_NVIDIA_MODEL_ALIASES`; unrecognized model strings
  pass through verbatim, so any id from `GET /v1/models` can be tested.
  Human free-preview catalog, sorted by popularity:
  `https://build.nvidia.com/models?filters=nimType%3Anim_type_preview&orderBy=weightPopular%3ADESC`.
  Machine-readable availability for Ben's key:
  `GET https://integrate.api.nvidia.com/v1/models` with
  `Authorization: Bearer $OVERLORD_WORKER_NVIDIA_API_KEY` (cached for normal gas
  checks; default TTL 300s). Per-model sampling and `chat_template_kwargs`
  tweaks are copied from the NVIDIA Build prototype snippets and applied
  automatically (`_NVIDIA_MODEL_PAYLOAD_OVERRIDES`). 429/5xx are retried with
  backoff.
  Like local-agent, these are one-shot workers, not resumable sessions;
  `model` and `files` hints work the same way.
- **NVIDIA model selection — which model for which worker task.** If unsure,
  omit `model` entirely: the default is `gpt-oss`
  (`openai/gpt-oss-120b`). In the 2026-07-16 same-prompt smoke matrix it was the
  fastest practical coding answer (7.2s) and the only quick NVIDIA model whose
  generated asserts plus external verifier passed the merge-ranges prompt.
  Current practical rank:
  - `gpt-oss` — DEFAULT for automatic NVIDIA worker dispatch. Best current pick
    for small/medium coding, review/analysis, structured extraction, and
    low-risk reasoning where a slightly odd writing style is acceptable.
  - `mistral` — best writing-heavy automatic fallback. It was close in the
    creative smoke and very fast (5.7s) on code, but missed the stricter
    integer-adjacent "touching ranges" interpretation in the coding smoke.
  - `nemotron` — fast NVIDIA-owned fallback. Latency was practical (19.6s), but
    it also missed integer-adjacent touching in the coding smoke.
  - `minimax` / `minimax-m3` and `minimax-m2.7` — manual-only. Minimax M3's code
    body passed the external verifier but emitted self-failing asserts; M2.7 was
    slower and missed integer-adjacent touching. Neither is trusted as a
    coding/tool worker yet.
  - `qwen3-next` / `qwen-next` — keep as a visible fallback and prefer it over
    old Qwen 3.5 variants, but do not auto-prefer it when gpt-oss/Mistral are
    available: the coding function passed external checks, but its own asserts
    were wrong and the run took 3m27s.
  - `llama` — last automatic NVIDIA fallback only. The coding function passed
    external checks, but its own asserts were wrong and the run took 10m06s.
  - `kimi` — broken on the live NVIDIA worker path: HTTP 404 on 2026-07-16.
  - `deepseek` — manual-only despite strong creative output quality. It took
    15m19s on the limerick smoke, then 15m22s and crashed with HTTP 504 on the
    tiny coding smoke, so it is not viable for routine worker dispatch.
  - `qwen3.5` (122B), `qwen3.5-397b`, or `mistral-large-3` — visible/manual-test
    fallbacks for experimentation; do not prefer them for routine dispatches.
    `qwen3.5-397b` is the biggest brain, but frequently queues/times out on the
    free tier; only use it where a failed/slow run is acceptable.
  - AVOID as auto-selected workers: `kimi` / `kimi-k2.6` (listed in the catalog,
    but live worker call returned HTTP 404 on 2026-07-16), `minimax` /
    `minimax-m3`, `minimax-m2.7`, `deepseek` / `deepseek-v4-pro`, and
    `deepseek-flash`. Wired for explicit experimentation only.
  Tool-calling verified live 2026-07-02 for: deepseek-v4-pro, qwen3-next,
  qwen3.5-122b, gpt-oss-120b, mistral-large-3, nemotron-3-super. Kimi was
  previously believed usable, but is currently disabled for auto-routing.
- **Worker brain selection** (for Overlords unsure which brain to dispatch):
  prefer calling the **`recommend_engine`** MCP tool, or pass `brain:"auto"` /
  `auto_route:true` to `dispatch_overlord_worker`. It checks live gas and job
  fit before spending Ben's premium sessions. The manual policy it encodes:
  `claude` for high-stakes, broad multi-file, ambiguous, or high-judgment work;
  if Claude is capped and Codex has gas, use `codex` as the next-smartest brain.
  If the job is too hard for NVIDIA/local and Codex is unavailable, **defer
  until Claude resets** instead of creating Claude cleanup work. `nvidia`
  gpt-oss is the current free automatic pick for medium, well-specified coding,
  debugging, and review tasks where verification is possible; use Mistral first
  for writing-heavy chores. Do not overreach on vague architecture or
  high-judgment tasks. `local-agent` is free and local, but rough: last resort
  for small contained tasks or a narrow large-single-file edit, not broad work.
  Proven 2026-07-16 dispatcher suitability matrix for low/medium coding chores:
  `gpt-oss` first; `mistral`; `nemotron`; `local-agent` as the free/local last
  resort; `minimax` manual/test-only; `qwen3-next`; `minimax-m2.7` manual-only;
  `llama`; `kimi` broken; `deepseek` unusable on NVIDIA free-tier dispatch.
- **Engine gas gauge + preference router** (built 2026-07-15, updated
  2026-07-16): before dispatch,
  know per engine whether it's usable *right now*. Three MCP tools plus a CLI:
  - `engine_gas_gauge` — probes `claude` (live 5-hour session + 7-day weekly
    usage caps → `gas`/`weekly_gas`), `nvidia` (key + cached `/models`
    reachability, plus a bounded popular-model watchlist: Nemotron, Qwen3-Next,
    Mistral, Llama, gpt-oss, DeepSeek, Kimi, MiniMax. Normal gas does **not**
    POST-test every model; per-model `gas` is synthetic from catalog presence
    plus recent worker/smoke reports: `100` means listed with no recent worker
    failure; `0` means missing, broken, recently rate-limited, or queued/timed
    out),
    `codex` (ChatGPT backend reachable + `~/.codex/auth.json` authed, layered
    with real 5-hour/weekly plan usage % parsed from the newest
    `~/.codex/sessions/**/rollout-*.jsonl`'s `rate_limits` entry — windows are
    matched by `window_minutes`, 300=5h/10080=weekly, not by primary/secondary
    position, since Codex has changed which one it populates; falls back to
    reachable+authed only when no usage entry is found yet), `local-agent`
    (llama.cpp/Ollama live, or startable). Each: `{usable, gas,
    weekly_gas, state, reason}`; state ∈
    `ready|degraded|out_of_gas|offline|unavailable`. Only `ready` is
    dispatchable; degraded/offline/out-of-gas entries are diagnostics, not
    candidates. Cloud engines are marked `degraded` from recent rate-limit/usage
    failures in `worker_reports/events.jsonl`.
  - `engine_gas_dashboard` — same readable first-page dashboard as
    `overlord gas`, returned as plain text for Codex/Claude. Use it when Ben
    asks for "gas", "/gas", current limits, or engine availability; paste the
    full dashboard output unless he asks for analysis.
  - `recommend_engine` — job-fit, gas, and cost-aware ranking. Decisions are
    `dispatch_now`, `dispatch_with_handoff` (use reserve Claude for Claude-shaped
    work), `defer_until_reset`, or `blocked`. Hints (`stakes`, `multi_file`,
    `ambiguous`, `needs_judgment`, `must_stay_local`, `writing_heavy`,
    `review_or_analysis`, `small_chore`, `context_size`, `prefer`) choose the
    suitability tier; for an `nvidia` pick it also chooses the model.
  - `dispatch_overlord_worker` gas guard: dispatching to a non-ready brain
    returns `{blocked:true, recommended:…}` WITHOUT writing the dispatch (so you
    re-dispatch to the suggested engine); `force=true` overrides, and the chosen
    brain's gauge entry is recorded in the dispatch `routing` block. With
    `brain:"auto"` / `auto_route:true`, the dispatch tool follows
    `recommend_engine`: it may write a normal dispatch, write a handoff + arm a
    resume timer for reserve-Claude work, or schedule Claude after reset instead
    of dispatching an unsuitable fallback.
    This is the backstop against the 2026-07-14 "dispatched Claude while out of
    gas" mistake. Config: `OVERLORD_CLAUDE_HANDOFF_USAGE_PERCENT` (default 85,
    shared with `~/.claude/hooks/near-limit-handoff.sh`; router reserve gas is
    `100 - this value`), `OVERLORD_ENGINE_CLAUDE_GAS_FLOOR` (default 5),
    `OVERLORD_ENGINE_CODEX_GAS_FLOOR` (default 5), `OVERLORD_ENGINE_CODEX_DISABLED`
    kill-switch. The old `CLAUDE_NEAR_LIMIT_PERCENT` is still accepted as a
    fallback by the hook/router, but prefer the Overlord env name.
  - `overlord gas` — terminal table of the gauge (no brain launched, like
    `overlord status`); `overlord gas --json` for the raw payload.
- `fetch_relay` (`modules/fetch_relay.py`, own service `overlord-fetch-relay`):
  a localhost-only (127.0.0.1) HTTP endpoint that renders pages with a real
  headless Chromium (Playwright + `playwright-stealth`) instead of a plain
  HTTP client, for sites the brains' own WebFetch tool domain-blocks (e.g.
  reddit.com) or that bot-wall plain curl by fingerprinting a missing JS
  engine. Call it with Bash: `curl -s
  'http://127.0.0.1:8791/fetch?url=<url>'` (default port 8791, overridable via
  `FETCH_RELAY_PORT`). Query params: `url` (required, must be `http(s)://`),
  `format=text|html|json` (default `text`, `json` also includes `title`,
  `final_url`, `status`, and optionally `html` with `include_html=1`). Text is
  a readability extraction via `trafilatura`. Anonymous only — every request
  gets a fresh throwaway browser context, no cookies/login/credential storage,
  matching Ben not using a Reddit account on this desktop. Safeguards: refuses
  to bind anything but loopback (can't become an open proxy), rejects
  non-http(s) URLs, and bounds every fetch with a hard timeout
  (`FETCH_RELAY_TIMEOUT_SECONDS`, default 20s). Runs continuously via the
  `overlord-fetch-relay.service` systemd --user unit (same install pattern as
  `overlord-bridge.service`: `cp overlord-fetch-relay.service
  ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl
  --user enable --now overlord-fetch-relay`). One-time setup after `pip
  install -r requirements.txt`: `playwright install chromium`.
- New project folders should be git repositories by default unless Ben says
  otherwise.
- The shared Overlord instructions are rooted in `AGENTS.md`, and Claude uses
  the `~/CLAUDE.md` / `~/.claude/CLAUDE.md` symlinks that point to it.
- `schedule_resume_after_cap`: Overlord MCP tool (in `plugins/overlord`) that
  auto-resumes a paused/handoff job shortly AFTER the Anthropic usage cap
  resets, so Ben never has to log in at 3am just to say "keep going". It reads
  the reset time itself from the live usage source (the same
  `api.anthropic.com/api/oauth/usage` call ccstatusline + the near-limit hook
  use; prefers the fresh `~/.cache/near-limit-handoff/usage.json`), computes
  reset + `after_reset_seconds` (default 300 = 5 min), and registers a DURABLE
  `systemd --user` transient timer. At fire time `resume_fire.py` drops a bridge
  dispatch (a fresh-session worker) so the persistent bridge runs the
  continuation and reports to Telegram — this SURVIVES the very session that hit
  the cap (unlike the harness `CronCreate`, which is session-only/in-memory).
  Args: `folder` + `task` (tell the worker to read HANDOFF.md and commit before
  done); `cap` = `session` (5-hour, default) or `weekly` (7-day); optional
  `resume_at`/`delay_seconds` overrides. Companion `cap_status` reports both
  caps' utilization %% + reset times. Returns the timer unit + a `cancel_cmd`
  (`systemctl --user stop <unit>.timer`). Built 2026-07-09.

- `net_ssh`: Overlord MCP tool (in `plugins/overlord`) that lets the **Overlord
  only** (never a dispatched worker) reach machines on Ben's LAN over SSH to
  update open-source software or diagnose them. It is a HELD session, not a
  per-command reconnect: `action:"connect"` opens a persistent SSH ControlMaster
  socket (one auth via Ben's ssh-agent — no identity file, no password prompt),
  `action:"run"` runs commands inside that live session with no re-auth,
  `action:"close"` tears it down, `action:"status"` lists live connections.
  Sessions auto-close after 30m idle (`idle_timeout`); the socket lives under the
  private `run/ssh/`; every call appends to `net_ssh_audit.log`. Worker exclusion
  is by cwd — the tool runs only from the home root, and a worker's MCP server
  runs in its project folder. **Gateway rule (behavioral, in AGENTS.md):** if Ben
  tells you to reach a host that IS the approval; if you decide on your own you
  need a host he didn't send you to, ask him first and wait for a yes. Always
  report connect/close state clearly (🔗/🔌) so Ben always knows whether a door
  is open. Auth is ssh-agent only, so an unkeyed host just fails to connect.
  Built 2026-07-22. Registered in `~/.claude.json`, so a new tool appears only in
  a fresh Overlord session.

## Keep In Sync

- Update this file whenever a new helper or repeatable workflow becomes part of
  the Overlord toolkit.
- If a capability matters to Ben, mention it here before assuming either
  assistant will remember it on its own.
