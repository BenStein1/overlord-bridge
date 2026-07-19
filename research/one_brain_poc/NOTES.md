# One-brain injection mechanism — resolved 2026-07-13

## The question

Can a turn be injected into an ALREADY-RUNNING local Claude Code session so it
appears live in whatever surface is attached (not spawn a new session, not
poll)?

## Answer: YES. A real local mechanism exists, and it is NOT one of the
previously-exhausted dead ends (SDK streaming-input, cloud Remote Control,
tmux-wrapped launcher, Monitor polling).

This mechanism postdates the prior "no local injection surface" diagnosis —
it ships in the installed Claude Code v2.1.207 native binary and was not
present (or not discovered) when Remote Control was ruled cloud-only.

## The mechanism

Claude Code ships an undocumented (`--help` doesn't list it at the top level,
but it works) **on-demand local daemon** (`claude daemon`) that hosts
**background jobs** (`claude --bg "<prompt>"`) behind a real pty
(`claude bg-pty-host`), reachable over a per-boot unix socket directory
(`/tmp/cc-daemon-<uid>/<hash>/`).

`claude attach <job-id>` is a thin client: it connects to that job's pty over
the socket and pipes terminal I/O both ways. Critically:

- **Multiple independent `claude attach <job-id>` clients can be connected to
  the same job at once.** Neither kicks the other. Both render the identical
  live TUI frame (verified via `tmux capture-pane` on two separate panes,
  each running its own `claude attach`).
- **Input typed into one attached client appears live in the other's
  rendered view**, and the reply is generated once — the session transcript
  JSONL stays a single file, single session id, no fork. This is exactly the
  "simultaneous possession" that SHARED_BRAIN_TASK.md said was impossible for
  two live `claude --resume` processes — it turns out it IS possible, but
  only for jobs hosted through this daemon/pty path, not for two independent
  `claude --resume` invocations touching the same session file directly.
- A **plain Python process with no TTY** can be the second client: open a
  pty pair (`os.openpty()`), exec `claude attach <job-id>` with the slave as
  its stdio, write the message + `\r` to the master fd. No ANSI parsing is
  needed to know when the reply is ready — **tail the session's JSONL
  transcript for the next `type: "assistant"` record** (the same
  file/pattern already implied by this repo's background-worker-completion
  detection). This is exactly the shape the bridge needs: it doesn't need to
  render a terminal, just inject a message and read back clean text.

## Evidence (OBSERVED, this run, 2026-07-13)

1. `claude --bg "Reply with exactly the word HELLO..."` → started job
   `66243b16`, backed by `claude bg-pty-host`.
2. tmux pane A: `claude attach 66243b16` — full native TUI rendered
   (statusline, model, effort, permission-mode footer, session/weekly usage —
   everything Ben's real terminal shows).
3. From a **separate** plain Python process (`inject_poc.py`, no tmux, no
   ANSI parsing — see that file): opened its own pty, exec'd
   `claude attach 66243b16` as a subprocess, wrote
   `"What is the capital of France? One word."` + `\r` to the pty master,
   then tailed the job's JSONL transcript for the new assistant record.
   Got back `"Paris"` in ~4s, printed to stdout.
4. **Pane A — which never received any command from step 3 — showed the same
   turn and the same `Paris` reply live**, with updated token/turn counters
   (`In: 4  Out: 11`), captured via `tmux capture-pane` immediately after
   step 3 finished, before anything else touched the job.
5. Transcript JSONL (`~/.claude/projects/-tmp-cc-onebrain-test2/66243b16-*.jsonl`)
   contains a single unforked sequence: `user` "What is the capital..." →
   `assistant` "Paris", same `sessionId` throughout.
6. Cleaned up: `claude stop 66243b16`, `claude stop 3a85e0ea` (an earlier test
   job from the same run), tmux test sessions killed, scratch dirs removed.
   Ben's real interactive session (pid 27391, `claude --resume
   6b0ed319-219a-4302-9c26-361d1766c650`, cwd `/home/user`) was never touched,
   attached to, or sent any input — confirmed alive and unchanged
   before/after via `ps` and `claude agents --json`.

## What this does NOT prove yet (honest gap — INFERRED or untested)

- **Only `--bg`-hosted jobs are attachable this way.** Tested directly:
  `claude attach <session-id>` against a plain interactive session (started
  without `--bg`) fails with `No job matching '<id>'. Run 'claude agents' to
  list running sessions.` — the job registry that `attach` uses is a
  different tracking structure from the general session roster. **This means
  today's default `overlord` path (`claude --resume <pin>`, a plain
  interactive session) is structurally NOT attachable by a second party.**
  To get the shared-brain property, the session Ben's terminal is in must
  itself be started as a background job (`claude --bg` + immediate
  self-attach) instead of a bare interactive `claude --resume`. That is a
  real structural change to the launch path, which is exactly why it's
  gated behind `--shared` (opt-in) rather than made default tonight.
- **Permission-prompt UI through `attach` is architecturally expected to
  work identically** (it's raw terminal passthrough, same TUI binary,
  nothing about the socket protocol suggests it special-cases prompts) but
  was **not directly observed** — Ben's global `~/.claude/settings.json` has
  `defaultMode: bypassPermissions`, which suppresses prompts in every mode
  tested here, bg or interactive. A worker/Ben with a non-bypass default
  should confirm a permission prompt renders and is answerable through
  `attach` before trusting this path with anything destructive.
- **Terminal resize behavior when a second client attaches with different
  dimensions is unverified.** The bg-pty-host is spawned with the *first*
  attacher's terminal size baked in (`bg-pty-host ... 200 50 -- ...`); it's
  unknown whether a later attach with different rows/cols resizes the shared
  pty (which would visibly disrupt whichever client attached first). The
  PoC's own Python-side attach and the tmux control pane happened to use the
  same 200x50 default in this test, so this was not exercised.
- **Full bridge.py rewiring is not done.** The bridge currently opens a
  fresh SDK-managed `claude` process per Telegram turn via
  `ClaudeSDKClient(resume=...)` (`modules/claude_brain.py`, Design B,
  already shipped). Switching the bridge to inject into a shared `--bg` job
  instead is a real architecture change to that module and was correctly
  judged out of scope to ship blind tonight (no Telegram access to verify a
  live round trip, and gate 3 says don't restart the bridge on an unproven
  path). See `PROGRESS.md` for the concrete next-step plan.
- **Daemon lifecycle risk**: the daemon is `origin: transient` — it exits
  when the last client disconnects and no bg workers are running (service
  install is disabled in this Claude Code version: "the daemon runs on
  demand and exits when the last client disconnects"). A `--shared`-mode
  Overlord session therefore depends on the daemon staying alive for as long
  as *any* attacher (Ben's terminal or the bridge) is connected — acceptable
  for Ben's terminal (same lifetime as today), but means the bridge must
  keep at least a light touch on the daemon (e.g. its own attach client, or
  a bg worker) for the job to survive between Telegram messages if Ben's
  terminal isn't open. Not built or tested this run.

## Files

- `inject_poc.py` — the Python-side injection PoC described above. Rerunnable
  against any live `--bg` job id + its transcript path.
