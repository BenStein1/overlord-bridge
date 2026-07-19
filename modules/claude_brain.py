"""ClaudeBrain: the Claude Agent SDK behind the :class:`Brain` interface.

This is the original Overlord logic, moved verbatim in behavior:

  * ``ClaudeSDKClient`` with the ``claude_code`` system-prompt preset and
    ``setting_sources=[user, project, local]`` so ~/.claude/CLAUDE.md +
    settings.json (the strict-allowlist deny rules) load as a hard floor.
  * ``can_use_tool`` routes each tool call through the shared
    :class:`PermissionGate` (off-limits pre-screen + Telegram Allow/Deny +
    timeout -> deny).
  * Session persistence via a ``.session`` file passed as ``resume=`` on
    startup, with a stale-session fallback. ``reset`` wipes it.

Session hand-off (2026-07-11): Claude Code will not let two live processes
hold the same session id — a second ``--resume`` on an id another live
process already holds gets silently forked onto a new id instead of resuming
in place (verified empirically; see SHARED_BRAIN_TASK.md). Keeping a
long-lived ``ClaudeSDKClient`` connected across Telegram turns therefore
permanently squats the pinned session and forks Ben's terminal every time he
runs ``overlord``. So this brain holds NO client between turns: ``query()``
connects fresh (re-reading the pin, so it picks up whatever session the CLI
most recently left the pin pointing at), then disconnects in a ``finally``
right after the turn, freeing the session for the CLI (or the next Telegram
turn) to resume in place. Verified: disconnecting a client fully releases
the underlying ``claude`` subprocess, and a subsequent resume of that id from
a fresh process lands on the same id with no fork.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
)

from modules import shared_job
from modules.brain import Brain, PermissionGate, collect_candidates

log = logging.getLogger("overlord.claude")

# Tool-input keys that carry a filesystem path.
PATH_KEYS = ("file_path", "path", "notebook_path", "directory")

# Prepended to every Telegram turn injected into the shared job, so the brain can
# tell a phone message from Ben typing at the keyboard -- a distinction injection
# would otherwise erase, since an injected turn is just an ordinary turn.
#
# Provenance ONLY. Ben, 2026-07-13: "you can be the same in telegram, just it may
# help to know the source of the message." So this must NOT instruct the brain to
# behave differently (shorter, terser, etc) -- same Nova on both mics. It only says
# where the message came in from, which is occasionally load-bearing: he can't see
# a rendered diff or click a file path on a phone, and he may not be at the desk.
TELEGRAM_TAG = "📱 [via Telegram]\n\n"


class ClaudeBrain(Brain):
    name = "claude"

    def __init__(
        self,
        *,
        gate: PermissionGate,
        cwd: str,
        model: str,
        session_file: Path,
    ) -> None:
        self.gate = gate
        self.cwd = cwd
        self.model = model
        self.session_file = session_file
        self._session_id: str | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    # ---------------------------------------------------------------- permission
    @staticmethod
    def _format_detail(
        tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> str:
        detail = ""
        if tool_name == "Bash" and isinstance(tool_input.get("command"), str):
            detail = tool_input["command"]
        else:
            for key in PATH_KEYS:
                if isinstance(tool_input.get(key), str):
                    detail = tool_input[key]
                    break
        title = getattr(context, "title", None) or getattr(context, "display_name", None)
        if title:
            detail = f"{title}\n\n{detail}" if detail else title
        return detail

    async def can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        candidates = collect_candidates(tool_input, PATH_KEYS)
        detail = self._format_detail(tool_name, tool_input, context)
        allow = await self.gate.request(tool_name, detail, candidates)
        if allow:
            return PermissionResultAllow()
        return PermissionResultDeny(message="Denied by operator via Telegram.")

    # ---------------------------------------------------------------- session
    def _load_session(self) -> str | None:
        try:
            sid = self.session_file.read_text().strip()
            return sid or None
        except FileNotFoundError:
            return None

    def _save_session(self, session_id: str) -> None:
        # Persist the latest id each turn (a resumed conversation may be assigned
        # a new id), so the next restart picks up where we left off.
        if session_id and session_id != self._session_id:
            self._session_id = session_id
            try:
                self.session_file.write_text(session_id)
            except OSError as exc:
                log.error("Could not persist session id: %s", exc)

    def _build_options(self, resume: str | None) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            cwd=self.cwd,
            model=self.model,
            system_prompt={"type": "preset", "preset": "claude_code"},
            setting_sources=["user", "project", "local"],
            permission_mode="default",
            can_use_tool=self.can_use_tool,
            resume=resume,
        )

    async def _open(self, resume: str | None) -> ClaudeSDKClient:
        """Connect a fresh client for a single turn (see module docstring)."""
        client = ClaudeSDKClient(options=self._build_options(resume))
        try:
            await client.connect()
            return client
        except Exception as exc:
            if resume is None:
                raise
            # Stale/invalid saved session -> drop it and start fresh.
            log.warning("Resume of %s failed (%s); starting fresh.", resume, exc)
            self._session_id = None
            self.session_file.unlink(missing_ok=True)
            client = ClaudeSDKClient(options=self._build_options(None))
            await client.connect()
            return client

    # ---------------------------------------------------------------- lifecycle
    async def connect(self, resume: str | None = None) -> None:
        # No live client is held between turns (see module docstring), so this
        # just seeds which session the first query() will resume. Honor an
        # explicit resume id; otherwise pick up the persisted pin.
        sid = resume if resume is not None else self._load_session()
        self._session_id = sid
        log.info(
            "ClaudeBrain ready (cwd=%s, model=%s, resumed=%s); "
            "sessions open per turn, not held",
            self.cwd,
            self.model,
            sid is not None,
        )

    async def reset(self) -> None:
        self.session_file.unlink(missing_ok=True)
        self._session_id = None
        log.info("Claude session reset via /new")

    async def disconnect(self) -> None:
        # Nothing to tear down: no client is held outside an active turn.
        return

    async def _watch_and_push(
        self,
        job: "shared_job.SharedJob",
        message: str,
        *,
        timeout_s: float = 1800.0,
        poll_s: float = 2.0,
    ) -> None:
        """Poll the shared job's transcript and push the COMPLETE reply to Ben on
        Telegram once the turn actually finishes.

        Turn completion is read from the transcript itself -- an assistant record
        with stop_reason 'end_turn', or a following human turn (see
        ``shared_job.poll_reply_status``) -- NOT from the reply text pausing. A
        reply that opens with one line, runs tool calls for a minute, then answers
        looks "paused" the whole time the tools run; the old stability heuristic
        mistook that pause for the end and pushed only the opening line,
        black-holing everything after it. Runs detached on the bridge loop so it
        outlives the turn that spawned it.
        """
        import time as _time

        deadline = _time.monotonic() + timeout_s
        last: str | None = None
        prev: str | None = None
        try:
            while _time.monotonic() < deadline:
                await asyncio.sleep(poll_s)
                reply, done = await asyncio.to_thread(
                    shared_job.poll_reply_status, job, message
                )
                if reply:
                    last = reply
                # Push only when the turn has ENDED and the reply text is the same
                # as the previous poll. end-of-turn can be stamped on the final
                # message's thinking record a beat before its visible-text record
                # is flushed; one stable poll guarantees we send the whole answer,
                # never a half-flushed one. A mid-turn tool pause never satisfies
                # `done`, so it can't trigger an early push.
                if done and reply and reply == prev:
                    self.gate.telegram.send(self.gate.owner, reply)
                    return
                prev = reply
            log.warning(
                "Async reply watcher for shared job %s gave up after %.0fs.",
                job.job_id, timeout_s,
            )
            if last:
                # Never closed cleanly but we have text: send it rather than
                # leaving Ben with only the ack.
                self.gate.telegram.send(self.gate.owner, last)
        except Exception as exc:  # noqa: BLE001 - a watcher must never crash the loop
            log.error("Async reply watcher for %s failed: %s", job.job_id, exc)

    async def query(self, text: str) -> AsyncIterator[str]:
        # Near-cap reset contract with ~/.claude/hooks/near-limit-handoff.sh: the
        # hook cannot just delete the pin itself, because query() re-pins on every
        # ResultMessage (see module docstring / lines below), which would silently
        # overwrite a hook-side delete seconds later. So the hook drops a flag and
        # the bridge honors it here, at the very start of the turn. Consumed
        # (unlinked) first and unconditionally, so a skip below can never leave it
        # around to retry forever.
        reset_flag = self.session_file.parent / f".reset-requested{self.session_file.suffix}"
        reset_requested = reset_flag.exists()
        if reset_requested:
            reset_flag.unlink(missing_ok=True)

        # One brain, two mics: if Ben's terminal is running `overlord --shared`,
        # his session is a live Claude Code background job that a second client
        # can attach to. Type this Telegram turn straight into THAT job, so it
        # lands in the same conversation he sees in his terminal — rather than
        # opening our own SDK process on the same session. Falls back to the
        # ordinary per-turn SDK path (Design B) whenever no live shared job
        # exists, so the plain `overlord` flow is untouched. Resolved once here
        # and reused below, rather than calling live_job twice per turn.
        job = await asyncio.to_thread(shared_job.live_job, self.session_file)

        if reset_requested:
            if job is not None:
                # Claude Code refuses to resume a session a background --bg job
                # holds. If we reset here, the pin would flip to a brand-new id
                # while Ben's terminal is still sitting on the old one -- silently
                # detaching Telegram from the session he's actually looking at,
                # with no way back. Skip the reset; the flag is already consumed
                # above so this does not retry forever.
                log.warning(
                    "Reset requested near usage cap but shared job %s is live; "
                    "skipping reset so Telegram isn't detached from Ben's session.",
                    job.job_id,
                )
            else:
                await self.reset()
                log.info("Reset requested near usage cap; starting a fresh session.")

        if job is not None:
            log.info("Injecting Telegram turn into shared job %s", job.job_id)
            # Stamp the provenance. Injection lands a Telegram message as an
            # ordinary turn, indistinguishable from Ben typing at the keyboard, so
            # the brain loses a signal that used to be implicit in "this arrived
            # over the bridge". Ben offered to type a tag himself; he shouldn't
            # have to -- the bridge already knows which mic it came from.
            tagged = TELEGRAM_TAG + text
            reply = await asyncio.to_thread(shared_job.inject, job, tagged)
            if isinstance(reply, str) and reply:
                # Idle session answered inside the sync window: inline, as before.
                if job.session_id:
                    self._save_session(job.session_id)
                yield reply
                return
            if reply is shared_job.DELIVERED_PENDING:
                # Delivered into the live session but no reply yet (queued behind
                # the current turn, or the turn is long). Ack now and push the real
                # reply when it lands -- NEVER make Ben resend a message that
                # actually arrived. This is the core of today's fix.
                if job.session_id:
                    self._save_session(job.session_id)
                asyncio.create_task(self._watch_and_push(job, tagged))
                yield (
                    "✅ Delivered into the live terminal session. It's mid-turn, so "
                    "I'll send the reply here the moment it finishes — no need to resend."
                )
                return
            # reply is None: genuinely not delivered. Do NOT fall through to the
            # SDK path -- Claude Code REFUSES to resume a session a background job
            # holds, and `_open`'s stale-session handler would read that as a dead
            # pin, DELETE it, and start a brand-new conversation, silently
            # detaching Telegram from the session Ben is sitting in. Report it; a
            # wiped pin is not recoverable, this is.
            log.warning("Shared job %s did not accept the message; not risking the pin.", job.job_id)
            yield (
                "⚠️ Couldn't hand that to the terminal session just now — the input "
                "line had text typed at the keyboard, or the session didn't render. "
                "Nothing was sent; try again in a moment."
            )
            return

        # Always resume from the freshest pin — this is what lets a terminal
        # CLI turn (which re-pins on exit) be picked up here, and vice versa.
        sid = self._load_session()
        self._session_id = sid
        client = await self._open(sid)
        try:
            await client.query(text)
            sent_text = False
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            sent_text = True
                            yield block.text
                elif isinstance(msg, ResultMessage):
                    if msg.session_id:
                        self._save_session(msg.session_id)
                    if msg.is_error:
                        yield f"⚠️ {msg.result or 'The Overlord hit an error.'}"
                    elif not sent_text and msg.result:
                        yield msg.result
        finally:
            try:
                await client.disconnect()
            except Exception as exc:
                log.error("Error disconnecting after turn: %s", exc)
