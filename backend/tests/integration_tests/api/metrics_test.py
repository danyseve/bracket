"""Observabilidad HTTP del backend (S3.8b, F3B).

Fija lo que /api/metrics debe permitir ver sin inspeccionar cuerpos ni cabeceras: clase de estado
por ruta, latencia en histograma, ausencia de identificadores en las etiquetas y los codigos de
error de F3 como series separadas (503 F3_WRITE_DISABLED frente a 409 del rechazo por revision).
"""

# pylint: disable=redefined-outer-name
from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any, Final
from uuid import uuid4

import aiohttp
import pytest
import pytest_asyncio
from starlette import status

from bracket.config import config
from bracket.routes.domain_idempotency import IDEMPOTENCY_KEY_HEADER
from bracket.utils.http import HTTPMethod
from tests.integration_tests.api.generate_bracket_http_support import (
    SYNTHETIC_SECRET,
    GenerationLab,
    generation_lab_context,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url, send_request_raw

LOGGER_NAME: Final = "bracket"


@pytest.fixture(scope="module", autouse=True)
def idempotency_secret() -> Iterator[None]:
    """Clave HMAC sintetica del laboratorio: sin ella las escrituras fallan cerrado."""
    patcher = pytest.MonkeyPatch()
    patcher.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    patcher.setattr(config, "idempotency_hmac_key_version", "v1")
    yield
    patcher.undo()


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def lab() -> AsyncIterator[GenerationLab]:
    async with generation_lab_context() as generation_lab:
        yield generation_lab


async def call(
    method: str,
    path: str,
    *,
    token: str | None = None,
    key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Peticion cruda contra el servidor de pruebas; la sesion es de la peticion."""
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if key is not None:
        headers[IDEMPOTENCY_KEY_HEADER] = key
    async with aiohttp.ClientSession() as session:
        async with session.request(
            method, get_root_uvicorn_url() + path, headers=headers, json=payload
        ) as response:
            text = await response.text()
            content_type = response.headers.get("content-type", "")
            assert content_type.startswith("application/json"), text
            return response.status, json.loads(text)


async def fetch_metrics() -> str:
    return await send_request_raw(HTTPMethod.GET, "metrics")


@pytest.mark.asyncio(loop_scope="session")
async def test_metrics_endpoint(startup_and_shutdown_uvicorn_server: None) -> None:
    text_response = await fetch_metrics()
    assert "HELP bracket_response_time" in text_response


@pytest.mark.asyncio(loop_scope="session")
async def test_status_class_is_observable_and_labels_carry_no_ids(
    startup_and_shutdown_uvicorn_server: None,
) -> None:
    """2xx y 4xx por ruta; un identificador real en la URL no aparece como etiqueta."""
    await send_request_raw(HTTPMethod.GET, "ping")
    await send_request_raw(HTTPMethod.GET, "tournaments")
    await send_request_raw(HTTPMethod.GET, "tournaments/1")
    text = await fetch_metrics()

    assert 'bracket_request_count{url="/ping",method="GET",status_class="2xx"}' in text
    assert 'bracket_request_count{url="/tournaments",method="GET",status_class="4xx"}' in text
    assert (
        'bracket_request_count{url="/tournaments/{tournament_id}",method="GET",status_class="4xx"}'
        in text
    )
    assert 'bracket_response_time_seconds_bucket{url="/ping",method="GET",le="+Inf"}' in text
    assert 'bracket_response_time_seconds_count{url="/ping",method="GET"} ' in text
    assert re.search(r'url="/tournaments/[0-9]+"', text) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_write_disabled_is_observable_and_separate_from_lock_timeout(
    lab: GenerationLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """503 F3_WRITE_DISABLED deja su propia serie, sin confundirse con un 503 de bloqueo."""
    monkeypatch.setattr(config, "f3_write_enabled", False)
    route = f"tournaments/{lab.tournament}/registration-window"

    status_code, body = await call(
        "POST",
        route,
        token=lab.owner_token,
        key=uuid4().hex,
        payload={"registration_deadline": None},
    )

    assert status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert body == {"detail": "F3_WRITE_DISABLED"}

    text = await fetch_metrics()
    template = "/tournaments/{tournament_id}/registration-window"
    assert f'bracket_request_count{{url="{template}",method="POST",status_class="5xx"}}' in text
    assert f'bracket_error_total{{route="{template}",code="F3_WRITE_DISABLED"}} ' in text
    assert f'bracket_error_total{{route="{template}",code="LOCK_TIMEOUT"}}' not in text


@pytest.mark.asyncio(loop_scope="session")
async def test_rejection_by_readiness_is_observable_without_secrets_in_logs(
    lab: GenerationLab, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """El 409 se cuenta por codigo; ni la clave de idempotencia ni el token van al log."""
    monkeypatch.setattr(config, "f3_write_enabled", True)
    key = uuid4().hex

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        status_code, body = await call(
            "POST",
            f"tournaments/{lab.tournament}/generate-bracket",
            token=lab.owner_token,
            key=key,
        )

    assert status_code == status.HTTP_409_CONFLICT
    assert body["detail"]["code"] == "BRACKET_NOT_READY"

    text = await fetch_metrics()
    template = "/tournaments/{tournament_id}/generate-bracket"
    assert f'bracket_error_total{{route="{template}",code="BRACKET_NOT_READY"}} ' in text
    assert f'bracket_request_count{{url="{template}",method="POST",status_class="4xx"}}' in text

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "code=BRACKET_NOT_READY" in log_text
    assert key not in log_text
    assert lab.owner_token not in log_text
