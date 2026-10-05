# pylint: disable=redefined-outer-name  # `protocol_data` y `protocol_server` son fixtures de modulo.
"""S3.3c-4 — adaptador HTTP del protocolo sobre una aplicacion **de prueba** aislada.

Verifica el comportamiento proximo a HTTP (identidad, cabecera, cuerpo crudo, codigos de estado y
repeticion) con un servidor real, pero sobre una ``FastAPI`` que se construye **aqui**: no se registra
ninguna ruta en la aplicacion productiva (lo comprueba ``unit_tests/idempotency_protocol_test.py``).

Dos detalles deliberados de la aplicacion de prueba, que la capa que publique rutas F3 tendra que
replicar:

* se registra un manejador de ``HTTPException`` que **conserva** ``exc.headers`` (``Retry-After``);
  el manejador global de ``app.py`` todavia no lo hace;
* la ruta lee el cuerpo crudo de :func:`idempotency_request` para construir el payload de la huella,
  y solo despues lo interpreta como JSON.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from databases import Database
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from bracket.config import config
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
from tests.integration_tests.idempotency_fixtures import SYNTHETIC_SECRET
from tests.integration_tests.mocks import get_mock_token
from tests.integration_tests.registration_fixtures import (
    RegistrationData,
    registration_data_context,
)

TEST_ROUTE = "/clubs/{club_id}/protocol-test/competitors"
INVALID_KEY = "con espacios"


def build_test_app() -> FastAPI:
    """Aplicacion **de prueba**: el adaptador no se registra en la aplicacion productiva."""
    test_app = FastAPI()

    @test_app.exception_handler(HTTPException)
    async def _handle_http_exception(_request: Request, exception: HTTPException) -> JSONResponse:
        return JSONResponse(
            {"detail": exception.detail},
            status_code=exception.status_code,
            headers=exception.headers,
        )

    @test_app.post(TEST_ROUTE)
    async def _create_competitor(
        context: ActorContext = Depends(actor_context_for_club),
        idempotency: ProtocolRequest = Depends(idempotency_request),
        owner_only: bool = False,
    ) -> JSONResponse:
        try:
            body = json.loads(idempotency.payload)
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=400, detail="INVALID_BODY") from error
        display_name = str(body.get("display_name", ""))

        async def operation() -> OperationOutcome:
            competitor = await create_competitor(context, display_name=display_name)
            return OperationOutcome(
                response_status=201,
                metadata={"resource_id": int(competitor.id), "resource_type": "competitor"},
                resource_type="competitor",
                resource_id=int(competitor.id),
            )

        result = await execute_idempotent_operation(
            context=context,
            request=idempotency,
            operation=operation,
            require_owner=owner_only,
        )
        return JSONResponse(status_code=result.response_status, content=result.response_body or {})

    return test_app


@dataclass(frozen=True)
class ProtocolResponse:
    status: int
    body: dict[str, Any]
    retry_after: str | None


async def post_competitor(
    port: int,
    club_id: int,
    *,
    token: str | None,
    key: str | None,
    payload: str,
    owner_only: bool = False,
) -> ProtocolResponse:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if key is not None:
        headers[IDEMPOTENCY_KEY_HEADER] = key
    endpoint = TEST_ROUTE.format(club_id=club_id)
    if owner_only:
        endpoint = f"{endpoint}?owner_only=true"

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url=f"http://127.0.0.1:{port}{endpoint}", data=payload, headers=headers
        ) as response:
            text = await response.text()
            parsed = json.loads(text) if text else {}
            return ProtocolResponse(
                status=response.status,
                body=parsed if isinstance(parsed, dict) else {"data": parsed},
                retry_after=response.headers.get("Retry-After"),
            )


@pytest_asyncio.fixture(loop_scope="session", scope="module")
async def protocol_server() -> AsyncIterator[int]:
    """Servidor HTTP de prueba (aplicacion aislada, puerto libre)."""
    port = find_free_port()
    server = UvicornTestServer(_app=build_test_app(), port=port)
    await server.up()
    try:
        yield port
    finally:
        await server.down()


@pytest_asyncio.fixture(loop_scope="session")
async def protocol_data(
    monkeypatch: pytest.MonkeyPatch, reinit_database: Database
) -> AsyncIterator[RegistrationData]:
    monkeypatch.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    monkeypatch.setattr(config, "idempotency_hmac_key_version", "v1")

    async with registration_data_context(reinit_database) as data:
        async with AsyncExitStack() as stack:
            stack.push_async_callback(_delete_created, data.tenant_a)
            yield data


async def _delete_created(tenant_club_id: int) -> None:
    await database.execute(
        query="DELETE FROM domain_change_log WHERE tenant_club_id = :c", values={"c": tenant_club_id}
    )
    await database.execute(
        query="DELETE FROM competitors WHERE managed_by_club_id = :c", values={"c": tenant_club_id}
    )
    await database.execute(
        query="DELETE FROM domain_idempotency_keys WHERE tenant_club_id = :c",
        values={"c": tenant_club_id},
    )


async def _token_for(user_id: int) -> str:
    email = await database.fetch_val(
        query="SELECT email FROM users WHERE id = :u", values={"u": user_id}
    )
    return get_mock_token(str(email))


async def _competitors(tenant_club_id: int) -> int:
    return int(
        await database.fetch_val(
            query="SELECT count(*) FROM competitors WHERE managed_by_club_id = :c",
            values={"c": tenant_club_id},
        )
    )


async def _audit_events(tenant_club_id: int) -> int:
    return int(
        await database.fetch_val(
            query=(
                "SELECT count(*) FROM domain_change_log "
                "WHERE tenant_club_id = :c AND entity = 'competitor'"
            ),
            values={"c": tenant_club_id},
        )
    )


async def test_sin_cabecera_de_clave_responde_400(
    protocol_server: int, protocol_data: RegistrationData
) -> None:
    response = await post_competitor(
        protocol_server,
        int(protocol_data.tenant_a),
        token=await _token_for(protocol_data.context_owner_a.actor_user_id),
        key=None,
        payload='{"display_name":"Sin Clave"}',
    )

    assert response.status == 400
    assert response.body["detail"] == "IDEMPOTENCY_REQUEST_INVALID"
    assert await _competitors(protocol_data.tenant_a) == 0


async def test_clave_con_formato_invalido_responde_400(
    protocol_server: int, protocol_data: RegistrationData
) -> None:
    response = await post_competitor(
        protocol_server,
        int(protocol_data.tenant_a),
        token=await _token_for(protocol_data.context_owner_a.actor_user_id),
        key=INVALID_KEY,
        payload='{"display_name":"Clave Invalida"}',
    )

    assert response.status == 400
    assert await _competitors(protocol_data.tenant_a) == 0


async def test_sin_token_responde_401(
    protocol_server: int, protocol_data: RegistrationData
) -> None:
    response = await post_competitor(
        protocol_server,
        int(protocol_data.tenant_a),
        token=None,
        key="protocol-http-key-sin-token",
        payload='{"display_name":"Sin Token"}',
    )

    assert response.status == 401


async def test_actor_sin_relacion_con_el_club_responde_401(
    protocol_server: int, protocol_data: RegistrationData
) -> None:
    response = await post_competitor(
        protocol_server,
        int(protocol_data.tenant_a),
        token=await _token_for(protocol_data.context_owner_b.actor_user_id),
        key="protocol-http-key-ajeno",
        payload='{"display_name":"Ajeno"}',
    )

    assert response.status == 401
    assert await _competitors(protocol_data.tenant_a) == 0


async def test_primera_peticion_y_repeticion_por_http(
    protocol_server: int, protocol_data: RegistrationData
) -> None:
    club_id = int(protocol_data.tenant_a)
    token = await _token_for(protocol_data.context_owner_a.actor_user_id)
    key = "protocol-http-key-replay"
    payload = '{"display_name":"Ana HTTP"}'

    first = await post_competitor(protocol_server, club_id, token=token, key=key, payload=payload)
    second = await post_competitor(protocol_server, club_id, token=token, key=key, payload=payload)

    assert first.status == 201
    assert second.status == 201
    assert second.body == first.body
    assert await _competitors(protocol_data.tenant_a) == 1
    assert await _audit_events(protocol_data.tenant_a) == 1


async def test_misma_clave_con_otro_cuerpo_responde_409(
    protocol_server: int, protocol_data: RegistrationData
) -> None:
    club_id = int(protocol_data.tenant_a)
    token = await _token_for(protocol_data.context_owner_a.actor_user_id)
    key = "protocol-http-key-conflicto"

    first = await post_competitor(
        protocol_server, club_id, token=token, key=key, payload='{"display_name":"Ana"}'
    )
    conflict = await post_competitor(
        protocol_server, club_id, token=token, key=key, payload='{"display_name":"Otra"}'
    )

    assert first.status == 201
    assert conflict.status == 409
    assert conflict.body["detail"] == "IDEMPOTENCY_KEY_REUSED"
    assert await _competitors(protocol_data.tenant_a) == 1


async def test_rol_insuficiente_responde_403_y_no_devuelve_lo_guardado(
    protocol_server: int, protocol_data: RegistrationData
) -> None:
    club_id = int(protocol_data.tenant_a)
    token = await _token_for(protocol_data.context_collaborator_a.actor_user_id)
    key = "protocol-http-key-rol"

    allowed = await post_competitor(
        protocol_server, club_id, token=token, key=key, payload='{"display_name":"Ana Rol"}'
    )
    denied = await post_competitor(
        protocol_server,
        club_id,
        token=token,
        key=key,
        payload='{"display_name":"Ana Rol"}',
        owner_only=True,
    )

    assert allowed.status == 201
    assert denied.status == 403
    assert denied.body["detail"] == "ROLE_NOT_ALLOWED"
    assert await _competitors(protocol_data.tenant_a) == 1


async def test_doble_clic_concurrente_por_http_un_solo_efecto(
    protocol_server: int, protocol_data: RegistrationData
) -> None:
    club_id = int(protocol_data.tenant_a)
    token = await _token_for(protocol_data.context_owner_a.actor_user_id)
    key = "protocol-http-key-doble-clic"
    payload = '{"display_name":"Ana Doble"}'

    responses = await asyncio.gather(
        post_competitor(protocol_server, club_id, token=token, key=key, payload=payload),
        post_competitor(protocol_server, club_id, token=token, key=key, payload=payload),
    )

    assert [response.status for response in responses] == [201, 201]
    assert responses[0].body == responses[1].body
    assert await _competitors(protocol_data.tenant_a) == 1
    assert await _audit_events(protocol_data.tenant_a) == 1


async def test_cuerpo_no_json_responde_400(
    protocol_server: int, protocol_data: RegistrationData
) -> None:
    response = await post_competitor(
        protocol_server,
        int(protocol_data.tenant_a),
        token=await _token_for(protocol_data.context_owner_a.actor_user_id),
        key="protocol-http-key-cuerpo",
        payload="no-es-json",
    )

    assert response.status == 400
