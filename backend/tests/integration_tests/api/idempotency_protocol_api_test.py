# pylint: disable=redefined-outer-name  # los fixtures vienen del modulo de pruebas del protocolo.
"""S3.3c-4 — adaptador HTTP del protocolo sobre una aplicacion **de prueba** aislada.

Verifica el comportamiento proximo a HTTP: la cabecera ``Idempotency-Key``, el rechazo con
codigo y forma estables, la repeticion de una respuesta perdida, el conflicto por cuerpo
distinto, la revalidacion de identidad/relacion/rol dentro de la peticion y la concurrencia real
de dos peticiones identicas.

La aplicacion de este modulo **no es la aplicacion productiva**: se construye aqui, declara una
unica ruta de prueba y no se registra en ``bracket/app.py`` (hay una prueba que lo comprueba).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from bracket.database import database
from bracket.logic.competitors import create_competitor
from bracket.logic.idempotency_protocol import OperationOutcome, ProtocolRequest
from bracket.models.db.domain import ActorContext
from bracket.routes.domain_auth import actor_context_for_club
from bracket.routes.domain_idempotency import (
    IDEMPOTENCY_KEY_HEADER,
    execute_idempotent_operation,
    idempotency_request,
)
from tests.integration_tests.api.shared import UvicornTestServer, find_free_port
from tests.integration_tests.idempotency_protocol_test import (
    count_audit_events,
    count_competitors,
    count_keys,
    lab_key,
    protocol_data,  # noqa: F401  (fixture: pytest la inyecta por nombre)
)
from tests.integration_tests.mocks import get_mock_token
from tests.integration_tests.registration_fixtures import RegistrationData

OWNER_NAME = "HTTP Protocolo"
TEST_PATH = "/clubs/{club_id}/protocol-test/competitors"
BODY = b'{"display_name":"HTTP Protocolo"}'
OTHER_BODY = b'{"display_name":"HTTP Otra Persona"}'


def build_test_app() -> FastAPI:
    """Aplicacion de prueba: adaptador real del protocolo, ruta de laboratorio."""
    app = FastAPI()

    @app.exception_handler(HTTPException)
    async def _http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        """Como tendra que hacer la capa que publique las rutas: conserva las cabeceras."""
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
        )

    @app.post(TEST_PATH)
    async def _create_competitor(
        context: ActorContext = Depends(actor_context_for_club),
        idempotency: ProtocolRequest = Depends(idempotency_request),
        owner_only: bool = False,
    ) -> JSONResponse:
        try:
            payload = json.loads(idempotency.payload)
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=400, detail="INVALID_BODY") from error
        display_name = str(payload.get("display_name", ""))

        async def operation() -> OperationOutcome:
            competitor = await create_competitor(context, display_name=display_name)
            return OperationOutcome(
                response_status=201,
                metadata={"resource_id": int(competitor.id), "resource_type": "competitor"},
                resource_type="competitor",
                resource_id=int(competitor.id),
            )

        result = await execute_idempotent_operation(
            context=context, request=idempotency, operation=operation, require_owner=owner_only
        )
        return JSONResponse(status_code=result.response_status, content=result.response_body or {})

    return app


@pytest_asyncio.fixture(loop_scope="session", scope="module")
async def protocol_server() -> AsyncIterator[str]:
    """Servidor HTTP real (uvicorn) con la aplicacion de prueba."""
    port = find_free_port()
    server = UvicornTestServer(_app=build_test_app(), port=port)
    await server.up()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await server.down()


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: dict[str, Any]
    retry_after: str | None


async def send(
    base_url: str,
    *,
    club_id: int,
    token: str | None,
    key: str | None,
    body: bytes = BODY,
    owner_only: bool = False,
) -> HttpResult:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if key is not None:
        headers[IDEMPOTENCY_KEY_HEADER] = key

    path = TEST_PATH.format(club_id=club_id)
    if owner_only:
        path = f"{path}?owner_only=true"

    async with aiohttp.ClientSession() as session:
        async with session.post(f"{base_url}{path}", data=body, headers=headers) as response:
            raw = await response.text()
            parsed = json.loads(raw) if raw else {}
            return HttpResult(
                status=response.status,
                body=parsed,
                retry_after=response.headers.get("Retry-After"),
            )


async def token_for(user_id: int) -> str:
    email = await database.fetch_val(
        query="SELECT email FROM users WHERE id = :user_id", values={"user_id": user_id}
    )
    return get_mock_token(str(email))


@dataclass(frozen=True)
class HttpProtocolLab:
    data: RegistrationData
    url: str


@pytest_asyncio.fixture(loop_scope="session")
async def http_protocol_data(
    protocol_data: RegistrationData,  # noqa: F811  (se reexpone para el fixture local)
    protocol_server: str,
) -> AsyncIterator[HttpProtocolLab]:
    """Los mismos datos y el mismo secreto sintetico que las pruebas del protocolo."""
    yield HttpProtocolLab(data=protocol_data, url=protocol_server)


@pytest.mark.asyncio(loop_scope="session")
async def test_sin_cabecera_de_clave_es_400(http_protocol_data: HttpProtocolLab) -> None:
    data = http_protocol_data.data
    token = await token_for(data.context_owner_a.actor_user_id)

    result = await send(
        http_protocol_data.url, club_id=data.tenant_a, token=token, key=None, body=BODY
    )

    assert result.status == 400
    assert result.body["detail"] == "IDEMPOTENCY_REQUEST_INVALID"
    assert await count_competitors(data.tenant_a, OWNER_NAME) == 0
    assert await count_keys(data.tenant_a) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_clave_con_formato_invalido_es_400(http_protocol_data: HttpProtocolLab) -> None:
    data = http_protocol_data.data
    token = await token_for(data.context_owner_a.actor_user_id)

    result = await send(
        http_protocol_data.url, club_id=data.tenant_a, token=token, key="corta", body=BODY
    )

    assert result.status == 400
    assert result.body["detail"] == "IDEMPOTENCY_REQUEST_INVALID"


@pytest.mark.asyncio(loop_scope="session")
async def test_sin_token_es_401(http_protocol_data: HttpProtocolLab) -> None:
    data = http_protocol_data.data

    result = await send(
        http_protocol_data.url, club_id=data.tenant_a, token=None, key=lab_key("http-sin-token")
    )

    assert result.status == 401
    assert await count_keys(data.tenant_a) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_actor_sin_relacion_con_el_club_es_401(http_protocol_data: HttpProtocolLab) -> None:
    data = http_protocol_data.data
    outsider = await token_for(data.context_outsider_a.actor_user_id)

    result = await send(
        http_protocol_data.url, club_id=data.tenant_a, token=outsider, key=lab_key("http-ajeno")
    )

    assert result.status == 401
    assert await count_keys(data.tenant_a) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_primera_peticion_201_y_repeticion_identica(
    http_protocol_data: HttpProtocolLab,
) -> None:
    data = http_protocol_data.data
    token = await token_for(data.context_owner_a.actor_user_id)
    key = lab_key("http-primera")

    first = await send(
        http_protocol_data.url, club_id=data.tenant_a, token=token, key=key, body=BODY
    )
    replay = await send(
        http_protocol_data.url, club_id=data.tenant_a, token=token, key=key, body=BODY
    )

    assert first.status == 201
    assert replay.status == 201
    assert replay.body == first.body
    assert await count_competitors(data.tenant_a, OWNER_NAME) == 1
    assert await count_audit_events(data.tenant_a, "competitor", "CREATE") == 1
    assert await count_keys(data.tenant_a) == 1

    stored = await database.fetch_one(
        query="""SELECT state, response_status FROM domain_idempotency_keys
                 WHERE tenant_club_id = :tenant_club_id AND idempotency_key = :idempotency_key""",
        values={"tenant_club_id": data.tenant_a, "idempotency_key": key},
    )
    assert stored is not None
    assert stored["state"] == "COMPLETED"
    assert stored["response_status"] == 201


@pytest.mark.asyncio(loop_scope="session")
async def test_misma_clave_con_otro_cuerpo_es_409(http_protocol_data: HttpProtocolLab) -> None:
    data = http_protocol_data.data
    token = await token_for(data.context_owner_a.actor_user_id)
    key = lab_key("http-conflicto")

    first = await send(
        http_protocol_data.url, club_id=data.tenant_a, token=token, key=key, body=BODY
    )
    conflict = await send(
        http_protocol_data.url, club_id=data.tenant_a, token=token, key=key, body=OTHER_BODY
    )

    assert first.status == 201
    assert conflict.status == 409
    assert conflict.body["detail"] == "IDEMPOTENCY_KEY_REUSED"
    assert await count_competitors(data.tenant_a, OWNER_NAME) == 1
    assert await count_competitors(data.tenant_a, "HTTP Otra Persona") == 0
    assert await count_keys(data.tenant_a) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_owner_obligatorio_en_la_ruta(http_protocol_data: HttpProtocolLab) -> None:
    data = http_protocol_data.data
    collaborator = await token_for(data.context_collaborator_a.actor_user_id)
    key = lab_key("http-rol")

    allowed = await send(
        http_protocol_data.url, club_id=data.tenant_a, token=collaborator, key=key, body=BODY
    )
    denied = await send(
        http_protocol_data.url,
        club_id=data.tenant_a,
        token=collaborator,
        key=key,
        body=BODY,
        owner_only=True,
    )

    assert allowed.status == 201
    assert denied.status == 403
    assert denied.body["detail"] == "ROLE_NOT_ALLOWED"


@pytest.mark.asyncio(loop_scope="session")
async def test_dos_peticiones_identicas_concurrentes_un_solo_efecto(
    http_protocol_data: HttpProtocolLab,
) -> None:
    data = http_protocol_data.data
    token = await token_for(data.context_owner_a.actor_user_id)
    key = lab_key("http-concurrente")

    async def one() -> HttpResult:
        return await send(
            http_protocol_data.url, club_id=data.tenant_a, token=token, key=key, body=BODY
        )

    results = await asyncio.gather(one(), one())

    assert [result.status for result in results] == [201, 201]
    assert results[0].body == results[1].body
    assert await count_competitors(data.tenant_a, OWNER_NAME) == 1
    assert await count_audit_events(data.tenant_a, "competitor", "CREATE") == 1
    assert await count_keys(data.tenant_a) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_cuerpo_invalido_es_400(http_protocol_data: HttpProtocolLab) -> None:
    data = http_protocol_data.data
    token = await token_for(data.context_owner_a.actor_user_id)

    result = await send(
        http_protocol_data.url,
        club_id=data.tenant_a,
        token=token,
        key=lab_key("http-cuerpo"),
        body=b"no es json",
    )

    assert result.status == 400
    assert result.body["detail"] == "INVALID_BODY"
    assert await count_keys(data.tenant_a) == 0
