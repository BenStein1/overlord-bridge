"""Brain-neutral live delivery of worker reports into the terminal Overlord.

Codex has a real local IPC precedent for this (``thread/inject_items`` against
the always-running app-server that both the bridge and a terminal TUI attach
to — see :mod:`modules.codex_brain`). Claude Code has no equivalent: each
``claude`` invocation is a self-contained process that only reads its session
transcript at start/resume, not while an interactive session is idling. There
is no local socket or hook surface for an external process to push content
into an already-running interactive ``claude`` REPL's conversation. (Claude
Code's own "Remote Control" feature does something similar, but only for the
official claude.ai web/mobile client over a cloud-mediated channel tied to an
Anthropic account login — not something this bridge can drive, and it is
disabled entirely when running under API-key auth.)

So this module builds the closest real substitute out of a mechanism that
*does* exist and is designed for exactly this — tmux, which owns the pty of
an interactive session and exposes first-class primitives for another process
to write into it:

  * ``append_feed`` — durable, always-on delivery. The ``overlord`` launcher
    runs the live brain in a tmux session with a second, passive pane running
    ``tail -F`` on the feed file this appends to. The kernel wakes that reader
    via inotify the instant the bridge writes, so the report shows up in the
    live terminal within the same second it lands in Telegram — genuine push,
    not a poll loop. It never touches the brain pane's input, so it cannot
    corrupt anything Ben is mid-typing.
  * ``inject_turn`` — optional, best-effort deep injection: types the report
    into the live brain pane as a real turn (via ``tmux send-keys``), so the
    brain actually reads it in-context the way Codex's inject_items does.
    Only used when ``pane_input_is_idle`` reports the pane's prompt line is
    empty; on any doubt it is treated as busy and skipped, because there is no
    reliable way to know from outside the pty whether Ben is about to type.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("overlord.cli_notify")

# Claude Code renders its empty-input placeholder hint (e.g. `Try "..."`)
# immediately wrapped in the ANSI "dim" SGR attribute (\x1b[2m); real typed
# text follows the prompt glyph with no such code. Verified empirically
# against Claude Code 2.1.198 by capturing a tmux pane (`tmux capture-pane
# -e`) both idle and mid-type. This is the only signal this module relies on
# to decide "safe to inject" — if the prompt line can't be found, injection
# is skipped.
_DIM_SGR = "\x1b[2m"


def _run_tmux(
    *args: str, timeout: float = 3.0
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("tmux %s failed: %s", args, exc)
        return None
    except FileNotFoundError:
        return None


def tmux_available() -> bool:
    return _run_tmux("-V") is not None


def tmux_session_exists(session: str) -> bool:
    result = _run_tmux("has-session", "-t", f"={session}")
    return result is not None and result.returncode == 0


def append_feed(feed_path: Path, text: str) -> None:
    """Append a formatted worker-report block to the durable CLI feed file."""
    feed_path.parent.mkdir(parents=True, exist_ok=True)
    block = text.strip("\n") + "\n" + ("-" * 60) + "\n"
    with feed_path.open("a", encoding="utf-8") as fh:
        fh.write(block)


def _pane_target(session: str) -> str:
    # Pane 0 is always the brain pane: the `overlord` launcher creates the
    # session with the brain as the first pane, then splits the report-tail
    # pane off of it.
    return f"={session}.0"


def pane_input_is_idle(session: str) -> bool | None:
    """Best-effort: is the brain pane's prompt line empty (safe to inject)?

    Returns ``None`` when the pane can't be inspected or the prompt line
    can't be located, so callers can treat "unknown" the same as "busy" and
    skip injection rather than risk mangling a message Ben is mid-typing.
    """
    result = _run_tmux(
        "capture-pane", "-e", "-p", "-t", _pane_target(session)
    )
    if result is None or result.returncode != 0:
        return None
    for line in reversed(result.stdout.splitlines()):
        glyph = line.find("❯")
        if glyph == -1:
            continue
        # The space after the glyph is U+00A0 (non-breaking), not U+0020 —
        # plain str.lstrip() (no argument) strips it along with regular
        # whitespace; str.lstrip(" ") would not.
        remainder = line[glyph + len("❯") :].lstrip()
        if not remainder:
            return True
        return remainder.startswith(_DIM_SGR)
    return None


def inject_turn(session: str, text: str) -> bool:
    """Type *text* into the live brain pane as a real turn, then press Enter.

    *text* must be single-line: tmux ``send-keys -l`` writes raw bytes, and an
    embedded newline would submit a partial message early.
    """
    if "\n" in text:
        raise ValueError("inject_turn text must be single-line")
    target = _pane_target(session)
    sent = _run_tmux("send-keys", "-t", target, "-l", "--", text)
    if sent is None or sent.returncode != 0:
        return False
    entered = _run_tmux("send-keys", "-t", target, "Enter")
    return entered is not None and entered.returncode == 0
