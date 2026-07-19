"""Bridge-owned worker lifecycle with async push-to-Telegram on completion.

The bug this fixes: the Overlord used to spawn workers via
``Bash(run_in_background=true)`` and say "I'll ping you" — but the bridge is
request->response and had no way to message Telegram between turns, so a
backgrounded worker's result went nowhere.

Design (brain-agnostic, works whether the Overlord brain is Claude or Codex, and
whether the *worker* is Claude, Codex, or a local agent):

  * The Overlord dispatches a worker by writing one small JSON file into
    ``dispatch/`` (a plain shell command, so either brain can do it).
  * The bridge watches that folder, launches the worker as an async subprocess
    (``claude -p``, ``codex exec``, or a local model tool loop) in the named
    project folder, and
  * when the worker finishes, **pushes its result to the owner on its own** — a
    true async outbound, no turn required.

Request JSON: {"name", "folder", "task",
               "brain": "claude"|"codex"|"local-agent"|"nvidia",
               "session": "<uuid for claude>", "resume": true|false,
               "approval_policy": "never", "sandbox": "workspace-write",
               "files": ["relative/path.py"]}

``approval_policy`` / ``sandbox`` are optional Codex worker overrides. They let
trusted per-project workers run without repeated prompts while keeping that
choice explicit in the dispatch file.

``local-agent`` is the local-model agent loop: it gives the selected model tools
for shell commands plus file list/read/write, executes them under the worker
folder, feeds results back to the model, and reports the final transcript.
``files`` is optional and only a starting hint; local agents can inspect the
project themselves through tools.

``nvidia`` is the same agent loop pointed at NVIDIA's free NIM cloud endpoint
(integrate.api.nvidia.com): big frontier-adjacent models (gpt-oss-120b,
Mistral Small, Nemotron, Qwen3-Next/3.5, DeepSeek manual-only, ...) with huge
contexts and zero local VRAM cost. Select with {"brain":"nvidia","model":"gpt-oss"}
etc.; aliases live in _NVIDIA_MODEL_ALIASES.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import threading
import uuid

from modules import shared_job
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("overlord.workers")

# Worker dispatch is confined to the same zones as the Overlord's allowlist.
_ALLOWED_ROOTS = (Path.home() / "Projects", Path.home() / "Documents")
DEFAULT_WORKER_TIMEOUT = 60 * 30  # 30 min ceiling per worker run

# codex prints "session id: <uuid>" in its output; grab it for the roster.
_CODEX_SID_RE = re.compile(r"session id:\s*([0-9a-fA-F][0-9a-fA-F-]{6,})")
_CODEX_APPROVAL_POLICIES = {"untrusted", "on-failure", "on-request", "never"}
_CODEX_SANDBOXES = {"read-only", "workspace-write", "danger-full-access"}
_THREAD_REPORT_STATUSES = {
    "finished",
    "failed",
    "timed_out",
    "start_failed",
    "crashed",
    "interrupted",
}
_SIGNAL_NUMBERS = {int(s) for s in signal.Signals}


def classify_worker_exit(returncode: int | None, shutting_down: bool) -> dict[str, Any]:
    """Classify how a worker process ended.

    A negative returncode means the OS killed the process with a signal, which is
    NOT the same as the task failing. The case that bit us: a worker that restarts
    overlord-bridge.service is itself a child of that service's cgroup, so systemd
    SIGTERMs it as part of the restart it asked for — *after* its work is already
    committed. Reporting that as a failure cries wolf.
    """
    if returncode == 0:
        return {"status": "finished", "failed": False}
    if returncode is not None and returncode < 0:
        signum = -returncode
        signame = signal.Signals(signum).name if signum in _SIGNAL_NUMBERS else f"signal {signum}"
        return {
            "status": "interrupted",
            "failed": False,
            "signal": signame,
            "cause": (
                "the bridge was restarting (likely the worker's own doing)"
                if shutting_down
                else "it was killed by an external signal"
            ),
        }
    return {"status": "failed", "failed": True}
_LOCAL_AGENT_BRAINS = {"local-agent"}
_NVIDIA_BRAINS = {"nvidia", "nim", "nvidia-nim"}
QWEN3_CODER_NEXT_MODEL = "openai/Qwen3-Coder-Next"
_AIDER_MODEL_ALIASES = {
    "qwen3-coder-next": QWEN3_CODER_NEXT_MODEL,
    "qwen3-coder-next-llamacpp": QWEN3_CODER_NEXT_MODEL,
    "qwen3-coder-next-tq1": QWEN3_CODER_NEXT_MODEL,
    "qwen2.5-coder": "ollama_chat/qwen2.5-coder:14b",
    "qwen2.5-coder-14b": "ollama_chat/qwen2.5-coder:14b",
    "qwen25-coder": "ollama_chat/qwen2.5-coder:14b",
    "qwen25-coder-14b": "ollama_chat/qwen2.5-coder:14b",
}
DEFAULT_AIDER_MODEL = QWEN3_CODER_NEXT_MODEL
DEFAULT_AIDER_API_BASE = "http://127.0.0.1:1234/v1"
DEFAULT_OLLAMA_OPENAI_API_BASE = "http://127.0.0.1:11434/v1"
DEFAULT_AIDER_CONTEXT_TOKENS = 256000
DEFAULT_AIDER_MIN_CONTEXT_TOKENS = 2048
DEFAULT_AIDER_CHAT_HISTORY_TOKENS = 2048
DEFAULT_AIDER_KEEP_ALIVE = "0"
DEFAULT_AIDER_OPENAI_API_KEY = "overlord-local"
DEFAULT_LOCAL_AGENT_MAX_STEPS = 24
DEFAULT_LOCAL_AGENT_COMMAND_TIMEOUT = 180
DEFAULT_LOCAL_AGENT_SLOT_CHECK_INTERVAL = 5
DEFAULT_LOCAL_AGENT_IDLE_STALL_GRACE = 20
DEFAULT_LOCAL_AGENT_TOOL_OUTPUT_CHARS = 24000
_ANSWER_ONLY_CREATIVE_TERMS = (
    "limerick",
    "poem",
    "haiku",
    "joke",
)
_ANSWER_ONLY_EXPLICIT_MARKERS = (
    "answer-only",
    "answer only",
    "do not inspect files",
    "do not run commands",
)
_PROJECT_WORK_TERMS = (
    "code",
    "commit",
    "debug",
    "edit",
    "file",
    "fix",
    "implement",
    "project",
    "repo",
    "script",
    "test",
)
DEFAULT_AIDER_LLAMACPP_BIN = Path("/tank/ai/llama.cpp/build/bin/llama-server")
DEFAULT_AIDER_LLAMACPP_MODEL = Path("/tank/ai/models/Qwen3-Coder-Next-UD-TQ1_0.gguf")
DEFAULT_AIDER_LLAMACPP_HF_REPO = "unsloth/Qwen3-Coder-Next-GGUF:UD-TQ1_0"
DEFAULT_AIDER_LLAMACPP_HOST = "127.0.0.1"
DEFAULT_AIDER_LLAMACPP_PORT = 1234
DEFAULT_AIDER_LLAMACPP_THREADS = 16
DEFAULT_AIDER_LLAMACPP_BATCH = 1024
DEFAULT_AIDER_LLAMACPP_N_CPU_MOE = 36
DEFAULT_AIDER_LLAMACPP_CACHE_TYPE_K = "q8_0"
DEFAULT_AIDER_LLAMACPP_CACHE_TYPE_V = "q8_0"
_AIDER_MODEL_CONTEXT_HARD_CAPS = {
    "openai/qwen3-coder-next": 256000,
    "ollama_chat/qwen2.5-coder:14b": 8192,
}


def _is_answer_only_worker_task(task: str, files: list[str] | None) -> bool:
    """True for smoke/creative prompts where tools would only add noise."""
    if files:
        return False
    text = str(task or "").strip().lower()
    if not text:
        return False
    if any(marker in text for marker in _ANSWER_ONLY_EXPLICIT_MARKERS):
        return True
    if not any(term in text for term in _ANSWER_ONLY_CREATIVE_TERMS):
        return False
    if re.search(r"\b[\w.-]+\.(?:css|html|js|json|md|py|rs|sh|ts|tsx|txt|yaml|yml)\b", text):
        return False
    return not any(re.search(rf"\b{re.escape(term)}\b", text) for term in _PROJECT_WORK_TERMS)


# NVIDIA NIM cloud workers (brain=nvidia): the same OpenAI-compatible agent
# loop as local-agent, pointed at integrate.api.nvidia.com. Free tier, huge
# contexts, no local VRAM cost. Aliases map friendly names to live NIM ids
# (verified against /v1/models 2026-07-02).
DEFAULT_NVIDIA_API_BASE = "https://integrate.api.nvidia.com/v1"
DEFAULT_NVIDIA_MODEL = "openai/gpt-oss-120b"
DEFAULT_NVIDIA_MAX_TOKENS = 16384
DEFAULT_NVIDIA_MAX_STEPS = 40
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
# Per-model request tweaks copied from each model's NVIDIA Build prototype box
# (for example https://build.nvidia.com/qwen/qwen3-next-80b-a3b-instruct). These
# are merged into the worker payload after defaults so the published sampling and
# template knobs win.
_NVIDIA_MODEL_PAYLOAD_OVERRIDES: dict[str, dict[str, Any]] = {
    "deepseek-ai/deepseek-v4-pro": {
        "temperature": 1.0,
        "top_p": 0.95,
        "max_tokens": 16384,
        "chat_template_kwargs": {"thinking": False},
    },
    "deepseek-ai/deepseek-v4-flash": {
        "temperature": 1.0,
        "top_p": 0.95,
        "max_tokens": 16384,
        "chat_template_kwargs": {"thinking": True, "reasoning_effort": "high"},
    },
    "moonshotai/kimi-k2.6": {
        "temperature": 1.0,
        "top_p": 1,
        "max_tokens": 16384,
        "seed": 0,
    },
    "qwen/qwen3-next-80b-a3b-instruct": {
        "temperature": 0.6,
        "top_p": 0.7,
        "max_tokens": 4096,
    },
    "qwen/qwen3.5-122b-a10b": {
        "temperature": 0.6,
        "top_p": 0.95,
        "max_tokens": 16384,
    },
    "qwen/qwen3.5-397b-a17b": {
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "presence_penalty": 0,
        "repetition_penalty": 1,
        "max_tokens": 16384,
    },
    "openai/gpt-oss-120b": {"temperature": 1.0, "top_p": 1, "max_tokens": 4096},
    "openai/gpt-oss-20b": {"temperature": 1.0, "top_p": 1, "max_tokens": 4096},
    "meta/llama-3.3-70b-instruct": {"temperature": 0.2, "top_p": 0.7, "max_tokens": 1024},
    "meta/llama-3.1-8b-instruct": {"temperature": 0.2, "top_p": 0.7, "max_tokens": 1024},
    "meta/llama-4-maverick-17b-128e-instruct": {
        "temperature": 1.0,
        "top_p": 1,
        "max_tokens": 512,
        "frequency_penalty": 0,
        "presence_penalty": 0,
    },
    "nvidia/nemotron-3-super-120b-a12b": {
        "temperature": 1.0,
        "top_p": 0.95,
        "max_tokens": 16384,
        "chat_template_kwargs": {"enable_thinking": True},
        "reasoning_budget": 16384,
    },
    "nvidia/nemotron-3-ultra-550b-a55b": {
        "temperature": 1.0,
        "top_p": 0.95,
        "max_tokens": 16384,
        "chat_template_kwargs": {"enable_thinking": True},
        "reasoning_budget": 16384,
    },
    "nvidia/nemotron-3-nano-30b-a3b": {
        "temperature": 1.0,
        "top_p": 1,
        "max_tokens": 16384,
        "reasoning_budget": 16384,
    },
    "nvidia/llama-3.1-nemotron-nano-vl-8b-v1": {
        "temperature": 1.0,
        "top_p": 0.01,
        "max_tokens": 1024,
        "seed": 50,
    },
    "mistralai/mistral-small-4-119b-2603": {
        "temperature": 0.1,
        "top_p": 1,
        "max_tokens": 16384,
        "reasoning_effort": "high",
    },
    "mistralai/mistral-large-3-675b-instruct-2512": {
        "temperature": 0.15,
        "top_p": 1,
        "max_tokens": 2048,
        "frequency_penalty": 0,
        "presence_penalty": 0,
    },
    "minimaxai/minimax-m2.7": {"temperature": 1.0, "top_p": 0.95, "max_tokens": 8192},
    "minimaxai/minimax-m3": {"temperature": 1.0, "top_p": 0.95, "max_tokens": 8192},
}
_SECRET_PATTERNS = (
    (
        re.compile(
            r"(?i)\b(token|api[_-]?key|password|passwd|secret|authorization)\b"
            r"\s*[:=]\s*([^\s]+)"
        ),
        r"\1=<redacted>",
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
)
_LOCAL_AGENT_DENY_FRAGMENTS = (
    "/.ssh",
    "/.gnupg",
    "/.1password",
    "/.netrc",
    "/.smbcredentials",
    "/.aws",
    "/.mozilla",
    "/.config/google-chrome",
    "id_rsa",
    "id_ed25519",
)
_LOCAL_AGENT_SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "node_modules_bak",
    ".aider",
}
_LOCAL_AGENT_SKIP_PATH_PARTS = {
    ("data", "processed"),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sanitize_text(text: str) -> str:
    cleaned = text.replace("\x00", "")
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def _bounded_tail(text: str, max_chars: int) -> tuple[str, bool]:
    cleaned = _sanitize_text(text).strip()
    if not cleaned:
        return "", False
    if max_chars <= 0:
        return "", True
    if len(cleaned) <= max_chars:
        return cleaned, False
    omitted = len(cleaned) - max_chars
    return f"[... {omitted} chars omitted ...]\n{cleaned[-max_chars:]}", True


def _safe_report_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    return (safe or "worker")[:80]


def _resolve_aider_model(model: str | None, default: str) -> str:
    raw = (model or default or DEFAULT_AIDER_MODEL).strip()
    return _AIDER_MODEL_ALIASES.get(raw.lower(), raw)


def _resolve_nvidia_model(model: str | None, default: str) -> str:
    raw = (model or default or DEFAULT_NVIDIA_MODEL).strip()
    return _NVIDIA_MODEL_ALIASES.get(raw.lower(), raw)


def _aider_context_hard_cap(model: str) -> int | None:
    return _AIDER_MODEL_CONTEXT_HARD_CAPS.get(model.lower())


def _aider_context_tokens_for_model(tokens: int, model: str) -> tuple[int, int | None]:
    hard_cap = _aider_context_hard_cap(model)
    context_tokens = max(2048, tokens)
    if hard_cap is not None:
        context_tokens = min(context_tokens, hard_cap)
    return context_tokens, hard_cap


def _is_ollama_aider_model(model: str) -> bool:
    lowered = model.lower()
    return lowered.startswith("ollama/") or lowered.startswith("ollama_chat/")


def _is_llamacpp_aider_model(model: str) -> bool:
    return model.lower() == QWEN3_CODER_NEXT_MODEL.lower()


def _openai_models_url(api_base: str) -> str:
    return api_base.rstrip("/") + "/models"


def _openai_chat_url(api_base: str) -> str:
    return api_base.rstrip("/") + "/chat/completions"


def _llamacpp_slots_url(api_base: str) -> str:
    base = api_base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base + "/slots"


async def _http_ok(url: str, timeout: float = 2.0) -> bool:
    def probe() -> bool:
        try:
            req = urllib.request.Request(url, headers={"Authorization": "Bearer overlord-local"})
            with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - local probe
                return 200 <= response.status < 500
        except (OSError, urllib.error.URLError, TimeoutError):
            return False

    return await asyncio.to_thread(probe)


async def _http_post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    api_key: str,
) -> dict[str, Any]:
    def post() -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Connection": "close",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - local API
            raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw)

    return await asyncio.to_thread(post)


async def _http_get_json(
    url: str,
    *,
    timeout: float,
    api_key: str,
) -> Any:
    def get() -> Any:
        headers = {"Accept": "application/json", "Connection": "close"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - local API
            raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw)

    return await asyncio.to_thread(get)


def _slot_int(slot: dict[str, Any], key: str) -> int:
    try:
        return int(slot.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _compact_llamacpp_slots(slots: list[Any]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for raw in slots:
        if not isinstance(raw, dict):
            continue
        next_token = raw.get("next_token")
        has_next_token = None
        if isinstance(next_token, list) and next_token:
            first = next_token[0]
            if isinstance(first, dict):
                has_next_token = first.get("has_next_token")
        compact.append(
            {
                "id": raw.get("id"),
                "id_task": raw.get("id_task"),
                "is_processing": bool(raw.get("is_processing")),
                "n_prompt_tokens": raw.get("n_prompt_tokens"),
                "n_prompt_tokens_processed": raw.get("n_prompt_tokens_processed"),
                "n_decoded": raw.get("n_decoded"),
                "n_remain": raw.get("n_remain"),
                "has_next_token": has_next_token,
            }
        )
    return compact


def _llamacpp_slots_indicate_idle_stall(slots: list[Any]) -> bool:
    dict_slots = [slot for slot in slots if isinstance(slot, dict)]
    if not dict_slots or any(bool(slot.get("is_processing")) for slot in dict_slots):
        return False
    return any(
        slot.get("id_task") is not None
        and (
            _slot_int(slot, "n_decoded") > 0
            or _slot_int(slot, "n_prompt_tokens_processed") > 0
        )
        for slot in dict_slots
    )


async def _http_post_json_with_llamacpp_stall_guard(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    api_key: str,
    slots_url: str,
) -> dict[str, Any]:
    post_task = asyncio.create_task(
        _http_post_json_retry(url, payload, timeout=timeout, api_key=api_key)
    )
    idle_since: float | None = None
    last_slots: list[dict[str, Any]] | None = None
    loop = asyncio.get_running_loop()

    while True:
        done, _ = await asyncio.wait(
            {post_task},
            timeout=DEFAULT_LOCAL_AGENT_SLOT_CHECK_INTERVAL,
        )
        if post_task in done:
            return post_task.result()

        try:
            raw_slots = await _http_get_json(slots_url, timeout=2, api_key=api_key)
        except Exception:  # noqa: BLE001
            idle_since = None
            last_slots = None
            continue

        slots = raw_slots if isinstance(raw_slots, list) else []
        if _llamacpp_slots_indicate_idle_stall(slots):
            last_slots = _compact_llamacpp_slots(slots)
            if idle_since is None:
                idle_since = loop.time()
                continue
            if loop.time() - idle_since >= DEFAULT_LOCAL_AGENT_IDLE_STALL_GRACE:
                post_task.cancel()
                detail = json.dumps(last_slots, separators=(",", ":"))[:1200]
                raise RuntimeError(
                    "local llama.cpp stopped processing the request but the HTTP "
                    f"response never completed; slot diagnostics={detail}"
                )
        else:
            idle_since = None
            last_slots = None


async def _http_post_json_retry(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    api_key: str,
    attempts: int = 3,
) -> dict[str, Any]:
    """POST with retry on 429/5xx — NIM free tier rate-limits per model."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await _http_post_json(url, payload, timeout=timeout, api_key=api_key)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == attempts:
                raise
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                delay = min(120.0, max(1.0, float(retry_after)))
            except (TypeError, ValueError):
                delay = min(60.0, 5.0 * attempt)
            log.warning("NIM HTTP %s; retrying in %.0fs (attempt %d/%d)", exc.code, delay, attempt, attempts)
            last_exc = exc
            await asyncio.sleep(delay)
        except TimeoutError as exc:
            if attempt == attempts:
                raise
            log.warning("NIM request timed out; retrying (attempt %d/%d)", attempt, attempts)
            last_exc = exc
    raise last_exc if last_exc else RuntimeError("unreachable")


