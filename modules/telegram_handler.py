"""Telegram I/O for the Overlord bridge.

Reference shape borrowed from an earlier assistant of Ben's: a
``threading.Thread`` that runs python-telegram-bot's async polling in its own
event loop, feeds inbound messages onto a queue, and exposes a thread-safe
``send`` for replies.

Improvements over the original:
  * **Owner gating** -- only the configured OWNER_CHAT_ID is ever serviced;
    every other update is dropped. The Overlord can touch the filesystem, so
    this is non-negotiable.
  * **Graceful shutdown** via ``stop()`` so the systemd service stops cleanly.
  * **4096-char chunking** so long Overlord replies don't get rejected by the
    Telegram API.
  * **Permission prompts** -- ``ask_permission`` sends an Allow/Deny inline
    keyboard; taps are routed back through ``on_permission_response``.
  * No Joshua-specific dependencies (uses stdlib ``logging``).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from queue import Queue
from typing import Callable, Iterator, Optional

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

log = logging.getLogger("overlord.telegram")

# Telegram hard limit on a single text message.
MAX_MESSAGE_LEN = 4096


class TelegramHandler(threading.Thread):
    """Runs Telegram long-polling in a dedicated thread/event loop."""

    def __init__(
        self,
        token: str,
        inbound_queue: "Queue[tuple[int, str]]",
        owner_chat_id: int,
        on_permission_response: Optional[Callable[[str, bool], None]] = None,
    ) -> None:
        super().__init__(daemon=True, name="telegram-handler")
        self.token = token
        self.inbound_queue = inbound_queue
        self.owner_chat_id = int(owner_chat_id)
        self.on_permission_response = on_permission_response

        self.bot = Bot(token=token)
        self.application = Application.builder().token(token).build()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()

        self.application.add_handler(CommandHandler("new", self._handle_new))
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )
        self.application.add_handler(CallbackQueryHandler(self._handle_callback))

    # ------------------------------------------------------------------ inbound
    async def _handle_message(self, update: Update, context: CallbackContext) -> None:
        if update.message is None:
            return
        chat_id = update.message.chat_id
        text = update.message.text or ""
        if chat_id != self.owner_chat_id:
            log.warning("Dropping message from unauthorized chat_id=%s", chat_id)
            return
        self.inbound_queue.put((chat_id, text))
        log.info("Message from owner: %s", text)

    async def _handle_new(self, update: Update, context: CallbackContext) -> None:
        """Owner-only /new -> enqueue a reset sentinel for the bridge."""
        if update.message is None:
            return
        chat_id = update.message.chat_id
        if chat_id != self.owner_chat_id:
            log.warning("Dropping /new from unauthorized chat_id=%s", chat_id)
            return
        self.inbound_queue.put((chat_id, "/new"))
        log.info("Owner requested /new session")

    async def _handle_callback(self, update: Update, context: CallbackContext) -> None:
        """Handle Allow/Deny taps from a permission prompt."""
        query = update.callback_query
        if query is None:
            return
        await query.answer()
        chat_id = query.message.chat_id if query.message else None
        if chat_id != self.owner_chat_id:
            return
        # callback_data format: "perm:<request_id>:<allow|deny>"
        try:
            _, request_id, decision = (query.data or "").split(":", 2)
        except ValueError:
            return
        allow = decision == "allow"
        try:
            original = query.message.text if query.message else ""
            verdict = "✅ Allowed" if allow else "⛔ Denied"
            await query.edit_message_text(f"{original}\n\n{verdict}")
        except Exception as exc:  # editing is best-effort
            log.debug("Could not edit permission message: %s", exc)
        if self.on_permission_response is not None:
            self.on_permission_response(request_id, allow)

    # ------------------------------------------------------------------ outbound
    def send(self, chat_id: int, text: str) -> None:
        """Thread-safe: queue a (chunked) reply onto the polling loop."""
        if not text or self.loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._send(chat_id, text), self.loop)

    # Joshua-compatible alias.
    send_response = send

    async def _send(self, chat_id: int, text: str) -> None:
        for chunk in _chunks(text):
            try:
                await self.bot.send_message(chat_id=chat_id, text=chunk)
            except Exception as exc:
                log.error("send_message failed: %s", exc)

    def ask_permission(self, chat_id: int, prompt_text: str, request_id: str) -> None:
        """Send an Allow/Deny prompt; the answer arrives via on_permission_response."""
        if self.loop is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._ask_permission(chat_id, prompt_text, request_id), self.loop
        )

    async def _ask_permission(
        self, chat_id: int, prompt_text: str, request_id: str
    ) -> None:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Allow", callback_data=f"perm:{request_id}:allow"
                    ),
                    InlineKeyboardButton(
                        "⛔ Deny", callback_data=f"perm:{request_id}:deny"
                    ),
                ]
            ]
        )
        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=prompt_text[:MAX_MESSAGE_LEN],
                reply_markup=keyboard,
            )
        except Exception as exc:
            log.error("ask_permission send failed: %s", exc)

    # ------------------------------------------------------------------ lifecycle
    def run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.application.initialize())
        self.loop.run_until_complete(self.application.start())
        self.loop.run_until_complete(self.application.updater.start_polling())
        self._ready.set()
        log.info("Telegram polling started")
        self.loop.run_forever()

    def wait_ready(self, timeout: float = 30.0) -> bool:
        return self._ready.wait(timeout)

    def stop(self) -> None:
        """Stop polling and tear down the event loop cleanly."""
        if self.loop is None or not self.loop.is_running():
            return

        async def _shutdown() -> None:
            try:
                if self.application.updater and self.application.updater.running:
                    await self.application.updater.stop()
                await self.application.stop()
                await self.application.shutdown()
            except Exception as exc:
                log.error("Error during Telegram shutdown: %s", exc)

        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), self.loop).result(timeout=10)
        except Exception as exc:
            log.error("Shutdown future failed: %s", exc)
        self.loop.call_soon_threadsafe(self.loop.stop)


def _chunks(text: str) -> Iterator[str]:
    for i in range(0, len(text), MAX_MESSAGE_LEN):
        yield text[i : i + MAX_MESSAGE_LEN]
