"""CodexBrain: drive OpenAI Codex via the ``codex app-server`` behind :class:`Brain`.

``codex app-server`` speaks bidirectional JSON-RPC 2.0. In private bridge mode
this is newline-delimited JSON over stdio; in shared TUI mode this is WebSocket
over a Unix socket opened by ``codex app-server --listen unix://...``. This
brain:

  * spawns ``codex app-server`` as a private subprocess or starts/connects to a
    shared Unix-socket listener;
  * performs the ``initialize`` -> ``initialized`` handshake;
  * starts (or resumes) a thread, sends each user turn with ``turn/start``, and
    streams assistant text from ``item/agentMessage/delta`` /
    ``item/completed`` notifications until ``turn/completed``;
  * answers server-initiated approval requests
    (``item/commandExecution/requestApproval``,
    ``item/fileChange/requestApproval``) by routing them through the SAME
    :class:`PermissionGate` the Claude path uses (off-limits pre-screen +
    Telegram Allow/Deny + timeout). Allow -> ``accept``; Deny -> ``decline``;
    timeout -> ``cancel``.

Protocol method names, params, and the approval decision values were checked
against ``codex app-server generate-json-schema`` for the installed Codex CLI.

App Server has native thread resume, so the persisted ``.session`` file holds
the Codex *threadId* and is replayed via ``thread/resume`` on startup.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import os
import shutil
import socket
from pathlib import Path
from typing import Any, AsyncIterator

from modules.brain import Brain, PermissionGate

log = logging.getLogger("overlord.codex")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INSTRUCTIONS_FILE = PROJECT_ROOT / "AGENTS.md"
HOME_INSTRUCTIONS_FILE = Path.home() / "AGENTS.md"
ENV_INSTRUCTIONS_FILE = "OVERLORD_CODEX_INSTRUCTIONS_FILE"
CODEX_USAGE_CACHE = Path.home() / ".cache" / "overlord" / "codex_usage.json"
VISIBLE_REPORT_PREFIX = "[BRIDGE/SYSTEM WORKER STATUS]"
APP_SERVER_STREAM_LIMIT_BYTES = 64 * 1024 * 1024
DEFAULT_APP_SERVER_SOCKET = PROJECT_ROOT / "run" / "codex-app-server.sock"

# --- checked JSON-RPC surface (codex app-server) ----------------------------
# Client -> server requests
M_INITIALIZE = "initialize"
M_THREAD_START = "thread/start"
M_THREAD_RESUME = "thread/resume"
M_THREAD_INJECT_ITEMS = "thread/inject_items"
M_TURN_START = "turn/start"
M_TURN_INTERRUPT = "turn/interrupt"
# Client -> server notifications
M_INITIALIZED = "initialized"
# Server -> client streaming notifications
N_AGENT_DELTA = "item/agentMessage/delta"
N_ITEM_COMPLETED = "item/completed"
N_TURN_STARTED = "turn/started"
N_TURN_COMPLETED = "turn/completed"
N_TURN_FAILED_STATUSES = {"failed", "interrupted"}
# Server -> client requests (approvals) we answer
R_EXEC_APPROVAL = "item/commandExecution/requestApproval"
R_FILECHANGE_APPROVAL = "item/fileChange/requestApproval"
APPROVAL_METHODS = {R_EXEC_APPROVAL, R_FILECHANGE_APPROVAL}
# Server -> client requests we explicitly decline (don't support interactively)
R_TOOL_USER_INPUT = "item/tool/requestUserInput"
R_MCP_ELICITATION = "mcpServer/elicitation/request"

# Approval decision values (string enum on the wire).
DECISION_ACCEPT = "accept"
DECISION_DECLINE = "decline"
DECISION_CANCEL = "cancel"


class CodexError(RuntimeError):
    """Raised for unrecoverable Codex App Server problems (binary missing, etc.)."""


def _find_rate_limits(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, dict):
        rate_limits = obj.get("rate_limits")
        if isinstance(rate_limits, dict):
            return rate_limits
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


def _codex_usage_from_rate_limits(
    rate_limits: dict[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    now = now if now is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()

    def window(data: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(data, dict):
            return None
        try:
            used = float(data.get("used_percent") or 0)
        except (TypeError, ValueError):
            used = 0.0
        resets_at = data.get("resets_at")
        reset_passed = isinstance(resets_at, (int, float)) and resets_at <= now
        effective_used = 0.0 if reset_passed else used
        return {
            "used": round(effective_used, 1),
            "gas": max(0, int(round(100 - effective_used))),
            "resets_at": resets_at,
            "reset_passed": reset_passed,
        }

    windows: dict[str, Any] = {}
    for candidate in (rate_limits.get("primary"), rate_limits.get("secondary")):
        if not isinstance(candidate, dict):
            continue
        minutes = candidate.get("window_minutes")
        if minutes == 300:
            windows["session"] = window(candidate)
        elif minutes == 10080:
            windows["weekly"] = window(candidate)
    if not windows:
        return None
    return {
        "session": windows.get("session"),
        "weekly": windows.get("weekly"),
        "age_hours": 0.0,
        "plan_type": rate_limits.get("plan_type"),
        "source": "codex_app_server",
    }


def _record_codex_usage_cache_from_message(msg: dict[str, Any]) -> None:
    rate_limits = _find_rate_limits(msg)
    if not rate_limits:
        return
    usage = _codex_usage_from_rate_limits(rate_limits)
    if usage is None:
        return
    try:
        CODEX_USAGE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        CODEX_USAGE_CACHE.write_text(json.dumps(usage), encoding="utf-8")
    except OSError as exc:
        log.debug("Could not write Codex usage cache: %s", exc)


class UnixWebSocketTransport:
    """Minimal WebSocket client for Codex app-server's Unix-socket transport."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, path: Path) -> "UnixWebSocketTransport":
        reader, writer = await asyncio.open_unix_connection(str(path))
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = "\r\n".join(
            [
                "GET / HTTP/1.1",
                "Host: localhost",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
                "",
                "",
            ]
        )
        writer.write(request.encode("ascii"))
        await writer.drain()
        header = await reader.readuntil(b"\r\n\r\n")
        status = header.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            writer.close()
            await writer.wait_closed()
            raise CodexError(
                "Codex app-server WebSocket handshake failed: "
                f"{status.decode(errors='replace')}"
            )
        return cls(reader, writer)

    async def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        length = len(payload)
        if length <= 125:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.extend([0x80 | 126, (length >> 8) & 0xFF, length & 0xFF])
        else:
            header.append(0x80 | 127)
            header.extend(length.to_bytes(8, "big"))
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.writer.write(bytes(header) + mask + masked)
        await self.writer.drain()

    async def recv_text(self) -> str | None:
        fragments: list[bytes] = []
        while True:
            first = await self.reader.readexactly(1)
            second = await self.reader.readexactly(1)
            fin = bool(first[0] & 0x80)
            opcode = first[0] & 0x0F
            masked = bool(second[0] & 0x80)
            length = second[0] & 0x7F
            if length == 126:
                length = int.from_bytes(await self.reader.readexactly(2), "big")
            elif length == 127:
                length = int.from_bytes(await self.reader.readexactly(8), "big")
            mask = await self.reader.readexactly(4) if masked else b""
            payload = await self.reader.readexactly(length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))

            if opcode == 0x8:
                return None
            if opcode == 0x9:
                await self._send_control(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x0):
                fragments.append(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8", errors="replace")

    async def _send_control(self, opcode: int, payload: bytes) -> None:
        if len(payload) > 125:
            payload = payload[:125]
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.writer.write(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + masked)
        await self.writer.drain()

    async def close(self) -> None:
        try:
            await self._send_control(0x8, b"")
        except Exception:
            pass
        self.writer.close()
        await self.writer.wait_closed()


class CodexBrain(Brain):
    name = "codex"

    def __init__(
        self,
        *,
        gate: PermissionGate,
        cwd: str,
        model: str | None,
        session_file: Path,
        codex_bin: str = "codex",
        approval_policy: str = "on-request",
        sandbox: str = "read-only",
        developer_instructions: str | None = None,
        app_server_proxy: bool = False,
        app_server_socket: str | None = None,
    ) -> None:
        self.gate = gate
        self.cwd = cwd
        self.model = model or None
        self.session_file = session_file
        self.codex_bin = codex_bin
        self.approval_policy = approval_policy
        self.sandbox = sandbox
        self.app_server_proxy = app_server_proxy
        self.app_server_socket = Path(app_server_socket or DEFAULT_APP_SERVER_SOCKET)
        self.developer_instructions = (
            developer_instructions
            if developer_instructions is not None
            else _load_developer_instructions()
        )

        self._proc: asyncio.subprocess.Process | None = None
        self._listener_proc: asyncio.subprocess.Process | None = None
        self._ws: UnixWebSocketTransport | None = None
        self._thread_id: str | None = None
        self._next_id = 0
        # Pending client->server requests: id -> Future resolved with the result.
        self._pending: dict[int, asyncio.Future] = {}
        # Per-turn streaming queue; receives whole assistant messages + a None
        # sentinel at end of turn.
        self._turn_queue: asyncio.Queue[str | None] | None = None
        self._turn_error: str | None = None
        # Accumulate streamed deltas per agentMessage item so we relay one
        # coherent Telegram message instead of a flood of tiny fragments.
        self._delta_buf: dict[str, str] = {}
        self._reader_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._visible_report_turn_active = False
        self._active_turn_id: str | None = None
        # mtime of the thread's rollout file at our last load — used to detect
        # terminal `codex resume` turns on the SAME thread so Telegram picks
        # them up (parity with the Claude brain). See _maybe_refresh.
        self._loaded_mtime: float = 0.0

    @property
    def session_id(self) -> str | None:
        return self._thread_id

    # ---------------------------------------------------------------- transport
    def _alloc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _send(self, obj: dict[str, Any]) -> None:
        if self._ws is not None:
            await self._ws.send_text(json.dumps(obj))
            return
        if self._proc is None or self._proc.stdin is None:
            raise CodexError("Codex app-server is not running.")
        data = (json.dumps(obj) + "\n").encode()
        async with self._write_lock:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a client->server request and await its result."""
        if self._reader_task is not None and self._reader_task.done():
            raise CodexError("Codex app-server stream closed.")
        req_id = self._alloc_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._send({"id": req_id, "method": method, "params": params})
        except Exception:
            self._pending.pop(req_id, None)
            raise
        return await fut

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"method": method, "params": params})

    async def _respond(self, req_id: Any, result: dict[str, Any]) -> None:
        """Reply to a server->client request (e.g. an approval)."""
        await self._send({"id": req_id, "result": result})

    # ---------------------------------------------------------------- reader loop
    async def _read_loop(self) -> None:
        try:
            if self._ws is not None:
                while True:
                    text = await self._ws.recv_text()
                    if text is None:
                        break
                    if not text.strip():
                        continue
                    try:
                        msg = json.loads(text)
                    except json.JSONDecodeError:
                        log.debug("Non-JSON websocket message from codex: %s", text[:200])
                        continue
                    await self._dispatch(msg)
            else:
                assert self._proc is not None and self._proc.stdout is not None
                stdout = self._proc.stdout
                while True:
                    line = await stdout.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        log.debug("Non-JSON line from codex: %s", line[:200])
                        continue
                    await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Codex reader loop crashed")
        finally:
            # Unblock anyone waiting on the process if it died.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CodexError("Codex app-server stream closed."))
            self._pending.clear()
            if self._turn_queue is not None:
                self._turn_error = self._turn_error or "Codex app-server stream closed."
                self._turn_queue.put_nowait(None)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        _record_codex_usage_cache_from_message(msg)

        # 1) Response to one of our requests.
        if "id" in msg and "method" not in msg:
            fut = self._pending.pop(msg["id"], None)
            if fut is None or fut.done():
                return
            if "error" in msg:
                fut.set_exception(CodexError(str(msg["error"])))
            else:
                fut.set_result(msg.get("result", {}))
            return

        method = msg.get("method")
        if method is None:
            return

        # 2) Server-initiated request (has an id) -> we must reply.
        if "id" in msg:
            await self._handle_server_request(msg)
            return

        # 3) Notification (no id).
        self._handle_notification(method, msg.get("params") or {})

    # ---------------------------------------------------------------- approvals
    async def _handle_server_request(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if self._visible_report_turn_active:
            await self._decline_visible_report_request(req_id, method)
            return

        if method in APPROVAL_METHODS:
            allow = await self._approve(method, params)
            decision = DECISION_ACCEPT if allow else DECISION_DECLINE
            await self._respond(req_id, {"decision": decision})
            return

        if method in (R_TOOL_USER_INPUT, R_MCP_ELICITATION):
            # We have no interactive text channel beyond Allow/Deny; decline.
            log.info("Declining unsupported server request %s", method)
            await self._respond(req_id, {"decision": DECISION_DECLINE})
            return

        # Unknown server request: respond with a benign decline so the turn isn't
        # wedged waiting on us.
        log.warning("Unknown server request %s; declining", method)
        await self._respond(req_id, {"decision": DECISION_DECLINE})

    async def _decline_visible_report_request(
        self, req_id: Any, method: str | None
    ) -> None:
        """Keep opt-in report turns from taking actions or prompting Ben."""
        log.warning("Declining %s during bridge visible worker-report turn", method)
        decision = DECISION_CANCEL if method in APPROVAL_METHODS else DECISION_DECLINE
        await self._respond(req_id, {"decision": decision})

    async def _approve(self, method: str, params: dict[str, Any]) -> bool:
        """Route a Codex approval through the shared gate. True=accept."""
        if method == R_EXEC_APPROVAL:
            command = params.get("command") or ""
            cwd = params.get("cwd") or ""
            detail = command or "(command execution)"
            if cwd:
                detail = f"{detail}\n(cwd: {cwd})"
            candidates = [command, cwd]
            return await self.gate.request("Codex: run command", detail, candidates)

        # File-change approval. The params don't always carry the path inline,
        # so surface the reason and let the operator decide after the shared
        # gate checks any grant root Codex provided.
        reason = params.get("reason") or "Codex wants to write file changes."
        grant_root = params.get("grantRoot") or ""
        candidates = [grant_root]
        return await self.gate.request("Codex: apply file changes", reason, candidates)

    # ---------------------------------------------------------------- streaming
    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == N_AGENT_DELTA:
            # Buffer deltas per item; flush as one message on item/completed so
            # Telegram gets coherent replies, not a flood of fragments.
            item_id = params.get("itemId") or ""
            delta = params.get("delta") or ""
            if delta:
                self._delta_buf[item_id] = self._delta_buf.get(item_id, "") + delta
            return

        if method == N_ITEM_COMPLETED:
            item = params.get("item") or {}
            if item.get("type") != "agentMessage":
                self._delta_buf.pop(item.get("id", ""), None)
                return
            item_id = item.get("id") or ""
            # Prefer the authoritative final text; fall back to buffered deltas.
            text = item.get("text") or self._delta_buf.get(item_id, "")
            self._delta_buf.pop(item_id, None)
            if text and text.strip() and self._turn_queue is not None:
                self._turn_queue.put_nowait(text)
            return

        if method == N_TURN_STARTED:
            turn = params.get("turn") or {}
            turn_id = turn.get("id")
            if isinstance(turn_id, str) and turn_id.strip():
                self._active_turn_id = turn_id
            return

        if method == N_TURN_COMPLETED:
            turn = params.get("turn") or {}
            turn_id = turn.get("id")
            if isinstance(turn_id, str) and turn_id == self._active_turn_id:
                self._active_turn_id = None
            status = turn.get("status")
            if isinstance(status, dict):
                status = status.get("type")
            if status in N_TURN_FAILED_STATUSES:
                err = (turn.get("error") or {})
                self._turn_error = (
                    err.get("message") if isinstance(err, dict) else None
                ) or f"Codex turn {status}."
            if self._turn_queue is not None:
                self._turn_queue.put_nowait(None)
            return

        if method == "error":
            log.error("Codex error notification: %s", params)
            err = params.get("error")
            if isinstance(err, dict) and err.get("message"):
                # Capture so query() surfaces the real cause; turn/completed's
                # generic "failed" would otherwise mask it.
                self._turn_error = err["message"]
            return

    # ---------------------------------------------------------------- session
    def _load_thread_id(self) -> str | None:
        try:
            sid = self.session_file.read_text().strip()
            return sid or None
        except FileNotFoundError:
            return None

    def _save_thread_id(self, thread_id: str) -> None:
        if thread_id and thread_id != self._thread_id:
            self._thread_id = thread_id
            try:
                self.session_file.write_text(thread_id)
            except OSError as exc:
                log.error("Could not persist Codex thread id: %s", exc)

    # ------------------------------------------------- shared-session detection
    def _rollout_path(self, tid: str | None) -> Path | None:
        """Newest rollout transcript for thread ``tid`` (date-nested store)."""
        if not tid:
            return None
        matches = sorted(
            Path.home().glob(f".codex/sessions/**/rollout-*-{tid}.jsonl")
        )
        return matches[-1] if matches else None

    def _rollout_mtime(self, tid: str | None) -> float:
        path = self._rollout_path(tid)
        if path is None:
            return 0.0
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    def _record_mtime(self) -> None:
        self._loaded_mtime = self._rollout_mtime(self._thread_id)

    def _set_active_turn_id_from_result(self, result: dict[str, Any]) -> None:
        turn = result.get("turn") or {}
        turn_id = turn.get("id")
        if isinstance(turn_id, str) and turn_id.strip():
            self._active_turn_id = turn_id

    async def _maybe_refresh(self) -> None:
        """Reload the thread from disk if it changed outside this process.

        Either the pinned id in the session file changed (a new shared thread was
        started from the terminal) or the same thread's rollout grew because Ben
        ran ``codex resume`` in a terminal. Re-issue ``thread/resume`` so Telegram
        reflects terminal work — parity with ClaudeBrain._maybe_refresh.
        """
        disk_tid = self._load_thread_id()
        target = None
        if disk_tid and disk_tid != self._thread_id:
            log.info("Shared Codex thread changed (%s -> %s); reloading",
                     self._thread_id, disk_tid)
            target = disk_tid
        elif self._thread_id and self._rollout_mtime(self._thread_id) > self._loaded_mtime + 0.001:
            log.info("Codex thread %s changed on disk (external turn); reloading",
                     self._thread_id)
            target = self._thread_id
        if target is None:
            return
        try:
            # Re-issue thread/resume without creating a replacement thread if
            # this best-effort refresh fails.
            await self._start_thread(target, fallback_to_fresh=False)
            self._record_mtime()
        except Exception as exc:
            log.error("Codex reload failed (%s); keeping current thread", exc)

    # ---------------------------------------------------------------- lifecycle
    async def _spawn(self) -> None:
        if shutil.which(self.codex_bin) is None:
            raise CodexError(
                f"Codex CLI '{self.codex_bin}' not found on PATH. Install it "
                "(npm i -g @openai/codex) or set OVERLORD_CODEX_BIN to the binary."
            )
        if self.app_server_proxy:
            await self._ensure_app_server_socket()
            self._ws = await UnixWebSocketTransport.connect(self.app_server_socket)
            self._reader_task = asyncio.create_task(self._read_loop())
            return
        else:
            argv = [self.codex_bin, "app-server"]
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # App-server can emit whole-thread JSONL notifications during resume.
            # Python's default StreamReader limit is 64 KiB, which is too small
            # for active Overlord threads and can kill worker-report handling.
            limit=APP_SERVER_STREAM_LIMIT_BYTES,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        # Drain stderr to the log so codex warnings/errors are visible.
        asyncio.create_task(self._drain_stderr())

    async def _ensure_app_server_socket(self) -> None:
        """Start a shared Unix-socket app-server for remote TUI mode."""
        if self._unix_socket_accepts_connections(self.app_server_socket):
            log.info("Codex app-server socket already listening at %s", self.app_server_socket)
            return

        self.app_server_socket.parent.mkdir(parents=True, exist_ok=True)
        self.app_server_socket.unlink(missing_ok=True)

        if not await self._start_shared_app_server_listener():
            self._listener_proc = await asyncio.create_subprocess_exec(
                self.codex_bin,
                "app-server",
                "--listen",
                f"unix://{self.app_server_socket}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=APP_SERVER_STREAM_LIMIT_BYTES,
            )
            asyncio.create_task(
                self._drain_stream(
                    self._listener_proc.stdout, "codex app-server listener stdout"
                )
            )
            asyncio.create_task(
                self._drain_stream(
                    self._listener_proc.stderr, "codex app-server listener stderr"
                )
            )

        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            if self._unix_socket_accepts_connections(self.app_server_socket):
                log.info(
                    "Codex app-server listener ready at %s; using websocket transport",
                    self.app_server_socket,
                )
                return
            if self._listener_proc is not None and self._listener_proc.returncode is not None:
                raise CodexError(
                    f"Codex app-server listener exited with {self._listener_proc.returncode}."
                )
            await asyncio.sleep(0.1)
        raise CodexError(
            f"Timed out waiting for Codex app-server socket {self.app_server_socket}."
        )

    async def _start_shared_app_server_listener(self) -> bool:
        systemd_run = shutil.which("systemd-run")
        systemctl = shutil.which("systemctl")
        codex_bin = shutil.which(self.codex_bin) or self.codex_bin
        if not systemd_run or not systemctl:
            return False

        unit = "overlord-codex-app-server.service"
        stop_proc = await asyncio.create_subprocess_exec(
            systemctl,
            "--user",
            "stop",
            unit,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await stop_proc.wait()
        proc = await asyncio.create_subprocess_exec(
            systemd_run,
            "--user",
            "--quiet",
            "--collect",
            f"--unit={unit.removesuffix('.service')}",
            "--property=Restart=on-failure",
            "--property=RestartSec=1",
            codex_bin,
            "app-server",
            "--listen",
            f"unix://{self.app_server_socket}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            log.warning(
                "Could not start shared Codex app-server via systemd-run: %s%s",
                (out or b"").decode(errors="replace"),
                (err or b"").decode(errors="replace"),
            )
            return False
        log.info("Started shared Codex app-server as user unit %s", unit)
        return True

    @staticmethod
    def _unix_socket_accepts_connections(path: Path) -> bool:
        if not path.exists():
            return False
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(0.25)
            sock.connect(str(path))
        except OSError:
            return False
        finally:
            sock.close()
        return True

    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        await self._drain_stream(self._proc.stderr, "codex stderr")

    async def _drain_stream(
        self, stream: asyncio.StreamReader | None, label: str
    ) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            log.debug("%s: %s", label, line.decode(errors="replace").rstrip())

    async def _handshake(self) -> None:
        await self._request(
            M_INITIALIZE,
            {
                "clientInfo": {"name": "overlord-bridge", "version": "0.1.0"},
            },
        )
        await self._notify(M_INITIALIZED, {})

    async def _start_thread(
        self, resume: str | None, *, fallback_to_fresh: bool = True
    ) -> None:
        common: dict[str, Any] = {
            "cwd": self.cwd,
            "approvalPolicy": self.approval_policy,
            "sandbox": self.sandbox,
        }
        if self.developer_instructions:
            common["developerInstructions"] = self.developer_instructions
        if self.model:
            common["model"] = self.model

        if resume:
            try:
                result = await self._request(
                    M_THREAD_RESUME, {**common, "threadId": resume}
                )
            except CodexError as exc:
                if not fallback_to_fresh:
                    raise
                log.warning("Codex resume of %s failed (%s); starting fresh.", resume, exc)
                self.session_file.unlink(missing_ok=True)
                self._thread_id = None
                result = await self._request(M_THREAD_START, common)
        else:
            result = await self._request(M_THREAD_START, common)

        thread = result.get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            raise CodexError(f"Codex did not return a thread id: {result}")
        self._save_thread_id(thread_id)

    async def connect(self, resume: str | None = None) -> None:
        sid = resume if resume is not None else self._load_thread_id()
        await self._spawn()
        await self._handshake()
        await self._start_thread(sid)
        self._record_mtime()
        log.info(
            "CodexBrain online (cwd=%s, model=%s, sandbox=%s, approval=%s, "
            "instructions=%s, resumed=%s, thread=%s)",
            self.cwd,
            self.model or "<default>",
            self.sandbox,
            self.approval_policy,
            "yes" if self.developer_instructions else "no",
            sid is not None,
            self._thread_id,
        )

    async def reset(self) -> None:
        self.session_file.unlink(missing_ok=True)
        self._thread_id = None
        await self._start_thread(None)
        self._record_mtime()
        log.info("Codex thread reset via /new (new thread=%s)", self._thread_id)

    async def disconnect(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._proc is not None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
            except Exception:
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        # In shared TUI mode the app-server owns the user's live terminal socket.
        # Leave it running across bridge restarts; the next bridge process will reconnect.

    async def query(self, text: str) -> AsyncIterator[str]:
        if self._thread_id is None:
            raise RuntimeError("CodexBrain.query called before connect()")
        # Pull in any terminal `codex resume` turns on this thread first.
        await self._maybe_refresh()
        self._turn_queue = asyncio.Queue()
        self._turn_error = None
        self._delta_buf.clear()

        result = await self._request(
            M_TURN_START,
            {
                "threadId": self._thread_id,
                "input": [{"type": "text", "text": text}],
            },
        )
        self._set_active_turn_id_from_result(result)

        try:
            while True:
                chunk = await self._turn_queue.get()
                if chunk is None:
                    break
                if chunk.strip():
                    yield chunk
        finally:
            self._turn_queue = None

        # Our own turn just bumped the rollout mtime; record it so the next
        # refresh only fires on genuinely external (terminal) changes.
        self._record_mtime()

        if self._turn_error:
            yield f"⚠️ {self._turn_error}"

    async def record_event(
        self, text: str, metadata: dict[str, Any] | None = None
    ) -> bool:
        """Append a bridge-owned status item to the Codex thread history.

        The local app-server schema exposes ``thread/inject_items`` for raw
        Responses API items. Use a developer-role message so the report is
        clearly bridge/system context rather than Ben's words, and do not start
        a model turn.
        """
        if self._thread_id is None or not text.strip():
            return False

        await self._maybe_refresh()

        meta = metadata or {}
        safe_event_id = _safe_worker_report_event_id(meta)
        item = {
            "type": "message",
            "role": "developer",
            "id": f"bridge-worker-report-{safe_event_id}",
            "content": [{"type": "input_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {
                "turn_id": f"bridge-worker-report-{safe_event_id}",
            },
        }

        try:
            await self._request(
                M_THREAD_INJECT_ITEMS,
                {"threadId": self._thread_id, "items": [item]},
            )
        except Exception as exc:  # noqa: BLE001 - best-effort audit path
            log.warning("Could not inject bridge event into Codex thread: %s", exc)
            return False

        self._record_mtime()
        return True

    async def interrupt_active_turn(self, turn_id: str | None = None) -> bool:
        """Interrupt an active turn on this thread, if one is known."""
        target_turn_id = turn_id or self._active_turn_id
        if self._thread_id is None or target_turn_id is None:
            return False
        try:
            await self._request(
                M_TURN_INTERRUPT,
                {
                    "threadId": self._thread_id,
                    "turnId": target_turn_id,
                },
            )
        except Exception as exc:  # noqa: BLE001 - best-effort control path
            log.warning("Could not interrupt Codex turn %s: %s", target_turn_id, exc)
            return False
        if target_turn_id == self._active_turn_id:
            self._active_turn_id = None
        return True

    async def record_visible_event(
        self, text: str, metadata: dict[str, Any] | None = None
    ) -> bool:
        """Start an opt-in visible worker-report turn in Codex.

        The turn is intentionally narrow: it asks Codex to emit a terse tagged
        assistant status message and sets a guard so any approval/tool request
        that happens during the turn is declined without involving Telegram.
        """
        if self._thread_id is None or not text.strip():
            return False

        await self._maybe_refresh()

        meta = metadata or {}
        safe_event_id = _safe_worker_report_event_id(meta)

        active_turn_id = self._active_turn_id
        external_active_turn_id = None
        if active_turn_id is None:
            external_active_turn_id = self._external_active_rollout_turn_id()
            active_turn_id = external_active_turn_id

        if active_turn_id is not None and not self._visible_report_turn_active:
            interrupted = await self.interrupt_active_turn(active_turn_id)
            if not interrupted and external_active_turn_id is None:
                log.warning(
                    "Codex visible worker report skipped; active turn could not be interrupted"
                )
                return False
            if self._turn_queue is not None:
                await self._wait_for_current_turn_to_settle()
            if external_active_turn_id is not None and interrupted:
                await self._wait_for_rollout_turn_to_settle(external_active_turn_id)
            if self._turn_queue is not None:
                log.warning(
                    "Codex visible worker report skipped; active turn is still running"
                )
                return False

        self._turn_queue = asyncio.Queue()
        self._turn_error = None
        self._delta_buf.clear()
        self._visible_report_turn_active = True
        chunks: list[str] = []
        try:
            result = await self._request(
                M_TURN_START,
                {
                    "threadId": self._thread_id,
                    "clientUserMessageId": (
                        f"bridge-visible-worker-report-{safe_event_id}"
                    ),
                    "input": [
                        {
                            "type": "text",
                            "text": _format_visible_worker_report_prompt(text),
                        }
                    ],
                },
            )
            self._set_active_turn_id_from_result(result)

            while True:
                chunk = await self._turn_queue.get()
                if chunk is None:
                    break
                if chunk.strip():
                    chunks.append(chunk)
        except Exception as exc:  # noqa: BLE001 - optional visibility path
            log.warning("Could not start Codex visible worker report turn: %s", exc)
            return False
        finally:
            self._visible_report_turn_active = False
            self._turn_queue = None
            self._record_mtime()

        if self._turn_error:
            log.warning(
                "Codex visible worker report turn ended with error: %s",
                self._turn_error,
            )
            return False
        if not "".join(chunks).strip():
            log.warning("Codex visible worker report turn produced no assistant text")
            return False
        return True

    async def _wait_for_current_turn_to_settle(self, timeout: float = 10.0) -> None:
        """Wait for the current streamed turn to finish after an interrupt."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self._turn_queue is not None and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

    async def _wait_for_rollout_turn_to_settle(
        self, turn_id: str, timeout: float = 10.0
    ) -> None:
        """Wait for an externally-started rollout turn to finish after interrupt."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if not self._rollout_turn_is_active(turn_id):
                return
            await asyncio.sleep(0.05)

    def _external_active_rollout_turn_id(self) -> str | None:
        """Infer an in-flight turn started by another Codex client, if any."""
        path = self._rollout_path(self._thread_id)
        if path is None:
            return None

        latest_turn_id: str | None = None
        completed_turn_ids: set[str] = set()
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    typ = event.get("type")
                    payload = event.get("payload") or {}
                    if typ == "turn_context":
                        turn_id = payload.get("turn_id")
                        if isinstance(turn_id, str) and turn_id.strip():
                            latest_turn_id = turn_id
                    elif typ == "event_msg" and payload.get("type") == "task_complete":
                        turn_id = payload.get("turn_id")
                        if isinstance(turn_id, str) and turn_id.strip():
                            completed_turn_ids.add(turn_id)
        except OSError as exc:
            log.warning("Could not inspect Codex rollout for active turn: %s", exc)
            return None

        if latest_turn_id and latest_turn_id not in completed_turn_ids:
            return latest_turn_id
        return None

    def _rollout_turn_is_active(self, turn_id: str) -> bool:
        """Return True until the rollout records task completion for turn_id."""
        path = self._rollout_path(self._thread_id)
        if path is None:
            return False
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") != "event_msg":
                        continue
                    payload = event.get("payload") or {}
                    if (
                        payload.get("type") == "task_complete"
                        and payload.get("turn_id") == turn_id
                    ):
                        return False
        except OSError as exc:
            log.warning("Could not inspect Codex rollout turn state: %s", exc)
            return False
        return True


def _load_developer_instructions() -> str:
    """Load Overlord role rules for Codex threads.

    Codex app-server runs with cwd at the home root for the Overlord, so it will
    not naturally discover this repository's AGENTS.md. Pass the instructions
    explicitly at thread start/resume instead of relying on a home-level symlink.
    """
    override = os.environ.get(ENV_INSTRUCTIONS_FILE, "").strip()
    candidates = (
        [Path(override).expanduser()]
        if override
        else [DEFAULT_INSTRUCTIONS_FILE, HOME_INSTRUCTIONS_FILE]
    )

    for path in candidates:
        try:
            text = path.read_text()
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("Could not read Codex instruction file %s: %s", path, exc)
            continue
        if text.strip():
            log.info("Loaded Codex developer instructions from %s", path)
            return text.strip()

    log.warning(
        "No Codex developer instructions found; expected %s or %s",
        DEFAULT_INSTRUCTIONS_FILE,
        HOME_INSTRUCTIONS_FILE,
    )
    return ""


def _safe_worker_report_event_id(metadata: dict[str, Any]) -> str:
    raw_event_id = str(metadata.get("event_id") or "worker-report")
    safe_event_id = "".join(
        ch if ch.isalnum() or ch in "._-" else "-" for ch in raw_event_id
    )
    return safe_event_id[:80] or "worker-report"


def _format_visible_worker_report_prompt(report_text: str) -> str:
    return "\n".join(
        [
            "[BRIDGE/SYSTEM WORKER REPORT TURN]",
            "This is automated bridge/system worker status, not Ben's message.",
            "Do not use tools, inspect files, dispatch workers, approve actions, "
            "ask questions, or start follow-up work.",
            "Reply with one terse assistant-visible status message beginning "
            f"{VISIBLE_REPORT_PREFIX}.",
            "Use only the facts in the report below.",
            "",
            report_text.strip(),
        ]
    )