def _under_allowed_root(folder: str) -> Path | None:
    try:
        p = Path(folder).expanduser().resolve()
    except Exception:
        return None
    for root in _ALLOWED_ROOTS:
        if p == root or root in p.parents:
            return p
    return None


def _worker_file_args(folder: Path, raw_files: Any) -> tuple[list[str], str | None]:
    if raw_files in (None, ""):
        return [], None
    if isinstance(raw_files, str):
        files = [raw_files]
    elif isinstance(raw_files, list):
        files = raw_files
    else:
        return [], "files_must_be_string_or_list"

    root = folder.resolve()
    result: list[str] = []
    seen: set[str] = set()
    for raw in files:
        name = str(raw or "").strip()
        if not name:
            continue
        path = Path(name).expanduser()
        candidate = path.resolve() if path.is_absolute() else (root / path).resolve()
        try:
            rel = candidate.relative_to(root)
        except ValueError:
            return [], f"file_outside_worker_folder:{name}"
        if not candidate.exists():
            return [], f"file_not_found:{name}"
        if not candidate.is_file():
            return [], f"file_not_regular:{name}"
        rel_s = rel.as_posix()
        if rel_s not in seen:
            result.append(rel_s)
            seen.add(rel_s)
    return result, None


def _local_agent_model_name(model: str) -> str:
    lowered = model.lower()
    if lowered.startswith("openai/"):
        return model.split("/", 1)[1]
    if lowered.startswith("ollama_chat/"):
        return model.split("/", 1)[1]
    if lowered.startswith("ollama/"):
        return model.split("/", 1)[1]
    return model


