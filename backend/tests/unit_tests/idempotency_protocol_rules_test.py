"""S3.3c-4 — invariantes del protocolo de idempotencia (sin base de datos).

Comprueba lo que no depende de PostgreSQL: el catalogo de codigos, la lista blanca de metadatos, el
fallo cerrado cuando falta el secreto HMAC y que el protocolo **no** publica rutas.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import get_type_hints

import pytest

import bracket.logic.idempotency_protocol as protocol
from bracket.config import config
from bracket.logic.idempotency_protocol import (
    IdempotencyNotConfiguredError,
    IdempotencyProtocolError,
    IdempotencyRequestInvalidError,
    IdempotencyStateError,
    OperationOutcome,
    ProtocolRequest,
    run_idempotent_operation,
)
from bracket.models.db.domain import ActorContext
from bracket.routes.domain_idempotency import protocol_http_exception
from bracket.utils.id_types import ClubId, UserId

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_MODULE = BACKEND_ROOT / "bracket" / "logic" / "idempotency_protocol.py"
ADAPTER_MODULE = BACKEND_ROOT / "bracket" / "routes" / "domain_idempotency.py"


def _protocol_error_classes() -> list[type[IdempotencyProtocolError]]:
    return [
        member
        for _name, member in inspect.getmembers(protocol, inspect.isclass)
        if issubclass(member, IdempotencyProtocolError) and member is not IdempotencyProtocolError
    ]


def _context() -> ActorContext:
    return ActorContext(tenant_club_id=ClubId(1), actor_user_id=UserId(1), actor_label="unit")


def _request(*, key: str | None) -> ProtocolRequest:
    return ProtocolRequest(
        method="POST", path="/clubs/1/competitors", payload=b"{}", idempotency_key=key
    )


def test_el_catalogo_http_cubre_exactamente_los_codigos_del_protocolo() -> None:
    """Un codigo nuevo sin decision explicita no puede colarse como 500 silencioso."""
    from bracket.routes.domain_idempotency import _ERROR_HTTP

    codes = {error.code for error in _protocol_error_classes()}
    assert codes == set(_ERROR_HTTP), codes ^ set(_ERROR_HTTP)


def test_los_codigos_reintentables_anuncian_retry_after() -> None:
    from bracket.routes.domain_idempotency import _ERROR_HTTP

    for error in _protocol_error_classes():
        status_code, retry_after = _ERROR_HTTP[error.code]
        assert (retry_after is not None) is error.retryable, error.code
        assert retry_after is None or retry_after > 0
        assert 400 <= status_code <= 599, error.code


def test_un_codigo_desconocido_no_puede_convertirse_en_exito() -> None:
    class _Invented(IdempotencyProtocolError):
        code = "NO_EXISTE"

    response = protocol_http_exception(_Invented("codigo sin decision"))
    assert response.status_code == 500
    assert response.headers is None


def test_limites_documentados() -> None:
    """Los valores citados en docs/26 son los que ejecuta el codigo."""
    assert protocol.LOCK_TIMEOUT_MS == 2000
    assert protocol.STATEMENT_TIMEOUT_MS == 5000
    assert protocol.RETRY_AFTER_SECONDS == 1
    assert protocol.LOCK_TIMEOUT_MS < protocol.STATEMENT_TIMEOUT_MS


def test_clave_invalida_no_repite_el_valor_recibido() -> None:
    for key in (None, "", "corta", "con espacios", "con/slash", "x" * 300):
        with pytest.raises(IdempotencyRequestInvalidError) as rejected:
            protocol._validated_key(_request(key=key))
        assert rejected.value.code == "IDEMPOTENCY_REQUEST_INVALID"
        if isinstance(key, str) and key:
            assert key not in str(rejected.value)


def test_metadatos_fuera_de_la_lista_blanca_son_error_de_invariante() -> None:
    with pytest.raises(IdempotencyStateError):
        protocol._completion_from(
            OperationOutcome(response_status=201, metadata={"respuesta_http_completa": {"a": 1}})
        )

    completion = protocol._completion_from(
        OperationOutcome(response_status=201, metadata={"resource_id": 7})
    )
    assert completion.response_status == 201
    assert completion.response_body == {"resource_id": 7}


async def test_sin_secreto_hmac_falla_cerrado_sin_ejecutar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin clave HMAC no hay huella ni ejecucion: el fallo es cerrado y previo a la transaccion."""
    monkeypatch.setattr(config, "idempotency_hmac_key", None)
    executed = False

    async def operation() -> OperationOutcome:
        nonlocal executed
        executed = True
        return OperationOutcome(response_status=201)

    with pytest.raises(IdempotencyNotConfiguredError) as rejected:
        await run_idempotent_operation(
            context=_context(),
            request=_request(key="unit-clave-valida"),
            operation=operation,
        )

    assert rejected.value.code == "IDEMPOTENCY_NOT_CONFIGURED"
    assert executed is False


def test_el_protocolo_no_publica_rutas_en_la_aplicacion_productiva() -> None:
    """El protocolo y su adaptador no declaran rutas ni se registran en la aplicacion."""
    for module in (PROTOCOL_MODULE, ADAPTER_MODULE):
        source = module.read_text(encoding="utf-8")
        assert "APIRouter" not in source, module
        assert "include_router" not in source, module
        assert "@app." not in source, module

    offenders = sorted(
        str(path.relative_to(BACKEND_ROOT))
        for path in (BACKEND_ROOT / "bracket").rglob("*.py")
        if path != ADAPTER_MODULE
        and "routes.domain_idempotency" in path.read_text(encoding="utf-8")
    )
    assert offenders == [], offenders


def test_la_firma_del_coordinador_exige_contexto_y_peticion() -> None:
    """El cliente no puede elegir tenant, actor ni estado: se pasan resueltos y keyword-only."""
    signature = inspect.signature(run_idempotent_operation)
    assert signature.parameters["context"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["request"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "state" not in signature.parameters
    assert signature.parameters["require_owner"].default is False
    hints = get_type_hints(run_idempotent_operation)
    assert hints["context"] is ActorContext
    assert hints["request"] is ProtocolRequest
