#!/usr/bin/env python3
"""Fire-time helper for the Overlord `schedule_resume_after_cap` MCP tool.

A systemd --user transient timer runs this at the scheduled resume time. It
reads a dispatch-payload TEMPLATE (written when the resume was scheduled),
assigns a FRESH session id, and drops it into the bridge dispatch queue so the
persistent bridge runs the continuation worker and reports to Telegram.

Kept as a permanent helper (rather than generating a shell script per schedule)
so the scheduling tool never has to embed the task text into shell quoting.

Env override for testing: set OVERLORD_DISPATCH_DIR to redirect where the
dispatch file is written (default: <bridge_root>/dispatch).
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import uuid
from pathlib import Path

BRIDGE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DISPATCH_DIR = BRIDGE_ROOT / "dispatch"
LOG = BRIDGE_ROOT / "scheduled_resumes" / "fired.log"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: resume_fire.py <template.json>", file=sys.stderr)
        return 2

    template = Path(sys.argv[1])
    try:
        payload = json.loads(template.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"resume_fire: cannot read template {template}: {exc}", file=sys.stderr)
        return 1

    dispatch_dir = Path(os.environ.get("OVERLORD_DISPATCH_DIR", str(DEFAULT_DISPATCH_DIR)))
    session = str(uuid.uuid4())
    payload["session"] = session

    dispatch_dir.mkdir(parents=True, exist_ok=True)
    out = dispatch_dir / f"{session}.json"
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(
            f"{datetime.datetime.now().isoformat()} fired {template.name} -> "
            f"{out.name} (worker {payload.get('name')!r})\n"
        )
    print(f"resume_fire: dispatched {payload.get('name')!r} session {session} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