def _resolve_worker_relative_path(folder: Path, raw_path: str) -> tuple[Path | None, str | None]:
    name = str(raw_path or "").strip()
    if not name:
        return None, "empty_path"
    root = folder.resolve()
    candidate = Path(name).expanduser()
    candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None, f"path_outside_worker_folder:{name}"
    return candidate, None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    candidates = [stripped]
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    candidates.extend(fenced)
    if "{" in stripped and "}" in stripped:
        candidates.append(stripped[stripped.find("{") : stripped.rfind("}") + 1])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _extract_xmlish_tool_call(text: str) -> dict[str, Any] | None:
    match = re.search(
        r"<tool_call>\s*<function=([A-Za-z0-9_:-]+)>\s*(.*?)\s*</function>\s*</tool_call>",
        text,
        flags=re.DOTALL,
    )
    if not match:
        return None
    tool = match.group(1)
    body = match.group(2)
    args: dict[str, Any] = {}
    for key, value in re.findall(
        r"<([A-Za-z0-9_:-]+)>\s*(.*?)\s*</\1>",
        body,
        flags=re.DOTALL,
    ):
        args[key] = value.strip()
    return {"tool": tool, "arguments": args}


def _local_agent_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": "Run a shell command in the worker project folder.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout_seconds": {"type": "integer"},
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a UTF-8-ish text file under the worker project folder.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "max_chars": {"type": "integer"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write a text file under the worker project folder.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List project files, excluding bulky generated folders.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "max_files": {"type": "integer"},
                    },
                },
            },
        },
    ]


def _local_agent_command_denial(command: str, folder: Path) -> str | None:
    lowered = command.lower()
    for fragment in _LOCAL_AGENT_DENY_FRAGMENTS:
        if fragment.lower() in lowered:
            return f"command_references_offlimits_fragment:{fragment}"
    denied_patterns = (
        r"(^|[;&|]\s*)sudo\b",
        r"(^|[;&|]\s*)su\b",
        r"\brm\s+(-[^\s]*[rf][^\s]*|-r|-f)\s+/",
        r"(^|[;&|]\s*)cd\s+/(?:\s|$|[;&|])",
        r"(^|[;&|]\s*)find\s+/(?:\s|$)",
        r"(^|[;&|]\s*)grep\b.*\s+/(?:\s|$)",
        r"(^|[;&|]\s*)rg\b.*\s+/(?:\s|$)",
        r"\bmkfs\b",
        r"\bdd\s+.*\bof=/dev/",
        r"\bshutdown\b",
        r"\breboot\b",
    )
    for pattern in denied_patterns:
        if re.search(pattern, lowered):
            return f"command_denied_by_safety_pattern:{pattern}"
    if re.search(r"(?<![\w.-])\.env(?!\.example)(?:\.[\w-]+)?(?![\w.-])", command):
        return "command_references_secret_env_file"
    root = str(folder.resolve())
    home_prefix = str(Path.home()) + "/"
    if home_prefix in command and root not in command:
        return "command_references_home_path_outside_worker_folder"
    return None


def _local_agent_path_denial(path: Path, folder: Path, *, write: bool) -> str | None:
    lowered = str(path).lower()
    for fragment in _LOCAL_AGENT_DENY_FRAGMENTS:
        if fragment.lower() in lowered:
            return f"path_references_offlimits_fragment:{fragment}"
    try:
        rel = path.resolve().relative_to(folder.resolve())
    except ValueError:
        return "path_outside_worker_folder"
    parts = rel.parts
    if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
        return "path_references_secret_env_file"
    if any(part in _LOCAL_AGENT_SKIP_DIRS for part in parts):
        return "path_in_ignored_directory"
    for banned in _LOCAL_AGENT_SKIP_PATH_PARTS:
        if len(parts) >= len(banned) and tuple(parts[: len(banned)]) == banned:
            return "path_in_generated_data"
    if write and (path.name in {"tags", "queryLog.log"} or path.suffix in {".faiss", ".pkl", ".db"}):
        return "write_to_generated_artifact_denied"
    return None


