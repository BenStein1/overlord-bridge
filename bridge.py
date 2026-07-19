#!/usr/bin/env python3
"""Overlord bridge: drive a home-rooted AI "Overlord" from Telegram.

The brain is swappable via ``OVERLORD_BRAIN`` (``claude`` | ``codex``, default
``claude``). Both brains present the same :class:`~modules.brain.Brain`
interface, so this file never needs to know which one is active.

Architecture:

    Telegram (phone)  <-->  TelegramHandler thread  <-->  inbound Queue
                                                              |
                                          asyncio main loop (this file)
                                                              |
                                              Brain (Claude SDK | Codex App Server)
                                                              |
                                       PermissionGate  -->  Allow/Deny buttons
                                                            back on the phone

Security:
  * Single-user: TelegramHandler drops every update whose chat_id != OWNER.
  * PermissionGate is the shared second layer for BOTH brains: obvious secrets
    (OFFLIMITS_FRAGMENTS) are auto-denied; anything else that would prompt is
    forwarded to the phone for tap-to-approve, with a timeout that denies.
  * Claude additionally loads ~/.claude/CLAUDE.md + settings.json (deny rules)
    as a hard floor. Codex ignores those, so its guardrails come from its
    sandbox/approval policy + the AGENTS.md in this folder (see README).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from queue import Queue
from typing import Any

from modules import cli_notify
from modules.brain import Brain, PermissionGate
from modules.telegram_handler import TelegramHandler
from modules.workers import DEFAULT_AIDER_CONTEXT_TOKENS, WorkerManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
# httpx/httpcore log full request URLs at INFO — which include the bot token.
# Keep them quiet so the token never lands in logs/journald.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger("overlord.bridge")

BASE_DIR = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    """Minimal .env loader (no external dependency)."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Invalid %s=%r; using %s", name, raw, default)
        return default


