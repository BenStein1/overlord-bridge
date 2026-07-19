#!/usr/bin/env python3
"""
PoC: inject a turn into an ALREADY-RUNNING local Claude Code background job,
from a plain Python process, the way the bridge would need to.

Mechanism under test (discovered 2026-07-13, did not exist / wasn't documented
at the time of the prior "no local injection surface" diagnosis):

  - `claude --bg "<prompt>"` starts a session hosted by the on-demand daemon
    (`claude daemon`), backed by a real pty (`claude bg-pty-host`).
  - `claude attach <job-id>` is a thin client that connects to that pty over a
    unix socket. Verified (see research/one_brain_poc/NOTES.md) that TWO
    concurrent `claude attach` clients on the same job see each other's input
    live, and the transcript stays a single unforked session.
  - This script is the "bridge" half: it does NOT render a TUI or parse ANSI.
    It opens its own pty, execs `claude attach <job-id>` as the child so the
    daemon sees a normal keystroke-typing client, writes the message + Enter
    to the pty master, then tails the session's JSONL transcript (the same
    kind of file/pattern already used elsewhere in this repo for background
    worker completion detection) for the new assistant reply.
  - A real `claude attach <job-id>` running in a human's terminal (or, in this
    PoC, a tmux pane standing in for one) is the control: it should show the
    injected turn live, proving the injection was visible on the attached
    surface, not just recorded to disk.

Usage:
    python3 inject_poc.py <job-id> <transcript-jsonl-path> "<message>"
"""
import json
import os
import pty
import subprocess
import sys
import time


def tail_new_assistant_text(transcript_path: str, since_line_count: int, timeout_s: float = 30.0) -> str | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with open(transcript_path) as f:
            lines = f.readlines()
        if len(lines) > since_line_count:
            for line in lines[since_line_count:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "assistant":
                    content = rec.get("message", {}).get("content")
                    if isinstance(content, list):
                        text = "".join(
                            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
                        )
                        if text:
                            return text
        time.sleep(0.3)
    return None


def main() -> int:
    job_id, transcript_path, message = sys.argv[1], sys.argv[2], sys.argv[3]

    with open(transcript_path) as f:
        line_count_before = sum(1 for _ in f)

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        ["claude", "attach", job_id],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
    )
    os.close(slave_fd)

    # Give the attach client time to connect and the TUI to render its first frame
    # before we type into it, same as a human would wait for the prompt to appear.
    time.sleep(2.0)

    os.write(master_fd, message.encode() + b"\r")

    print(f"[poc] injected via pty-write to `claude attach {job_id}`: {message!r}", file=sys.stderr)

    reply = tail_new_assistant_text(transcript_path, line_count_before + 0)
    if reply is None:
        print("[poc] FAILED: no new assistant reply observed in transcript within timeout", file=sys.stderr)
        proc.terminate()
        os.close(master_fd)
        return 1

    print(f"[poc] observed new assistant reply via jsonl tail: {reply!r}", file=sys.stderr)
    print(reply)

    proc.terminate()
    os.close(master_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
