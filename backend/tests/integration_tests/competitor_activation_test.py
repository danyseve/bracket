# pylint: disable=redefined-outer-name  # `activation_data` es el fixture de este modulo.
"""S2-bis de F3B — reactivacion de identidad de competidores (``activate_competitor``).

TDD: estas pruebas se escribieron y ejecutaron **antes** de anadir la accion ``ACTIVATE``,
``sql_activate_competitor`` y ``activate_competitor`` (rojo: el import falla y la auditoria
no admite la accion) y despues en verde con la implementacion. Ver el informe de la fase
para la evidencia literal de rojo/verde.

Contrato verificado (servicio interno, sin HTTP ni UI):

* el tenant y el actor llegan siempre en un :class:`ActorContext` explicito; ``managed_by_club_id``
  no se acepta desde fuera del contexto;
* la reactivacion exige relacion OWNER con el tenant (la baja logica y la reactivacion son las
  dos operaciones de estado sobre la identidad);
* competidor inexistente y competidor ajeno dan el **mismo** error (sin oraculo de existencia);
* es idempotente: reactivar una identidad activa no escribe estado ni auditoria;
* la reactivacion y su evento ``ACTIVATE`` se confirman o se revierten juntos;
* la baja y la reactivacion **no** tocan el historial de nombre ni los snapshots;
* ni los errores ni la auditoria contienen valores, solo nombres de campo (sin PII).

Validacion exclusivamente contra la base de laboratorio (``bracket_ci`` con ``ENVIRONMENT=CI``,
o ``bracket_test``); ``bracket_dev`` no se usa para escrituras.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import cast

import pytest
import pytest_asyncio
from databases import Database

from bracket.database import database
from bracket.logic.competitors import (
    CompetitorNotFoundError,
    InsufficientPrivilegesError,
    InvalidCompetitorDataError,
    TenantNotAuthorizedError,
    activate_competitor,
    create_competitor,
    deactivate_competitor,
)
from bracket.models.db.club import Club, ClubInsertable
from bracket.models.db.domain import ActorContext, Competitor
from bracket.models.db.user import UserInDB
from bracket.models.db.user_x_club import UserXClubInsertable, UserXClubRelation
from bracket.sql.domain_reads import get_competitor, get_competitor_name_history
from bracket.utils.dummy_records import DUMMY_CLUB
from bracket.utils.id_types import ClubId, CompetitorId, UserId
from tests.integration_tests.mocks import get_mock_user
from tests.integration_tests.sql import inserted_club, inserted_user, inserted_user_x_club

_INSERT_COMPETITOR_DIRECT = """
    INSERT INTO competitors (display_name, managed_by_club_id, active, created)
    VALUES (:display_name, :managed_by_club_id, true, NOW())
    RETURNING id, display_name, active, managed_by_club_id, created, updated_at
"""

_AUDIT_EVENTS = """
    SELECT entity, entity_id, action, changed_fields, actor_user_id, actor_label, reason,
        reason_code, reason_note, created
    FROM domain_change_log
    WHERE entity = 'competitor' AND entity_id = :entity_id
    ORDER BY id
"""

_DELETE_COMPETITORS_FOR_TENANTS = """
    DELETE FROM competitors WHERE managed_by_club_id = ANY(:tenant_ids)
"""

_DELETE_AUDIT_FOR_TENANTS = """
    DELETE FROM domain_change_log
    WHERE entity = 'competitor'
    AND entity_id IN (
        SELECT id FROM competitors WHERE managed_by_club_id = ANY(:tenant_ids)
    )
