#!/usr/bin/env python3
"""Engine gas gauge + brain preference router for the Overlord.

Before the Overlord dispatches a worker it should know, per engine, whether that
engine is *usable right now* (token/cap gas, offline/unreachable, rate-limited)
and — given the task — which engine to prefer. On 2026-07-14 a Codex Overlord
dispatched a Claude worker while Claude was out of gas; this module exists to
make that impossible: ``recommend_engine`` picks the right agent up front, and
``engine_gas_gauge`` is the shared truth both it and the dispatch guard read.

Deliberately dependency-light and synchronous (curl + stdlib) so it can be:
  * imported by ``overlord_mcp.py`` and exposed as MCP tools,
  * used as a pre-dispatch guard inside ``dispatch_overlord_worker``,
  * run as ``python engine_gauge.py`` for the ``overlord gas`` CLI table,
  * unit-tested with every probe monkeypatched (no network).

Engines and their gas signals:
  * claude      — Anthropic usage caps (same source ccstatusline / the
                  near-limit hook use). Real gas %; blocks at a floor.
  * nvidia      — NIM free cloud: API key present + /models reachable.
  * codex       — intermittent-but-credited ChatGPT-OAuth engine: polled for
                  reachability + auth (NOT hardcoded retired), layered with its
                  real 5-hour + weekly plan usage % parsed from the newest
                  session rollout log's `rate_limits` entry.
  * local-agent — local llama.cpp/Ollama: server live, startable, or offline.

Config comes from the same env keys ``bridge.py`` reads (see ``.env.example``);
``.env`` is loaded from the bridge root so the gauge and the workers agree on
endpoints even though this runs as a separate process.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

try:  # dependency-light: dotenv is already a bridge requirement, but degrade gracefully
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv missing is non-fatal
    load_dotenv = None  # type: ignore[assignment]

BRIDGE_ROOT = Path(__file__).resolve().parents[3]
ENV_PATH = BRIDGE_ROOT / ".env"
if load_dotenv is not None and ENV_PATH.exists():
    # override=False: a value already exported into the MCP server's environment
    # wins over the file, matching how bridge.py treats real env as authoritative.
    load_dotenv(ENV_PATH, override=False)

# --- claude usage (mirrors overlord_mcp._usage_json; kept self-contained so the
#     gauge has zero import coupling to the MCP module) ----------------------
USAGE_CACHE = Path.home() / ".cache" / "near-limit-handoff" / "usage.json"
CRED_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_API = "https://api.anthropic.com/api/oauth/usage"

# --- codex auth / reachability / usage limits ------------------------------
CODEX_AUTH = Path.home() / ".codex" / "auth.json"
CODEX_REACH_URL = "https://chatgpt.com/backend-api/"
# Codex writes its plan USAGE limits (not transient rate-limiting) into each
# session rollout log as a `rate_limits` object: primary = 5-hour window,
# secondary = 7-day/weekly window, each {used_percent, window_minutes, resets_at
# (unix epoch)}. The freshest entry is only as current as the last Codex turn;
# a window whose resets_at is already in the past has rolled over -> 0% used.
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CODEX_GAS_FLOOR = max(0, int(os.environ.get("OVERLORD_ENGINE_CODEX_GAS_FLOOR", "5") or 5))
CODEX_WEEKLY_DEGRADED_FLOOR = 10
# Tail-only read: rate_limits lines land near the end of a rollout file (it's
# appended every turn), so a full read isn't needed and was the ~38s slow path.
CODEX_USAGE_TAIL_BYTES = 256 * 1024
CODEX_USAGE_MAX_FILES = 3
CODEX_USAGE_CACHE = Path.home() / ".cache" / "overlord" / "codex_usage.json"
CODEX_USAGE_CACHE_TTL = 60

# --- engine defaults (kept in sync with modules/workers.py) ----------------
DEFAULT_NVIDIA_API_BASE = "https://integrate.api.nvidia.com/v1"
NVIDIA_DISCOVER_URL = (
    "https://build.nvidia.com/models?filters=nimType%3Anim_type_preview"
    "&orderBy=weightPopular%3ADESC"
)
NVIDIA_REFERENCE_URL = "https://docs.api.nvidia.com/nim/reference/models-1"
DEFAULT_AIDER_API_BASE = "http://127.0.0.1:1234/v1"
DEFAULT_OLLAMA_API_BASE = "http://127.0.0.1:11434/v1"
DEFAULT_LLAMACPP_BIN = "/tank/ai/llama.cpp/build/bin/llama-server"

ENGINES = ("claude", "codex", "local-agent", "nvidia")

# Gas floor: block Claude when the 5-hour session cap has less than this many
# percent left. Env-overridable so the default is the tuning surface.
CLAUDE_GAS_FLOOR = max(0, int(os.environ.get("OVERLORD_ENGINE_CLAUDE_GAS_FLOOR", "5") or 5))
# Weekly cap below this many percent remaining -> "degraded" (not dispatchable).
CLAUDE_WEEKLY_DEGRADED_FLOOR = 10
# Shared with ~/.claude/hooks/near-limit-handoff.sh: at/above this five-hour
# usage %, Claude is reserve gas. The router derives the gas floor from the same
# threshold so the hook and dispatch policy cannot drift.
CLAUDE_HANDOFF_USAGE_PERCENT = max(
    0,
    min(
        100,
        int(
            os.environ.get("OVERLORD_CLAUDE_HANDOFF_USAGE_PERCENT")
            or os.environ.get("CLAUDE_NEAR_LIMIT_PERCENT")
            or "85"
        ),
    ),
)
CLAUDE_RESERVE_GAS_FLOOR = 100 - CLAUDE_HANDOFF_USAGE_PERCENT
DISPATCHABLE_STATES = {"ready"}

# Heuristic degraded window for cloud engines with no clean usage endpoint
# (nvidia rate limits, codex "up and down"): if >= N recent worker reports for
# that engine failed with a rate-limit/quota signature inside the window, mark
# it degraded so it is reported clearly but not selected.
DEGRADED_LOOKBACK_MINUTES = 30
DEGRADED_MIN_HITS = 2
DEGRADED_SCAN_LINES = 60
NVIDIA_MODEL_FAILURE_LOOKBACK_HOURS = max(
    1, int(os.environ.get("OVERLORD_NVIDIA_MODEL_FAILURE_LOOKBACK_HOURS", "24") or 24)
)
NVIDIA_MODEL_SCAN_LINES = 300
NVIDIA_MODEL_SLOW_TEST_SECONDS = max(
    1, int(os.environ.get("OVERLORD_NVIDIA_MODEL_SLOW_TEST_SECONDS", "120") or 120)
)
NVIDIA_CATALOG_CACHE = Path.home() / ".cache" / "overlord" / "nvidia_models.json"
NVIDIA_CATALOG_CACHE_TTL = max(0, int(os.environ.get("OVERLORD_NVIDIA_CATALOG_CACHE_TTL", "300") or 300))
_RATE_LIMIT_SIGNATURE = re.compile(
    r"rate.?limit|\b429\b|quota|usage limit|insufficient|exhaust|too many requests|"
    r"\bbilling\b|credit|overloaded|capacity",
    re.IGNORECASE,
)
_MODEL_NOT_FOUND_SIGNATURE = re.compile(
    r"\b404\b|not found|model[^.\n]*(?:missing|unavailable|does not exist)",
    re.IGNORECASE,
)
_MODEL_QUEUE_SIGNATURE = re.compile(r"queue|queued|timeout|timed out", re.IGNORECASE)
_EVENTS_PATH = BRIDGE_ROOT / "worker_reports" / "events.jsonl"

# Keep this worker-facing alias set in sync with modules/workers.py. The full
# NVIDIA catalog is dynamic from GET /v1/models; aliases are the models Ben can
# ask for by family/friendly name and the auto-router can choose from when marked
# auto-safe.
_NVIDIA_MODEL_ALIASES = {
    "deepseek": "deepseek-ai/deepseek-v4-pro",
    "deepseek-v4": "deepseek-ai/deepseek-v4-pro",
    "deepseek-v4-pro": "deepseek-ai/deepseek-v4-pro",
    "deepseek-flash": "deepseek-ai/deepseek-v4-flash",
    "deepseek-v4-flash": "deepseek-ai/deepseek-v4-flash",
    "kimi": "moonshotai/kimi-k2.6",
    "kimi-k2.6": "moonshotai/kimi-k2.6",
    "minimax": "minimaxai/minimax-m3",
    "minimax-m3": "minimaxai/minimax-m3",
    "minimax-m2.7": "minimaxai/minimax-m2.7",
    "qwen": "qwen/qwen3-next-80b-a3b-instruct",
    "qwen-next": "qwen/qwen3-next-80b-a3b-instruct",
    "qwen3-next": "qwen/qwen3-next-80b-a3b-instruct",
    "qwen3.5": "qwen/qwen3.5-122b-a10b",
    "qwen3.5-122b": "qwen/qwen3.5-122b-a10b",
    "qwen3.5-397b": "qwen/qwen3.5-397b-a17b",
    "gpt-oss": "openai/gpt-oss-120b",
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "gpt-oss-20b": "openai/gpt-oss-20b",
    "mistral": "mistralai/mistral-small-4-119b-2603",
    "mistral-large-3": "mistralai/mistral-large-3-675b-instruct-2512",
    "mistral-small": "mistralai/mistral-small-4-119b-2603",
    "llama": "meta/llama-3.3-70b-instruct",
    "llama-3.3": "meta/llama-3.3-70b-instruct",
    "llama-3.3-70b": "meta/llama-3.3-70b-instruct",
    "llama-3.1-8b": "meta/llama-3.1-8b-instruct",
    "llama-4-maverick": "meta/llama-4-maverick-17b-128e-instruct",
    "nemotron": "nvidia/nemotron-3-super-120b-a12b",
    "nemotron-super": "nvidia/nemotron-3-super-120b-a12b",
    "nemotron-ultra": "nvidia/nemotron-3-ultra-550b-a55b",
    "nemotron-nano": "nvidia/nemotron-3-nano-30b-a3b",
    "llama-nemotron-super": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    "llama-nemotron-nano-vl": "nvidia/llama-3.1-nemotron-nano-vl-8b-v1",
}
_NVIDIA_BIG_FAMILIES = ("nemotron", "qwen", "mistral", "llama", "gpt-oss", "deepseek", "kimi", "minimax")
_NVIDIA_MONITORED_ALIASES = (
    "gpt-oss",
    "mistral",
    "nemotron",
    "qwen3-next",
    "llama",
    "minimax",
    "minimax-m2.7",
    "kimi",
    "deepseek",
)
_NVIDIA_DISABLED_AUTO_ALIASES = {
    # Measured bad as workers. Keep callable manually with force/model if Ben is
    # experimenting, but never auto-select them.
    "deepseek",
    "deepseek-v4",
    "deepseek-v4-pro",
    "kimi",
    "kimi-k2.6",
    "minimax",
    "minimax-m3",
    "minimax-m2.7",
    "deepseek-flash",
    "deepseek-v4-flash",
}
_NVIDIA_AUTO_CANDIDATES = {
    "small": ["gpt-oss", "mistral", "nemotron", "qwen3-next", "llama"],
    "writing": ["mistral", "nemotron", "gpt-oss", "qwen3-next", "llama"],
    "review": ["gpt-oss", "mistral", "nemotron", "qwen3-next", "llama"],
    "default": ["gpt-oss", "mistral", "nemotron", "qwen3-next", "llama"],
}


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _curl_http_code(url: str, *, headers: dict[str, str] | None = None, timeout: int = 3) -> str:
    """Return the HTTP status code as a string ('404', '200', ...) or '000' if
    the host was unreachable within *timeout*. Any non-'000' code == reachable."""
    argv = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", str(timeout)]
    for key, val in (headers or {}).items():
        argv += ["-H", f"{key}: {val}"]
    argv.append(url)
    try:
        proc = subprocess.run(argv, text=True, capture_output=True, timeout=timeout + 4, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return "000"
    return (proc.stdout or "").strip() or "000"


def _curl_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 6,
) -> tuple[dict[str, Any] | None, str | None]:
    argv = ["curl", "-sS", "--max-time", str(timeout), "-H", "Accept: application/json"]
    for key, val in (headers or {}).items():
        argv += ["-H", f"{key}: {val}"]
    argv.append(url)
    try:
        proc = subprocess.run(argv, text=True, capture_output=True, timeout=timeout + 4, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"request failed: {exc}"
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout or f"curl exited {proc.returncode}").strip()
    try:
        data = json.loads(proc.stdout)
    except Exception as exc:  # noqa: BLE001
        return None, f"invalid JSON from {url}: {exc}"
    if not isinstance(data, dict):
        return None, f"unexpected JSON shape from {url}"
    return data, None


def _reachable(url: str, *, headers: dict[str, str] | None = None, timeout: int = 3) -> bool:
    return _curl_http_code(url, headers=headers, timeout=timeout) != "000"


# --------------------------------------------------------------------------- claude
def _claude_usage(max_age: int = 120) -> dict[str, Any] | None:
    """Live usage caps: reuse the near-limit hook's fresh cache, else hit the
    same OAuth usage API. Returns the raw payload (five_hour / seven_day blocks
    with utilization as a 0-100 percent) or None if unavailable."""
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
    try:
        proc = subprocess.run(
            ["curl", "-s", "--max-time", "6",
             "-H", f"Authorization: Bearer {token}",
             "-H", "anthropic-beta: oauth-2025-04-20",
             USAGE_API],
            text=True, capture_output=True, timeout=15, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout)
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


def _probe_claude() -> dict[str, Any]:
    data = _claude_usage()
    if not data:
        return _entry(
            "claude", usable=False, gas=None, state="offline",
            reason="could not read Anthropic usage caps (no fresh cache, no live token)",
        )
    five = data.get("five_hour") or {}
    seven = data.get("seven_day") or {}
    util = five.get("utilization")
    if util is None:
        return _entry("claude", usable=False, gas=None, state="offline",
                      reason="usage payload missing five_hour utilization")
    used = int(round(float(util)))
    gas = max(0, 100 - used)
    resets_at = five.get("resets_at")
    weekly_util = seven.get("utilization")
    weekly_used = None if weekly_util is None else int(round(float(weekly_util)))
    weekly_gas = None if weekly_used is None else max(0, 100 - weekly_used)
    wk_txt = f", weekly {weekly_used}% used" if weekly_used is not None else ""
    detail = {"resets_at": resets_at, "weekly_resets_at": seven.get("resets_at"),
              "session_used": used, "weekly_used": weekly_used}

    if gas < CLAUDE_GAS_FLOOR:
        return _entry(
            "claude", usable=False, gas=gas, weekly_gas=weekly_gas, state="out_of_gas",
            reason=f"5-hour session {used}% used ({gas}% left){wk_txt}; "
                   f"resets {resets_at or 'unknown'}", detail=detail,
        )
    if weekly_gas is not None and weekly_gas < CLAUDE_WEEKLY_DEGRADED_FLOOR:
        return _entry(
            "claude", usable=False, gas=gas, weekly_gas=weekly_gas, state="degraded",
            reason=f"{used}% session used but weekly cap nearly spent ({weekly_used}% used)",
            detail=detail,
        )
    return _entry(
        "claude", usable=True, gas=gas, weekly_gas=weekly_gas, state="ready",
        reason=f"5-hour session {used}% used{wk_txt}; resets {resets_at or 'unknown'}",
        detail=detail,
    )


# --------------------------------------------------------------------------- nvidia
def _probe_nvidia() -> dict[str, Any]:
    api_key = os.environ.get("OVERLORD_WORKER_NVIDIA_API_KEY", "").strip()
    if not api_key:
        return _entry("nvidia", usable=False, gas=None, state="unavailable",
                      reason="no OVERLORD_WORKER_NVIDIA_API_KEY in .env")
    api_base = (os.environ.get("OVERLORD_WORKER_NVIDIA_API_BASE", "").strip()
                or DEFAULT_NVIDIA_API_BASE)
    url = api_base.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    data, err, catalog_source = _nvidia_models_payload(url, headers=headers)
    if data is None:
        if not _reachable(url, headers=headers):
            return _entry("nvidia", usable=False, gas=None, state="offline",
                          reason=f"NIM endpoint unreachable ({api_base})")
        return _entry(
            "nvidia", usable=True, gas=None, state="ready",
            reason=f"NIM endpoint reachable but model catalog unreadable ({err or 'unknown error'})",
            detail={"api_base": api_base, "models_url": url, "catalog_error": err,
                    "free_models_url": NVIDIA_DISCOVER_URL, "reference_url": NVIDIA_REFERENCE_URL},
        )

    catalog_ids = _nvidia_catalog_ids(data)
    if not catalog_ids:
        return _entry("nvidia", usable=False, gas=None, state="offline",
                      reason=f"NIM model catalog empty or unreadable ({api_base})")

    model_entries = _nvidia_model_gas(catalog_ids)
    worker_entries = model_entries
    auto_entries = [m for m in model_entries if m.get("auto_candidate")]
    ready_auto_workers = [m for m in auto_entries if m.get("state") == "ready"]
    unavailable_workers = [m for m in worker_entries if m.get("state") != "ready"]
    detail = {
        "models_url": url,
        "free_models_url": NVIDIA_DISCOVER_URL,
        "reference_url": NVIDIA_REFERENCE_URL,
        "model_scope": "bounded popular-family watchlist, not full catalog output",
        "catalog_source": catalog_source,
        "active_probe": "not run during normal gas; use worker smoke/report history",
        "ready_auto": len(ready_auto_workers),
        "auto_total": len(auto_entries),
        "models": model_entries,
        "gas_source": "100=listed/no recent failure; 0=missing/broken/rate-limited/queued",
    }
    if not ready_auto_workers:
        return _entry(
            "nvidia", usable=False, gas=0, state="degraded",
            reason=f"NIM reachable but no auto-dispatch NVIDIA worker model is ready ({api_base})",
            detail=detail,
        )
    bad_count = len(unavailable_workers)
    suffix = f"; {bad_count} worker model(s) unavailable" if bad_count else ""
    return _entry(
        "nvidia", usable=True, gas=100, state="ready",
        reason=f"NIM endpoint reachable; {len(ready_auto_workers)}/{len(auto_entries)} "
               f"auto-dispatch worker models ready{suffix}",
        detail=detail,
    )


def _nvidia_models_payload(
    url: str,
    *,
    headers: dict[str, str],
) -> tuple[dict[str, Any] | None, str | None, str]:
    if NVIDIA_CATALOG_CACHE_TTL > 0:
        try:
            if NVIDIA_CATALOG_CACHE.exists():
                age = datetime.datetime.now().timestamp() - NVIDIA_CATALOG_CACHE.stat().st_mtime
                cached = json.loads(NVIDIA_CATALOG_CACHE.read_text(encoding="utf-8"))
                if age < NVIDIA_CATALOG_CACHE_TTL and cached.get("url") == url:
                    data = cached.get("data")
                    if isinstance(data, dict):
                        return data, None, "cache"
        except Exception:  # noqa: BLE001
            pass

    data, err = _curl_json(url, headers=headers)
    if data is not None:
        try:
            NVIDIA_CATALOG_CACHE.parent.mkdir(parents=True, exist_ok=True)
            NVIDIA_CATALOG_CACHE.write_text(
                json.dumps({"url": url, "data": data}), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            pass
    return data, err, "live"


def _nvidia_catalog_ids(data: dict[str, Any]) -> set[str]:
    raw = data.get("data")
    if not isinstance(raw, list):
        return set()
    ids: set[str] = set()
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.add(item["id"].strip())
        elif isinstance(item, str):
            ids.add(item.strip())
    return {mid for mid in ids if mid}


def _resolve_nvidia_model_alias(model: str) -> str:
    raw = str(model or "").strip()
    return _NVIDIA_MODEL_ALIASES.get(raw.lower(), raw)


def _nvidia_model_aliases_for(model_id: str) -> list[str]:
    return sorted(alias for alias, mid in _NVIDIA_MODEL_ALIASES.items() if mid == model_id)


def _nvidia_model_gas(catalog_ids: set[str]) -> list[dict[str, Any]]:
    issues = _recent_nvidia_model_issues()
    test_times = _recent_nvidia_model_test_times()
    known_worker_ids = set(_NVIDIA_MODEL_ALIASES.values())
    monitored_ids = {
        _resolve_nvidia_model_alias(alias)
        for alias in _NVIDIA_MONITORED_ALIASES
    }
    auto_ids = {
        model_id for alias, model_id in _NVIDIA_MODEL_ALIASES.items()
        if alias not in _NVIDIA_DISABLED_AUTO_ALIASES
    }
    # Gauge output stays intentionally small: the API call sees the catalog, but
    # the per-model gas report is only Ben's first-page/popular-family watchlist
    # plus any known worker model that recently failed.
    all_ids = sorted(
        (monitored_ids | set(issues) | set(test_times))
        & (known_worker_ids | catalog_ids | set(issues) | set(test_times))
    )
    entries: list[dict[str, Any]] = []
    for model_id in all_ids:
        alias = _nvidia_primary_alias(model_id)
        family = _nvidia_model_families(model_id)[0] if _nvidia_model_families(model_id) else None
        present = model_id in catalog_ids
        issue = issues.get(model_id)
        if not present:
            state = "unavailable"
            gas = 0
            reason = "not listed by GET /v1/models"
        elif issue:
            state = str(issue["state"])
            gas = 0
            reason = str(issue["reason"])
        else:
            state = "ready"
            gas = 100
            reason = "listed by GET /v1/models; no recent worker failure"
        row = {
            "alias": alias,
            "id": model_id,
            "auto_candidate": model_id in auto_ids,
            "gas": gas,
            "state": state,
        }
        if family:
            row["family"] = family
        if model_id in test_times:
            row.update(test_times[model_id])
        if not present or state != "ready":
            row["reason"] = reason
        entries.append(row)
    return entries


def _nvidia_primary_alias(model_id: str) -> str | None:
    aliases = _nvidia_model_aliases_for(model_id)
    for preferred in _NVIDIA_MONITORED_ALIASES:
        if preferred in aliases:
            return preferred
    for preferred in ("qwen3-next", "deepseek", "nemotron", "gpt-oss", "mistral", "llama", "kimi", "minimax"):
        if preferred in aliases:
            return preferred
    return aliases[0] if aliases else None


def _nvidia_model_families(model_id: str) -> list[str]:
    lower = str(model_id or "").lower()
    families: list[str] = []
    checks = {
        "nemotron": ("nemotron",),
        "qwen": ("qwen", "qwq"),
        "mistral": ("mistral", "mixtral", "ministral"),
        "llama": ("llama",),
        "gpt-oss": ("gpt-oss",),
        "deepseek": ("deepseek",),
        "kimi": ("kimi", "moonshot"),
        "minimax": ("minimax",),
    }
    for family in _NVIDIA_BIG_FAMILIES:
        if any(needle in lower for needle in checks[family]):
            families.append(family)
    return families


def _recent_nvidia_model_issues() -> dict[str, dict[str, Any]]:
    try:
        if not _EVENTS_PATH.exists():
            return {}
        lines = _EVENTS_PATH.read_text(encoding="utf-8").splitlines()[-NVIDIA_MODEL_SCAN_LINES:]
    except Exception:  # noqa: BLE001
        return {}

    cutoff = _now() - datetime.timedelta(hours=NVIDIA_MODEL_FAILURE_LOOKBACK_HOURS)
    issues: dict[str, dict[str, Any]] = {}
    clean_after_failure: set[str] = set()
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if ev.get("brain") != "nvidia":
            continue
        ts_raw = str(ev.get("finished_at") or ev.get("timestamp") or ev.get("recorded_at") or "")
        ts = _parse_iso(ts_raw)
        if ts is not None and ts < cutoff:
            continue
        model = _nvidia_event_model(ev)
        if not model or model in issues or model in clean_after_failure:
            continue
        if ev.get("status") == "finished":
            clean_after_failure.add(model)
            continue
        if ev.get("status") not in {"failed", "timed_out", "crashed"}:
            continue
        tail = str(ev.get("result_tail") or "")
        if _MODEL_NOT_FOUND_SIGNATURE.search(tail):
            state = "broken"
            reason = "recent worker call returned not-found/404"
        elif _RATE_LIMIT_SIGNATURE.search(tail):
            state = "out_of_gas"
            reason = "recent worker call hit NVIDIA free-tier quota/rate limit"
        elif _MODEL_QUEUE_SIGNATURE.search(tail) or ev.get("status") == "timed_out":
            state = "degraded"
            reason = "recent worker call queued or timed out"
        else:
            continue
        issues[model] = {
            "state": state,
            "reason": reason,
            "status": ev.get("status"),
            "timestamp": ts_raw,
            "worker": ev.get("name"),
            "tail": tail[-500:],
        }
    return issues


def _recent_nvidia_model_test_times() -> dict[str, dict[str, Any]]:
    try:
        if not _EVENTS_PATH.exists():
            return {}
        lines = _EVENTS_PATH.read_text(encoding="utf-8").splitlines()[-NVIDIA_MODEL_SCAN_LINES:]
    except Exception:  # noqa: BLE001
        return {}

    out: dict[str, dict[str, Any]] = {}
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if ev.get("brain") != "nvidia":
            continue
        if ev.get("status") not in {"finished", "failed", "timed_out", "crashed"}:
            continue
        model = _nvidia_event_model(ev)
        if not model or model in out:
            continue
        seconds = _float_or_none(ev.get("duration_seconds", ev.get("duration_sec")))
        if seconds is None:
            continue
        out[model] = {
            "test_seconds": round(seconds, 3),
            "test_status": ev.get("status"),
            "test_worker": ev.get("name"),
            "test_timestamp": ev.get("finished_at") or ev.get("timestamp") or ev.get("recorded_at"),
            "slow": seconds >= NVIDIA_MODEL_SLOW_TEST_SECONDS,
        }
    return out


def _recent_engine_test_time(brain: str) -> dict[str, Any]:
    try:
        if not _EVENTS_PATH.exists():
            return {}
        lines = _EVENTS_PATH.read_text(encoding="utf-8").splitlines()[-NVIDIA_MODEL_SCAN_LINES:]
    except Exception:  # noqa: BLE001
        return {}

    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if ev.get("brain") != brain:
            continue
        if ev.get("status") not in {"finished", "failed", "timed_out", "crashed"}:
            continue
        seconds = _float_or_none(ev.get("duration_seconds", ev.get("duration_sec")))
        if seconds is None:
            continue
        return {
            "test_seconds": round(seconds, 3),
            "test_status": ev.get("status"),
            "test_worker": ev.get("name"),
            "test_timestamp": ev.get("finished_at") or ev.get("timestamp") or ev.get("recorded_at"),
            "test_model": ev.get("model") or ev.get("effective_local_agent_model"),
            "slow": seconds >= NVIDIA_MODEL_SLOW_TEST_SECONDS,
        }
    return {}


def _nvidia_event_model(ev: dict[str, Any]) -> str:
    model = str(ev.get("effective_local_agent_model") or "").strip()
    if not model:
        model = _resolve_nvidia_model_alias(str(ev.get("model") or ""))
    return model


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- codex
def _find_rate_limits(obj: Any) -> dict[str, Any] | None:
    """Recursively locate a `rate_limits` object inside a parsed rollout JSON
    line. The key's nesting depth has moved between Codex CLI versions (seen
    directly under the event payload historically, under payload.rate_limits
    with 2026-07 builds) so search rather than hardcode a path."""
    if isinstance(obj, dict):
        rl = obj.get("rate_limits")
        if isinstance(rl, dict):
            return rl
        for value in obj.values():
            found = _find_rate_limits(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_rate_limits(item)
            if found is not None:
                return found
    return None


def _codex_usage(max_scan_files: int = CODEX_USAGE_MAX_FILES) -> dict[str, Any] | None:
    """Pull Codex's 5-hour + weekly USAGE limits from the freshest session
    rollout log(s). Returns {session:{used,gas,resets_at,reset_passed}|None,
    weekly:{...}|None, age_hours} or None if no usage entry could be found.
    A window whose resets_at has passed has rolled over -> 0% used / 100% gas.

    Windows are matched by `window_minutes` (300 = 5-hour session, 10080 =
    weekly), not by "primary"/"secondary" position: as of 2026-07-13 Codex
    started reporting only the weekly window as `primary` with `secondary:
    null`, having previously always reported primary=5h/secondary=weekly.
    Cached for CODEX_USAGE_CACHE_TTL seconds since even a tail-only read of
    a few rollout files isn't free to run on every gauge probe."""
    try:
        if CODEX_USAGE_CACHE.exists():
            age = datetime.datetime.now().timestamp() - CODEX_USAGE_CACHE.stat().st_mtime
            if age < CODEX_USAGE_CACHE_TTL:
                return json.loads(CODEX_USAGE_CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass

    try:
        files = sorted(CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:max_scan_files]
    except Exception:  # noqa: BLE001
        files = []
    now = _now().timestamp()

    def window(w: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(w, dict):
            return None
        try:
            used = float(w.get("used_percent") or 0)
        except (TypeError, ValueError):
            used = 0.0
        resets = w.get("resets_at")
        reset_passed = isinstance(resets, (int, float)) and resets <= now
        eff_used = 0.0 if reset_passed else used
        return {"used": round(eff_used, 1), "gas": max(0, int(round(100 - eff_used))),
                "resets_at": resets, "reset_passed": reset_passed}

    result: dict[str, Any] | None = None
    for f in files:
        try:
            with f.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - CODEX_USAGE_TAIL_BYTES))
                tail = fh.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        rl = None
        for line in reversed(tail.splitlines()):
            if '"rate_limits"' not in line:
                continue
            try:
                rl = _find_rate_limits(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
            if rl is not None:
                break
        if rl is None:
            continue
        windows: dict[str, Any] = {}
        for w in (rl.get("primary"), rl.get("secondary")):
            if not isinstance(w, dict):
                continue
            minutes = w.get("window_minutes")
            if minutes == 300:
                windows["session"] = window(w)
            elif minutes == 10080:
                windows["weekly"] = window(w)
        if not windows:
            continue
        try:
            age_hours = round((now - f.stat().st_mtime) / 3600, 1)
        except OSError:
            age_hours = None
        result = {"session": windows.get("session"), "weekly": windows.get("weekly"),
                  "age_hours": age_hours, "plan_type": rl.get("plan_type")}
        break

    try:
        CODEX_USAGE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        CODEX_USAGE_CACHE.write_text(json.dumps(result), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return result


def _probe_codex() -> dict[str, Any]:
    if _truthy(os.environ.get("OVERLORD_ENGINE_CODEX_DISABLED")):
        return _entry("codex", usable=False, gas=None, state="unavailable",
                      reason="disabled via OVERLORD_ENGINE_CODEX_DISABLED")
    if not CODEX_AUTH.exists():
        return _entry("codex", usable=False, gas=None, state="unavailable",
                      reason="no ~/.codex/auth.json (run `codex login`)")
    try:
        auth = json.loads(CODEX_AUTH.read_text(encoding="utf-8"))
        has_token = bool((auth.get("tokens") or {}).get("access_token"))
    except Exception:  # noqa: BLE001
        has_token = False
    if not has_token:
        return _entry("codex", usable=False, gas=None, state="unavailable",
                      reason="~/.codex/auth.json present but no access token")
    if not _reachable(CODEX_REACH_URL):
        return _entry("codex", usable=False, gas=None, state="offline",
                      reason="ChatGPT backend unreachable")

    # Layer the real 5-hour + weekly usage limits on top of reachability.
    usage = _codex_usage()
    sess = usage and usage.get("session")
    wk = usage and usage.get("weekly")
    gas = sess["gas"] if sess else None
    weekly_gas = wk["gas"] if wk else None
    age = usage.get("age_hours") if usage else None
    plan_type = usage.get("plan_type") if usage else None
    detail = {"session": sess, "weekly": wk, "plan_type": plan_type}
    stale = "" if (age is None or age < 24) else f", usage {age}h old"

    if gas is None and weekly_gas is None:
        # reachable+authed but no usage log at all — fall back to the heuristic.
        degraded = _recent_rate_limit_hits("codex")
        if degraded:
            return _entry("codex", usable=False, gas=None, state="degraded",
                          reason=f"reachable+authed; usage-limited recently ({degraded} hits/"
                                 f"{DEGRADED_LOOKBACK_MINUTES}m); no usage log yet")
        return _entry("codex", usable=True, gas=None, state="ready",
                      reason="reachable + authed (no usage log yet)")

    used = sess["used"] if sess else None
    used_txt = f"5-hour usage {used}% used" if used is not None else "5-hour usage unknown"
    wk_used = wk["used"] if wk else None
    wk_txt = f", weekly {wk_used}% used" if wk_used is not None else ""
    if gas is not None and gas < CODEX_GAS_FLOOR:
        return _entry("codex", usable=False, gas=gas, weekly_gas=weekly_gas, state="out_of_gas",
                      reason=f"{used_txt} ({gas}% left){wk_txt}{stale}",
                      detail=detail)
    if weekly_gas is not None and weekly_gas < CODEX_WEEKLY_DEGRADED_FLOOR:
        return _entry("codex", usable=False, gas=gas, weekly_gas=weekly_gas, state="degraded",
                      reason=f"{used_txt} but weekly nearly spent ({wk_used}% used){stale}",
                      detail=detail)
    return _entry("codex", usable=True, gas=gas, weekly_gas=weekly_gas, state="ready",
                  reason=f"{used_txt}{wk_txt}{stale}",
                  detail=detail)


# --------------------------------------------------------------------------- local-agent
def _probe_local_agent() -> dict[str, Any]:
    llama_base = (os.environ.get("OVERLORD_WORKER_LOCAL_AGENT_API_BASE", "").strip()
                  or DEFAULT_AIDER_API_BASE)
    ollama_base = (os.environ.get("OVERLORD_WORKER_LOCAL_AGENT_OLLAMA_API_BASE", "").strip()
                   or DEFAULT_OLLAMA_API_BASE)
    test_time = _recent_engine_test_time("local-agent")
    if _reachable(llama_base.rstrip("/") + "/models", headers={"Authorization": "Bearer overlord-local"}):
        return _entry("local-agent", usable=True, gas=None, state="ready",
                      reason=f"llama.cpp server live ({llama_base})",
                      detail=test_time or None)
    if _reachable(ollama_base.rstrip("/") + "/models"):
        return _entry("local-agent", usable=True, gas=None, state="ready",
                      reason=f"Ollama server live ({ollama_base})",
                      detail=test_time or None)
    bin_path = (os.environ.get("OVERLORD_WORKER_LOCAL_AGENT_LLAMACPP_BIN", "").strip()
                or DEFAULT_LLAMACPP_BIN)
    hf_repo = os.environ.get("OVERLORD_WORKER_LOCAL_AGENT_LLAMACPP_HF_REPO", "").strip()
    if Path(bin_path).exists() or hf_repo:
        return _entry("local-agent", usable=True, gas=None, state="ready",
                      reason="server down but startable (llama.cpp bin/model present) - will cold-start",
                      detail={"startable": True, **test_time})
    return _entry("local-agent", usable=False, gas=None, state="offline",
                  reason=f"no live local server and no llama-server binary at {bin_path}")


# --------------------------------------------------------------------------- helpers
def _entry(engine: str, *, usable: bool, gas: int | None, state: str,
           reason: str, weekly_gas: int | None = None,
           detail: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {"engine": engine, "usable": usable, "gas": gas, "weekly_gas": weekly_gas,
           "state": state, "reason": reason}
    if detail:
        out["detail"] = detail
    return out


def is_dispatchable_engine(entry: dict[str, Any] | None) -> bool:
    """True when an engine entry is safe to select for a new dispatch."""
    return bool(entry and entry.get("usable") and entry.get("state") in DISPATCHABLE_STATES)


def _is_claude_reserve(entry: dict[str, Any] | None) -> bool:
    if not entry or entry.get("engine") != "claude":
        return False
    gas = entry.get("gas")
    return isinstance(gas, int) and gas <= CLAUDE_RESERVE_GAS_FLOOR


def _recent_rate_limit_hits(brain: str) -> int:
    """Count recent worker-report failures for *brain* that look rate/usage
    limited, within the lookback window. Best-effort; any parse error -> 0."""
    try:
        if not _EVENTS_PATH.exists():
            return 0
        lines = _EVENTS_PATH.read_text(encoding="utf-8").splitlines()[-DEGRADED_SCAN_LINES:]
    except Exception:  # noqa: BLE001
        return 0
    cutoff = _now() - datetime.timedelta(minutes=DEGRADED_LOOKBACK_MINUTES)
    hits = 0
    for line in lines:
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if ev.get("brain") != brain:
            continue
        if ev.get("status") not in {"failed", "timed_out", "crashed"}:
            continue
        ts = _parse_iso(str(ev.get("finished_at") or ev.get("timestamp") or ev.get("recorded_at") or ""))
        if ts is not None and ts < cutoff:
            continue
        if _RATE_LIMIT_SIGNATURE.search(str(ev.get("result_tail") or "")):
            hits += 1
    return hits


def _parse_iso(value: str) -> datetime.datetime | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


_PROBES = {
    "claude": _probe_claude,
    "nvidia": _probe_nvidia,
    "codex": _probe_codex,
    "local-agent": _probe_local_agent,
}


def engine_gas_gauge(engines: tuple[str, ...] = ENGINES) -> dict[str, Any]:
    """Probe every engine and return {engine: entry} plus a checked_at stamp.

    Each entry: {engine, usable, gas(0-100|null), state, reason, detail?}.
    Probes are bounded (~3s each) and run sequentially so a single offline
    engine never hangs the whole gauge.
    """
    gauge: dict[str, Any] = {"checked_at": _now().isoformat().replace("+00:00", "Z"), "engines": {}}
    for name in engines:
        probe = _PROBES.get(name)
        gauge["engines"][name] = probe() if probe else _entry(
            name, usable=False, gas=None, state="unavailable", reason="unknown engine")
    return gauge


# --------------------------------------------------------------------------- router
# nvidia model pick by task type (mirrors CAPABILITIES.md "NVIDIA model selection").
def _nvidia_model_for(
    writing_heavy: bool,
    review_or_analysis: bool,
    small_chore: bool,
    nvidia_entry: dict[str, Any] | None = None,
) -> str:
    if review_or_analysis:
        candidates = _NVIDIA_AUTO_CANDIDATES["review"]
    elif writing_heavy:
        candidates = _NVIDIA_AUTO_CANDIDATES["writing"]
    elif small_chore:
        candidates = _NVIDIA_AUTO_CANDIDATES["small"]
    else:
        candidates = _NVIDIA_AUTO_CANDIDATES["default"]

    statuses = _nvidia_model_statuses(nvidia_entry)
    for alias in candidates:
        if alias in _NVIDIA_DISABLED_AUTO_ALIASES:
            continue
        model_id = _resolve_nvidia_model_alias(alias)
        status = statuses.get(model_id)
        # If the gauge could not read model details, keep the old conservative
        # default behavior for known-good aliases. If details exist, require
        # model-level ready so Kimi-style 404s are skipped.
        if not statuses or (status and status.get("state") == "ready"):
            return alias
    for bucket in ("default", "small", "review", "writing"):
        for alias in _NVIDIA_AUTO_CANDIDATES[bucket]:
            if alias in _NVIDIA_DISABLED_AUTO_ALIASES:
                continue
            model_id = _resolve_nvidia_model_alias(alias)
            status = statuses.get(model_id)
            if status and status.get("state") == "ready":
                return alias
    return candidates[0]


def _nvidia_model_statuses(nvidia_entry: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    detail = nvidia_entry.get("detail") if isinstance(nvidia_entry, dict) else None
    models = detail.get("models") if isinstance(detail, dict) else None
    if not isinstance(models, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for model in models:
        if isinstance(model, dict) and isinstance(model.get("id"), str):
            out[model["id"]] = model
    return out


def _default_ranking() -> list[str]:
    # Medium, well-specified default: spend free sharp NIM capacity before Ben's
    # premium sessions, with Codex as the next-smartest cloud fallback.
    return ["nvidia", "codex", "claude"]


def _job_profile(
    *,
    stakes: str,
    multi_file: bool,
    ambiguous: bool,
    needs_judgment: bool,
    needs_resume: bool,
    must_stay_local: bool,
    small_chore: bool,
    context_size: str,
) -> tuple[str, list[str], str, str]:
    stakes_l = str(stakes).lower()
    large_context = str(context_size).lower() == "large"
    high_complexity = (
        stakes_l == "high" or multi_file or ambiguous or needs_judgment or needs_resume
    )
    small_task = small_chore or (stakes_l == "low" and not high_complexity)

    if must_stay_local:
        return (
            "local-only",
            ["local-agent"],
            "must stay local (privacy/offline) - cloud engines excluded",
            "local-only task: spend no cloud or premium quota",
        )
    if high_complexity:
        return (
            "high",
            ["claude", "codex"],
            "high-complexity/ambiguous/multi-file/resume work - Claude/Codex only",
            "do not send this to NVIDIA/local unless the task is narrowed first",
        )
    if small_task:
        return (
            "small",
            ["nvidia", "codex", "local-agent", "claude"],
            "small/well-scoped chore - free sharp engines first, local as last resort",
            "avoid premium Claude gas for basic work",
        )

    order = _default_ranking()
    if large_context:
        # The local model is rough, but its 256k context can rescue a narrow
        # single-file job when the stronger cloud choices are unavailable.
        order = ["nvidia", "codex", "local-agent", "claude"]
    return (
        "medium",
        order,
        "medium well-specified coding - NVIDIA gpt-oss is the practical free fit",
        "use free NVIDIA first; avoid weak-model overreach on vague work",
    )


def _fit_label(profile: str, engine: str, *, context_size: str) -> str:
    large_context = str(context_size).lower() == "large"
    if profile == "high":
        return {"claude": "best", "codex": "next-smartest"}.get(engine, "not-suitable")
    if profile == "medium":
        labels = {"nvidia": "strong-free-fit", "codex": "strong-fallback", "claude": "premium-fallback"}
        if engine == "local-agent":
            return "last-resort-single-file-context" if large_context else "not-suitable"
        return labels.get(engine, "not-suitable")
    if profile == "small":
        return {
            "nvidia": "best-free-fit",
            "codex": "strong-fallback",
            "local-agent": "free-last-resort",
            "claude": "premium-overkill",
        }.get(engine, "not-suitable")
    if profile == "local-only":
        return "only-fit" if engine == "local-agent" else "excluded"
    return "unknown"


def _skip_reason(
    name: str,
    entry: dict[str, Any] | None,
    *,
    allow_reserve_claude: bool,
) -> str | None:
    if not entry:
        return "not probed"
    if not is_dispatchable_engine(entry):
        return f"{entry.get('state', 'unknown')} is not dispatch-ready: {entry.get('reason', 'no reason')}"
    if name == "claude" and _is_claude_reserve(entry) and not allow_reserve_claude:
        gas = entry.get("gas")
        used = 100 - gas if isinstance(gas, int) else "unknown"
        return (
            f"Claude reserve gas ({used}% five-hour usage, {gas}% left); "
            "save it for Claude-shaped work or defer until reset"
        )
    return None


def _defer_for_claude(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if not entry or entry.get("engine") != "claude":
        return None
    detail = entry.get("detail") if isinstance(entry.get("detail"), dict) else {}
    state = entry.get("state")
    if state == "out_of_gas":
        return {
            "cap": "session",
            "reason": entry.get("reason", "Claude five-hour cap is exhausted"),
            "resets_at": detail.get("resets_at"),
        }
    if state == "degraded" and entry.get("weekly_gas") is not None:
        return {
            "cap": "weekly",
            "reason": entry.get("reason", "Claude weekly cap is nearly spent"),
            "resets_at": detail.get("weekly_resets_at"),
        }
    if is_dispatchable_engine(entry) and _is_claude_reserve(entry):
        return {
            "cap": "session",
            "reason": entry.get("reason", "Claude is in five-hour reserve gas"),
            "resets_at": detail.get("resets_at"),
        }
    return None


def recommend_engine(
    *,
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
    """Pick the engine to dispatch to, gated by the live gauge, and explain why.

    Routing is job-fit first, gas/cost second. Complex work goes only to
    Claude/Codex or waits for Claude; medium well-specified work prefers free
    NVIDIA; local-agent is a small-task last resort. Returns:
      {recommended:{brain, model?}, ranking:[{engine, usable, state, reason}...],
       skipped:[{engine, why}...], decision, rationale, gauge}
    """
    gauge = engine_gas_gauge()
    engines = gauge["engines"]
    profile, order, why_order, cost_policy = _job_profile(
        stakes=stakes,
        multi_file=multi_file,
        ambiguous=ambiguous,
        needs_judgment=needs_judgment,
        needs_resume=needs_resume,
        must_stay_local=must_stay_local,
        small_chore=small_chore,
        context_size=context_size,
    )

    # Honor an explicit prefer hint by floating it to the front (if plausible).
    ignored_prefer = None
    if prefer and prefer in ENGINES:
        if prefer in order:
            order = [prefer] + [e for e in order if e != prefer]
            why_order = f"caller preferred {prefer}; " + why_order
        else:
            ignored_prefer = f"caller preferred {prefer}, but it is not suitable for {profile} work"

    allow_reserve_claude = profile == "high"
    skipped: list[dict[str, str]] = []
    picked: str | None = None
    for name in order:
        entry = engines.get(name)
        reason = _skip_reason(name, entry, allow_reserve_claude=allow_reserve_claude)
        if reason is None:
            picked = name
            break
        skipped.append({"engine": name, "why": reason})

    ranking = [
        {"engine": e, "usable": engines[e]["usable"], "state": engines[e]["state"],
         "dispatchable": is_dispatchable_engine(engines[e]),
         "reserve": _is_claude_reserve(engines[e]),
         "gas": engines[e].get("gas"), "weekly_gas": engines[e].get("weekly_gas"),
         "suitability": _fit_label(profile, e, context_size=context_size),
         "reason": engines[e]["reason"]}
        for e in order if e in engines
    ]

    if picked is None:
        defer = _defer_for_claude(engines.get("claude"))
        if defer and profile in {"high", "medium"}:
            rationale = (
                f"Deferred until Claude reset. {why_order}. No ready suitable engine is available; "
                f"{defer['reason']}."
            )
            if ignored_prefer:
                rationale += f" {ignored_prefer}."
            return {
                "recommended": None,
                "decision": "defer_until_reset",
                "ranking": ranking,
                "skipped": skipped,
                "suitability": {"profile": profile, "order": order, "why": why_order},
                "cost_policy": cost_policy,
                "defer_reason": defer["reason"],
                "schedule": {"brain": "claude", "cap": defer["cap"], "resets_at": defer.get("resets_at")},
                "rationale": rationale,
                "gauge": gauge,
            }
        return {
            "recommended": None,
            "decision": "blocked",
            "ranking": ranking,
            "skipped": skipped,
            "suitability": {"profile": profile, "order": order, "why": why_order},
            "cost_policy": cost_policy,
            "defer_reason": None,
            "rationale": f"No dispatch-ready engine for this task ({why_order}). "
                         + "; ".join(f"{s['engine']} ({s['why']})" for s in skipped),
            "gauge": gauge,
        }

    recommended: dict[str, Any] = {"brain": picked}
    if picked == "nvidia":
        recommended["model"] = _nvidia_model_for(
            writing_heavy, review_or_analysis, small_chore, engines.get("nvidia")
        )

    if picked == "local-agent":
        recommended["model"] = "qwen3-coder-next"

    picked_entry = engines[picked]
    decision = "dispatch_now"
    handoff = {"required": False}
    if picked == "claude" and _is_claude_reserve(picked_entry):
        decision = "dispatch_with_handoff"
        handoff = {
            "required": True,
            "cap": "session",
            "threshold_usage": 100 - CLAUDE_RESERVE_GAS_FLOOR,
            "reason": "Claude is in five-hour reserve gas; write HANDOFF.md and arm resume-after-cap",
        }

    # Rationale: explicitly name any preferred-but-skipped engine and why.
    parts = [f"Picked {picked}"]
    if recommended.get("model"):
        parts[0] += f" ({recommended['model']})"
    parts.append(why_order)
    parts.append(cost_policy)
    if skipped:
        parts.append("skipped " + ", ".join(f"{s['engine']}: {s['why']}" for s in skipped))
    if ignored_prefer:
        parts.append(ignored_prefer)
    if handoff["required"]:
        parts.append(handoff["reason"])
    if context_size == "large" and picked == "local-agent":
        parts.append("local-agent is a rough last resort; use it only for a narrow large single-file job")

    return {
        "recommended": recommended,
        "decision": decision,
        "ranking": ranking,
        "skipped": skipped,
        "suitability": {"profile": profile, "order": order, "why": why_order},
        "cost_policy": cost_policy,
        "defer_reason": None,
        "handoff": handoff,
        "rationale": ". ".join(parts) + ".",
        "gauge": gauge,
    }


# --------------------------------------------------------------------------- CLI
_STATE_ICON = {
    "ready": "✅",
    "degraded": "🟡",
    "cold": "🥶",
    "out_of_gas": "❌",
    "offline": "❌",
    "unavailable": "❌",
    "broken": "❌",
}


def _format_test_duration(seconds: Any) -> str | None:
    value = _float_or_none(seconds)
    if value is None:
        return None
    if value < 10:
        text = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{text}s"
    total = int(round(value))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _format_entry_test_cli(entry: dict[str, Any]) -> str:
    detail = entry.get("detail") if isinstance(entry, dict) else None
    if not isinstance(detail, dict):
        return ""
    test_duration = _format_test_duration(detail.get("test_seconds"))
    if not test_duration:
        return ""
    slow = " slow" if detail.get("slow") else ""
    return f" test {test_duration}{slow}"


def _format_nvidia_models_cli(entry: dict[str, Any]) -> list[str]:
    detail = entry.get("detail") if isinstance(entry, dict) else None
    models = detail.get("models") if isinstance(detail, dict) else None
    if not isinstance(models, list):
        return []

    order = {alias: idx for idx, alias in enumerate(_NVIDIA_MONITORED_ALIASES)}

    def sort_key(model: dict[str, Any]) -> tuple[int, str]:
        alias = str(model.get("alias") or model.get("id") or "")
        return (order.get(alias, 999), alias)

    lines = ["    models:"]
    for model in sorted((m for m in models if isinstance(m, dict)), key=sort_key):
        alias = str(model.get("alias") or model.get("id") or "unknown")
        state = str(model.get("state") or "unknown")
        icon = _STATE_ICON.get(state, "•")
        manual = "" if model.get("auto_candidate") else " (manual-only)"
        test_duration = _format_test_duration(model.get("test_seconds"))
        test = f" test {test_duration}" if test_duration else ""
        slow = " slow" if model.get("slow") else ""
        reason = str(model.get("reason") or "").strip()
        suffix = f" - {reason}" if reason and state != "ready" else ""
        lines.append(f"      {alias:<14} {icon} {state}{manual}{test}{slow}{suffix}")
    return lines


_STATUS_LABEL = {
    "ready": "UP",
    "degraded": "WARN",
    "cold": "COLD",
    "out_of_gas": "OUT",
    "offline": "DOWN",
    "unavailable": "NOAUTH",
    "broken": "BROKEN",
}


def _format_cap_usage(label: str, gas: Any) -> str:
    if gas is None:
        return f"{label} unk"
    used = 100 - int(gas)
    return f"{label} {used}% used"


def _format_reset_short(value: Any) -> str:
    if value in (None, ""):
        return "unk"
    dt: datetime.datetime | None = None
    if isinstance(value, (int, float)):
        dt = datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
    elif isinstance(value, str):
        try:
            dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    if dt is None:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    dt = dt.astimezone()
    now = datetime.datetime.now().astimezone()
    clock = dt.strftime("%-I:%M%p").lower()
    if dt.date() == now.date():
        return clock
    return f"{dt.strftime('%b')} {dt.day} {clock}"


def _cloud_status_note(name: str, entry: dict[str, Any]) -> str:
    state = str(entry.get("state") or "")
    if name == "codex":
        detail = entry.get("detail") if isinstance(entry.get("detail"), dict) else {}
        plan = str(detail.get("plan_type") or "").strip()
        parts = []
        if plan:
            parts.append(f"plan {plan}")
        if state == "ready":
            parts.append("auth ok, CLI reachable")
        elif state == "unavailable":
            parts.append("auth missing")
        elif state == "offline":
            parts.append("CLI/backend unreachable")
        else:
            parts.append(entry.get("reason", "check needed"))
        return " | ".join(parts)
    if state in {"ready", "out_of_gas", "degraded"}:
        return "usage API ok"
    return entry.get("reason", "check needed")


def _format_cloud_cli(name: str, entry: dict[str, Any]) -> str:
    icon = _STATE_ICON.get(entry["state"], "•")
    status = _STATUS_LABEL.get(entry["state"], entry["state"]).upper()
    detail = entry.get("detail") if isinstance(entry.get("detail"), dict) else {}
    session_detail = detail.get("session") if name == "codex" else {
        "resets_at": detail.get("resets_at")
    }
    weekly_detail = detail.get("weekly") if name == "codex" else {
        "resets_at": detail.get("weekly_resets_at")
    }
    session = _format_cap_usage("5h", entry.get("gas"))
    weekly = _format_cap_usage("wk", entry.get("weekly_gas"))
    session_reset = _format_reset_short(
        session_detail.get("resets_at") if isinstance(session_detail, dict) else None
    )
    weekly_reset = _format_reset_short(
        weekly_detail.get("resets_at") if isinstance(weekly_detail, dict) else None
    )
    note = _cloud_status_note(name, entry)
    resets = f"5h reset: {session_reset}, wk reset: {weekly_reset}"
    return f"  {name:<7} {icon} {status:<6} {session:<13} | {weekly:<11} | {resets:<42} | {note}"


def _local_summary(reason: str) -> str:
    if "Ollama server live" in reason:
        return "Ollama live"
    if "llama.cpp server live" in reason:
        return "llama.cpp live"
    if "server down but startable" in reason:
        return "cold-startable"
    return reason


def _short_model_reason(reason: str) -> str:
    reason_l = reason.lower()
    if "not-found" in reason_l or "404" in reason_l:
        return "not-found/404"
    if "queued" in reason_l or "timed out" in reason_l or "timeout" in reason_l:
        return "queued/timed out"
    if "rate" in reason_l or "429" in reason_l:
        return "rate-limited"
    return reason


def _format_nvidia_dashboard_cli(entry: dict[str, Any]) -> list[str]:
    detail = entry.get("detail") if isinstance(entry, dict) else None
    models = detail.get("models") if isinstance(detail, dict) else None
    ready_auto = detail.get("ready_auto") if isinstance(detail, dict) else None
    auto_total = detail.get("auto_total") if isinstance(detail, dict) else None
    icon = _STATE_ICON.get(entry["state"], "•")
    status = _STATUS_LABEL.get(entry["state"], entry["state"]).upper()
    if isinstance(ready_auto, int) and isinstance(auto_total, int):
        unavailable = len([m for m in (models or []) if isinstance(m, dict) and m.get("state") != "ready"])
        summary = f"{ready_auto}/{auto_total} auto ready"
        if unavailable:
            summary += f", {unavailable} issues"
    else:
        summary = entry.get("reason", "status unknown")
    lines = [f"  nvidia  {icon} {status:<6} {summary}"]
    if not isinstance(models, list):
        return lines

    order = {alias: idx for idx, alias in enumerate(_NVIDIA_MONITORED_ALIASES)}

    def sort_key(model: dict[str, Any]) -> tuple[int, str]:
        alias = str(model.get("alias") or model.get("id") or "")
        return (order.get(alias, 999), alias)

    for model in sorted((m for m in models if isinstance(m, dict)), key=sort_key):
        alias = str(model.get("alias") or model.get("id") or "unknown")
        state = str(model.get("state") or "unknown")
        model_icon = _STATE_ICON.get(state, "•")
        test_duration = _format_test_duration(model.get("test_seconds")) or "-"
        slow = " slow" if model.get("slow") else ""
        reason = _short_model_reason(str(model.get("reason") or "").strip())
        suffix = f" {reason}" if reason and state != "ready" else ""
        line = f"    {alias:<14} {model_icon} {state:<8} {test_duration:<7}".rstrip()
        lines.append(f"{line}{slow}{suffix}")
    return lines


def format_gauge_cli(gauge: dict[str, Any] | None = None) -> str:
    gauge = gauge or engine_gas_gauge()
    engines = gauge.get("engines", {})
    lines = ["Engine gas gauge"]

    lines.append("\nCloud caps:")
    for name in ("claude", "codex"):
        entry = engines.get(name)
        if entry:
            lines.append(_format_cloud_cli(name, entry))

    local = engines.get("local-agent")
    if local:
        icon = _STATE_ICON.get(local["state"], "•")
        status = _STATUS_LABEL.get(local["state"], local["state"]).upper()
        test = _format_entry_test_cli(local).replace(" test ", "test ", 1).strip()
        test = f" | {test}" if test else ""
        lines.append("\nLocal:")
        lines.append(f"  local   {icon} {status:<6} {_local_summary(local['reason'])}{test}")

    nvidia = engines.get("nvidia")
    if nvidia:
        lines.append("\nNVIDIA models:")
        lines.extend(_format_nvidia_dashboard_cli(nvidia))
    lines.append(f"\nchecked_at: {gauge.get('checked_at')}")
    return "\n".join(lines)


def main() -> None:
    import sys

    if "--json" in sys.argv[1:]:
        print(json.dumps(engine_gas_gauge(), indent=2, sort_keys=True))
    else:
        print(format_gauge_cli())


if __name__ == "__main__":
    main()
