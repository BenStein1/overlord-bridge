"""Inject a Telegram turn into the SHARED Claude Code job — the bridge's mic.

This is the bridge half of "one brain, two microphones" (SHARED_BRAIN_TASK.md).
``overlord --shared`` puts Ben's terminal in a Claude Code *background job*
(``claude --bg --resume <pin>``) and attaches to it; this module lets the bridge
attach to that same job and type into it, so a Telegram message and Ben's
terminal are literally the same conversation.

Why this works when ``--resume`` did not
----------------------------------------
Two independent ``claude --resume`` processes cannot hold one session id — the
second silently forks (SHARED_BRAIN_TASK.md). A ``--bg`` job is different: it is
hosted by Claude Code's on-demand local daemon behind a real pty
(``claude bg-pty-host``), and ``claude attach <job>`` is a thin client onto that
pty. MULTIPLE attach clients can be connected at once; input typed by one shows
up live in the other, and the transcript stays a single unforked session.
Verified end to end (research/one_brain_poc/NOTES.md, plus a mismatched-size
re-probe): a TTY-less process attached at 200x50 typed into a job whose other
client was a 100x30 terminal — the turn appeared live in that terminal, the
reply was readable from the transcript, and the file held exactly one sessionId.
That last probe also settles the open resize question: the mismatched attach did
NOT reflow the first client, so the bridge cannot garble Ben's terminal.

We read the reply from the session's JSONL transcript rather than scraping the
TUI: no ANSI parsing, and it is the same "tail the transcript" pattern the repo
already uses for worker-completion detection.

PERMISSIONS — read this before trusting the path with anything destructive.
An injected turn executes inside the job's own ``claude`` process, so its tool
calls go through that process's permission settings, NOT through this bridge's
:class:`PermissionGate` (Telegram Allow/Deny). The gate still guards the
fall-back SDK path. Ben's ``~/.claude/settings.json`` currently sets
``defaultMode: bypassPermissions``, so this is consistent with the posture he
chose for the CLI — but it does mean a Telegram turn on the shared job will not
raise an Allow/Deny prompt. Turning the gate back on for this path means running
the job with a non-bypass permission mode and confirming the prompt renders
through ``attach`` (untested — see PROGRESS.md).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import pty
import struct
import subprocess
import termios
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("overlord.shared_job")

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# The pty we attach with. Size is irrelevant to the other client (probed: a
# mismatched attach does not reflow the first client), so a roomy default keeps
# any TUI wrapping in our own throwaway pty from mangling the text we type.
ATTACH_COLS = 200
ATTACH_ROWS = 50

# How long to wait for `claude attach` to finish "Attaching…" and actually paint
# its input prompt. Typing before the prompt exists drops the keystrokes, and
# judging idleness off a half-painted frame just reports "can't tell".
PROMPT_WAIT_S = 15.0


@dataclass(frozen=True)
class SharedJob:
    job_id: str
    session_id: str   # the PINNED conversation id, straight from the sidecar
    cwd: str


BRIDGE_ROOT = Path(__file__).resolve().parents[1]


def default_session_file(brain: str = "claude") -> Path:
    """The pin `overlord` and the bridge share (``<repo>/.session.<brain>``)."""
    return BRIDGE_ROOT / f".session.{brain}"


def sidecar_for(session_file: Path) -> Path:
    """Where ``overlord --shared`` records the job it created for this pin."""
    return Path(str(session_file) + ".job")


def _roster() -> list[dict]:
    try:
        out = subprocess.run(
            ["claude", "agents", "--json"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            return []
        data = json.loads(out.stdout or "[]")
        return data if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001 - never let discovery break a turn
        log.warning("Could not read `claude agents --json`: %s", exc)
        return []


def live_job(session_file: Path) -> SharedJob | None:
    """The live shared job for this pin, or None (→ caller uses the SDK path).

    Deliberately does NOT create a job. If Ben's terminal is a plain interactive
    ``claude --resume`` (today's default `overlord`), starting a ``--bg`` job on
    the same session id would put a SECOND live process on that session file —
    the exact class of bug Design B was built to kill. Job creation belongs to
    ``overlord --shared``; the bridge only ever joins one that already exists.
    """
    try:
        pin = session_file.read_text().strip()
    except FileNotFoundError:
        return None
    if not pin:
        return None

    # The roster is the authority, and it is keyed on the job's LIVE session id.
    # Note `claude --bg --resume <old>` does NOT resume in place: it starts a NEW
    # session id whose transcript carries the history forward, leaving the old
    # file frozen. So the pin is kept pointing at the LIVE id (the launcher
    # re-pins after creating the job), and a job is found by matching it. This
    # also means the shared job is discoverable with no sidecar at all — which
    # matters, because a stale sidecar must never send us to a frozen transcript.
    dead = ("exited", "stopped", "failed")
    for entry in _roster():
        if entry.get("kind") != "background" or entry.get("status") in dead:
            continue
        if str(entry.get("sessionId") or "") == pin:
            return SharedJob(
                job_id=str(entry.get("id") or ""),
                session_id=pin,
                cwd=str(entry.get("cwd") or ""),
            )

    # Sidecar fall-back: "<job-id>:<live-session-id>", written by `overlord --shared`.
    try:
        recorded = sidecar_for(session_file).read_text().strip()
    except FileNotFoundError:
        return None
    job_id, _, recorded_sid = recorded.partition(":")
    if not job_id or (recorded_sid and recorded_sid != pin):
        return None
    for entry in _roster():
        if entry.get("id") == job_id and entry.get("kind") == "background" \
                and entry.get("status") not in dead:
            return SharedJob(
                job_id=job_id,
                session_id=recorded_sid or pin,
                cwd=str(entry.get("cwd") or ""),
            )
    return None


def transcript_for(job: SharedJob) -> Path | None:
    """The JSONL the job writes its conversation to — named for its LIVE session.

    Returned even if it does not exist yet. A job resumed with no prompt has not
    written a transcript, and "fall back to the newest file in the dir" then
    resolves to the FROZEN pre-resume transcript — a file that never grows, so the
    reply is never seen and the turn times out (observed: the answer landed in the
    live file while the bridge tailed the dead one). Only guess by mtime when we
    have no session id at all.

    The job's cwd is NOT a reliable locator. A shared job's working directory
    legitimately moves — into a git worktree, a project subdir — while its
    transcript stays under the project dir of wherever the session was FIRST
    created. So when we know the session id, find the file by its (globally
    unique) name rather than trusting the cwd-derived slug; a stale cwd otherwise
    resolves to a project dir that does not exist and every Telegram delivery
    fails INSTANTLY with "No transcript found" (observed: entering a worktree to
    make a fix moved this job's cwd and silently broke the whole bridge path).
    """
    if job.session_id:
        name = f"{job.session_id}.jsonl"
        # Fast path: the project dir derived from the current cwd, when it holds
        # the file. Cheap, and correct whenever the cwd hasn't wandered.
        if job.cwd:
            direct = PROJECTS_DIR / job.cwd.replace("/", "-").replace(".", "-") / name
            if direct.exists():
                return direct
        # cwd moved or never matched: the filename is unique, so locate it across
        # all project dirs — newest-written wins, i.e. the live file, never a
        # frozen twin left behind by a resume.
        matches = sorted(PROJECTS_DIR.glob(f"*/{name}"), key=lambda p: p.stat().st_mtime)
        if matches:
            return matches[-1]
        # Not written yet (just-resumed, no prompt): hand back a stable cwd-derived
        # target to tail, so the first reply is still seen once it lands.
        if job.cwd:
            proj = PROJECTS_DIR / job.cwd.replace("/", "-").replace(".", "-")
            if proj.is_dir():
                return proj / name
        return None

    if not job.cwd:
        return None
    proj = PROJECTS_DIR / job.cwd.replace("/", "-").replace(".", "-")
    if not proj.is_dir():
        return None
    transcripts = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return transcripts[-1] if transcripts else None


def _text_of(rec: dict) -> str:
    content = rec.get("message", {}).get("content")
    if isinstance(content, list):
        return "".join(
            c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return content if isinstance(content, str) else ""


def _reply_status(transcript: Path, message: str) -> "tuple[str | None, bool]":
    """(reply_text, turn_complete) for OUR injected message.

    reply_text is the assistant text following our message, bounded to this turn
    (up to the next human turn). turn_complete is True only once the assistant has
    actually FINISHED: Claude Code marks every mid-turn assistant record
    stop_reason 'tool_use' and only the final one 'end_turn', and a following
    human turn also closes ours. Keying completion off that marker -- instead of
    the reply text merely pausing -- is the fix for the black hole where a reply
    that opened with one line, ran tool calls for a minute, then answered, was
    pushed as just its opening line (the pause during the tools read as "done").

    Anchoring on a line-count baseline is not safe: resuming a conversation into a
    background job writes a FRESH transcript that REPLAYS prior history, so
    "everything after the baseline" happily returns last week's answer. Find our
    own message, then read what came after it.
    """
    try:
        lines = transcript.read_text(errors="replace").splitlines()
    except OSError:
        return None, False

    recs: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    needle = " ".join(message.split())
    anchor = -1
    for i, rec in enumerate(recs):
        if rec.get("type") == "user" and needle in " ".join(_text_of(rec).split()):
            anchor = i
    if anchor < 0:
        return None, False

    chunks: list[str] = []
    done = False
    for rec in recs[anchor + 1:]:
        typ = rec.get("type")
        if typ == "user":
            # A tool_result is part of our turn; a real human/next message ends it.
            content = rec.get("message", {}).get("content")
            is_tool_result = isinstance(content, list) and any(
                isinstance(c, dict) and c.get("type") == "tool_result"
                for c in content
            )
            if not is_tool_result:
                done = True
                break
            continue
        if typ == "assistant":
            t = _text_of(rec).strip()
            if t:
                chunks.append(t)
            if rec.get("message", {}).get("stop_reason") == "end_turn":
                # The turn is finished. Do NOT stop here: Claude Code splits the
                # final assistant message across consecutive records (a thinking
                # record, then the visible text), each stamped end_turn, so
                # breaking on the first drops the actual answer. Keep collecting;
                # the loop ends at EOF or the next human turn.
                done = True

    text = "\n\n".join(chunks) if chunks else None
    return text, done


def _reply_to(transcript: Path, message: str) -> str | None:
    """The assistant text following our injected message (see _reply_status)."""
    return _reply_status(transcript, message)[0]


# inject() returns this when the message was typed (delivered/queued) but no
# reply arrived inside the short synchronous window. It is NOT a failure: the
# caller acks and watches the transcript for the reply to push later. A distinct
# sentinel keeps a real non-delivery (None) from being confused with a slow
# answer to a message that DID land.
DELIVERED_PENDING = object()


def poll_reply(job: "SharedJob", message: str) -> str | None:
    """The current assistant reply to ``message`` in the job's transcript, if any.

    Re-resolves the transcript each call (the live file can rotate when a session
    is resumed), so a background watcher can poll this without holding any pty.
    """
    transcript = transcript_for(job)
    if transcript is None:
        return None
    return _reply_to(transcript, message)


def poll_reply_status(job: "SharedJob", message: str) -> "tuple[str | None, bool]":
    """Like :func:`poll_reply`, but also reports whether the turn has finished.

    Re-resolves the transcript each call so a background watcher can poll without
    holding a pty. The completion flag lets the watcher push the COMPLETE reply at
    end-of-turn instead of guessing from a pause in the text -- a pause any
    mid-turn tool call trivially produces.
    """
    transcript = transcript_for(job)
    if transcript is None:
        return None, False
    return _reply_status(transcript, message)


def _count_lines(path: Path) -> int:
    try:
        with path.open(errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _drain(master_fd: int, seconds: float) -> str:
    """Read whatever the attached TUI renders at us for a moment."""
    import select

    buf = bytearray()
    deadline = time.time() + seconds
    while time.time() < deadline:
        ready, _, _ = select.select([master_fd], [], [], 0.2)
        if not ready:
            continue
        try:
            chunk = os.read(master_fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        buf.extend(chunk)
    return buf.decode(errors="replace")


def _read_frame(master_fd: int, max_wait: float = PROMPT_WAIT_S) -> str:
    """Accumulate output until the TUI has actually drawn its input prompt.

    A fixed sleep is not enough: `claude attach` spends the first seconds on
    "Attaching…" and only then paints the input box. Judging idleness off that
    early frame sees no prompt at all, reports "can't tell", and (correctly but
    uselessly) refuses every injection. So read until a prompt line shows up.
    """
    import select

    buf = bytearray()
    deadline = time.time() + max_wait
    while time.time() < deadline:
        ready, _, _ = select.select([master_fd], [], [], 0.3)
        if ready:
            try:
                chunk = os.read(master_fd, 65536)
            except OSError:
                break
            if chunk:
                buf.extend(chunk)
        if _has_prompt(buf.decode(errors="replace")):
            # The prompt is up; take one more beat so the frame (and any text
            # Ben has already typed into it) is fully painted before we judge.
            buf.extend(_drain(master_fd, 0.8).encode())
            break
    return buf.decode(errors="replace")


_ANSI = None


def _plain(frame: str) -> str:
    global _ANSI
    if _ANSI is None:
        import re
        _ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r")
    return _ANSI.sub("", frame)


def _screen_lines(frame: str) -> list[str]:
    """Render the pty stream into an actual screen, the way a terminal would.

    Scanning the raw stream for the last "❯" does NOT work: Claude Code renders
    every past user turn as its own "❯ <message>" line, and a byte stream carries
    cursor moves rather than screen order — so "last ❯ in the bytes" happily
    picks a line of conversation history and concludes Ben is mid-sentence when
    the input box is in fact empty (observed; it refused every injection).
    Emulating the screen gives real geometry, where the input box is simply the
    bottom-most prompt line.
    """
    import pyte

    screen = pyte.Screen(ATTACH_COLS + 50, ATTACH_ROWS + 10)
    pyte.Stream(screen).feed(frame)
    return list(screen.display)


def _prompt_lines(frame: str) -> list[str]:
    return [ln for ln in _screen_lines(frame) if ln.lstrip().startswith("❯")]


def _has_prompt(frame: str) -> bool:
    return bool(_prompt_lines(frame))


def _is_rule(line: str) -> bool:
    """A horizontal border rule of the TUI (the `───…` lines around the box)."""
    return line.count("─") >= 20


def _is_status_bar(line: str) -> bool:
    """The `Model: … Ctx: … Session: …` status line just under the input box.

    Matched on several of its fixed field labels together so a stray "Model:"
    in conversation text can't be mistaken for the real status bar.
    """
    s = line.lstrip()
    return s.startswith("Model:") and ("Ctx" in line or "Session:" in line)


def _input_box_state(frame: str) -> tuple[str, str]:
    """Locate the REAL input box and report ``(state, typed_text)``.

    ``state`` is one of:
      * ``"empty"`` — the box is on screen and holds no text Ben typed. Safe to
        type: if a turn is running the keystrokes simply queue.
      * ``"text"``  — the box holds genuine text (Ben is mid-message at the CLI).
        Typing now would append to his line; never inject.
      * ``"absent"``— the box isn't on the rendered screen (attach hasn't painted,
        or a broken frame). Caller decides (retry / bounce).

    Why not "the bottom-most ❯ line": Claude Code renders every past user turn AND
    a queued message as its own ``❯ …`` line, so the last ❯ in the frame is often
    conversation history or a message already waiting — read as "Ben is typing"
    it made the injector refuse every delivery (observed: "text at prompt" held
    the whole wait while the real box sat empty). The real input box is the ❯
    region bounded by the two horizontal rules directly above the ``Model:/cwd:``
    status bar; anything above the upper rule is scrollback, not the box.
    """
    lines = _screen_lines(frame)

    status = next(
        (i for i in range(len(lines) - 1, -1, -1) if _is_status_bar(lines[i])),
        None,
    )
    if status is None:
        return ("absent", "")

    lower = next((i for i in range(status - 1, -1, -1) if _is_rule(lines[i])), None)
    if lower is None:
        return ("absent", "")
    upper = next((i for i in range(lower - 1, -1, -1) if _is_rule(lines[i])), None)
    if upper is None:
        return ("absent", "")

    box = lines[upper + 1:lower]
    prompt_idx = next((j for j, ln in enumerate(box) if "❯" in ln), None)
    if prompt_idx is None:
        # Rules and status bar are there but no ❯ between them: treat as not-yet
        # painted rather than empty, so we don't type into a half-drawn frame.
        return ("absent", "")

    first = box[prompt_idx]
    typed = first[first.index("❯") + 1:]
    typed = "\n".join([typed] + box[prompt_idx + 1:]).strip()
    return ("text" if typed else "empty", typed)


# Greyed hints Claude Code renders INSIDE an otherwise-empty input box. These are
# chrome, not text Ben typed, so the box is safe to type into. Matched loosely
# (substring, case-insensitive) so a minor wording change across CLI versions
# does not silently reopen the "queued hint reads as typed text" jam.
_INPUT_BOX_HINTS = (
    "press up to edit queued message",  # a message is queued while a turn runs
    "queued message",
)


def _is_input_box_placeholder(typed: str) -> bool:
    low = typed.lower()
    return any(hint in low for hint in _INPUT_BOX_HINTS)


def input_line_is_idle(frame: str) -> bool | None:
    """Is the input box empty — i.e. safe to type without mangling Ben's message?

    True  = the input box is empty (or holds only UI chrome); typing is safe.
    False = there is text sitting at the prompt (Ben is mid-message). Typing now
            would append to HIS line and our Enter would submit his half-written
            sentence. Never inject.
    None  = couldn't tell; callers treat this as "don't risk it".

    The input box is the BOTTOM-most prompt line on the rendered screen; the ones
    above it are conversation history.
    """
    try:
        state, typed = _input_box_state(frame)
    except Exception as exc:  # noqa: BLE001 - a guard that errors must not inject
        log.warning("Could not render the attached frame (%s); refusing to type.", exc)
        return None
    if state == "absent":
        return None
    # When a turn is running with a message queued, Claude Code paints a greyed
    # hint IN the empty box (e.g. "Press up to edit queued messages"). That is
    # chrome, not text Ben typed: the box is empty and typing now just queues our
    # message too, which is what we want. Treat these hints as an empty box.
    if state == "text" and _is_input_box_placeholder(typed):
        return True
    return state == "empty"


def notify(text: str, *, session_file: Path | None = None) -> bool:
    """Surface an unsolicited message (a worker report) in Ben's live terminal.

    This is the automatic half of the concurrent-notification ask: the bridge
    already pushes worker reports to Telegram, and injecting the same report into
    the shared job makes it appear in whatever terminal is attached — with no
    per-dispatch arming, no tmux launcher wrap, and no Monitor polling (all three
    previously rejected). No-ops when there is no live shared job.

    Refuses to type while Ben has text sitting at the prompt: our Enter would
    submit his half-written message. Telegram delivery is unaffected either way,
    so a skip degrades to today's behavior rather than losing the report.
    """
    session_file = session_file or default_session_file()
    job = live_job(session_file)
    if job is None:
        return False
    return inject(job, text, expect_reply=False, idle_wait_s=15.0) is not None


def inject(
    job: SharedJob,
    message: str,
    *,
    timeout_s: float = 240.0,
    expect_reply: bool = True,
    idle_wait_s: float = 45.0,
    sync_window_s: float = 20.0,
) -> "str | object | None":
    """Type ``message`` into the shared job and return one of three things:

    * the reply text (``str``) -- an idle session answered inside the short
      synchronous window; delivered inline, unchanged UX.
    * :data:`DELIVERED_PENDING` -- the message was typed (queued behind a running
      turn, or the turn is just long) but no reply came in the sync window. NOT a
      failure: the caller acks and pushes the reply when it lands.
    * ``None`` -- a hard failure only (no transcript, or the attach subprocess
      errored). The message is typed regardless of what's at the CLI prompt -- a
      half-typed terminal line no longer blocks delivery -- so this is now rare.
      The caller must not wipe the pin.
    """
    transcript = transcript_for(job)
    if transcript is None:
        log.warning("No transcript found for shared job %s.", job.job_id)
        return None

    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(
        slave_fd, termios.TIOCSWINSZ,
        struct.pack("HHHH", ATTACH_ROWS, ATTACH_COLS, 0, 0),
    )
    proc = subprocess.Popen(
        ["claude", "attach", job.job_id],
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        start_new_session=True,
    )
    os.close(slave_fd)

    try:
        # Ben's directive (2026-07-17): DELIVER the message regardless of what is
        # at the CLI prompt. Wait only for the attach client to paint *a* prompt so
        # our keystrokes aren't dropped into a half-attached pty -- do NOT gate on
        # idleness. The old idle/text guard, judged off the flaky attach frame, is
        # exactly what bounced every message all day, and Ben frequently has text
        # typed at his own terminal. If a half-line is there our paste appends and
        # submits; he has explicitly accepted that over losing the message ("I
        # dont care if theres text at the CLI. It just needs to send it.").
        _read_frame(master_fd)

        # Bracketed paste, then a separate Enter. A bare newline inside the text
        # would otherwise be consumed as a line break by the TUI and leave the
        # message parked in the input box, unsent (observed).
        payload = message.replace("\r\n", "\n").replace("\r", "\n")
        os.write(master_fd, b"\x1b[200~" + payload.encode() + b"\x1b[201~")
        time.sleep(0.6)
        os.write(master_fd, b"\r")

        if not expect_reply:
            # An unsolicited notice (a worker report). We only need it delivered
            # into the conversation; whatever Nova says back shows up in Ben's
            # terminal on its own, and nobody is waiting on it here.
            time.sleep(1.0)
            return ""

        # The message is delivered now (typed; queued if a turn is running).
        # Give a short synchronous window for a fast reply -- an idle session
        # answering a quick question comes back inline here. If the turn is long,
        # or the message queued behind a running one, DON'T block and DON'T report
        # failure: hand back DELIVERED_PENDING so the caller acks and pushes the
        # reply when it lands. This is the fix for the false "timed out" bounce on
        # a message that actually arrived (Nova's turns routinely run minutes).
        sync_deadline = time.time() + sync_window_s
        while time.time() < sync_deadline:
            time.sleep(0.5)
            reply, done = _reply_status(transcript, message)
            # Only return inline once the turn has actually FINISHED inside the
            # window (an idle session answering a quick question). Returning the
            # first text block early -- before the tool calls and the real answer
            # -- is exactly what cut the reply off at its opening line. If it isn't
            # done, hand back DELIVERED_PENDING; the async watcher pushes the whole
            # thing at end-of-turn.
            if done and reply:
                return reply
        return DELIVERED_PENDING
    except Exception as exc:  # noqa: BLE001 - a failed inject must fall back
        log.error("Shared-job injection failed: %s", exc)
        return None
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        try:
            os.close(master_fd)
        except OSError:
            pass
