"""Web API for test-first custom OpenAI-compatible provider profiles."""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import time
import uuid
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.api.settings_routes import activate_custom_provider
from src.providers.custom_profiles import CustomProviderStore, STORE_LOCK

AuthDep = Callable[..., Awaitable[Any] | Any]
_TEST_TTL_S = 300.0
_PROVIDER_REQUEST_TIMEOUT_S = 60.0
_TESTS: dict[str, tuple[float, str]] = {}


def _build_store() -> CustomProviderStore:
    return CustomProviderStore()


class CustomProviderTestRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key: str = Field(min_length=1, max_length=500)


class CustomProviderSaveRequest(CustomProviderTestRequest):
    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=100)
    test_id: str = Field(min_length=1, max_length=80)


class CustomProviderActivateRequest(BaseModel):
    confirm: bool = False


def _validate_custom_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Provider base URL must be an HTTP(S) URL without embedded credentials")
    hostname = parsed.hostname.lower()
    if hostname in {"localhost", "localhost.localdomain", "metadata.google.internal"}:
        raise ValueError("Provider base URL cannot target local or metadata services")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise ValueError("Provider base URL cannot target local or private network addresses")
    if normalized.endswith("/chat/completions"):
        normalized = normalized[:-len("/chat/completions")]
    return normalized


def _reject_private_dns_targets(base_url: str) -> None:
    hostname = urlsplit(base_url).hostname
    if not hostname:
        raise ValueError("Provider base URL must include a hostname")
    try:
        results = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("Provider hostname could not be resolved") from exc
    for result in results:
        address = ipaddress.ip_address(result[4][0])
        if (
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise ValueError("Provider hostname resolves to a local or private network address")


def _completion_url(base_url: str) -> str:
    url = _validate_custom_base_url(base_url)
    if url.endswith("/chat/completions"):
        return url
    return f"{url}/chat/completions"


async def _perform_completion_test(*, base_url: str, model: str, api_key: str) -> dict[str, Any]:
    started = time.perf_counter()
    normalized_base_url = _validate_custom_base_url(base_url)
    import asyncio

    await asyncio.to_thread(_reject_private_dns_targets, normalized_base_url)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: PROVIDER_TEST_OK"}],
        "max_tokens": 32,
        "temperature": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=_PROVIDER_REQUEST_TIMEOUT_S, follow_redirects=False) as client:
            response = await client.post(_completion_url(normalized_base_url), headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"Provider returned HTTP {exc.response.status_code}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError("Provider test failed: endpoint did not return valid JSON") from exc
    try:
        content = payload["choices"][0]["message"].get("content", "")
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Provider test failed: response had no chat completion") from exc
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Provider test failed: response had no text")
    return {
        "response_preview": str(content).replace(api_key, "[redacted]").strip()[:500],
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


def _fingerprint(payload: CustomProviderTestRequest) -> str:
    raw = "\x00".join((payload.base_url.strip(), payload.model.strip(), payload.api_key.strip()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prune_tests() -> None:
    now = time.monotonic()
    for test_id, (expires, _) in list(_TESTS.items()):
        if expires <= now:
            _TESTS.pop(test_id, None)


def _activate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return activate_custom_provider(profile, store=_build_store())


def register_custom_provider_routes(
    app: FastAPI,
    require_local_or_auth: AuthDep,
    require_settings_write_auth: AuthDep,
) -> None:
    @app.get("/settings/custom-providers", dependencies=[Depends(require_local_or_auth)])
    def list_custom_providers() -> dict[str, Any]:
        return {"status": "ok", "providers": _build_store().list_public()}

    @app.post("/settings/custom-providers/test", dependencies=[Depends(require_settings_write_auth)])
    async def test_custom_provider(payload: CustomProviderTestRequest) -> dict[str, Any]:
        try:
            payload.base_url = _validate_custom_base_url(payload.base_url)
            if not payload.model.strip() or not payload.api_key.strip():
                raise ValueError("Model and API key must not be blank")
            result = await _perform_completion_test(
                base_url=payload.base_url, model=payload.model, api_key=payload.api_key
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _prune_tests()
        test_id = uuid.uuid4().hex
        _TESTS[test_id] = (time.monotonic() + _TEST_TTL_S, _fingerprint(payload))
        return {"status": "ok", "test_id": test_id, **result}

    @app.post("/settings/custom-providers", dependencies=[Depends(require_settings_write_auth)])
    def save_custom_provider(payload: CustomProviderSaveRequest) -> dict[str, Any]:
        with STORE_LOCK:
            _prune_tests()
            try:
                payload.base_url = _validate_custom_base_url(payload.base_url)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            expected = _TESTS.get(payload.test_id)
            if expected is None or expected[0] <= time.monotonic() or expected[1] != _fingerprint(payload):
                raise HTTPException(status_code=409, detail="A successful provider test is required before saving")
            try:
                base_url = _validate_custom_base_url(payload.base_url)
                profile = _build_store().save(
                    profile_id=payload.id,
                    label=payload.label,
                    base_url=base_url,
                    model=payload.model,
                    api_key=payload.api_key,
                )
            except (RuntimeError, ValueError, OSError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            _TESTS.pop(payload.test_id, None)
            return {"status": "ok", "provider": profile}

    @app.post("/settings/custom-providers/{profile_id}/activate", dependencies=[Depends(require_settings_write_auth)])
    def activate_custom_provider(profile_id: str, payload: CustomProviderActivateRequest) -> dict[str, Any]:
        if not payload.confirm:
            raise HTTPException(status_code=400, detail="Explicit confirmation is required to activate a provider")
        try:
            with STORE_LOCK:
                profile = _build_store().get(profile_id)
                return _activate_profile(profile)
        except (RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
