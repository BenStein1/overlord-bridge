#!/usr/bin/env python3
"""MCP wrapper for Ben's local Overlord capabilities.

This intentionally uses a tiny newline-delimited JSON-RPC loop instead of the
Python MCP stdio helper because the helper's async stdin wrapper hangs in this
local Codex sandbox. The protocol surface here is the small subset Codex needs:
initialize, tools/list, tools/call, and initialized notifications.
"""

from __future__ import annotations

import datetime
import importlib
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

# The gauge/router live next to this file; ensure the script dir is importable
# whether we're run as the MCP server (__main__) or imported by tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine_gauge as _engine_gauge  # noqa: E402


BRIDGE_ROOT = Path(__file__).resolve().parents[3]
CAPABILITIES_PATH = BRIDGE_ROOT / "CAPABILITIES.md"
DISPATCH_DIR = BRIDGE_ROOT / "dispatch"
OVERLORD_BIN = BRIDGE_ROOT / "overlord"
EMAIL_BIN = BRIDGE_ROOT / "overlord-email"
SERVER_VERSION = "0.1.0"

# --- auto-resume-after-cap plumbing ---------------------------------------
SCHEDULED_DIR = BRIDGE_ROOT / "scheduled_resumes"
RESUME_FIRE = Path(__file__).resolve().parent / "resume_fire.py"
VENV_PYTHON = BRIDGE_ROOT / ".venv" / "bin" / "python"
# Same usage source the near-limit hook + ccstatusline use.
USAGE_CACHE = Path.home() / ".cache" / "near-limit-handoff" / "usage.json"
CRED_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_API = "https://api.anthropic.com/api/oauth/usage"


def _gauge_module():
    """Reload gas/routing code so long-lived MCP servers do not keep stale guards."""
    return importlib.reload(_engine_gauge)


def engine_gas_gauge() -> dict[str, Any]:
    return _gauge_module().engine_gas_gauge()


def engine_gas_dashboard() -> str:
    gauge_mod = _gauge_module()
    return gauge_mod.format_gauge_cli(gauge_mod.engine_gas_gauge())


def recommend_engine(**kwargs: Any) -> dict[str, Any]:
    return _gauge_module().recommend_engine(**kwargs)


