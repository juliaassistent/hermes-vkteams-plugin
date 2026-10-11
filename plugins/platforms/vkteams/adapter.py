"""VK Teams (Среда) platform adapter for Hermes Agent.

Ported from the OpenClaw ``vkteams`` channel plugin (TypeScript) to the
Hermes platform-plugin contract.  The OpenClaw plugin is untouched; this
is an independent Python implementation of the same VK Teams Bot API
integration.

VK Teams Bot API (verified against https://api.icq.net/bot/v1):
  - Auth: ``token`` query parameter (URL-encoded; ``:`` -> ``%3A``).
  - No webhooks — long polling only: ``events/get?pollTime=<s>&lastEventId=<id>``.
  - ``messages/sendText`` accepts ``format`` JSON with grouped styles:
    ``{"bold":[{"offset":0,"length":4}], "link":[{"offset":0,"length":2,"url":"..."}]}``
    (offsets are CHARACTER offsets, not bytes).
  - ``messages/editText``, ``messages/deleteMessages``,
    ``chats/sendActions`` (typing), ``chats/getInfo``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

try:
    from gateway.platform_registry import platform_registry, PlatformEntry
    from gateway.platforms.base import (
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )
    from gateway.config import PlatformConfig, Platform
except Exception:  # pragma: no cover - import-time fallback for tooling
    BasePlatformAdapter = object  # type: ignore
    MessageEvent = None  # type: ignore
    MessageType = None  # type: ignore
    SendResult = None  # type: ignore
    PlatformConfig = None  # type: ignore
    Platform = None  # type: ignore
    platform_registry = None  # type: ignore
    PlatformEntry = None  # type: ignore

DEFAULT_API_BASE_URL = "https://api.icq.net/bot/v1"
DEFAULT_POLL_TIME = 30
MAX_MESSAGE_LENGTH = 4096

# ---------------------------------------------------------------------------
# Markdown -> VK Teams format-styles
# ---------------------------------------------------------------------------

_FORMAT_TYPES = (
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "link",
    "mention",
    "inline_code",
    "pre",
    "ordered_list",
    "unordered_list",
    "quote",
)

_INLINE_RE = re.compile(
    r"(\*\*|__)(.+?)\1"          # **bold** / __bold__
    r"|(?<!\*)\*([^*\n]+)\*(?!\*)"  # *italic*
    r"|(?<!_)_([^_\n]+)_(?!_)"      # _italic_
    r"|`([^`\n]+)`"                 # `code`
    r"|~~([^~\n]+)~~"               # ~~strike~~
    r"|\[([^\]]+)\]\(([^)\s]+)\)"   # [text](url)
)


def _markdown_to_format(md: str) -> Dict[str, Any]:
    """Convert markdown to VK Teams grouped format + clean text.

    Returns ``{"text": str, "format": dict|None}``.  Offsets are character
    offsets into the cleaned text (VK Teams rejects byte offsets for
    Cyrillic).  Block-level constructs (quotes, lists, code fences) are
    handled line-by-line.
    """
    styles: Dict[str, List[Dict[str, Any]]] = {t: [] for t in _FORMAT_TYPES}
    out: List[str] = []
    offset = 0

    def push(s: str) -> None:
        nonlocal offset
        if not s:
            return
        out.append(s)
        offset += len(s)

    def push_inline(raw: str) -> None:
        last = 0
        for m in _INLINE_RE.finditer(raw):
            push(raw[last : m.start()])
            last = m.end()
            if m.group(2) is not None:
                inner = m.group(2)
                start = offset
                push(inner)
                styles["bold"].append({"offset": start, "length": len(inner)})
            elif m.group(3) is not None or m.group(4) is not None:
                inner = m.group(3) or m.group(4)
                start = offset
                push(inner)
                styles["italic"].append({"offset": start, "length": len(inner)})
            elif m.group(5) is not None:
                inner = m.group(5)
                start = offset
                push(inner)
                styles["inline_code"].append({"offset": start, "length": len(inner)})
            elif m.group(6) is not None:
                inner = m.group(6)
                start = offset
                push(inner)
                styles["strikethrough"].append({"offset": start, "length": len(inner)})
            elif m.group(7) is not None and m.group(8) is not None:
                text = m.group(7)
                url = m.group(8)
                start = offset
                push(text)
                styles["link"].append(
                    {"offset": start, "length": len(text), "url": url}
                )
        push(raw[last:])

    lines = md.split("\n")
    in_code_block = False
    code_buffer: List[str] = []
    list_type: Optional[str] = None
    list_start = 0
    in_quote = False
    quote_start = 0

    def flush_list() -> None:
        nonlocal list_type
        if list_type:
            length = offset - list_start
            if length > 0:
                styles[list_type].append(
                    {"offset": list_start, "length": length}
                )
            list_type = None

    def flush_quote() -> None:
        nonlocal in_quote
        if in_quote:
            length = offset - quote_start
            if length > 0:
                styles["quote"].append({"offset": quote_start, "length": length})
            in_quote = False

    for line in lines:
        # Code fence ```...```
        if line.lstrip().startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_buffer = []
            else:
                in_code_block = False
                content = "\n".join(code_buffer)
                start = offset
                push(content)
                if content:
                    styles["pre"].append({"offset": start, "length": len(content)})
                push("\n")
            continue
        if in_code_block:
            code_buffer.append(line)
            continue

        # Quote >...
        qm = re.match(r"^>\s?(.*)$", line)
        if qm:
            flush_list()
            if not in_quote:
                in_quote = True
                quote_start = offset
            push_inline(qm.group(1))
            push("\n")
            continue
        flush_quote()

        # Lists
        um = re.match(r"^[-*+]\s+(.*)$", line)
        om = re.match(r"^\d+[.)]\s+(.*)$", line)
        if um or om:
            typ = "ordered_list" if om else "unordered_list"
            if list_type != typ:
                flush_list()
                list_type = typ
                list_start = offset
            push_inline((um or om).group(1))
            push("\n")
            continue
        flush_list()

        push_inline(line)
        push("\n")

    flush_list()
    flush_quote()
    if in_code_block and code_buffer:
        content = "\n".join(code_buffer)
        start = offset
        push(content)
        if content:
            styles["pre"].append({"offset": start, "length": len(content)})

    text = "".join(out)
    if text.endswith("\n"):
        text = text[:-1]

    format_data = {k: v for k, v in styles.items() if v}
    return {"text": text, "format": format_data or None}


def _split_chunks(text: str, limit: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """Split long text into chunks on paragraph/line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    lines = text.split("\n")
    acc = ""
    for line in lines:
        if len(acc) + len(line) + 1 > limit and acc:
            chunks.append(acc)
            acc = line
        else:
            acc = f"{acc}\n{line}" if acc else line
    if acc:
        chunks.append(acc)
    return chunks or [text]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class VkTeamsAdapter(BasePlatformAdapter):  # type: ignore[misc]
    """VK Teams (Среда) long-polling bot adapter for Hermes."""

    supports_async_delivery = True
    splits_long_messages = True
    typed_command_prefix = "/"

    def __init__(self, config: PlatformConfig, **kwargs: Any):
        platform = Platform("vkteams")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}
        self.token = (
            os.getenv("VKTEAMS_TOKEN", "")
            or str(extra.get("token", "") or "").strip()
        )
        self.api_base_url = (
            os.getenv("VKTEAMS_API_BASE_URL", "")
            or str(extra.get("api_base_url", "") or DEFAULT_API_BASE_URL).strip()
        ).rstrip("/")
        try:
            self.poll_time = int(
                os.getenv("VKTEAMS_POLL_TIME", "") or extra.get("poll_time", DEFAULT_POLL_TIME)
            )
        except (TypeError, ValueError):
            self.poll_time = DEFAULT_POLL_TIME

        # Auth / ACL
        allowed = os.getenv("VKTEAMS_ALLOWED_USERS", "") or extra.get("allow_from", "")
        self.allow_from = {str(x).strip().lower() for x in allowed.split(",") if str(x).strip()}
        allow_all = os.getenv("VKTEAMS_ALLOW_ALL_USERS", "") or extra.get("allow_all", "")
        self.allow_all = str(allow_all).strip().lower() in {"1", "true", "yes", "on"}

        # Runtime state
        self._http: Optional[httpx.AsyncClient] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._last_event_id = 0
        self._running = False
        self._own_user_id = ""
        self._last_msg_id: Dict[str, str] = {}
        self._typing_sent_at: Dict[str, float] = {}

        logger.info(
            "VK Teams adapter initialized: base=%s poll=%ss allow_all=%s",
            self.api_base_url,
            self.poll_time,
            self.allow_all,
        )

    # -- identity -----------------------------------------------------------

    @property
    def name(self) -> str:
        return "VK Teams"

    def is_connected(self) -> bool:
        return self._running and self._poll_task is not None and not self._poll_task.done()

    # -- HTTP helpers -------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(90.0))
        return self._http

    async def _api_get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        params = {k: v for k, v in params.items() if v is not None}
        params["token"] = self.token
        resp = await self._client().get(f"{self.api_base_url}{path}", params=params)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok", False):
            raise RuntimeError(
                f"VK Teams API error: {data.get('description', 'unknown')}"
            )
        return data

    # -- lifecycle ----------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.token:
            logger.error("VK Teams: token not configured")
            return False
        try:
            self._client()
            info = await self._api_get("/self/get", {})
            self._own_user_id = str(info.get("userId", ""))
            logger.info("VK Teams connected as bot %s", self._own_user_id)
        except Exception as e:
            logger.error("VK Teams connect failed: %s", e)
            return False
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- long polling -------------------------------------------------------

    async def _poll_loop(self) -> None:
        retry_delay = 3.0
        while self._running:
            try:
                data = await self._api_get(
                    "/events/get",
                    {"pollTime": self.poll_time, "lastEventId": self._last_event_id},
                )
                retry_delay = 3.0
                events = data.get("events", []) or []
                if events:
                    self._last_event_id = max(int(e.get("eventId", 0)) for e in events)
                for event in events:
                    if event.get("type") != "newMessage":
                        continue
                    try:
                        await self._handle_event(event.get("payload") or {})
                    except Exception as e:
                        logger.exception("VK Teams event handling failed: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning("VK Teams poll error: %s (retry in %ss)", e, retry_delay)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30.0)

    async def _handle_event(self, payload: Dict[str, Any]) -> None:
        chat = payload.get("chat") or {}
        chat_id = str(chat.get("chatId", ""))
        chat_type = str(chat.get("type", "private"))
        if chat_type != "private":
            return
        sender = (payload.get("from") or {}).get("userId")
        if not sender:
            return
        # Ignore own bot messages
        if str(sender) == self._own_user_id:
            return

        text = str(payload.get("text") or "")
        msg_id = str(payload.get("msgId") or "")
        ts = payload.get("timestamp")
        try:
            timestamp = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except (TypeError, ValueError):
            timestamp = datetime.now(tz=timezone.utc)

        # Media parts (files/voice/sticker) are surfaced as document/voice
        media_urls: List[str] = []
        media_types: List[str] = []
        msg_type = MessageType.TEXT
        parts = payload.get("parts") or []
        for part in parts:
            ptype = part.get("type")
            if ptype in ("file", "voice", "sticker"):
                file_id = (part.get("payload") or {}).get("fileId")
                if file_id:
                    media_urls.append(f"vkteams-file://{file_id}")
                    media_types.append(
                        "audio/ogg" if ptype == "voice" else "application/octet-stream"
                    )
                    msg_type = MessageType.VOICE if ptype == "voice" else MessageType.DOCUMENT

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_id,
            chat_type="dm",
            user_id=str(sender),
            user_name=str(sender),
            message_id=msg_id,
        )

        event = MessageEvent(
            source=source,
            text=text,
            message_type=msg_type,
            media_urls=media_urls,
            media_types=media_types,
            timestamp=timestamp,
            raw_message=payload,
        )
        await self.handle_message(event)

    # -- outbound -----------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message with markdown -> VK Teams format-styles."""
        try:
            prepared = _markdown_to_format(content)
            chunks = _split_chunks(prepared["text"])
            last_msg_id: Optional[str] = None
            for i, chunk in enumerate(chunks):
                params: Dict[str, Any] = {"chatId": chat_id, "text": chunk}
                if i == 0 and reply_to:
                    params["replyMsgId"] = reply_to
                if i == 0 and prepared["format"]:
                    params["format"] = json.dumps(prepared["format"])
                data = await self._api_get("/messages/sendText", params)
                last_msg_id = str(data.get("msgId") or "")
            return SendResult(success=True, message_id=last_msg_id)
        except Exception as e:
            logger.warning("VK Teams send failed: %s", e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        try:
            prepared = _markdown_to_format(content)
            params: Dict[str, Any] = {
                "chatId": chat_id,
                "msgId": message_id,
                "text": prepared["text"],
            }
            if prepared["format"]:
                params["format"] = json.dumps(prepared["format"])
            data = await self._api_get("/messages/editText", params)
            return SendResult(success=True, message_id=str(data.get("msgId") or message_id))
        except Exception as e:
            logger.warning("VK Teams edit failed: %s", e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        try:
            await self._api_get(
                "/messages/deleteMessages", {"chatId": chat_id, "msgId": message_id}
            )
            return True
        except Exception as e:
            logger.warning("VK Teams delete failed: %s", e)
            return False

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        now = time.monotonic()
        if now - self._typing_sent_at.get(chat_id, 0.0) < 4.0:
            return
        self._typing_sent_at[chat_id] = now
        try:
            await self._api_get(
                "/chats/sendActions", {"chatId": chat_id, "actions": "typing"}
            )
        except Exception:
            pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        try:
            data = await self._api_get("/chats/getInfo", {"chatId": chat_id})
            return {
                "name": f"{data.get('firstName', '')} {data.get('lastName', '')}".strip(),
                "type": "dm" if data.get("type") == "private" else str(data.get("type", "dm")),
            }
        except Exception as e:
            logger.warning("VK Teams get_chat_info failed: %s", e)
            return {"name": chat_id, "type": "dm"}

    def format_message(self, content: str) -> str:
        """VK Teams renders format-styles natively; keep text as-is."""
        return content


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def check_requirements() -> bool:
    if not os.getenv("VKTEAMS_TOKEN"):
        return False
    try:
        import httpx  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    token = os.getenv("VKTEAMS_TOKEN", "") or extra.get("token", "")
    return bool(token)


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> Optional[Dict[str, Any]]:
    token = os.getenv("VKTEAMS_TOKEN", "")
    if not token:
        return None
    return {"token": token}


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="vkteams",
        label="VK Teams (Среда)",
        adapter_factory=lambda cfg: VkTeamsAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["VKTEAMS_TOKEN"],
        install_hint="pip install httpx   # VK Teams adapter requires httpx",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="VKTEAMS_HOME_CHANNEL",
        allowed_users_env="VKTEAMS_ALLOWED_USERS",
        allow_all_env="VKTEAMS_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="💬",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via VK Teams (Среда). The adapter renders "
            "markdown formatting (bold, italic, links, quotes, lists, code "
            "blocks) natively. Keep responses conversational."
        ),
    )