def env_first(names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        raw = os.environ.get(name, "").strip()
        if raw:
            return raw
    return default


def env_int_first(names: tuple[str, ...], default: int) -> int:
    for name in names:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            continue
        try:
            return int(raw)
        except ValueError:
            log.warning("Invalid %s=%r; trying fallback/default", name, raw)
    return default


def env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    path = Path(raw).expanduser() if raw else default
    return path if path.is_absolute() else BASE_DIR / path

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_RAW = os.environ.get("OWNER_CHAT_ID", "")
CWD = os.environ.get("OVERLORD_CWD", str(Path.home()))
MODEL = os.environ.get("OVERLORD_MODEL", "opus")
PERMISSION_TIMEOUT = int(os.environ.get("OVERLORD_PERMISSION_TIMEOUT", "300"))
# Auto-approve every action EXCEPT off-limits secrets (which are always denied).
# Trades the per-tool Telegram Allow/Deny prompt for "just do it".
AUTO_APPROVE = os.environ.get("OVERLORD_AUTO_APPROVE", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
BRAIN = os.environ.get("OVERLORD_BRAIN", "claude").strip().lower()

# Codex-specific (only consulted when BRAIN == "codex").
CODEX_BIN = os.environ.get("OVERLORD_CODEX_BIN", "codex")
CODEX_MODEL = os.environ.get("OVERLORD_CODEX_MODEL", "")  # "" -> codex default
CODEX_APPROVAL_POLICY = os.environ.get("OVERLORD_CODEX_APPROVAL_POLICY", "on-request")
CODEX_SANDBOX = os.environ.get("OVERLORD_CODEX_SANDBOX", "read-only")
CODEX_APP_SERVER_PROXY = env_bool("OVERLORD_CODEX_APP_SERVER_PROXY", False)
CODEX_APP_SERVER_SOCKET = os.environ.get("OVERLORD_CODEX_APP_SERVER_SOCKET", "")

# Worker report/audit settings. Telegram completion messages remain on; these
# flags only control optional active-conversation context/visibility paths.
WORKER_REPORT_TO_THREAD = env_bool("WORKER_REPORT_TO_THREAD", True)
WORKER_REPORT_VISIBLE_TURN = env_bool("WORKER_REPORT_VISIBLE_TURN", False)
WORKER_REPORT_DIR = env_path("WORKER_REPORT_DIR", BASE_DIR / "worker_reports")
WORKER_REPORT_TAIL_CHARS = max(0, env_int("WORKER_REPORT_TAIL_CHARS", 4000))
WORKER_TELEGRAM_TAIL_CHARS = max(0, env_int("WORKER_TELEGRAM_TAIL_CHARS", 12000))
WORKER_REPORT_EVENTS_MAX_BYTES = max(
    0, env_int("WORKER_REPORT_EVENTS_MAX_BYTES", 1024 * 1024)
)

# Live terminal delivery (dual delivery alongside Telegram, brain-neutral —
# see modules/cli_notify.py). CLI_FEED is the durable, always-on, safe path:
# the `overlord` launcher tails this file in a passive tmux pane next to the
# live brain, so reports appear the instant they land, without touching
# anything Ben might be typing. TERMINAL_INJECT is the opt-in, best-effort
# deep path: type the report into the live brain pane as a real turn, but
# only when the pane's prompt line is confirmed idle.
WORKER_REPORT_CLI_FEED = env_bool("WORKER_REPORT_CLI_FEED", True)
WORKER_REPORT_CLI_FEED_PATH = env_path(
    "WORKER_REPORT_CLI_FEED_PATH", WORKER_REPORT_DIR / "cli_feed.log"
)
WORKER_REPORT_TERMINAL_INJECT = env_bool("WORKER_REPORT_TERMINAL_INJECT", False)
WORKER_REPORT_TMUX_SESSION = os.environ.get("WORKER_REPORT_TMUX_SESSION", "").strip()

if not TOKEN or not OWNER_RAW:
    sys.exit("ERROR: TELEGRAM_BOT_TOKEN and OWNER_CHAT_ID must be set in .env")
OWNER = int(OWNER_RAW)

# Per-brain session files: a Claude session id and a Codex thread id are not
# interchangeable, so each brain keeps its own pinned thread across brain
# switches. Normalize unknown brains to "claude".
_BRAIN_NAME = "codex" if BRAIN == "codex" else "claude"
SESSION_FILE = BASE_DIR / f".session.{_BRAIN_NAME}"
# The `overlord` launcher names its tmux session "overlord-<brain>" to match.
WORKER_REPORT_TMUX_SESSION = WORKER_REPORT_TMUX_SESSION or f"overlord-{_BRAIN_NAME}"
# One-time migration: the original single ".session" held the Claude id.
_LEGACY_SESSION = BASE_DIR / ".session"
if _BRAIN_NAME == "claude" and _LEGACY_SESSION.exists() and not SESSION_FILE.exists():
    _LEGACY_SESSION.rename(SESSION_FILE)


def format_worker_thread_report(report: dict[str, Any]) -> str:
    """Human-readable worker report for active thread context."""
    fields = [
        ("name", report.get("name")),
        ("folder", report.get("folder")),
        ("brain", report.get("brain")),
        ("status", report.get("status")),
        ("session", report.get("session")),
        ("real_codex_session_id", report.get("real_codex_session_id")),
        ("started_at", report.get("started_at")),
        ("finished_at", report.get("finished_at")),
        ("duration_seconds", report.get("duration_seconds")),
        ("exit_code", report.get("exit_code")),
    ]
    lines = [
        "[BRIDGE/SYSTEM WORKER REPORT]",
        "Automated bridge status report. This is not Ben's message. "
        "Use it only as factual context for future worker-status questions.",
    ]
    for key, value in fields:
        if value is not None:
            lines.append(f"{key}: {value}")
    result_tail = str(report.get("result_tail") or "").strip()
    if result_tail:
        lines.extend(["result_tail:", result_tail])
    return "\n".join(lines)


def format_worker_inline_report(report: dict[str, Any]) -> str:
    """Single-line summary safe for tmux ``send-keys -l`` (no embedded newline)."""
    name = report.get("name") or "worker"
    status = report.get("status") or "unknown"
    duration = report.get("duration_seconds")
    bits = [f"status={status}"]
    if duration is not None:
        bits.append(f"{duration}s")
    folder = report.get("folder")
    if folder:
        bits.append(f"folder={folder}")
    return (
        f"[worker report] {name}: {', '.join(bits)} "
        f"(full report: worker_reports/latest/{name}.json)"
    )


def build_brain(gate: PermissionGate) -> Brain:
    """Instantiate the configured brain. Default and unknown values -> claude."""
    if BRAIN == "codex":
        # Imported lazily so the Claude path never needs codex deps and vice versa.
        from modules.codex_brain import CodexBrain

        return CodexBrain(
            gate=gate,
            cwd=CWD,
            model=CODEX_MODEL,
            session_file=SESSION_FILE,
            codex_bin=CODEX_BIN,
            approval_policy=CODEX_APPROVAL_POLICY,
            sandbox=CODEX_SANDBOX,
            app_server_proxy=CODEX_APP_SERVER_PROXY,
            app_server_socket=CODEX_APP_SERVER_SOCKET or None,
        )

    if BRAIN not in ("claude", ""):
        log.warning("Unknown OVERLORD_BRAIN=%r; falling back to 'claude'.", BRAIN)

    from modules.claude_brain import ClaudeBrain

    return ClaudeBrain(
        gate=gate,
        cwd=CWD,
        model=MODEL,
        session_file=SESSION_FILE,
    )


class Bridge:
    def __init__(self) -> None:
        self.inbound: "Queue[tuple[int, str]]" = Queue()
        self._shutdown_event = asyncio.Event()
        self.gate = PermissionGate(
            telegram=None,  # set right after the handler exists
            owner=OWNER,
            timeout=PERMISSION_TIMEOUT,
            auto_approve=AUTO_APPROVE,
        )
        self.telegram = TelegramHandler(
            TOKEN,
            self.inbound,
            OWNER,
            on_permission_response=self.gate.on_permission_response,
        )
        self.gate.telegram = self.telegram
        self.brain = build_brain(self.gate)
        self._brain_lock = asyncio.Lock()
        self._thread_report_unsupported_logged = False
        self._visible_report_unsupported_logged = False
        # Brain-agnostic worker dispatch: watches dispatch/ and pushes worker
        # results to the owner on completion (no turn required).
        self.workers = WorkerManager(
            send=self.telegram.send,
            owner=OWNER,
            # So a worker killed by a bridge restart is reported as interrupted,
            # not as a failure (a worker that restarts the bridge kills itself:
            # it lives in this service's cgroup).
            is_shutting_down=self._shutdown_event.is_set,
            dispatch_dir=BASE_DIR / "dispatch",
            claude_bin="claude",
            codex_bin=CODEX_BIN,
            claude_model=os.environ.get("OVERLORD_WORKER_MODEL", "sonnet"),
            codex_model=os.environ.get("OVERLORD_WORKER_CODEX_MODEL", "").strip() or None,
            aider_model=env_first(
                ("OVERLORD_WORKER_LOCAL_AGENT_MODEL", "OVERLORD_WORKER_AIDER_MODEL"),
                "openai/Qwen3-Coder-Next",
            ),
            aider_api_base=env_first(
                (
                    "OVERLORD_WORKER_LOCAL_AGENT_API_BASE",
                    "OVERLORD_WORKER_AIDER_API_BASE",
                ),
                "http://127.0.0.1:1234/v1",
            ),
            local_agent_ollama_api_base=env_first(
                ("OVERLORD_WORKER_LOCAL_AGENT_OLLAMA_API_BASE",),
                "http://127.0.0.1:11434/v1",
            ),
            aider_map_tokens=max(
                0,
                env_int_first(
                    (
                        "OVERLORD_WORKER_LOCAL_AGENT_MAP_TOKENS",
                        "OVERLORD_WORKER_AIDER_MAP_TOKENS",
                    ),
                    0,
                ),
            ),
            aider_context_tokens=max(
                2048,
                env_int_first(
                    (
                        "OVERLORD_WORKER_LOCAL_AGENT_CONTEXT_TOKENS",
                        "OVERLORD_WORKER_AIDER_CONTEXT_TOKENS",
                    ),
                    DEFAULT_AIDER_CONTEXT_TOKENS,
                ),
            ),
            aider_min_context_tokens=max(
                1024,
                env_int_first(
                    (
                        "OVERLORD_WORKER_LOCAL_AGENT_MIN_CONTEXT_TOKENS",
                        "OVERLORD_WORKER_AIDER_MIN_CONTEXT_TOKENS",
                    ),
                    2048,
                ),
            ),
            aider_chat_history_tokens=max(
                1024,
                env_int_first(
                    (
                        "OVERLORD_WORKER_LOCAL_AGENT_CHAT_HISTORY_TOKENS",
                        "OVERLORD_WORKER_AIDER_CHAT_HISTORY_TOKENS",
                    ),
                    2048,
                ),
            ),
            aider_keep_alive=env_first(
                (
                    "OVERLORD_WORKER_LOCAL_AGENT_KEEP_ALIVE",
                    "OVERLORD_WORKER_AIDER_KEEP_ALIVE",
                ),
                "0",
            ),
            aider_openai_api_key=env_first(
                (
                    "OVERLORD_WORKER_LOCAL_AGENT_OPENAI_API_KEY",
                    "OVERLORD_WORKER_AIDER_OPENAI_API_KEY",
                ),
                "overlord-local",
            ),
            aider_llamacpp_bin=env_first(
                (
                    "OVERLORD_WORKER_LOCAL_AGENT_LLAMACPP_BIN",
                    "OVERLORD_WORKER_AIDER_LLAMACPP_BIN",
                ),
                "/tank/ai/llama.cpp/build/bin/llama-server",
            ),
            aider_llamacpp_model=env_first(
                (
                    "OVERLORD_WORKER_LOCAL_AGENT_LLAMACPP_MODEL",
                    "OVERLORD_WORKER_AIDER_LLAMACPP_MODEL",
                ),
                "/tank/ai/models/Qwen3-Coder-Next-UD-TQ1_0.gguf",
            ),
            aider_llamacpp_hf_repo=env_first(
                (
                    "OVERLORD_WORKER_LOCAL_AGENT_LLAMACPP_HF_REPO",
                    "OVERLORD_WORKER_AIDER_LLAMACPP_HF_REPO",
                ),
                "unsloth/Qwen3-Coder-Next-GGUF:UD-TQ1_0",
            ),
            aider_llamacpp_host=env_first(
                (
                    "OVERLORD_WORKER_LOCAL_AGENT_LLAMACPP_HOST",
                    "OVERLORD_WORKER_AIDER_LLAMACPP_HOST",
                ),
                "127.0.0.1",
            ),
            aider_llamacpp_port=env_int_first(
                (
                    "OVERLORD_WORKER_LOCAL_AGENT_LLAMACPP_PORT",
                    "OVERLORD_WORKER_AIDER_LLAMACPP_PORT",
                ),
                1234,
            ),
            aider_llamacpp_threads=env_int_first(
                (
                    "OVERLORD_WORKER_LOCAL_AGENT_LLAMACPP_THREADS",
                    "OVERLORD_WORKER_AIDER_LLAMACPP_THREADS",
                ),
                16,
            ),
            aider_llamacpp_batch=env_int_first(
                (
                    "OVERLORD_WORKER_LOCAL_AGENT_LLAMACPP_BATCH",
                    "OVERLORD_WORKER_AIDER_LLAMACPP_BATCH",
                ),
                1024,
            ),
            aider_llamacpp_n_cpu_moe=env_int_first(
                (
                    "OVERLORD_WORKER_LOCAL_AGENT_LLAMACPP_N_CPU_MOE",
                    "OVERLORD_WORKER_AIDER_LLAMACPP_N_CPU_MOE",
                ),
                36,
            ),
            aider_llamacpp_cache_type_k=env_first(
                (
                    "OVERLORD_WORKER_LOCAL_AGENT_LLAMACPP_CACHE_TYPE_K",
                    "OVERLORD_WORKER_AIDER_LLAMACPP_CACHE_TYPE_K",
                ),
                "q8_0",
            ),
            aider_llamacpp_cache_type_v=env_first(
                (
                    "OVERLORD_WORKER_LOCAL_AGENT_LLAMACPP_CACHE_TYPE_V",
                    "OVERLORD_WORKER_AIDER_LLAMACPP_CACHE_TYPE_V",
                ),
                "q8_0",
            ),
            nvidia_model=env_first(("OVERLORD_WORKER_NVIDIA_MODEL",), ""),
            nvidia_api_base=env_first(("OVERLORD_WORKER_NVIDIA_API_BASE",), ""),
            nvidia_api_key=env_first(
                ("OVERLORD_WORKER_NVIDIA_API_KEY", "NVIDIA_API_KEY"), ""
            ),
            report_dir=WORKER_REPORT_DIR,
            report_tail_chars=WORKER_REPORT_TAIL_CHARS,
            telegram_tail_chars=WORKER_TELEGRAM_TAIL_CHARS,
            report_events_max_bytes=WORKER_REPORT_EVENTS_MAX_BYTES,
            on_report=self._handle_worker_report,
        )

    # ---------------------------------------------------------------- main loop
    async def run(self) -> None:
        self.telegram.start()
        if not self.telegram.wait_ready(30):
            log.error("Telegram handler failed to start within 30s")
            return

        resumed = SESSION_FILE.exists()
        try:
            await self.brain.connect()
        except Exception as exc:
            log.exception("Brain failed to start")
            self.telegram.send(OWNER, f"⚠️ Failed to start {self.brain.name}: {exc}")
            return

        log.info(
            "Overlord online (brain=%s, cwd=%s, resumed=%s, auto_approve=%s)",
            self.brain.name,
            CWD,
            resumed,
            AUTO_APPROVE,
        )
        approve_note = (
            "\n⚡ Auto-approve ON — actions run without asking (secrets still blocked)."
            if AUTO_APPROVE
            else ""
        )
        self.telegram.send(
            OWNER,
            (
                f"🟢 Overlord online [{self.brain.name}] — picked up where we left off. "
                "(/new for a clean slate.)"
                if resumed
                else f"🟢 Overlord online [{self.brain.name}]. Send me a task. "
                "(/new starts a clean session.)"
            )
            + approve_note,
        )
        # Start the brain-agnostic worker dispatch watcher (background).
        asyncio.create_task(self.workers.watch())
        try:
            while not self._shutdown_event.is_set():
                chat_id, text = await asyncio.get_running_loop().run_in_executor(
                    None, self._inbound_get_with_shutdown
                )
                if chat_id is None:
                    break
                if text.strip() == "/new":
                    await self._reset_session(chat_id)
                    continue
                await self._handle_turn(chat_id, text)
        finally:
            await self.brain.disconnect()
            self.telegram.stop()

    def _inbound_get_with_shutdown(self) -> tuple[int | None, str | None]:
        if self._shutdown_event.is_set():
            return None, None
        item = self.inbound.get()
        if self._shutdown_event.is_set():
            return None, None
        return item

    def request_shutdown(self) -> None:
        self._shutdown_event.set()
        self.inbound.put((-1, ""))

    async def _handle_worker_report(self, report: dict[str, Any]) -> None:
        if not any(
            (
                WORKER_REPORT_TO_THREAD,
                WORKER_REPORT_VISIBLE_TURN,
                WORKER_REPORT_CLI_FEED,
                WORKER_REPORT_TERMINAL_INJECT,
            )
        ):
            return
        text = format_worker_thread_report(report)

        if WORKER_REPORT_TO_THREAD or WORKER_REPORT_VISIBLE_TURN:
            await self._record_worker_report_in_brain(report, text)

        # Live terminal delivery: independent of the brain-context paths above
        # (no self._brain_lock — this never touches self.brain, only the
        # terminal's own tmux pane), so it never waits on an in-flight turn.
        await self._deliver_worker_report_to_terminal(report, text)

    async def _record_worker_report_in_brain(
        self, report: dict[str, Any], text: str
    ) -> None:
        recorded: bool | None = None
        visible: bool | None = None
        async with self._brain_lock:
            if WORKER_REPORT_TO_THREAD:
                try:
                    recorded = await self.brain.record_event(text, metadata=report)
                except Exception as exc:  # noqa: BLE001 - optional report path
                    log.warning("Worker report thread append failed: %s", exc)
                    recorded = False
            if WORKER_REPORT_VISIBLE_TURN:
                try:
                    visible = await self.brain.record_visible_event(
                        text, metadata=report
                    )
                except Exception as exc:  # noqa: BLE001 - optional report path
                    log.warning("Worker report visible turn failed: %s", exc)
                    visible = False

        if recorded is True:
            log.info(
                "Recorded worker report in active %s thread: %s/%s",
                self.brain.name,
                report.get("name"),
                report.get("status"),
            )
        elif WORKER_REPORT_TO_THREAD and not self._thread_report_unsupported_logged:
            self._thread_report_unsupported_logged = True
            log.info(
                "Worker report thread append is unsupported for brain=%s; "
                "using Telegram plus %s",
                self.brain.name,
                WORKER_REPORT_DIR,
            )
        if visible is True:
            log.info(
                "Started visible worker report turn in active %s thread: %s/%s",
                self.brain.name,
                report.get("name"),
                report.get("status"),
            )
        elif WORKER_REPORT_VISIBLE_TURN and not self._visible_report_unsupported_logged:
            self._visible_report_unsupported_logged = True
            log.info(
                "Visible worker report turns are unsupported for brain=%s; "
                "using Telegram plus %s",
                self.brain.name,
                WORKER_REPORT_DIR,
            )

    async def _deliver_worker_report_to_terminal(
        self, report: dict[str, Any], text: str
    ) -> None:
        """Push the report into the live terminal Overlord (see modules/cli_notify.py).

        CLI_FEED (default on) is durable and safe: append to a feed file that
        the `overlord` launcher tails in a passive tmux pane, so it appears in
        the live terminal without ever touching Ben's input. TERMINAL_INJECT
        (default off) is opt-in, best-effort deep injection: type the report
        into the live brain pane as a real turn, only when that pane's prompt
        line is confirmed idle.
        """
        loop = asyncio.get_running_loop()
        if WORKER_REPORT_CLI_FEED:
            try:
                await loop.run_in_executor(
                    None, cli_notify.append_feed, WORKER_REPORT_CLI_FEED_PATH, text
                )
            except OSError as exc:
                log.warning("Worker report CLI feed append failed: %s", exc)

        if not WORKER_REPORT_TERMINAL_INJECT:
            return
        session = WORKER_REPORT_TMUX_SESSION
        exists = await loop.run_in_executor(
            None, cli_notify.tmux_session_exists, session
        )
        if not exists:
            log.debug(
                "Terminal inject skipped for %s; no live tmux session %s",
                report.get("name"),
                session,
            )
            return
        idle = await loop.run_in_executor(None, cli_notify.pane_input_is_idle, session)
        if not idle:
            log.info(
                "Terminal inject skipped for %s (tmux session %s not confirmed idle)",
                report.get("name"),
                session,
            )
            return
        inline = format_worker_inline_report(report)
        injected = await loop.run_in_executor(
            None, cli_notify.inject_turn, session, inline
        )
        if injected:
            log.info(
                "Injected worker report into live tmux session %s: %s/%s",
                session,
                report.get("name"),
                report.get("status"),
            )
        else:
            log.warning("Terminal inject failed for tmux session %s", session)

    async def _reset_session(self, chat_id: int) -> None:
        try:
            async with self._brain_lock:
                await self.brain.reset()
        except Exception as exc:
            log.exception("Brain reset failed")
            self.telegram.send(chat_id, f"⚠️ Could not reset session: {exc}")
            return
        self.telegram.send(
            chat_id, "🆕 Fresh session started — previous context cleared."
        )
        log.info("Session reset via /new")

    async def _handle_turn(self, chat_id: int, text: str) -> None:
        log.info("Dispatching turn: %s", text)
        try:
            async with self._brain_lock:
                async for chunk in self.brain.query(text):
                    if chunk and chunk.strip():
                        self.telegram.send(chat_id, chunk)
        except Exception as exc:  # noqa: BLE001 - report any turn failure to phone
            log.exception("Turn failed")
            self.telegram.send(chat_id, f"⚠️ Error: {exc}")


async def _run_bridge() -> None:
    bridge = Bridge()
    loop = asyncio.get_running_loop()
    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, bridge.request_shutdown)
        except NotImplementedError:
            signal.signal(sig, lambda *_: bridge.request_shutdown())
    try:
        await bridge.run()
    finally:
        bridge.telegram.stop()


def main() -> None:
    try:
        asyncio.run(_run_bridge())
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down Overlord bridge")


if __name__ == "__main__":
    main()
