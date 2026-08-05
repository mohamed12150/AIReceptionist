# receptionist/messaging/channels/whatsapp.py
from __future__ import annotations

import logging
import os

import httpx

from receptionist.config import WhatsAppChannel as WhatsAppChannelConfig
from receptionist.messaging.models import Message, DispatchContext
from receptionist.messaging.retry import retry_with_backoff, RetryPolicy

logger = logging.getLogger("receptionist")

_CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
_TAWATUR_SEND_URL = "https://tawatur.cloud/api/v1/messages/send"

# CallMeBot returns HTTP 200 even for several failure modes and reports the
# problem in the response body instead. These lowercase markers cover the
# documented ones (bad/missing API key, unactivated phone). Matches are
# permanent failures — retrying will not fix a wrong key.
_PERMANENT_BODY_MARKERS = (
    "apikey is invalid",
    "apikey missing",
    "phone number indicated is not registered",
)


class _PermanentDeliveryError(Exception):
    """Configuration/auth failure — no retry."""


def _format_whatsapp_text(message: Message, context: DispatchContext) -> str:
    """Render a caller message as a short WhatsApp notification (Arabic)."""
    return (
        f"📞 رسالة جديدة — {context.business_name}\n"
        f"الاسم: {message.caller_name}\n"
        f"الرقم: {message.callback_number}\n"
        f"الرسالة: {message.message}"
    )


class WhatsAppChannel:
    """Sends a WhatsApp notification for each caller message via CallMeBot.

    The API key comes from the environment variable named in
    `config.apikey_env` — resolved per delivery so a key rotation doesn't
    require a process restart. A missing key is a permanent failure (logged
    and recorded in .failures/, never retried).

    `transport` is injectable for tests (httpx.MockTransport).
    """

    def __init__(
        self,
        config: WhatsAppChannelConfig,
        initial_delay: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.policy = RetryPolicy(max_attempts=3, initial_delay=initial_delay, factor=2.0)
        self._transport = transport

    async def deliver(self, message: Message, context: DispatchContext) -> None:
        apikey = os.environ.get(self.config.apikey_env, "").strip()
        if not apikey:
            raise _PermanentDeliveryError(
                f"WhatsApp channel: env var {self.config.apikey_env} is not set"
            )

        text = _format_whatsapp_text(message, context)
        if self.config.provider == "tawatur":
            send = lambda: self._send_tawatur(apikey, text)  # noqa: E731
        else:
            send = lambda: self._send_callmebot(apikey, text)  # noqa: E731

        await retry_with_backoff(
            send,
            self.policy,
            is_transient=lambda e: not isinstance(e, _PermanentDeliveryError),
        )

    async def _send_callmebot(self, apikey: str, text: str) -> None:
        params = {
            "phone": self.config.phone,
            "text": text,
            "apikey": apikey,
        }
        async with httpx.AsyncClient(
            timeout=15.0, transport=self._transport,
        ) as client:
            resp = await client.get(_CALLMEBOT_URL, params=params)
        body_lower = resp.text.lower()
        if any(marker in body_lower for marker in _PERMANENT_BODY_MARKERS):
            # Do NOT include the body in the error — it may echo the key.
            raise _PermanentDeliveryError(
                "CallMeBot rejected the request (invalid/missing API key "
                "or unactivated phone)"
            )
        if resp.status_code >= 400:
            # 4xx/5xx without a known permanent marker: treat as transient
            # (CallMeBot occasionally 503s under load).
            raise RuntimeError(f"CallMeBot HTTP {resp.status_code}")
        logger.info(
            "WhatsAppChannel sent to %s via callmebot", self.config.phone,
            extra={"component": "messaging.channels.whatsapp"},
        )

    async def _send_tawatur(self, token: str, text: str) -> None:
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Workspace-Id": self.config.workspace_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = {
            "whatsapp_account_id": self.config.whatsapp_account_id,
            "to": self.config.phone,
            "type": "text",
            "text": text,
        }
        async with httpx.AsyncClient(
            timeout=15.0, transport=self._transport,
        ) as client:
            resp = await client.post(_TAWATUR_SEND_URL, json=body, headers=headers)
        if resp.status_code in {401, 403, 404, 422}:
            # Bad token, wrong workspace/account id, or invalid payload —
            # retrying cannot fix these. Status code only; never echo the body
            # (it may contain identifiers).
            raise _PermanentDeliveryError(f"tawatur rejected: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise RuntimeError(f"tawatur HTTP {resp.status_code}")
        logger.info(
            "WhatsAppChannel sent to %s via tawatur", self.config.phone,
            extra={"component": "messaging.channels.whatsapp"},
        )
