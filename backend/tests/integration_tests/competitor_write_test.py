# pylint: disable=redefined-outer-name  # `write_data` es el fixture de este modulo (igual que conftest).
"""S2 de F3B — escritura de identidad de competidores (capa interna de dominio).

TDD: estas pruebas se escribieron y ejecutaron **antes** de subir
``bracket/sql/domain_writes.py`` y ``bracket/logic/competitors.py`` (rojo con
``ModuleNotFoundError``) y despues en verde con la implementacion.

Contrato verificado (servicio interno, sin HTTP todavia):

* el tenant y el actor llegan siempre en un :class:`ActorContext` explicito;
* ``managed_by_club_id`` nunca se acepta desde fuera del contexto;
* el actor debe tener acceso al tenant (``users_x_clubs``);
* dos personas con el mismo nombre son dos identidades (sin deduplicacion);
* un competidor puede existir sin academia;
* competidor, historial de nombre y evento de auditoria se confirman o revierten juntos;
* ni los errores ni los logs de este modulo contienen nombres ni identificadores.

Validacion exclusivamente contra ``bracket_test`` (esquema F3A aplicado); el
``bracket_dev`` no se usa para escrituras.
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
from pydantic import ValidationError

from bracket.database import database
from bracket.logic.competitors import (
    CompetitorNotFoundError,
    InsufficientPrivilegesError,
    InvalidCompetitorDataError,
    TenantNotAuthorizedError,
    create_competitor,
    deactivate_competitor,
    update_competitor_display_name,
)
from bracket.models.db.club import Club, ClubInsertable
from bracket.models.db.domain import ActorContext, Competitor, CompetitorBasicDataUpdate
from bracket.models.db.user import UserInDB
from bracket.models.db.user_x_club import UserXClubInsertable, UserXClubRelation
from bracket.sql.domain_reads import (
    get_competitor,
    get_competitor_affiliations,
    get_competitor_name_history,
    get_competitors_for_tenant,
)
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


# Los 9 campos son el escenario S2 completo (dos tenants, tres actores y sus contextos);
# agruparlos obligaria a reescribir los tests y empeoraria la legibilidad del fixture.
@dataclass
class WriteData:  # pylint: disable=too-many-instance-attributes
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
async def write_data(reinit_database: Database) -> AsyncIterator[WriteData]:
    async with AsyncExitStack() as stack:
        tenant_a = await stack.enter_async_context(
            inserted_club(ClubInsertable(name="Tenant A", created=DUMMY_CLUB.created))
        )
        tenant_b = await stack.enter_async_context(
            inserted_club(ClubInsertable(name="Tenant B", created=DUMMY_CLUB.created))
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

        yield WriteData(
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

        # Limpieza antes de que el AsyncExitStack retire clubes y usuarios (orden FK-seguro).
        await _cleanup_competitors_and_audit([tenant_a.id, tenant_b.id])


# --- contextos y autorizacion -----------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_actor_context_requires_explicit_tenant_and_actor(write_data: WriteData) -> None:
    with pytest.raises(ValidationError):
        ActorContext(tenant_club_id=write_data.tenant_a.id)  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        ActorContext(
            tenant_club_id=write_data.tenant_a.id,
            actor_user_id=None,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_create_rejects_tenant_that_does_not_exist(write_data: WriteData) -> None:
    context = ActorContext(tenant_club_id=ClubId(999_999), actor_user_id=write_data.actor.id)

    with pytest.raises(TenantNotAuthorizedError):
        await create_competitor(context, display_name="No Debe Existir")

    assert await get_competitors_for_tenant(ClubId(999_999)) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_create_rejects_actor_without_access_to_the_tenant(write_data: WriteData) -> None:
    before = await get_competitors_for_tenant(write_data.tenant_a.id)

    with pytest.raises(TenantNotAuthorizedError):
        await create_competitor(write_data.context_outsider, display_name="Sin Acceso")

    assert await get_competitors_for_tenant(write_data.tenant_a.id) == before


@pytest.mark.asyncio(loop_scope="session")
async def test_create_rejects_actor_that_does_not_exist(write_data: WriteData) -> None:
    context = ActorContext(tenant_club_id=write_data.tenant_a.id, actor_user_id=UserId(999_999))

    with pytest.raises(TenantNotAuthorizedError):
        await create_competitor(context, display_name="Actor Inexistente")


# --- creacion ----------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_create_competitor_persists_identity_without_sports_club(
    write_data: WriteData,
) -> None:
    competitor = await create_competitor(
        write_data.context_a, display_name="  Persona Uno  ", reason_code="PLANNED_ENTRY"
    )

    assert competitor.display_name == "Persona Uno", (
        "el nombre se normaliza (sin espacios extremos)"
    )
    assert competitor.managed_by_club_id == write_data.tenant_a.id, "el tenant sale del contexto"
    assert competitor.active is True
    assert competitor.updated_at is None

    read_back = await get_competitor(competitor.id, tenant_club_id=write_data.tenant_a.id)
    assert read_back == competitor, "el competidor creado se lee con el contrato de S1"
    assert (
        await get_competitor_affiliations(competitor.id, tenant_club_id=write_data.tenant_a.id)
        == []
    ), "un competidor puede existir sin academia"


@pytest.mark.asyncio(loop_scope="session")
async def test_two_people_with_the_same_name_are_not_merged(write_data: WriteData) -> None:
    first = await create_competitor(write_data.context_a, display_name="Nombre Identico")
    second = await create_competitor(write_data.context_a, display_name="Nombre Identico")

    assert first.id != second.id

    matches = await get_competitors_for_tenant(
        write_data.tenant_a.id, display_name="Nombre Identico"
    )
    assert len(matches) == 2, "sin deduplicacion automatica por nombre"


@pytest.mark.asyncio(loop_scope="session")
async def test_creation_records_initial_name_history_and_audit_event(
    write_data: WriteData,
) -> None:
    sensitive_name = "NOMBRE-SENSIBLE-CREACION"
    competitor = await create_competitor(
        write_data.context_a, display_name=sensitive_name, reason_code="PLANNED_ENTRY"
    )

    history = await get_competitor_name_history(
        competitor.id, tenant_club_id=write_data.tenant_a.id
    )
    assert len(history) == 1
    assert history[0].display_name == sensitive_name
    assert history[0].valid_to is None, "el nombre vigente queda abierto"

    events = await _audit_events(competitor.id)
    assert len(events) == 1
    event = events[0]
    assert event["entity"] == "competitor"
    assert event["entity_id"] == competitor.id
    assert event["action"] == "CREATE"
    assert event["actor_user_id"] == write_data.actor.id
    assert event["reason_code"] == "PLANNED_ENTRY"
    assert event["reason"] == "alta de competidor"
    assert cast("list[str]", event["changed_fields"]) == ["display_name"], "solo NOMBRES de campo"
    assert sensitive_name not in " ".join(str(value) for value in event.values())


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_failure_rolls_back_the_competitor(
    write_data: WriteData, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_audit(**kwargs: object) -> None:
        raise RuntimeError("auditoria no disponible")

    monkeypatch.setattr("bracket.logic.competitors.sql_insert_domain_change_log", failing_audit)

    with pytest.raises(RuntimeError):
        await create_competitor(write_data.context_a, display_name="ROLLBACK-TRANSACCIONAL")

    matches = await get_competitors_for_tenant(
        write_data.tenant_a.id, display_name="ROLLBACK-TRANSACCIONAL", include_inactive=True
    )
    assert matches == [], "si la auditoria falla, el competidor se revierte"


@pytest.mark.asyncio(loop_scope="session")
async def test_failure_in_name_history_also_rolls_back_everything(
    write_data: WriteData, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_history(**kwargs: object) -> None:
        raise RuntimeError("historial no disponible")

    monkeypatch.setattr(
        "bracket.logic.competitors.sql_insert_competitor_name_history", failing_history
    )

    with pytest.raises(RuntimeError):
        await create_competitor(
            write_data.context_a, display_name="ROLLBACK-HISTORIAL", reason_code="PLANNED_ENTRY"
        )

    assert (
        await get_competitors_for_tenant(
            write_data.tenant_a.id,
            display_name="ROLLBACK-HISTORIAL",
            include_inactive=True,
        )
        == []
    ), "si falla el historial de nombre, el competidor se revierte"

    rows = await database.fetch_all(
        query="SELECT id FROM domain_change_log WHERE reason_code = :reason_code",
        values={"reason_code": "PLANNED_ENTRY"},
    )
    assert rows == [], "y el evento de auditoria tampoco se confirma"


# --- actualizacion de datos basicos ------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_update_display_name_keeps_history_and_audit(write_data: WriteData) -> None:
    competitor = await create_competitor(write_data.context_a, display_name="Nombre Original")

    updated = await update_competitor_display_name(
        write_data.context_a,
        competitor.id,
        CompetitorBasicDataUpdate(display_name="Nombre Corregido"),
        reason_code="DATA_CORRECTION",
    )

    assert updated.id == competitor.id
    assert updated.display_name == "Nombre Corregido"
    assert updated.updated_at is not None, "se registra la fecha de modificacion"
    assert updated.managed_by_club_id == write_data.tenant_a.id

    history = await get_competitor_name_history(
        competitor.id, tenant_club_id=write_data.tenant_a.id
    )
    assert [row.display_name for row in history] == ["Nombre Original", "Nombre Corregido"]
    assert history[0].valid_to is not None, "el nombre anterior se cierra"
    assert history[1].valid_to is None

    events = await _audit_events(competitor.id)
    assert [event["action"] for event in events] == ["CREATE", "UPDATE"]
    assert cast("list[str]", events[1]["changed_fields"]) == ["display_name"]
    assert events[1]["reason_code"] == "DATA_CORRECTION"
    assert events[1]["reason"] == "actualizacion de datos basicos"


@pytest.mark.asyncio(loop_scope="session")
async def test_update_with_the_same_name_is_a_noop(write_data: WriteData) -> None:
    competitor = await create_competitor(write_data.context_a, display_name="Nombre Estable")

    unchanged = await update_competitor_display_name(
        write_data.context_a,
        competitor.id,
        CompetitorBasicDataUpdate(display_name="  Nombre Estable  "),
    )

    assert unchanged.updated_at is None, "sin cambios no se toca updated_at"
    history = await get_competitor_name_history(
        competitor.id, tenant_club_id=write_data.tenant_a.id
    )
    assert len(history) == 1, "sin cambios no se anade historial"
    events = await _audit_events(competitor.id)
    assert [event["action"] for event in events] == ["CREATE"], "sin cambios no se audita"


@pytest.mark.asyncio(loop_scope="session")
async def test_update_rejects_a_competitor_of_another_tenant(write_data: WriteData) -> None:
    with pytest.raises(CompetitorNotFoundError):
        await update_competitor_display_name(
            write_data.context_a,
            write_data.competitor_b.id,
            CompetitorBasicDataUpdate(display_name="Intento Cruzado"),
        )

    untouched = await get_competitor(
        write_data.competitor_b.id, tenant_club_id=write_data.tenant_b.id
    )
    assert untouched is not None
    assert untouched.display_name == "Persona Del Tenant B"
    assert await _audit_events(write_data.competitor_b.id) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_update_on_unknown_competitor_fails(write_data: WriteData) -> None:
    with pytest.raises(CompetitorNotFoundError):
        await update_competitor_display_name(
            write_data.context_a,
            CompetitorId(999_999),
            CompetitorBasicDataUpdate(display_name="Inexistente"),
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_basic_data_contract_does_not_allow_changing_the_tenant(
    write_data: WriteData,
) -> None:
    # El contrato de datos basicos no expone el tenant (ni ningun otro campo de identidad).
    assert "managed_by_club_id" not in list(CompetitorBasicDataUpdate.model_fields)

    # Y la API interna no acepta el tenant ni como parametro: solo lo toma del contexto.
    for operation in (create_competitor, update_competitor_display_name, deactivate_competitor):
        assert "managed_by_club_id" not in inspect.signature(operation).parameters

    competitor = await create_competitor(write_data.context_a, display_name="Tenant Del Contexto")
    assert competitor.managed_by_club_id == write_data.tenant_a.id


# --- baja logica -------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_deactivation_is_logical_and_idempotent(write_data: WriteData) -> None:
    competitor = await create_competitor(write_data.context_a, display_name="Persona Baja")

    deactivated = await deactivate_competitor(
        write_data.context_a, competitor.id, reason_code="ADMINISTRATIVE"
    )
    assert deactivated.active is False
    assert deactivated.updated_at is not None

    still_there = await get_competitor(competitor.id, tenant_club_id=write_data.tenant_a.id)
    assert still_there is not None and still_there.active is False, "la baja es logica, no fisica"
    assert await get_competitors_for_tenant(write_data.tenant_a.id) == [], "excluido por defecto"
    assert len(await get_competitors_for_tenant(write_data.tenant_a.id, include_inactive=True)) == 1

    events = await _audit_events(competitor.id)
    assert [event["action"] for event in events] == ["CREATE", "DEACTIVATE"]
    assert cast("list[str]", events[1]["changed_fields"]) == ["active"]

    again = await deactivate_competitor(write_data.context_a, competitor.id)
    assert again.active is False
    events_after = await _audit_events(competitor.id)
    assert len(events_after) == 2, "la segunda baja no duplica auditoria"


@pytest.mark.asyncio(loop_scope="session")
async def test_deactivate_rejects_a_competitor_of_another_tenant(write_data: WriteData) -> None:
    with pytest.raises(CompetitorNotFoundError):
        await deactivate_competitor(write_data.context_a, write_data.competitor_b.id)

    untouched = await get_competitor(
        write_data.competitor_b.id, tenant_club_id=write_data.tenant_b.id
    )
    assert untouched is not None and untouched.active is True


@pytest.mark.asyncio(loop_scope="session")
async def test_collaborator_can_create_and_rename_but_not_deactivate(
    write_data: WriteData,
) -> None:
    """Politica: OWNER/COLLABORATOR para alta y edicion; solo OWNER para la baja logica."""
    collaborator = write_data.context_collaborator

    competitor = await create_competitor(collaborator, display_name="Persona Colaboradora")
    assert competitor.managed_by_club_id == write_data.tenant_a.id

    renamed = await update_competitor_display_name(
        collaborator, competitor.id, CompetitorBasicDataUpdate(display_name="Persona Renombrada")
    )
    assert renamed.display_name == "Persona Renombrada"

    with pytest.raises(InsufficientPrivilegesError):
        await deactivate_competitor(collaborator, competitor.id, reason_code="ADMINISTRATIVE")

    fresh = await get_competitor(competitor.id, tenant_club_id=write_data.tenant_a.id)
    assert fresh is not None and fresh.active is True, "el colaborador no desactiva"
    events = await _audit_events(competitor.id)
    assert [event["action"] for event in events] == ["CREATE", "UPDATE"], "sin evento de baja"


@pytest.mark.asyncio(loop_scope="session")
async def test_deactivate_on_unknown_competitor_fails(write_data: WriteData) -> None:
    with pytest.raises(CompetitorNotFoundError):
        await deactivate_competitor(write_data.context_a, CompetitorId(999_999))


# --- datos invalidos ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "invalid_name",
    ["", "   ", "\t", "Nombre\nCon Salto", "Nombre\x00Nulo", "x" * 201],
)
@pytest.mark.asyncio(loop_scope="session")
async def test_invalid_names_are_rejected(write_data: WriteData, invalid_name: str) -> None:
    before = await get_competitors_for_tenant(write_data.tenant_a.id, include_inactive=True)

    with pytest.raises(InvalidCompetitorDataError):
        await create_competitor(write_data.context_a, display_name=invalid_name)

    after = await get_competitors_for_tenant(write_data.tenant_a.id, include_inactive=True)
    assert after == before, "un dato invalido no deja rastro"


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_or_blank_reason_code_is_rejected(write_data: WriteData) -> None:
    """Un codigo desconocido o en blanco se rechaza antes de escribir nada (catalogo cerrado)."""
    for invalid_code in ("", "   ", "PLANNED", "NO_EXISTE", "withdraw"):
        with pytest.raises(InvalidCompetitorDataError):
            await create_competitor(
                write_data.context_a, display_name="Nombre Valido", reason_code=invalid_code
            )


# --- ausencia de PII ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_no_pii_in_logs_errors_or_audit(
    write_data: WriteData, caplog: pytest.LogCaptureFixture
) -> None:
    sensitive_name = "PII-NO-DEBE-SALIR"

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(CompetitorNotFoundError) as exc_info:
            await update_competitor_display_name(
                write_data.context_a,
                CompetitorId(999_999),
                CompetitorBasicDataUpdate(display_name=sensitive_name),
            )

        competitor = await create_competitor(
            write_data.context_a, display_name=sensitive_name, reason_code="PLANNED_ENTRY"
        )
        with pytest.raises(InsufficientPrivilegesError) as privileges_exc:
            await deactivate_competitor(
                write_data.context_collaborator, competitor.id, reason_code="ADMINISTRATIVE"
            )
        await deactivate_competitor(write_data.context_a, competitor.id)

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