def _local_agent_json_result(result: dict[str, Any], limit: int) -> str:
    text = json.dumps(result, ensure_ascii=False, sort_keys=True)
    bounded, truncated = _bounded_tail(text, limit)
    if truncated:
        return json.dumps(
            {
                "ok": result.get("ok", False),
                "truncated": True,
                "tail": bounded,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    return bounded


class WorkerReportStore:
    """Durable worker audit trail plus latest per-worker snapshot."""

    def __init__(
        self,
        root: Path,
        *,
        max_tail_chars: int,
        max_events_bytes: int,
    ) -> None:
        self.root = Path(root)
        self.latest_dir = self.root / "latest"
        self.events_path = self.root / "events.jsonl"
        self.max_tail_chars = max_tail_chars
        self.max_events_bytes = max_events_bytes
        self.latest_dir.mkdir(parents=True, exist_ok=True)

    def record(self, report: dict[str, Any]) -> None:
        event = dict(report)
        event.setdefault("recorded_at", _now_iso())
        if isinstance(event.get("result_tail"), str):
            event["result_tail"], event["result_truncated"] = _bounded_tail(
                event["result_tail"], self.max_tail_chars
            )
        self._rotate_if_needed()
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        latest_path = self.latest_dir / f"{_safe_report_name(str(event.get('name') or 'worker'))}.json"
        latest_path.write_text(line + "\n", encoding="utf-8")

    def _rotate_if_needed(self) -> None:
        if self.max_events_bytes <= 0:
            return
        try:
            if self.events_path.stat().st_size <= self.max_events_bytes:
                return
        except FileNotFoundError:
            return
        rotated = self.root / "events.1.jsonl"
        try:
            rotated.unlink(missing_ok=True)
            self.events_path.replace(rotated)
        except OSError as exc:
            log.warning("Could not rotate worker report log: %s", exc)


class WorkerManager:
    """Watches a dispatch dir, runs workers, pushes their results to the owner."""

    def __init__(
        self,
        *,
        send: Callable[[int, str], None],
        owner: int,
        dispatch_dir: Path,
        claude_bin: str = "claude",
        codex_bin: str = "codex",
        claude_model: str = "sonnet",
        codex_model: str | None = None,
        aider_model: str = DEFAULT_AIDER_MODEL,
        aider_api_base: str = DEFAULT_AIDER_API_BASE,
        local_agent_ollama_api_base: str = DEFAULT_OLLAMA_OPENAI_API_BASE,
        aider_map_tokens: int = 0,
        aider_context_tokens: int = DEFAULT_AIDER_CONTEXT_TOKENS,
        aider_min_context_tokens: int = DEFAULT_AIDER_MIN_CONTEXT_TOKENS,
        aider_chat_history_tokens: int = DEFAULT_AIDER_CHAT_HISTORY_TOKENS,
        aider_keep_alive: str = DEFAULT_AIDER_KEEP_ALIVE,
        aider_openai_api_key: str = DEFAULT_AIDER_OPENAI_API_KEY,
        aider_llamacpp_bin: Path | str = DEFAULT_AIDER_LLAMACPP_BIN,
        aider_llamacpp_model: Path | str = DEFAULT_AIDER_LLAMACPP_MODEL,
        aider_llamacpp_hf_repo: str = DEFAULT_AIDER_LLAMACPP_HF_REPO,
        aider_llamacpp_host: str = DEFAULT_AIDER_LLAMACPP_HOST,
        aider_llamacpp_port: int = DEFAULT_AIDER_LLAMACPP_PORT,
        aider_llamacpp_threads: int = DEFAULT_AIDER_LLAMACPP_THREADS,
        aider_llamacpp_batch: int = DEFAULT_AIDER_LLAMACPP_BATCH,
        aider_llamacpp_n_cpu_moe: int = DEFAULT_AIDER_LLAMACPP_N_CPU_MOE,
        aider_llamacpp_cache_type_k: str = DEFAULT_AIDER_LLAMACPP_CACHE_TYPE_K,
        aider_llamacpp_cache_type_v: str = DEFAULT_AIDER_LLAMACPP_CACHE_TYPE_V,
        nvidia_model: str = DEFAULT_NVIDIA_MODEL,
        nvidia_api_base: str = DEFAULT_NVIDIA_API_BASE,
        nvidia_api_key: str = "",
        nvidia_max_tokens: int = DEFAULT_NVIDIA_MAX_TOKENS,
        nvidia_max_steps: int = DEFAULT_NVIDIA_MAX_STEPS,
        timeout: int = DEFAULT_WORKER_TIMEOUT,
        report_dir: Path | None = None,
        report_tail_chars: int = 4000,
        telegram_tail_chars: int = 12000,
        report_events_max_bytes: int = 1024 * 1024,
        on_report: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        is_shutting_down: Callable[[], bool] | None = None,
    ) -> None:
        # send(chat_id, text): thread-safe Telegram send (TelegramHandler.send)
        self._send = send
        self.owner = owner
        # Lets the manager tell "the bridge is going down and took its workers
        # with it" apart from "the worker's task actually failed".
        self._is_shutting_down = is_shutting_down or (lambda: False)
        self.dispatch_dir = Path(dispatch_dir)
        self.dispatch_dir.mkdir(parents=True, exist_ok=True)
        self.claude_bin = claude_bin
        self.codex_bin = codex_bin
        # Workers run a LIGHTER model than the Overlord — they do focused
        # code/task work, not orchestration. Overridable per dispatch ("model").
        self.claude_model = claude_model
        self.codex_model = codex_model
        self.aider_model = aider_model
        self.aider_api_base = aider_api_base
        self.local_agent_ollama_api_base = (
            local_agent_ollama_api_base or DEFAULT_OLLAMA_OPENAI_API_BASE
        )
        self.aider_map_tokens = max(0, aider_map_tokens)
        self.aider_context_tokens = max(2048, aider_context_tokens)
        self.aider_min_context_tokens = max(1024, aider_min_context_tokens)
        self.aider_chat_history_tokens = max(1024, aider_chat_history_tokens)
        self.aider_keep_alive = str(aider_keep_alive or DEFAULT_AIDER_KEEP_ALIVE)
        self.aider_openai_api_key = aider_openai_api_key or DEFAULT_AIDER_OPENAI_API_KEY
        self.aider_llamacpp_bin = Path(aider_llamacpp_bin)
        self.aider_llamacpp_model = Path(aider_llamacpp_model)
        self.aider_llamacpp_hf_repo = (
            aider_llamacpp_hf_repo or DEFAULT_AIDER_LLAMACPP_HF_REPO
        )
        self.aider_llamacpp_host = aider_llamacpp_host or DEFAULT_AIDER_LLAMACPP_HOST
        self.aider_llamacpp_port = max(1, int(aider_llamacpp_port))
        self.aider_llamacpp_threads = max(1, int(aider_llamacpp_threads))
        self.aider_llamacpp_batch = max(128, int(aider_llamacpp_batch))
        self.aider_llamacpp_n_cpu_moe = max(0, int(aider_llamacpp_n_cpu_moe))
        self.aider_llamacpp_cache_type_k = (
            aider_llamacpp_cache_type_k or DEFAULT_AIDER_LLAMACPP_CACHE_TYPE_K
        )
        self.aider_llamacpp_cache_type_v = (
            aider_llamacpp_cache_type_v or DEFAULT_AIDER_LLAMACPP_CACHE_TYPE_V
        )
        self._llamacpp_lock = asyncio.Lock()
        self.nvidia_model = nvidia_model or DEFAULT_NVIDIA_MODEL
        self.nvidia_api_base = nvidia_api_base or DEFAULT_NVIDIA_API_BASE
        self.nvidia_api_key = (nvidia_api_key or "").strip()
        self.nvidia_max_tokens = max(1024, int(nvidia_max_tokens))
        self.nvidia_max_steps = max(4, int(nvidia_max_steps))
        self.timeout = timeout
        self._running: dict[str, asyncio.Task] = {}
        self.report_tail_chars = report_tail_chars
        self.telegram_tail_chars = telegram_tail_chars
        self.on_report = on_report
        self.report_store = (
            WorkerReportStore(
                report_dir,
                max_tail_chars=report_tail_chars,
                max_events_bytes=report_events_max_bytes,
            )
            if report_dir is not None
            else None
        )

    async def _start_job_llamacpp_server(
        self,
        context_tokens: int,
    ) -> asyncio.subprocess.Process | None:
        if await _http_ok(_openai_models_url(self.aider_api_base)):
            return None
        async with self._llamacpp_lock:
            if await _http_ok(_openai_models_url(self.aider_api_base)):
                return None
            if not self.aider_llamacpp_bin.exists():
                raise FileNotFoundError(
                    f"llama-server not found at {self.aider_llamacpp_bin}; "
                    "build llama.cpp or set OVERLORD_WORKER_LOCAL_AGENT_LLAMACPP_BIN"
                )
            argv = [
                str(self.aider_llamacpp_bin),
                "--host",
                self.aider_llamacpp_host,
                "--port",
                str(self.aider_llamacpp_port),
                "--alias",
                "Qwen3-Coder-Next",
                "-fa",
                "on",
                "-t",
                str(self.aider_llamacpp_threads),
                "--ctx-size",
                str(context_tokens),
                "--batch-size",
                str(self.aider_llamacpp_batch),
                "--ubatch-size",
                str(self.aider_llamacpp_batch),
                "-ctk",
                self.aider_llamacpp_cache_type_k,
                "-ctv",
                self.aider_llamacpp_cache_type_v,
                "--temp",
                "0.8",
                "--top-p",
                "0.95",
                "--min-p",
                "0.01",
                "--top-k",
                "40",
                "--seed",
                "3407",
                "--jinja",
                "--n-gpu-layers",
                "all",
                "--n-cpu-moe",
                str(self.aider_llamacpp_n_cpu_moe),
                "--cache-ram",
                "0",
                "--no-mmap",
                "--no-ui",
            ]
            if self.aider_llamacpp_model.exists():
                argv += ["-m", str(self.aider_llamacpp_model)]
            else:
                argv += ["-hf", self.aider_llamacpp_hf_repo]
            log.info("Starting local llama.cpp server for Qwen3-Coder-Next: %s", argv)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            deadline = asyncio.get_running_loop().time() + 240
            while asyncio.get_running_loop().time() < deadline:
                if proc.returncode is not None:
                    raise RuntimeError(
                        "local llama.cpp Qwen3-Coder-Next server exited during startup "
                        f"with code {proc.returncode}"
                    )
                if await _http_ok(_openai_models_url(self.aider_api_base)):
                    log.info("Local llama.cpp Qwen3-Coder-Next server is ready")
                    return proc
                await asyncio.sleep(1)
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            raise TimeoutError("local llama.cpp Qwen3-Coder-Next server did not become ready")

    async def _stop_job_llamacpp_server(
        self,
        proc: asyncio.subprocess.Process | None,
    ) -> None:
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=15)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    async def _local_agent_execute_tool(
        self,
        folder: Path,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        aliases = {
            "run": "run_command",
            "bash": "run_command",
            "shell": "run_command",
            "read": "read_file",
            "write": "write_file",
            "list": "list_files",
        }
        tool_name = aliases.get(tool_name, tool_name)

        if tool_name == "run_command":
            command = str(arguments.get("command") or arguments.get("cmd") or "").strip()
            if not command:
                return {"ok": False, "error": "missing_command"}
            denial = _local_agent_command_denial(command, folder)
            if denial:
                return {"ok": False, "error": denial, "command": command}
            timeout = arguments.get("timeout_seconds", DEFAULT_LOCAL_AGENT_COMMAND_TIMEOUT)
            try:
                timeout_i = max(1, min(int(timeout), DEFAULT_LOCAL_AGENT_COMMAND_TIMEOUT))
            except (TypeError, ValueError):
                timeout_i = DEFAULT_LOCAL_AGENT_COMMAND_TIMEOUT
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(folder),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_i)
                timed_out = False
            except asyncio.TimeoutError:
                proc.kill()
                out, err = await proc.communicate()
                timed_out = True
            stdout = (out or b"").decode(errors="replace")
            stderr = (err or b"").decode(errors="replace")
            stdout_tail, stdout_truncated = _bounded_tail(stdout, DEFAULT_LOCAL_AGENT_TOOL_OUTPUT_CHARS)
            stderr_tail, stderr_truncated = _bounded_tail(stderr, DEFAULT_LOCAL_AGENT_TOOL_OUTPUT_CHARS)
            return {
                "ok": proc.returncode == 0 and not timed_out,
                "tool": tool_name,
                "command": command,
                "exit_code": proc.returncode,
                "timed_out": timed_out,
                "stdout": stdout_tail,
                "stdout_truncated": stdout_truncated,
                "stderr": stderr_tail,
                "stderr_truncated": stderr_truncated,
            }

        if tool_name == "read_file":
            path, error = _resolve_worker_relative_path(folder, str(arguments.get("path") or ""))
            if error or path is None:
                return {"ok": False, "error": error or "bad_path"}
            denial = _local_agent_path_denial(path, folder, write=False)
            if denial:
                return {"ok": False, "error": denial, "path": str(path)}
            if not path.exists() or not path.is_file():
                return {"ok": False, "error": "file_not_found", "path": str(path)}
            max_chars = arguments.get("max_chars", DEFAULT_LOCAL_AGENT_TOOL_OUTPUT_CHARS)
            try:
                max_chars_i = max(1, min(int(max_chars), DEFAULT_LOCAL_AGENT_TOOL_OUTPUT_CHARS))
            except (TypeError, ValueError):
                max_chars_i = DEFAULT_LOCAL_AGENT_TOOL_OUTPUT_CHARS
            text = path.read_text(encoding="utf-8", errors="replace")
            bounded, truncated = _bounded_tail(text, max_chars_i)
            return {
                "ok": True,
                "tool": tool_name,
                "path": path.relative_to(folder).as_posix(),
                "content": bounded,
                "truncated": truncated,
            }

        if tool_name == "write_file":
            path, error = _resolve_worker_relative_path(folder, str(arguments.get("path") or ""))
            if error or path is None:
                return {"ok": False, "error": error or "bad_path"}
            denial = _local_agent_path_denial(path, folder, write=True)
            if denial:
                return {"ok": False, "error": denial, "path": str(path)}
            content = arguments.get("content")
            if not isinstance(content, str):
                return {"ok": False, "error": "content_must_be_string"}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {
                "ok": True,
                "tool": tool_name,
                "path": path.relative_to(folder).as_posix(),
                "bytes": len(content.encode("utf-8")),
            }

        if tool_name == "list_files":
            max_files = arguments.get("max_files", 300)
            try:
                max_files_i = max(1, min(int(max_files), 1000))
            except (TypeError, ValueError):
                max_files_i = 300
            files: list[str] = []
            for root, dirs, names in os.walk(folder):
                root_path = Path(root)
                rel_parts = root_path.relative_to(folder).parts if root_path != folder else ()
                dirs[:] = [
                    d
                    for d in dirs
                    if d not in _LOCAL_AGENT_SKIP_DIRS
                    and not any(tuple(rel_parts + (d,))[: len(skip)] == skip for skip in _LOCAL_AGENT_SKIP_PATH_PARTS)
                ]
                for name in sorted(names):
                    path = root_path / name
                    denial = _local_agent_path_denial(path, folder, write=False)
                    if denial:
                        continue
                    files.append(path.relative_to(folder).as_posix())
                    if len(files) >= max_files_i:
                        return {"ok": True, "tool": tool_name, "files": files, "truncated": True}
            return {"ok": True, "tool": tool_name, "files": files, "truncated": False}

        return {"ok": False, "error": f"unknown_tool:{tool_name}"}

    async def _run_local_agent_worker(
        self,
        *,
        name: str,
        folder: Path,
        task: str,
        session: str | None,
        resume: bool,
        model: str | None,
        files: list[str] | None,
        brain: str = "local-agent",
    ) -> None:
        sid = session or str(uuid.uuid4())
        started_at = _now_iso()
        started_mono = asyncio.get_running_loop().time()
        is_nvidia = brain == "nvidia"
        if is_nvidia:
            amodel = _resolve_nvidia_model(model, self.nvidia_model)
            context_tokens, hard_cap = None, None
            api_base = self.nvidia_api_base
            api_key = self.nvidia_api_key
            max_steps = self.nvidia_max_steps
            max_tokens = self.nvidia_max_tokens
        else:
            amodel = _resolve_aider_model(model, self.aider_model)
            context_tokens, hard_cap = _aider_context_tokens_for_model(
                self.aider_context_tokens, amodel
            )
            api_base = (
                self.local_agent_ollama_api_base
                if _is_ollama_aider_model(amodel)
                else self.aider_api_base
            )
            api_key = self.aider_openai_api_key
            max_steps = DEFAULT_LOCAL_AGENT_MAX_STEPS
            max_tokens = 4096
        llamacpp_proc: asyncio.subprocess.Process | None = None
        self._emit_report(
            self._base_report(
                name=name,
                folder=folder,
                brain=brain,
                session=sid,
                status="started",
                started_at=started_at,
                resume=resume,
                model=model,
                files=files or [],
                effective_local_agent_model=amodel,
                local_agent_api_base=api_base,
                local_agent_context_tokens=context_tokens,
                local_agent_context_hard_cap_tokens=hard_cap,
            )
        )

        preload_blocks: list[str] = []
        for hinted_path in files or []:
            path, error = _resolve_worker_relative_path(folder, hinted_path)
            if error or path is None:
                continue
            denial = _local_agent_path_denial(path, folder, write=False)
            if denial or not path.exists() or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            bounded, truncated = _bounded_tail(text, 4000)
            preload_blocks.append(
                f"--- FILE: {hinted_path} ---\n{bounded}"
                + ("\n[truncated]" if truncated else "")
            )
            if len(preload_blocks) >= 8:
                break

        answer_only = _is_answer_only_worker_task(task, files)
        if answer_only:
            system_prompt = (
                "You are an answer-only worker running inside a bridge harness. "
                "Return the requested answer directly. Do not inspect files, run "
                "commands, create files, or ask for tools. The backend runner is "
                "an implementation detail."
            )
        else:
            system_prompt = (
                "You are a local coding agent running inside a bridge harness. "
                "Use tools to inspect, edit, test, and commit the project. "
                "The backend runner is an implementation detail; behave like an agent. "
                "Operate only inside the worker project folder. "
                "Never scan / or the home directory broadly. "
                "Treat blocked commands or files as final; pivot immediately instead of retrying. "
                "If the task is answer-only, creative writing, or a smoke prompt that does not "
                "need project context, do not inspect files or run commands; return the final "
                "answer directly. "
                "Prefer the suggested starting files and any preloaded file contents before exploring further. "
                "Prefer targeted rg/sed/git/python/npm commands. "
                "When editing files, read them first and then write complete updated text. "
                "Do not touch generated/vendor artifacts unless the user explicitly asks. "
                "Before final, run relevant validation and git status. "
                "If you changed project files, make a local git commit. "
                "If formal tool calls are unavailable, output one JSON object exactly like "
                "{\"tool\":\"read_file\",\"arguments\":{\"path\":\"README.md\"}} or "
                "{\"final\":\"summary\"}."
            )

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": (
                    f"Worker folder: {folder}\n"
                    f"Suggested starting files: {', '.join(files or []) or '(none)'}\n\n"
                    f"Task:\n{task}"
                ),
            },
        ]
        if preload_blocks:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Here are the suggested starting files already loaded for you. "
                        "Use them before broad exploration.\n\n"
                        + "\n\n".join(preload_blocks)
                    ),
                }
            )
        transcript: list[str] = []
        denial_counts: dict[str, int] = {}
        tool_call_counts: dict[str, int] = {}

        def denial_followup(tool_name: str, arguments: dict[str, Any], result: dict[str, Any]) -> str | None:
            if result.get("ok", True):
                return None
            error = str(result.get("error") or "").strip()
            if not error:
                return None
            target = (
                str(arguments.get("path") or "")
                or str(arguments.get("command") or arguments.get("cmd") or "")
            )
            fingerprint = json.dumps(
                {
                    "tool": tool_name,
                    "target": target,
                    "error": error,
                },
                sort_keys=True,
            )
            denial_counts[fingerprint] = denial_counts.get(fingerprint, 0) + 1
            repeated = denial_counts[fingerprint]
            if error in {"path_references_secret_env_file", "command_references_secret_env_file"}:
                return (
                    "Secret env files are intentionally off-limits. Do not try `.env` or "
                    "its variants again. They are not required for this task. Pivot to visible "
                    "repo files like `README.md`, `config.py`, `main.py`, frontend files, or "
                    "startup scripts and continue the job."
                )
            if error.startswith("command_denied_by_safety_pattern") or error.startswith(
                "command_references_offlimits_fragment"
            ):
                return (
                    "That command is blocked by the bridge safety rules. Stop retrying the same "
                    "blocked path and choose another way to inspect the project from allowed files "
                    "and commands."
                )
            if repeated >= 2:
                return (
                    "You just repeated the same denied tool request. Do not retry it again. Use a "
                    "different file, command, or approach and keep moving."
                )
            return None

        def repeated_tool_call_result(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
            fingerprint = json.dumps(
                {
                    "tool": tool_name,
                    "arguments": arguments,
                },
                sort_keys=True,
            )
            tool_call_counts[fingerprint] = tool_call_counts.get(fingerprint, 0) + 1
            if tool_call_counts[fingerprint] < 3:
                return None
            return {
                "ok": False,
                "tool": tool_name,
                "error": "duplicate_tool_call_blocked",
                "message": (
                    "You have already made this same tool call multiple times. "
                    "Do not repeat it again; choose a different approach."
                ),
            }

        try:
            if is_nvidia:
                if not api_key:
                    raise RuntimeError(
                        "nvidia worker brain needs OVERLORD_WORKER_NVIDIA_API_KEY in .env"
                    )
            elif _is_llamacpp_aider_model(amodel):
                llamacpp_proc = await self._start_job_llamacpp_server(context_tokens)
            elif not (amodel.lower().startswith("openai/") or _is_ollama_aider_model(amodel)):
                raise RuntimeError(
                    f"local-agent does not know how to start backend model alias {amodel}"
                )

            final_text = ""
            for step in range(1, max_steps + 1):
                payload: dict[str, Any] = {
                    # NIM ids keep their org prefix; local aliases strip it.
                    "model": amodel if is_nvidia else _local_agent_model_name(amodel),
                    "messages": messages,
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                }
                if not answer_only:
                    payload["tools"] = _local_agent_tools()
                    payload["tool_choice"] = "auto"
                if is_nvidia:
                    payload.update(_NVIDIA_MODEL_PAYLOAD_OVERRIDES.get(amodel.lower(), {}))
                chat_url = _openai_chat_url(api_base)
                if not is_nvidia and _is_llamacpp_aider_model(amodel):
                    response = await _http_post_json_with_llamacpp_stall_guard(
                        chat_url,
                        payload,
                        timeout=min(self.timeout, 600),
                        api_key=api_key,
                        slots_url=_llamacpp_slots_url(api_base),
                    )
                else:
                    response = await _http_post_json_retry(
                        chat_url,
                        payload,
                        timeout=min(self.timeout, 600),
                        api_key=api_key,
                    )
                choice = (response.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                content = str(message.get("content") or "").strip()
                tool_calls = message.get("tool_calls") or []
                if content:
                    transcript.append(f"assistant[{step}]: {content}")

                if tool_calls:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": content,
                            "tool_calls": tool_calls,
                        }
                    )
                    for call in tool_calls:
                        fn = (call.get("function") or {}) if isinstance(call, dict) else {}
                        tool_name = str(fn.get("name") or "").strip()
                        raw_args = fn.get("arguments") or "{}"
                        try:
                            arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            arguments = {}
                        duplicate_result = repeated_tool_call_result(tool_name, arguments)
                        result = (
                            duplicate_result
                            if duplicate_result is not None
                            else await self._local_agent_execute_tool(folder, tool_name, arguments)
                        )
                        result_text = _local_agent_json_result(result, DEFAULT_LOCAL_AGENT_TOOL_OUTPUT_CHARS)
                        transcript.append(f"tool[{step}:{tool_name}]: {result_text}")
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.get("id", f"call_{step}"),
                                "content": result_text,
                            }
                        )
                        followup = denial_followup(tool_name, arguments, result)
                        if followup:
                            transcript.append(f"coach[{step}:{tool_name}]: {followup}")
                            messages.append({"role": "user", "content": followup})
                    continue

                fallback = _extract_json_object(content) or _extract_xmlish_tool_call(content)
                if fallback:
                    if "final" in fallback:
                        final_text = str(fallback.get("final") or "").strip()
                        break
                    if answer_only:
                        final_text = content or "(no final response)"
                        break
                    tool_name = str(
                        fallback.get("tool")
                        or fallback.get("name")
                        or fallback.get("action")
                        or ""
                    ).strip()
                    arguments = fallback.get("arguments")
                    if not isinstance(arguments, dict):
                        arguments = {
                            key: value
                            for key, value in fallback.items()
                            if key not in {"tool", "name", "action"}
                        }
                    duplicate_result = repeated_tool_call_result(tool_name, arguments)
                    result = (
                        duplicate_result
                        if duplicate_result is not None
                        else await self._local_agent_execute_tool(folder, tool_name, arguments)
                    )
                    result_text = _local_agent_json_result(result, DEFAULT_LOCAL_AGENT_TOOL_OUTPUT_CHARS)
                    transcript.append(f"tool[{step}:{tool_name}]: {result_text}")
                    messages.append({"role": "assistant", "content": content})
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Tool result for {tool_name}:\n{result_text}",
                        }
                    )
                    followup = denial_followup(tool_name, arguments, result)
                    if followup:
                        transcript.append(f"coach[{step}:{tool_name}]: {followup}")
                        messages.append({"role": "user", "content": followup})
                    continue

                final_text = content or "(no final response)"
                break
            else:
                final_text = f"Stopped after {max_steps} {brain} agent steps."

            finished_at = _now_iso()
            duration = round(asyncio.get_running_loop().time() - started_mono, 3)
            report_text = "\n\n".join(transcript[-12:] + ([f"final: {final_text}"] if final_text else []))
            telegram_detail, _ = _bounded_tail(report_text or "(no output)", self.telegram_tail_chars)
            report_tail, report_truncated = _bounded_tail(
                report_text or "(no output)",
                self.report_tail_chars,
            )
            self._emit_report(
                self._base_report(
                    name=name,
                    folder=folder,
                    brain=brain,
                    session=sid,
                    status="finished",
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_seconds=duration,
                    exit_code=0,
                    model=model,
                    result_tail=report_tail,
                    result_truncated=report_truncated,
                    effective_local_agent_model=amodel,
                    local_agent_api_base=api_base,
                    local_agent_context_tokens=context_tokens,
                    local_agent_context_hard_cap_tokens=hard_cap,
                )
            )
            self._send(self.owner, f"✅ [{name}] finished in {folder.name}:\n\n{telegram_detail}\n\n(session: {sid})")
        except Exception as exc:  # noqa: BLE001
            log.exception("%s agent worker %s crashed", brain, name)
            finished_at = _now_iso()
            duration = round(asyncio.get_running_loop().time() - started_mono, 3)
            self._emit_report(
                self._base_report(
                    name=name,
                    folder=folder,
                    brain=brain,
                    session=sid,
                    status="crashed",
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_seconds=duration,
                    exit_code=None,
                    model=model,
                    result_tail=str(exc),
                    effective_local_agent_model=amodel,
                    local_agent_api_base=api_base,
                    local_agent_context_tokens=context_tokens,
                    local_agent_context_hard_cap_tokens=hard_cap,
                )
            )
            self._send(self.owner, f"⚠️ [{name}] crashed: {exc}")
        finally:
            try:
                await self._stop_job_llamacpp_server(llamacpp_proc)
            except Exception:  # noqa: BLE001
                log.exception("Could not stop per-job llama.cpp server for %s", name)

    # ------------------------------------------------------------------ watcher
    async def watch(self, poll: float = 2.0) -> None:
        log.info("Worker dispatch watcher started on %s", self.dispatch_dir)
        while True:
            try:
                for f in sorted(self.dispatch_dir.glob("*.json")):
                    try:
                        raw = f.read_text()
                    finally:
                        f.unlink(missing_ok=True)  # consume once
                    try:
                        self._accept(json.loads(raw))
                    except Exception as exc:  # noqa: BLE001
                        log.error("Bad dispatch request %s: %s", f.name, exc)
            except Exception:  # noqa: BLE001
                log.exception("Dispatch watcher loop error")
            await asyncio.sleep(poll)

    def _emit_report(self, report: dict[str, Any]) -> None:
        report.setdefault("event_id", uuid.uuid4().hex)
        report.setdefault("timestamp", _now_iso())
        if self.report_store is not None:
            try:
                self.report_store.record(report)
            except Exception:  # noqa: BLE001 - audit failure must not kill workers
                log.exception("Could not write worker report")
        if self.on_report is not None and report.get("status") in _THREAD_REPORT_STATUSES:
            task = asyncio.create_task(self._safe_on_report(dict(report)))
            task.add_done_callback(self._log_report_callback_error)
        if report.get("status") in _THREAD_REPORT_STATUSES:
            self._surface_in_live_cli(report)

    def _surface_in_live_cli(self, report: dict[str, Any]) -> None:
        """Concurrent delivery: the same report, in Telegram AND the live CLI.

        Telegram already gets pushed above. If Ben's terminal is running
        `overlord --shared`, his session is a live shared job we can type into —
        so the report lands in the terminal he's actually looking at, as a real
        in-context turn. Automatic: no per-dispatch arming by the Overlord, no
        tmux launcher wrap, no Monitor polling (all three previously rejected).

        Best-effort by construction. It no-ops when there is no shared job, and
        `shared_job.notify` refuses to type while Ben has a half-written message
        at the prompt. Either way Telegram delivery is untouched, so the worst
        case is today's behavior, never a lost report.
        """
        name = report.get("name") or "worker"
        status = report.get("status")
        folder = Path(str(report.get("folder") or "")).name
        tail = (report.get("result_tail") or "").strip()
        icon = {"finished": "✅", "crashed": "⚠️", "timed_out": "⏱️"}.get(str(status), "•")
        text = f"{icon} [{name}] {status} in {folder}."
        if tail:
            text = f"{text}\n\n{tail}"

        def _push() -> None:
            try:
                if shared_job.notify(text):
                    log.info("Surfaced %s report for %s in the live CLI session", status, name)
            except Exception:  # noqa: BLE001 - never let CLI delivery break a worker
                log.exception("Could not surface worker report in the live CLI")

        threading.Thread(target=_push, name=f"cli-notify-{name}", daemon=True).start()

    async def _safe_on_report(self, report: dict[str, Any]) -> None:
        assert self.on_report is not None
        await self.on_report(report)

    @staticmethod
    def _log_report_callback_error(task: asyncio.Task) -> None:
        try:
            task.result()
        except Exception:  # noqa: BLE001
            log.exception("Worker report callback failed")

    @staticmethod
    def _base_report(
        *,
        name: str,
        folder: str | Path,
        brain: str,
        session: str | None,
        status: str,
        **extra: Any,
    ) -> dict[str, Any]:
        report = {
            "name": name,
            "folder": str(folder),
            "brain": brain,
            "session": session,
            "status": status,
            "timestamp": _now_iso(),
        }
        report.update(extra)
        return report

    def _accept(self, data: dict) -> None:
        name = str(data.get("name") or "worker").strip() or "worker"
        folder = str(data.get("folder") or "")
        task = str(data.get("task") or "").strip()
        brain = str(data.get("brain") or "claude").strip().lower()
        if brain in _LOCAL_AGENT_BRAINS:
            brain = "local-agent"
        elif brain in _NVIDIA_BRAINS:
            brain = "nvidia"
        session = str(data.get("session") or "").strip() or None
        resume = bool(data.get("resume"))
        model = str(data.get("model") or "").strip() or None  # optional override
        files_raw = data.get("files")
        approval_policy = (
            str(data.get("approval_policy") or data.get("codex_approval_policy") or "")
            .strip()
            .lower()
            or None
        )
        sandbox = (
            str(data.get("sandbox") or data.get("codex_sandbox") or "")
            .strip()
            .lower()
            or None
        )
        bypass_codex_sandbox = bool(
            data.get("dangerously_bypass_approvals_and_sandbox")
            or data.get("codex_bypass_approvals_and_sandbox")
        )

        target = _under_allowed_root(folder)
        if target is None or not target.is_dir():
            self._emit_report(
                self._base_report(
                    name=name,
                    folder=folder,
                    brain=brain,
                    session=session,
                    status="rejected",
                    reason="bad_folder",
                )
            )
            self._send(self.owner, f"❌ [{name}] bad/missing folder: {folder}")
            return
        if not task:
            self._emit_report(
                self._base_report(
                    name=name,
                    folder=target,
                    brain=brain,
                    session=session,
                    status="rejected",
                    reason="empty_task",
                )
            )
            self._send(self.owner, f"❌ [{name}] empty task — not dispatched.")
            return
        if name in self._running:
            self._emit_report(
                self._base_report(
                    name=name,
                    folder=target,
                    brain=brain,
                    session=session,
                    status="duplicate_ignored",
                    reason="already_running",
                )
            )
            self._send(self.owner, f"⏳ [{name}] already running; ignoring duplicate.")
            return
        if brain not in ("claude", "codex", "local-agent", "nvidia"):
            self._emit_report(
                self._base_report(
                    name=name,
                    folder=target,
                    brain=brain,
                    session=session,
                    status="rejected",
                    reason="bad_brain",
                )
            )
            self._send(
                self.owner,
                f"❌ [{name}] bad worker brain: {brain}. Use claude, codex, local-agent, or nvidia.",
            )
            return
        files, files_error = (
            _worker_file_args(target, files_raw)
            if brain in ("local-agent", "nvidia")
            else ([], None)
        )
        if files_error:
            self._emit_report(
                self._base_report(
                    name=name,
                    folder=target,
                    brain=brain,
                    session=session,
                    status="rejected",
                    reason="bad_worker_files",
                    detail=files_error,
                )
            )
            self._send(self.owner, f"❌ [{name}] bad worker files: {files_error}")
            return
        if brain == "codex" and approval_policy and approval_policy not in _CODEX_APPROVAL_POLICIES:
            self._emit_report(
                self._base_report(
                    name=name,
                    folder=target,
                    brain=brain,
                    session=session,
                    status="rejected",
                    reason="bad_codex_approval_policy",
                )
            )
            self._send(self.owner, f"❌ [{name}] bad Codex approval policy: {approval_policy}")
            return
        if brain == "codex" and sandbox and sandbox not in _CODEX_SANDBOXES:
            self._emit_report(
                self._base_report(
                    name=name,
                    folder=target,
                    brain=brain,
                    session=session,
                    status="rejected",
                    reason="bad_codex_sandbox",
                )
            )
            self._send(self.owner, f"❌ [{name}] bad Codex sandbox: {sandbox}")
            return

        self._emit_report(
            self._base_report(
                name=name,
                folder=target,
                brain=brain,
                session=session,
                status="accepted",
                resume=resume,
                model=model,
                files=files,
            )
        )
        self._running[name] = asyncio.create_task(
            self._run(
                name,
                target,
                task,
                brain,
                session,
                resume,
                model,
                files,
                approval_policy,
                sandbox,
                bypass_codex_sandbox,
            )
        )
        log.info("Accepted worker %s (brain=%s) in %s", name, brain, target)

    # ------------------------------------------------------------------ runner
    async def _run(
        self,
        name: str,
        folder: Path,
        task: str,
        brain: str,
        session: str | None,
        resume: bool,
        model: str | None = None,
        files: list[str] | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
        bypass_codex_sandbox: bool = False,
    ) -> None:
        if brain in ("local-agent", "nvidia"):
            try:
                await self._run_local_agent_worker(
                    name=name,
                    folder=folder,
                    task=task,
                    session=session,
                    resume=resume,
                    model=model,
                    files=files,
                    brain=brain,
                )
            finally:
                self._running.pop(name, None)
            return

        sid = session or str(uuid.uuid4())
        if brain == "codex":
            cmodel = model or self.codex_model
            argv = [self.codex_bin]
            if bypass_codex_sandbox:
                argv += ["--dangerously-bypass-approvals-and-sandbox"]
            else:
                if approval_policy:
                    argv += ["--ask-for-approval", approval_policy]
                if sandbox:
                    argv += ["--sandbox", sandbox]
            argv += ["exec"]
            if resume and session:
                argv += ["resume", session]
            if cmodel:
                argv += ["--model", cmodel]
            argv += ["--skip-git-repo-check", task]
            env = None
        else:  # claude
            ccmodel = model or self.claude_model
            if resume and session:
                argv = [self.claude_bin, "-p", task, "--resume", session]
            else:
                argv = [self.claude_bin, "-p", task, "--session-id", sid]
            if ccmodel:
                argv += ["--model", ccmodel]
            # bypassPermissions: autonomous (no prompts) AND can edit/write/bash,
            # while STILL honoring settings.json deny rules — measured: a bypass
            # worker is blocked from deny-listed paths but writes normally. (dontAsk
            # was wrong: it denies Edit/Write in headless, so workers can't work.)
            argv += ["--permission-mode", "bypassPermissions"]
            env = None

        started_at = _now_iso()
        started_mono = asyncio.get_running_loop().time()
        llamacpp_proc: asyncio.subprocess.Process | None = None
        self._emit_report(
            self._base_report(
                name=name,
                folder=folder,
                brain=brain,
                session=sid,
                status="started",
                started_at=started_at,
                resume=resume,
                model=model,
            )
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(folder),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception:  # noqa: BLE001
                    pass
                finished_at = _now_iso()
                duration = round(asyncio.get_running_loop().time() - started_mono, 3)
                self._emit_report(
                    self._base_report(
                        name=name,
                        folder=folder,
                        brain=brain,
                        session=sid,
                        status="timed_out",
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_seconds=duration,
                        exit_code=None,
                        result_tail=f"hit the {self.timeout // 60}-min cap and was killed",
                    )
                )
                self._send(
                    self.owner,
                    f"⏱ [{name}] hit the {self.timeout // 60}-min cap and was killed "
                    f"(resume to continue).",
                )
                return

            output = (out or b"").decode(errors="replace").strip()
            errtxt = (err or b"").decode(errors="replace").strip()

            # Resolve the real session id for the roster.
            if brain == "codex":
                m = _CODEX_SID_RE.search(output) or _CODEX_SID_RE.search(errtxt)
                real_sid = m.group(1) if m else (session or "?")
            else:
                real_sid = session if (resume and session) else sid
            tail = f"\n\n(session: {real_sid})"
            finished_at = _now_iso()
            duration = round(asyncio.get_running_loop().time() - started_mono, 3)

            verdict = classify_worker_exit(proc.returncode, self._is_shutting_down())
            effective_failed = verdict["failed"]
            if verdict["status"] == "interrupted":
                signame = verdict["signal"]
                cause = verdict["cause"]
                detail = (errtxt or output or "").strip()
                telegram_detail, _ = _bounded_tail(detail or "(no output captured)", self.telegram_tail_chars)
                report_tail, report_truncated = _bounded_tail(
                    detail or "(no output captured)", self.report_tail_chars
                )
                self._emit_report(
                    self._base_report(
                        name=name,
                        folder=folder,
                        brain=brain,
                        session=real_sid,
                        status="interrupted",
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_seconds=duration,
                        exit_code=proc.returncode,
                        result_tail=report_tail,
                        result_truncated=report_truncated,
                        real_codex_session_id=real_sid if brain == "codex" else None,
                    )
                )
                self._send(
                    self.owner,
                    f"🔄 [{name}] was INTERRUPTED in {folder.name} ({signame}) — {cause}.\n\n"
                    f"This is NOT necessarily a failure: its work may already be committed. "
                    f"Check:\n"
                    f"  git -C {folder} log --oneline -3\n"
                    f"  {folder}/HANDOFF.md\n\n{telegram_detail}{tail}",
                )
            elif not effective_failed:
                telegram_detail, _ = _bounded_tail(
                    output or "(no output)", self.telegram_tail_chars
                )
                report_tail, report_truncated = _bounded_tail(
                    output or "(no output)", self.report_tail_chars
                )
                self._emit_report(
                    self._base_report(
                        name=name,
                        folder=folder,
                        brain=brain,
                        session=real_sid,
                        status="finished",
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_seconds=duration,
                        exit_code=proc.returncode,
                        result_tail=report_tail,
                        result_truncated=report_truncated,
                        real_codex_session_id=real_sid if brain == "codex" else None,
                    )
                )
                self._send(
                    self.owner,
                    f"✅ [{name}] finished in {folder.name}:\n\n{telegram_detail}{tail}",
                )
            else:
                detail = errtxt or output or f"exit {proc.returncode}"
                telegram_detail, _ = _bounded_tail(detail, self.telegram_tail_chars)
                report_tail, report_truncated = _bounded_tail(
                    detail, self.report_tail_chars
                )
                self._emit_report(
                    self._base_report(
                        name=name,
                        folder=folder,
                        brain=brain,
                        session=real_sid,
                        status="failed",
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_seconds=duration,
                        exit_code=proc.returncode,
                        result_tail=report_tail,
                        result_truncated=report_truncated,
                        real_codex_session_id=real_sid if brain == "codex" else None,
                    )
                )
                self._send(
                    self.owner,
                    f"⚠️ [{name}] failed in {folder.name} (exit {proc.returncode}):\n\n{telegram_detail}",
                )
        except FileNotFoundError as exc:
            finished_at = _now_iso()
            duration = round(asyncio.get_running_loop().time() - started_mono, 3)
            self._emit_report(
                self._base_report(
                    name=name,
                    folder=folder,
                    brain=brain,
                    session=sid,
                    status="start_failed",
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_seconds=duration,
                    exit_code=None,
                    result_tail=str(exc),
                )
            )
            self._send(self.owner, f"⚠️ [{name}] could not start ({brain} not found): {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("Worker %s crashed", name)
            finished_at = _now_iso()
            duration = round(asyncio.get_running_loop().time() - started_mono, 3)
            self._emit_report(
                self._base_report(
                    name=name,
                    folder=folder,
                    brain=brain,
                    session=sid,
                    status="crashed",
                    started_at=started_at,
                    finished_at=finished_at,
                    duration_seconds=duration,
                    exit_code=None,
                    result_tail=str(exc),
                )
            )
            self._send(self.owner, f"⚠️ [{name}] crashed: {exc}")
        finally:
            try:
                await self._stop_job_llamacpp_server(llamacpp_proc)
            except Exception:  # noqa: BLE001
                log.exception("Could not stop per-job llama.cpp server for %s", name)
            self._running.pop(name, None)