def _run(argv: list[str], *, timeout: int = 60) -> dict[str, Any]:
    proc = subprocess.run(
        argv,
        cwd=str(BRIDGE_ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def _capability_blocks() -> list[dict[str, str]]:
    text = CAPABILITIES_PATH.read_text(encoding="utf-8")
    blocks: list[dict[str, str]] = []
    current_title = ""
    current_lines: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("- `") or line.startswith("- "):
            if current_lines:
                blocks.append(
                    {"title": current_title, "body": "\n".join(current_lines).strip()}
                )
            current_lines = [line]
            current_title = line.lstrip("- ").split(":", 1)[0].strip("` ")
            continue
        if current_lines and (line.startswith("  ") or not line):
            current_lines.append(line)

    if current_lines:
        blocks.append({"title": current_title, "body": "\n".join(current_lines).strip()})
    return blocks


def list_overlord_capabilities() -> list[dict[str, str]]:
    return _capability_blocks()


def find_overlord_capabilities(query: str) -> list[dict[str, str]]:
    needles = [part.casefold() for part in query.split() if part.strip()]
    if not needles:
        return _capability_blocks()
    matches = []
    for block in _capability_blocks():
        haystack = f"{block['title']}\n{block['body']}".casefold()
        if all(needle in haystack for needle in needles):
            matches.append(block)
    return matches


def overlord_status() -> dict[str, Any]:
    return _run([str(OVERLORD_BIN), "status"], timeout=30)


def send_overlord_email(
    to: list[str],
    subject: str,
    body: str,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    argv = [str(EMAIL_BIN)]
    for recipient in to:
        argv.extend(["--to", recipient])
    argv.extend(["--subject", subject, "--body", body])
    for attachment in attachments or []:
        argv.extend(["--attach", attachment])
    return _run(argv, timeout=90)


def _gauge_key(brain: str) -> str:
    """Map a dispatch brain string onto a gauge engine key."""
    b = (brain or "claude").strip().lower()
    if b in {"auto", "recommend", "best"}:
        return "auto"
    if b in {"nim", "nvidia-nim"}:
        return "nvidia"
    if b in {"local-agent", "local_agent", "aider"}:
        return "local-agent"
    return b


def _continuation_task(task: str) -> str:
    return (
        "AUTO-RESUME after the Claude usage cap reset. A previous worker was "
        "deferred or protected because Claude was near/out of gas.\n\n"
        "First read HANDOFF.md in the project folder if it exists. It should "
        "record what was in flight, what was decided, and the concrete next "
        "step.\n\n"
        f"Original task:\n{task}\n\n"
        "Continue that work to completion. Commit before done; do NOT push. If "
        "the work is already complete, verify it and report that instead of "
        "redoing it."
    )


def _append_worker_handoff(folder: str, name: str, task: str, recommendation: dict[str, Any]) -> dict[str, Any]:
    path = Path(folder).expanduser() / "HANDOFF.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().astimezone().isoformat(timespec="minutes")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                "\n"
                f"## Worker checkpoint - {timestamp} - {name}\n"
                "Claude is in reserve gas for a Claude-shaped task. A resume "
                "timer should be armed before/while dispatching.\n\n"
                f"Decision: {recommendation.get('decision')}\n\n"
                f"Task:\n{task}\n\n"
                "Next step: continue the task with the best available Claude/"
                "Codex-quality worker; commit before done and do not push.\n"
            )
        return {"ok": True, "path": str(path)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "path": str(path), "error": str(exc)}


def dispatch_overlord_worker(
    name: str,
    folder: str,
    task: str,
    brain: str = "claude",
    session: str | None = None,
    model: str | None = None,
    resume: bool = False,
    approval_policy: str | None = None,
    sandbox: str | None = None,
    force: bool = False,
    auto_route: bool = False,
    stakes: str = "medium",
    multi_file: bool = False,
    ambiguous: bool = False,
    needs_judgment: bool = False,
    needs_resume: bool = False,
    must_stay_local: bool = False,
    writing_heavy: bool = False,
    review_or_analysis: bool = False,
    small_chore: bool = False,
    context_size: str = "small",
    prefer: str | None = None,
) -> dict[str, Any]:
    key = _gauge_key(brain)
    recommendation: dict[str, Any] | None = None
    handoff: dict[str, Any] | None = None
    scheduled_resume: dict[str, Any] | None = None
    if auto_route or key == "auto":
        recommendation = recommend_engine(
            stakes=stakes,
            multi_file=multi_file,
            ambiguous=ambiguous,
            needs_judgment=needs_judgment,
            needs_resume=needs_resume,
            must_stay_local=must_stay_local,
            writing_heavy=writing_heavy,
            review_or_analysis=review_or_analysis,
            small_chore=small_chore,
            context_size=context_size,
            prefer=prefer,
        )
        decision = recommendation.get("decision")
        if decision == "defer_until_reset":
            schedule = recommendation.get("schedule") or {}
            scheduled_resume = schedule_resume_after_cap(
                folder=folder,
                task=_continuation_task(task),
                cap=str(schedule.get("cap") or "session"),
                name=f"{name}Resume",
                brain="claude",
            )
            return {
                "ok": bool(scheduled_resume.get("ok")),
                "dispatched": False,
                "scheduled": True,
                "decision": decision,
                "recommendation": recommendation,
                "schedule": scheduled_resume,
                "reason": recommendation.get("rationale"),
            }
        rec = recommendation.get("recommended")
        if not rec:
            return {
                "ok": False,
                "blocked": True,
                "dispatched": False,
                "decision": decision or "blocked",
                "recommendation": recommendation,
                "reason": recommendation.get("rationale"),
            }
        brain = rec["brain"]
        model = model or rec.get("model")
        key = _gauge_key(brain)
        if decision == "dispatch_with_handoff":
            handoff = _append_worker_handoff(folder, name, task, recommendation)
            scheduled_resume = schedule_resume_after_cap(
                folder=folder,
                task=_continuation_task(task),
                cap=str((recommendation.get("handoff") or {}).get("cap") or "session"),
                name=f"{name}Resume",
                brain="claude",
            )
            if not handoff.get("ok") or not scheduled_resume.get("ok"):
                return {
                    "ok": False,
                    "blocked": True,
                    "dispatched": False,
                    "decision": decision,
                    "recommendation": recommendation,
                    "handoff": handoff,
                    "schedule": scheduled_resume,
                    "reason": "reserve-Claude dispatch requires a handoff and resume timer",
                }

    # Gas guard: don't dispatch into an engine the gauge says is non-ready.
    # This is the backstop that would have caught the 2026-07-14
    # "dispatched Claude while it was out of gas" mistake even if the Overlord
    # never consulted recommend_engine. `force=True` overrides it.
    routing: dict[str, Any] = {}
    gauge_mod = _gauge_module()
    if key in gauge_mod.ENGINES:
        gauge = gauge_mod.engine_gas_gauge()
        entry = gauge["engines"].get(key)
        routing = {
            "brain": key,
            "engine": entry,
            "forced": bool(force),
            "checked_at": gauge.get("checked_at"),
        }
        if recommendation is not None:
            routing["recommendation"] = recommendation
        if handoff is not None:
            routing["handoff"] = handoff
        if scheduled_resume is not None:
            routing["scheduled_resume"] = scheduled_resume
        if entry is not None and not gauge_mod.is_dispatchable_engine(entry) and not force:
            rec = gauge_mod.recommend_engine()
            return {
                "ok": False,
                "blocked": True,
                "brain": key,
                "reason": f"{key} is not usable right now: {entry.get('reason')}",
                "engine": entry,
                "recommended": rec.get("recommended"),
                "recommendation": rec,
                "hint": "re-dispatch to the recommended brain, or pass force=true to override "
                        "if you know the gauge is wrong.",
            }

    dispatch_id = session or str(uuid.uuid4())
    payload: dict[str, Any] = {
        "name": name,
        "folder": str(Path(folder).expanduser()),
        "task": task,
        "brain": brain,
        "session": dispatch_id,
    }
    if model:
        payload["model"] = model
    if resume:
        payload["resume"] = True
    if approval_policy:
        payload["approval_policy"] = approval_policy
    if sandbox:
        payload["sandbox"] = sandbox
    if routing:
        payload["routing"] = routing

    DISPATCH_DIR.mkdir(parents=True, exist_ok=True)
    path = DISPATCH_DIR / f"{dispatch_id}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "dispatched": True, "decision": (recommendation or {}).get("decision", "manual"),
            "dispatch_file": str(path), "session": dispatch_id,
            "payload": payload, "routing": routing or None}


def _parse_iso(value: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _seconds_until(when: datetime.datetime) -> float:
    now = datetime.datetime.now(when.tzinfo) if when.tzinfo else datetime.datetime.now()
    return max(0.0, (when - now).total_seconds())


def _usage_json(max_age: int = 120) -> dict[str, Any] | None:
    """Return the live usage caps, reusing the near-limit hook's fresh cache
    when possible, otherwise hitting the same OAuth usage API ccstatusline uses."""
    try:
        if USAGE_CACHE.exists():
            age = datetime.datetime.now().timestamp() - USAGE_CACHE.stat().st_mtime
            if age < max_age:
                return json.loads(USAGE_CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    try:
        token = (
            json.loads(CRED_PATH.read_text(encoding="utf-8"))
            .get("claudeAiOauth", {})
            .get("accessToken")
        )
    except Exception:  # noqa: BLE001
        token = None
    if not token:
        return None
    res = _run(
        [
            "curl", "-s", "--max-time", "6",
            "-H", f"Authorization: Bearer {token}",
            "-H", "anthropic-beta: oauth-2025-04-20",
            USAGE_API,
        ],
        timeout=15,
    )
    if not res["ok"] or not res["stdout"]:
        return None
    try:
        data = json.loads(res["stdout"])
    except Exception:  # noqa: BLE001
        return None
    if (data.get("five_hour") or {}).get("utilization") is None:
        return None
    try:
        USAGE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_CACHE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return data


def _cap_reset(cap: str = "session") -> dict[str, Any]:
    data = _usage_json()
    if not data:
        return {"ok": False, "error": "could not read usage caps (no fresh cache, no live token)"}
    key = "seven_day" if cap in ("weekly", "seven_day", "7d", "week") else "five_hour"
    block = data.get(key) or {}
    return {
        "ok": True,
        "cap": key,
        "utilization": block.get("utilization"),
        "resets_at": block.get("resets_at"),
    }


def cap_status() -> dict[str, Any]:
    """Report both usage caps (utilization % + reset time) from the live source."""
    return {"session": _cap_reset("session"), "weekly": _cap_reset("weekly")}


def schedule_resume_after_cap(
    folder: str,
    task: str,
    cap: str = "session",
    after_reset_seconds: int = 300,
    name: str = "Resume",
    brain: str = "claude",
    resume_at: str | None = None,
    delay_seconds: int | None = None,
) -> dict[str, Any]:
    """Set a DURABLE systemd --user timer that, after the usage cap resets, drops
    a bridge dispatch to continue a paused/handoff job -- so Ben never has to log
    in at 3am just to say "keep going". Reads the reset time itself from the live
    usage source unless resume_at/delay_seconds override it."""
    reset_info: dict[str, Any] | None = None
    if delay_seconds is not None:
        delay = int(delay_seconds)
    elif resume_at:
        when = _parse_iso(resume_at)
        if when is None:
            return {"ok": False, "error": f"unparseable resume_at {resume_at!r}"}
        delay = int(_seconds_until(when)) + int(after_reset_seconds)
    else:
        reset_info = _cap_reset(cap)
        if not reset_info.get("ok"):
            return {"ok": False, "error": reset_info.get("error"),
                    "hint": "pass resume_at (ISO) or delay_seconds to override"}
        resets_at = reset_info.get("resets_at")
        when = _parse_iso(resets_at) if resets_at else None
        if when is None:
            return {"ok": False, "error": f"no usable resets_at for cap {cap!r}",
                    "cap_reset": reset_info, "hint": "pass resume_at or delay_seconds"}
        delay = int(_seconds_until(when)) + int(after_reset_seconds)

    if delay < 60:  # systemd/practical floor; also guards an already-past reset
        delay = 60

    payload = {
        "name": name,
        "folder": str(Path(folder).expanduser()),
        "task": task,
        "brain": brain,
    }
    SCHEDULED_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "resume"
    unit = f"overlord-resume-{slug}-{uuid.uuid4().hex[:8]}"
    template_path = SCHEDULED_DIR / f"{unit}.json"
    template_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    fire_dt = datetime.datetime.now().astimezone() + datetime.timedelta(seconds=delay)

    # Installed unit files, NOT `systemd-run --on-active=`. A systemd-run timer is
    # TRANSIENT: it lives only in the user manager's memory, so a reboot erases it.
    # Ben's PC hard-crashed 2026-07-13 with a resume timer pending and the timer
    # simply ceased to exist -- the exact scenario this tool exists to cover.
    # OnCalendar + Persistent=true is what survives: if the machine is down when the
    # timer was due, systemd fires it on the next boot instead of dropping it.
    # (Requires lingering, which is already enabled for this user.)
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / f"{unit}.service").write_text(
        f"""[Unit]
Description=Overlord scheduled resume: {name}

[Service]
Type=oneshot
ExecStart={VENV_PYTHON} {RESUME_FIRE} {template_path}
# One-shot: retire the timer once it has fired so a later boot cannot replay it.
ExecStartPost=/usr/bin/systemctl --user disable --now {unit}.timer
""",
        encoding="utf-8",
    )
    (unit_dir / f"{unit}.timer").write_text(
        f"""[Unit]
Description=Overlord scheduled resume timer: {name}

[Timer]
OnCalendar={fire_dt.strftime('%Y-%m-%d %H:%M:%S')}
Persistent=true
AccuracySec=30s
Unit={unit}.service

[Install]
WantedBy=timers.target
""",
        encoding="utf-8",
    )

    reload_res = _run(["systemctl", "--user", "daemon-reload"], timeout=30)
    res = _run(
        ["systemctl", "--user", "enable", "--now", f"{unit}.timer"], timeout=30
    )
    return {
        "ok": bool(reload_res["ok"] and res["ok"]),
        "unit": f"{unit}.timer",
        "delay_seconds": delay,
        "fires_at_local": fire_dt.isoformat(timespec="seconds"),
        "survives_reboot": True,
        "cap_reset": reset_info,
        "template": str(template_path),
        "cancel_cmd": f"systemctl --user disable --now {unit}.timer",
        "systemd": ({"ok": True} if reload_res["ok"] and res["ok"]
                    else {"daemon_reload": reload_res, "enable": res}),
    }


ToolFn = Callable[..., Any]
TOOLS: dict[str, tuple[ToolFn, dict[str, Any]]] = {
    "list_overlord_capabilities": (
        list_overlord_capabilities,
        {
            "description": (
                "List all shared Overlord capabilities from CAPABILITIES.md. "
                "Use this to discover local tools and workflows: voice_gateway "
                "phone calls, Telegram phone bridge, overlord launcher, "
                "overlord status/health, brain swap, Allow/Deny approvals, "
                "worker dispatch, overlord-email, worker reports, trusted "
                "Codex workers, local Aider/Ollama coding workers, new project "
                "git defaults, and shared AGENTS.md instructions."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ),
    "find_overlord_capabilities": (
        find_overlord_capabilities,
        {
            "description": (
                "Search Ben's shared Overlord capabilities by keyword. This is "
                "the lazy-load discovery hook for local capabilities such as "
                "voice_gateway phone calls, Telegram phone bridge, overlord "
                "launcher, overlord status/health, brain swap, Allow/Deny "
                "approvals, worker dispatch, overlord-email, worker reports, "
                "trusted Codex workers, local Aider/Ollama coding workers, new "
                "project git defaults, and shared AGENTS.md instructions."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    ),
    "overlord_status": (
        overlord_status,
        {
            "description": (
                "Run `overlord status` to identify the active brain and bridge "
                "health. This launches no worker and no brain."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ),
    "send_overlord_email": (
        send_overlord_email,
        {
            "description": (
                "Send email through Ben's configured Overlord Gmail helper. Use "
                "when Ben asks to email something or send a note/file."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "to": {"type": "array", "items": {"type": "string"}},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "attachments": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["to", "subject", "body"],
                "additionalProperties": False,
            },
        },
    ),
    "dispatch_overlord_worker": (
        dispatch_overlord_worker,
        {
            "description": (
                "Dispatch a named Overlord worker through the bridge queue. "
                "Use brain='auto' or auto_route=true to let recommend_engine "
                "pick by job fit, gas, and cost first. Brains: claude (default; high-stakes/"
                "multi-file/judgment), nvidia (free NIM cloud for contained "
                "well-specified small/medium tasks; pick model via "
                "find_overlord_capabilities('nvidia model selection') — default "
                "gpt-oss), codex (intermittent-but-credited ChatGPT engine: "
                "dispatch ONLY when the gauge says it's up — it burns the free "
                "OpenAI credits and spares Claude quota), local-agent (fully "
                "local/offline contained small edits). "
                "GAS GUARD: if the chosen brain is not dispatch-ready this "
                "returns {blocked:true, recommended:...} WITHOUT dispatching so "
                "you can re-dispatch to the suggested engine; pass force=true to "
                "override. Workers that touch code must commit before reporting done."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "folder": {"type": "string"},
                    "task": {"type": "string"},
                    "brain": {"type": "string", "default": "claude"},
                    "session": {"type": "string"},
                    "model": {"type": "string"},
                    "resume": {"type": "boolean", "default": False},
                    "approval_policy": {"type": "string"},
                    "sandbox": {"type": "string"},
                    "force": {"type": "boolean", "default": False,
                              "description": "dispatch even if the gas gauge marks the brain unusable"},
                    "auto_route": {"type": "boolean", "default": False},
                    "stakes": {"type": "string", "enum": ["low", "medium", "high"],
                               "default": "medium"},
                    "multi_file": {"type": "boolean", "default": False},
                    "ambiguous": {"type": "boolean", "default": False},
                    "needs_judgment": {"type": "boolean", "default": False},
                    "needs_resume": {"type": "boolean", "default": False},
                    "must_stay_local": {"type": "boolean", "default": False},
                    "writing_heavy": {"type": "boolean", "default": False},
                    "review_or_analysis": {"type": "boolean", "default": False},
                    "small_chore": {"type": "boolean", "default": False},
                    "context_size": {"type": "string", "enum": ["small", "large"],
                                     "default": "small"},
                    "prefer": {"type": "string"},
                },
                "required": ["name", "folder", "task"],
                "additionalProperties": False,
            },
        },
    ),
    "engine_gas_gauge": (
        engine_gas_gauge,
        {
            "description": (
                "Gas gauge for every dispatchable engine (claude, nvidia, codex, "
                "local-agent): per-engine {usable, gas%, state, reason}. State is "
                "ready|degraded|out_of_gas|offline|unavailable. Only ready is "
                "dispatchable; degraded/offline/out_of_gas are diagnostics. claude gas is "
                "the live 5-hour session cap; nvidia/codex/local are probed for "
                "reachability/auth. Read this before dispatch so you never send a "
                "worker to an engine that's out of gas or down. recommend_engine "
                "wraps this with task-aware ranking."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ),
    "engine_gas_dashboard": (
        engine_gas_dashboard,
        {
            "description": (
                "Plain-text Overlord gas dashboard, same readable output as "
                "`overlord gas`. Use this when Ben asks for gas, /gas, the gas "
                "gauge, or the current Claude/Codex/local/NVIDIA availability. "
                "Return the full dashboard as-is unless Ben asks for analysis."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ),
    "recommend_engine": (
        recommend_engine,
        {
            "description": (
                "Pick the worker brain to dispatch to, gated by the live gas "
                "gauge, and explain why. Call this BEFORE dispatch_overlord_worker "
                "so you pick right the first time. It routes by job fit first, "
                "then gas/cost: Claude/Codex for complex work, NVIDIA gpt-oss "
                "for medium well-scoped work, local-agent as a small-task last "
                "resort. Returns {decision, recommended:{brain, model?}, ranking, "
                "skipped, rationale, gauge}; decision may be dispatch_now, "
                "dispatch_with_handoff, defer_until_reset, or blocked. Pass task "
                "hints to rank correctly: stakes (low|medium|high), "
                "multi_file, ambiguous, needs_judgment, needs_resume, "
                "must_stay_local, writing_heavy, review_or_analysis, small_chore, "
                "context_size (small|large), prefer."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "stakes": {"type": "string", "enum": ["low", "medium", "high"],
                               "default": "medium"},
                    "multi_file": {"type": "boolean", "default": False},
                    "ambiguous": {"type": "boolean", "default": False},
                    "needs_judgment": {"type": "boolean", "default": False},
                    "needs_resume": {"type": "boolean", "default": False},
                    "must_stay_local": {"type": "boolean", "default": False},
                    "writing_heavy": {"type": "boolean", "default": False},
                    "review_or_analysis": {"type": "boolean", "default": False},
                    "small_chore": {"type": "boolean", "default": False},
                    "context_size": {"type": "string", "enum": ["small", "large"],
                                     "default": "small"},
                    "prefer": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    ),
    "cap_status": (
        cap_status,
        {
            "description": (
                "Report Ben's live Anthropic usage caps: the 5-hour 'session' cap "
                "and the 7-day 'weekly' cap, each with utilization %% and reset "
                "time. Same source ccstatusline and the near-limit hook use."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ),
    "schedule_resume_after_cap": (
        schedule_resume_after_cap,
        {
            "description": (
                "Set a DURABLE (survives this session ending) systemd user timer "
                "that auto-resumes a paused/handoff job shortly AFTER the usage "
                "cap resets -- so Ben never has to log in at 3am just to say 'keep "
                "going'. At fire time it drops a bridge dispatch; the persistent "
                "bridge runs the continuation worker and reports to Telegram. It "
                "reads the reset time itself from the live usage source (default "
                "cap='session' 5-hour; use 'weekly' for the 7-day). Give it the "
                "project `folder` and the continuation `task` (tell the worker to "
                "read HANDOFF.md and commit before done). Override timing with "
                "resume_at (ISO) or delay_seconds only if needed. Unlike "
                "CronCreate this is NOT session-only, so it survives the very cap "
                "that cut work off. Returns the timer unit + a cancel command."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string"},
                    "task": {"type": "string"},
                    "cap": {"type": "string", "default": "session",
                            "description": "'session' (5-hour, default) or 'weekly' (7-day)"},
                    "after_reset_seconds": {"type": "integer", "default": 300,
                            "description": "fire this many seconds after the reset (default 300 = 5 min)"},
                    "name": {"type": "string", "default": "Resume"},
                    "brain": {"type": "string", "default": "claude"},
                    "resume_at": {"type": "string",
                            "description": "override: ISO 8601 time to base the timer on instead of the live reset"},
                    "delay_seconds": {"type": "integer",
                            "description": "override: fire exactly this many seconds from now (ignores reset lookup)"},
                },
                "required": ["folder", "task"],
                "additionalProperties": False,
            },
        },
    ),
}


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _tool_list() -> list[dict[str, Any]]:
    tools = []
    for name, (_, meta) in TOOLS.items():
        tools.append(
            {
                "name": name,
                "description": meta["description"],
                "inputSchema": meta["inputSchema"],
            }
        )
    return tools


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name not in TOOLS:
        raise ValueError(f"unknown Overlord tool: {name}")
    fn, _ = TOOLS[name]
    result = fn(**arguments)
    text = result if isinstance(result, str) else json.dumps(result, indent=2, sort_keys=True)
    return {
        "content": [
            {
                "type": "text",
                "text": text,
            }
        ],
        "isError": False,
    }


def _handle_request(message: dict[str, Any]) -> None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    try:
        if method == "initialize":
            protocol_version = params.get("protocolVersion") or "2025-11-25"
            result = {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "overlord", "version": SERVER_VERSION},
                "instructions": (
                    "Use Overlord tools for Ben's local email, status, worker "
                    "dispatch, and capability lookup."
                ),
            }
        elif method == "tools/list":
            result = {"tools": _tool_list()}
        elif method == "tools/call":
            result = _call_tool(params.get("name", ""), params.get("arguments") or {})
        elif method == "notifications/initialized":
            return
        else:
            if request_id is None:
                return
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
            )
            return
    except Exception as exc:  # noqa: BLE001 - convert tool failures to MCP errors.
        if method == "tools/call":
            result = {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            }
        else:
            if request_id is not None:
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32000, "message": str(exc)},
                    }
                )
            return

    if request_id is not None:
        _write({"jsonrpc": "2.0", "id": request_id, "result": result})


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"parse error: {exc}"},
                }
            )
            continue
        _handle_request(message)


if __name__ == "__main__":
    main()
