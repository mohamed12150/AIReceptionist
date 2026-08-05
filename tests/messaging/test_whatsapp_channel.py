# tests/messaging/test_whatsapp_channel.py
from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from receptionist.config import WhatsAppChannel as WhatsAppChannelConfig
from receptionist.messaging.channels.whatsapp import (
    WhatsAppChannel,
    _format_whatsapp_text,
    _PermanentDeliveryError,
)
from receptionist.messaging.models import DispatchContext, Message


def _cfg(**overrides) -> WhatsAppChannelConfig:
    data = {"type": "whatsapp", "phone": "+249912345678"}
    data.update(overrides)
    return WhatsAppChannelConfig.model_validate(data)


def _message() -> Message:
    return Message(
        caller_name="محمد أحمد",
        callback_number="+249911111111",
        message="أريد موعداً غداً",
        business_name="عيادة النور",
    )


def _context() -> DispatchContext:
    return DispatchContext(business_name="عيادة النور", call_id="call-1")


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_config_accepts_international_phone():
    cfg = _cfg(phone="+249912345678")
    assert cfg.phone == "+249912345678"
    assert cfg.provider == "callmebot"
    assert cfg.apikey_env == "CALLMEBOT_APIKEY"


def test_config_rejects_non_numeric_phone():
    with pytest.raises(ValidationError):
        _cfg(phone="not-a-phone")


def test_config_rejects_unknown_provider():
    with pytest.raises(ValidationError):
        _cfg(provider="twilio")


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def test_format_includes_all_fields():
    text = _format_whatsapp_text(_message(), _context())
    assert "عيادة النور" in text
    assert "محمد أحمد" in text
    assert "+249911111111" in text
    assert "أريد موعداً غداً" in text


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deliver_success(monkeypatch):
    monkeypatch.setenv("CALLMEBOT_APIKEY", "123456")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, text="Message queued.")

    channel = WhatsAppChannel(
        _cfg(), initial_delay=0.0, transport=httpx.MockTransport(handler),
    )
    await channel.deliver(_message(), _context())

    assert seen["params"]["phone"] == "+249912345678"
    assert seen["params"]["apikey"] == "123456"
    assert "محمد أحمد" in seen["params"]["text"]


@pytest.mark.asyncio
async def test_deliver_missing_apikey_is_permanent(monkeypatch):
    monkeypatch.delenv("CALLMEBOT_APIKEY", raising=False)
    channel = WhatsAppChannel(_cfg(), initial_delay=0.0)
    with pytest.raises(_PermanentDeliveryError):
        await channel.deliver(_message(), _context())


@pytest.mark.asyncio
async def test_deliver_invalid_apikey_body_is_permanent_no_retry(monkeypatch):
    monkeypatch.setenv("CALLMEBOT_APIKEY", "wrong")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="APIKey is invalid.")

    channel = WhatsAppChannel(
        _cfg(), initial_delay=0.0, transport=httpx.MockTransport(handler),
    )
    with pytest.raises(_PermanentDeliveryError):
        await channel.deliver(_message(), _context())
    assert calls["n"] == 1  # permanent error must not be retried


@pytest.mark.asyncio
async def test_deliver_5xx_retries_then_succeeds(monkeypatch):
    monkeypatch.setenv("CALLMEBOT_APIKEY", "123456")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text="Message queued.")

    channel = WhatsAppChannel(
        _cfg(), initial_delay=0.0, transport=httpx.MockTransport(handler),
    )
    await channel.deliver(_message(), _context())
    assert calls["n"] == 3


# ---------------------------------------------------------------------------
# tawatur provider
# ---------------------------------------------------------------------------

def _tawatur_cfg(**overrides) -> WhatsAppChannelConfig:
    data = {
        "type": "whatsapp",
        "provider": "tawatur",
        "phone": "+249912345678",
        "workspace_id": "01WORKSPACE",
        "whatsapp_account_id": "01ACCOUNT",
    }
    data.update(overrides)
    return WhatsAppChannelConfig.model_validate(data)


def test_tawatur_config_requires_ids():
    with pytest.raises(ValidationError):
        _tawatur_cfg(workspace_id=None)
    with pytest.raises(ValidationError):
        _tawatur_cfg(whatsapp_account_id=None)


def test_tawatur_default_apikey_env():
    assert _tawatur_cfg().apikey_env == "TAWATUR_API_TOKEN"
    # callmebot keeps its own default
    assert _cfg().apikey_env == "CALLMEBOT_APIKEY"


@pytest.mark.asyncio
async def test_tawatur_deliver_success(monkeypatch):
    monkeypatch.setenv("TAWATUR_API_TOKEN", "tok.secret")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["workspace"] = request.headers.get("X-Workspace-Id")
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"data": {"message_id": "m1", "status": "queued"}},
        )

    channel = WhatsAppChannel(
        _tawatur_cfg(), initial_delay=0.0, transport=httpx.MockTransport(handler),
    )
    await channel.deliver(_message(), _context())

    assert seen["url"].endswith("/api/v1/messages/send")
    assert seen["auth"] == "Bearer tok.secret"
    assert seen["workspace"] == "01WORKSPACE"
    assert seen["body"]["whatsapp_account_id"] == "01ACCOUNT"
    assert seen["body"]["to"] == "+249912345678"
    assert seen["body"]["type"] == "text"
    assert "محمد أحمد" in seen["body"]["text"]


@pytest.mark.asyncio
async def test_tawatur_401_is_permanent_no_retry(monkeypatch):
    monkeypatch.setenv("TAWATUR_API_TOKEN", "bad")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"message": "Unauthenticated."})

    channel = WhatsAppChannel(
        _tawatur_cfg(), initial_delay=0.0, transport=httpx.MockTransport(handler),
    )
    with pytest.raises(_PermanentDeliveryError):
        await channel.deliver(_message(), _context())
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_tawatur_5xx_retries(monkeypatch):
    monkeypatch.setenv("TAWATUR_API_TOKEN", "tok")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"data": {"status": "queued"}})

    channel = WhatsAppChannel(
        _tawatur_cfg(), initial_delay=0.0, transport=httpx.MockTransport(handler),
    )
    await channel.deliver(_message(), _context())
    assert calls["n"] == 2