"""


async def _audit_events(competitor_id: CompetitorId) -> list[dict[str, object]]:
    records = await database.fetch_all(query=_AUDIT_EVENTS, values={"entity_id": competitor_id})
    return [dict(record._mapping) for record in records]


async def _insert_competitor_direct(*, display_name: str, managed_by_club_id: ClubId) -> Competitor:
    """Inserta un competidor de otro tenant saltandose el servicio (escenario de acceso cruzado)."""
    record = await database.fetch_one(
        query=_INSERT_COMPETITOR_DIRECT,
        values={"display_name": display_name, "managed_by_club_id": managed_by_club_id},
    )
    assert record is not None
    return Competitor.model_validate(dict(record._mapping))


async def _cleanup_competitors_and_audit(tenant_ids: list[int]) -> None:
    await database.execute(query=_DELETE_AUDIT_FOR_TENANTS, values={"tenant_ids": tenant_ids})
    await database.execute(query=_DELETE_COMPETITORS_FOR_TENANTS, values={"tenant_ids": tenant_ids})


@dataclass
class ActivationData:  # pylint: disable=too-many-instance-attributes
    """Dos tenants, tres actores y sus contextos autorizados."""

    tenant_a: Club
    tenant_b: Club
    actor: UserInDB
    collaborator: UserInDB
    outsider: UserInDB
    competitor_b: Competitor
    context_a: ActorContext
    context_collaborator: ActorContext
    context_outsider: ActorContext


@pytest_asyncio.fixture(loop_scope="session")
async def activation_data(reinit_database: Database) -> AsyncIterator[ActivationData]:
    async with AsyncExitStack() as stack:
        tenant_a = await stack.enter_async_context(
            inserted_club(ClubInsertable(name="Tenant A (S2-bis)", created=DUMMY_CLUB.created))
        )
        tenant_b = await stack.enter_async_context(
            inserted_club(ClubInsertable(name="Tenant B (S2-bis)", created=DUMMY_CLUB.created))
        )
        actor = await stack.enter_async_context(inserted_user(get_mock_user()))
        collaborator = await stack.enter_async_context(inserted_user(get_mock_user()))
        outsider = await stack.enter_async_context(inserted_user(get_mock_user()))
        await stack.enter_async_context(
            inserted_user_x_club(
                UserXClubInsertable(
                    user_id=actor.id, club_id=tenant_a.id, relation=UserXClubRelation.OWNER
                )
            )
        )
        await stack.enter_async_context(
            inserted_user_x_club(
                UserXClubInsertable(
                    user_id=collaborator.id,
                    club_id=tenant_a.id,
                    relation=UserXClubRelation.COLLABORATOR,
                )
            )
        )
        competitor_b = await _insert_competitor_direct(
            display_name="Persona Del Tenant B", managed_by_club_id=tenant_b.id
        )

        yield ActivationData(
            tenant_a=tenant_a,
            tenant_b=tenant_b,
            actor=actor,
            collaborator=collaborator,
            outsider=outsider,
            competitor_b=competitor_b,
            context_a=ActorContext(tenant_club_id=tenant_a.id, actor_user_id=actor.id),
            context_collaborator=ActorContext(
                tenant_club_id=tenant_a.id, actor_user_id=collaborator.id
            ),
            context_outsider=ActorContext(tenant_club_id=tenant_a.id, actor_user_id=outsider.id),
        )

        await _cleanup_competitors_and_audit([tenant_a.id, tenant_b.id])


async def _deactivated_competitor(
    data: ActivationData, *, name: str = "Persona Reactivable"
) -> Competitor:
    """Identidad creada y dada de baja con las operaciones de dominio (no con SQL crudo)."""
    competitor = await create_competitor(data.context_a, display_name=name)
    return await deactivate_competitor(data.context_a, competitor.id, reason_code="ADMINISTRATIVE")


# --- reactivacion valida -----------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_activation_reactivates_the_identity_and_records_audit(
    activation_data: ActivationData,
) -> None:
    deactivated = await _deactivated_competitor(activation_data)
    assert deactivated.active is False

    activated = await activate_competitor(
        activation_data.context_a, deactivated.id, reason_code="ADMINISTRATIVE"
    )

    assert activated.id == deactivated.id
    assert activated.active is True, "la reactivacion pone active = true"
    assert activated.updated_at is not None, "se registra la fecha de la operacion"
    assert activated.managed_by_club_id == activation_data.tenant_a.id
    assert activated.display_name == deactivated.display_name, "la reactivacion no renombra"

    read_back = await get_competitor(deactivated.id, tenant_club_id=activation_data.tenant_a.id)
    assert read_back == activated, "el estado reactivado se lee con el contrato de S1"

    events = await _audit_events(deactivated.id)
    assert [event["action"] for event in events] == ["CREATE", "DEACTIVATE", "ACTIVATE"]
    activation_event = events[2]
    assert activation_event["entity"] == "competitor"
    assert activation_event["entity_id"] == deactivated.id
    assert cast("list[str]", activation_event["changed_fields"]) == ["active"], "solo NOMBRES"
    assert activation_event["actor_user_id"] == activation_data.actor.id
    assert activation_event["actor_label"] is None
    assert activation_event["reason_code"] == "ADMINISTRATIVE"
    assert activation_event["reason"] == "reactivacion de competidor"


@pytest.mark.asyncio(loop_scope="session")
async def test_activation_uses_a_default_reason_when_none_is_given(
    activation_data: ActivationData,
) -> None:
    deactivated = await _deactivated_competitor(
        activation_data, name="Persona Con Motivo Por Defecto"
    )

    await activate_competitor(activation_data.context_a, deactivated.id)

    events = await _audit_events(deactivated.id)
    assert events[2]["reason"] == "reactivacion de competidor"


@pytest.mark.asyncio(loop_scope="session")
async def test_activation_of_an_already_active_competitor_is_a_noop(
    activation_data: ActivationData,
) -> None:
    competitor = await create_competitor(activation_data.context_a, display_name="Persona Activa")

    unchanged = await activate_competitor(activation_data.context_a, competitor.id)

    assert unchanged == competitor, "la identidad activa se devuelve tal cual"
    assert unchanged.updated_at is None, "sin cambios no se toca updated_at"
    events = await _audit_events(competitor.id)
    assert [event["action"] for event in events] == ["CREATE"], "sin cambios no se audita"


@pytest.mark.asyncio(loop_scope="session")
async def test_activation_is_idempotent_and_does_not_duplicate_audit(
    activation_data: ActivationData,
) -> None:
    deactivated = await _deactivated_competitor(activation_data, name="Persona Idempotente")

    first = await activate_competitor(activation_data.context_a, deactivated.id)
    second = await activate_competitor(activation_data.context_a, deactivated.id)

    assert first.active is True and second.active is True
    assert second.updated_at == first.updated_at, "la segunda reactivacion no reescribe la fila"
    events = await _audit_events(deactivated.id)
    assert [event["action"] for event in events] == ["CREATE", "DEACTIVATE", "ACTIVATE"], (
        "la segunda reactivacion no duplica auditoria"
    )


# --- autorizacion y aislamiento ----------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_activation_rejects_a_competitor_of_another_tenant(
    activation_data: ActivationData,
) -> None:
    foreign = await _insert_competitor_direct(
        display_name="Identidad Ajena Inactiva", managed_by_club_id=activation_data.tenant_b.id
    )
    await database.execute(
        query="UPDATE competitors SET active = false WHERE id = :c", values={"c": foreign.id}
    )

    with pytest.raises(CompetitorNotFoundError) as exc_info:
        await activate_competitor(activation_data.context_a, foreign.id)

    assert exc_info.value.args[0] == "competidor no encontrado", "mismo error que si no existiera"
    untouched = await get_competitor(foreign.id, tenant_club_id=activation_data.tenant_b.id)
    assert untouched is not None and untouched.active is False
    assert await _audit_events(foreign.id) == [], "una operacion rechazada no audita"


@pytest.mark.asyncio(loop_scope="session")
async def test_activation_on_unknown_competitor_fails(activation_data: ActivationData) -> None:
    with pytest.raises(CompetitorNotFoundError) as not_found:
        await activate_competitor(activation_data.context_a, CompetitorId(999_999))

    assert not_found.value.args[0] == "competidor no encontrado"


@pytest.mark.asyncio(loop_scope="session")
async def test_collaborator_cannot_reactivate(activation_data: ActivationData) -> None:
    deactivated = await _deactivated_competitor(activation_data, name="Persona Del Colaborador")

    with pytest.raises(InsufficientPrivilegesError):
        await activate_competitor(
            activation_data.context_collaborator,
            deactivated.id,
            reason_code="ADMINISTRATIVE",
        )

    fresh = await get_competitor(deactivated.id, tenant_club_id=activation_data.tenant_a.id)
    assert fresh is not None and fresh.active is False, "el colaborador no reactiva"
    events = await _audit_events(deactivated.id)
    assert [event["action"] for event in events] == ["CREATE", "DEACTIVATE"], "sin evento ACTIVATE"


@pytest.mark.asyncio(loop_scope="session")
async def test_activation_rejects_an_actor_without_access_to_the_tenant(
    activation_data: ActivationData,
) -> None:
    deactivated = await _deactivated_competitor(activation_data, name="Persona Sin Acceso")

    with pytest.raises(TenantNotAuthorizedError):
        await activate_competitor(activation_data.context_outsider, deactivated.id)

    fresh = await get_competitor(deactivated.id, tenant_club_id=activation_data.tenant_a.id)
    assert fresh is not None and fresh.active is False


@pytest.mark.asyncio(loop_scope="session")
async def test_activation_rejects_an_actor_that_does_not_exist(
    activation_data: ActivationData,
) -> None:
    deactivated = await _deactivated_competitor(activation_data, name="Persona Actor Inexistente")
    context = ActorContext(
        tenant_club_id=activation_data.tenant_a.id, actor_user_id=UserId(999_999)
    )

    with pytest.raises(TenantNotAuthorizedError):
        await activate_competitor(context, deactivated.id)


@pytest.mark.asyncio(loop_scope="session")
async def test_activation_rejects_a_tenant_that_does_not_exist(
    activation_data: ActivationData,
) -> None:
    deactivated = await _deactivated_competitor(activation_data, name="Persona Tenant Inexistente")
    context = ActorContext(tenant_club_id=ClubId(999_999), actor_user_id=activation_data.actor.id)

    with pytest.raises(TenantNotAuthorizedError):
        await activate_competitor(context, deactivated.id)

    fresh = await get_competitor(deactivated.id, tenant_club_id=activation_data.tenant_a.id)
    assert fresh is not None and fresh.active is False


@pytest.mark.asyncio(loop_scope="session")
async def test_activation_does_not_accept_the_tenant_from_outside(
    activation_data: ActivationData,
) -> None:
    # La firma interna no acepta el tenant: solo lo toma del contexto autorizado.
    assert "managed_by_club_id" not in inspect.signature(activate_competitor).parameters
    assert "tenant_club_id" not in inspect.signature(activate_competitor).parameters

    deactivated = await _deactivated_competitor(activation_data, name="Persona Tenant Del Contexto")
    activated = await activate_competitor(activation_data.context_a, deactivated.id)
    assert activated.managed_by_club_id == activation_data.tenant_a.id


# --- transaccionalidad, historial y PII ---------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_failure_rolls_back_the_reactivation(
    activation_data: ActivationData, monkeypatch: pytest.MonkeyPatch
) -> None:
    deactivated = await _deactivated_competitor(activation_data, name="ROLLBACK-REACTIVACION")

    async def failing_audit(**kwargs: object) -> None:
        raise RuntimeError("auditoria no disponible")

    monkeypatch.setattr("bracket.logic.competitors.sql_insert_domain_change_log", failing_audit)

    with pytest.raises(RuntimeError):
        await activate_competitor(activation_data.context_a, deactivated.id)

    monkeypatch.undo()
    still_inactive = await get_competitor(
        deactivated.id, tenant_club_id=activation_data.tenant_a.id
    )
    assert still_inactive is not None and still_inactive.active is False, (
        "si falla la auditoria, la reactivacion se revierte"
    )
    events = await _audit_events(deactivated.id)
    assert [event["action"] for event in events] == ["CREATE", "DEACTIVATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_activation_keeps_the_name_history_untouched(
    activation_data: ActivationData,
) -> None:
    competitor = await create_competitor(
        activation_data.context_a, display_name="Persona Historica"
    )
    await deactivate_competitor(activation_data.context_a, competitor.id)
    history_before = await get_competitor_name_history(
        competitor.id, tenant_club_id=activation_data.tenant_a.id
    )

    await activate_competitor(activation_data.context_a, competitor.id)

    history_after = await get_competitor_name_history(
        competitor.id, tenant_club_id=activation_data.tenant_a.id
    )
    assert history_after == history_before, "ni la baja ni la reactivacion tocan el historial"
    assert len(history_after) == 1, "la reactivacion no abre una entrada de nombre"
    assert history_after[0].valid_to is None, "la entrada vigente sigue abierta"


@pytest.mark.asyncio(loop_scope="session")
async def test_round_trip_restores_visibility_and_audits_each_step(
    activation_data: ActivationData,
) -> None:
    competitor = await create_competitor(
        activation_data.context_a, display_name="Persona Ida Y Vuelta"
    )
    assert (
        await get_competitor_name_history(competitor.id, tenant_club_id=activation_data.tenant_a.id)
        != []
    )

    await deactivate_competitor(activation_data.context_a, competitor.id)
    hidden = await get_competitor(competitor.id, tenant_club_id=activation_data.tenant_a.id)
    assert hidden is not None and hidden.active is False

    await activate_competitor(activation_data.context_a, competitor.id)
    visible = await get_competitor(competitor.id, tenant_club_id=activation_data.tenant_a.id)
    assert visible is not None and visible.active is True

    await deactivate_competitor(activation_data.context_a, competitor.id)

    events = await _audit_events(competitor.id)
    assert [event["action"] for event in events] == [
        "CREATE",
        "DEACTIVATE",
        "ACTIVATE",
        "DEACTIVATE",
    ]
    assert all(cast("list[str]", event["changed_fields"]) == ["active"] for event in events[1:]), (
        "cada cambio de estado audita el nombre del campo, nunca su valor"
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_activation_rejects_an_invalid_reason(activation_data: ActivationData) -> None:
    deactivated = await _deactivated_competitor(activation_data, name="Persona Motivo Invalido")

    for invalid_code in ("   ", "\t", "motivo con espacio", "NO_EXISTE", "x" * 33):
        with pytest.raises(InvalidCompetitorDataError):
            await activate_competitor(
                activation_data.context_a, deactivated.id, reason_code=invalid_code
            )
    # Los caracteres de control se rechazan en la entrada original: tambien en los bordes,
    # donde un recorte los haria desaparecer (S3.3c-2).
    for invalid_note in (
        "\t",
        "\nnota inicial",
        "nota final\n",
        "nota con\tsalto interno",
        "x" * 201,
        "aviso a persona@example.com",
    ):
        with pytest.raises(InvalidCompetitorDataError):
            await activate_competitor(
                activation_data.context_a,
                deactivated.id,
                reason_code="ADMINISTRATIVE",
                reason_note=invalid_note,
            )

    fresh = await get_competitor(deactivated.id, tenant_club_id=activation_data.tenant_a.id)
    assert fresh is not None and fresh.active is False, "un motivo invalido no reactiva"
    events = await _audit_events(deactivated.id)
    assert [event["action"] for event in events] == ["CREATE", "DEACTIVATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_no_pii_in_logs_errors_or_audit(
    activation_data: ActivationData, caplog: pytest.LogCaptureFixture
) -> None:
    sensitive_name = "PII-REACTIVACION-NO-DEBE-SALIR"

    with caplog.at_level(logging.DEBUG):
        competitor = await create_competitor(activation_data.context_a, display_name=sensitive_name)
        await deactivate_competitor(activation_data.context_a, competitor.id)
        with pytest.raises(CompetitorNotFoundError) as exc_info:
            await activate_competitor(activation_data.context_a, CompetitorId(999_999))
        with pytest.raises(InsufficientPrivilegesError) as privileges_exc:
            await activate_competitor(
                activation_data.context_collaborator, competitor.id, reason_code="ADMINISTRATIVE"
            )
        await activate_competitor(activation_data.context_a, competitor.id)

    assert sensitive_name not in str(exc_info.value), "la excepcion no repite el nombre"
    assert sensitive_name not in str(privileges_exc.value), "el error de permisos tampoco"

    module_leaks = [
        record.name
        for record in caplog.records
        if sensitive_name in record.getMessage() and not record.name.startswith("databases")
    ]
    assert module_leaks == [], "ni el modulo ni la logica registran nombres"

    raw_rows = await database.fetch_all(
        query="SELECT entity, action, changed_fields, reason, reason_code, reason_note, actor_label"
        " FROM domain_change_log"
    )
    audit_dump = " ".join(str(value) for row in raw_rows for value in row._mapping.values())
    assert sensitive_name not in audit_dump, "la auditoria guarda nombres de campo, no valores"
