# pylint: disable=redefined-outer-name  # `audit_data` es el fixture de este modulo.
"""S3.3a — atribucion explicita de tenant en la auditoria F3 (capa interna de dominio).

TDD: estas pruebas se escribieron y ejecutaron **antes** de cambiar
``bracket/sql/domain_writes.py``, ``bracket/schema.py`` y la migracion (rojo: el parametro
``tenant_club_id`` no existia y ``DomainWriteScopeError`` no se importaba) y despues en verde.

Contrato verificado:

* cada evento de auditoria queda atribuido al tenant de su entidad, y el tenant sale del
  :class:`ActorContext`, nunca del payload;
* la coherencia entidad <-> tenant la comprueba la propia sentencia: una referencia **valida**
  a ``clubs(id)`` no basta (tenant existente pero distinto del de la entidad -> error);
* si la comprobacion falla no se escribe nada y la operacion entera revierte (ni operacion sin
  evento ni evento sin operacion);
* los helpers de historial de nombres no pueden tocar el historial de otro tenant;
* una operacion efectiva deja exactamente un evento; un no-op, ninguno;
* las entidades de plataforma (tenant NULL) no tienen escritor de auditoria autorizado;
* los eventos no contienen valores (solo nombres de campo) ni tocan los snapshots.

Validacion exclusivamente contra la base de tests (``bracket_test``); ``bracket_dev`` no se usa.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from databases import Database

from bracket.database import database
from bracket.logic.competitors import (
    TenantNotAuthorizedError,
    create_competitor,
    deactivate_competitor,
    update_competitor_display_name,
)
from bracket.logic.registrations import (
    TournamentNotFoundError,
    create_registration,
    withdraw_registration,
)
from bracket.models.db.club import Club, ClubInsertable
from bracket.models.db.domain import (
    ActorContext,
    Competitor,
    CompetitorBasicDataUpdate,
    DomainWriteScopeError,
)
from bracket.models.db.tournament import Tournament
from bracket.models.db.user import UserInDB
from bracket.models.db.user_x_club import UserXClubInsertable, UserXClubRelation
from bracket.sql.domain_reads import get_competitor, get_registration
from bracket.sql.domain_writes import (
    sql_close_open_competitor_name_history,
    sql_insert_competitor,
    sql_insert_competitor_name_history,
    sql_insert_domain_change_log,
)
from bracket.utils.dummy_records import DUMMY_CLUB, DUMMY_TOURNAMENT
from bracket.utils.id_types import ClubId, CompetitorId
from tests.integration_tests.mocks import get_mock_user
from tests.integration_tests.registration_fixtures import build_draft
from tests.integration_tests.sql import (
    inserted_club,
    inserted_tournament,
    inserted_user,
    inserted_user_x_club,
)

SELECT_EVENTS = """
    SELECT id, entity, entity_id, action, changed_fields, tenant_club_id
    FROM domain_change_log
    WHERE entity = :entity AND entity_id = :entity_id
    ORDER BY id
"""

SELECT_EVENT_COUNT_FOR_TENANT = """
    SELECT count(*) FROM domain_change_log
    WHERE entity = :entity AND entity_id = :entity_id AND tenant_club_id = :tenant_club_id
"""

SELECT_OPEN_HISTORY = """
    SELECT id, display_name FROM competitors_name_history
    WHERE competitor_id = :competitor_id AND valid_to IS NULL
    ORDER BY id
"""

SELECT_SNAPSHOTS = """
    SELECT competitor_name_snapshot, sports_club_name_snapshot, category_key, category_label,
           revision, superseded_by_registration_id
    FROM tournament_registrations WHERE id = :registration_id
"""

INSERT_PLATFORM_COMPETITOR = """
    INSERT INTO competitors (display_name, managed_by_club_id, active, created)
    VALUES (:display_name, NULL, true, NOW())
    RETURNING id
"""

DELETE_REGISTRATIONS = "DELETE FROM tournament_registrations WHERE tournament_id = :tournament_id"
DELETE_AUDIT_FOR_TENANTS = "DELETE FROM domain_change_log WHERE tenant_club_id = ANY(:tenant_ids)"
DELETE_COMPETITORS_FOR_TENANTS = (
    "DELETE FROM competitors WHERE managed_by_club_id = ANY(:tenant_ids)"
)
DELETE_ANY_AUDIT_FOR_COMPETITOR = (
    "DELETE FROM domain_change_log WHERE entity = 'competitor' AND entity_id = :competitor_id"
)


async def _events(entity: str, entity_id: int) -> list[dict[str, Any]]:
    records = await database.fetch_all(
        query=SELECT_EVENTS, values={"entity": entity, "entity_id": entity_id}
    )
    return [dict(record._mapping) for record in records]


async def _event_count_for_tenant(entity: str, entity_id: int, tenant_club_id: int) -> int:
    count = await database.fetch_val(
        query=SELECT_EVENT_COUNT_FOR_TENANT,
        values={"entity": entity, "entity_id": entity_id, "tenant_club_id": tenant_club_id},
    )
    return int(count)


@dataclass
class AuditData:  # pylint: disable=too-many-instance-attributes
    """Dos tenants con torneo propio, cuatro actores y sus contextos autorizados."""

    tenant_a: Club
    tenant_b: Club
    tournament_a: Tournament
    actor: UserInDB
    collaborator: UserInDB
    outsider: UserInDB
    actor_b: UserInDB
    competitor_a: Competitor
    competitor_b: Competitor
    context_a: ActorContext
    context_collaborator: ActorContext
    context_outsider: ActorContext
    context_b: ActorContext


@pytest_asyncio.fixture(loop_scope="session")
async def audit_data(reinit_database: Database) -> AsyncIterator[AuditData]:
    async with AsyncExitStack() as stack:
        tenant_a = await stack.enter_async_context(
            inserted_club(ClubInsertable(name="S3.3a Tenant A", created=DUMMY_CLUB.created))
        )
        tenant_b = await stack.enter_async_context(
            inserted_club(ClubInsertable(name="S3.3a Tenant B", created=DUMMY_CLUB.created))
        )
        tournament_a = await stack.enter_async_context(
            inserted_tournament(
                DUMMY_TOURNAMENT.model_copy(
                    update={"club_id": tenant_a.id, "dashboard_endpoint": "f3b-s33a-tenant-a"}
                )
            )
        )
        actor = await stack.enter_async_context(inserted_user(get_mock_user()))
        collaborator = await stack.enter_async_context(inserted_user(get_mock_user()))
        outsider = await stack.enter_async_context(inserted_user(get_mock_user()))
        actor_b = await stack.enter_async_context(inserted_user(get_mock_user()))
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
        await stack.enter_async_context(
            inserted_user_x_club(
                UserXClubInsertable(
                    user_id=actor_b.id, club_id=tenant_b.id, relation=UserXClubRelation.OWNER
                )
            )
        )

        context_a = ActorContext(tenant_club_id=tenant_a.id, actor_user_id=actor.id)
        context_b = ActorContext(tenant_club_id=tenant_b.id, actor_user_id=actor_b.id)
        competitor_a = await create_competitor(context_a, display_name="Identidad De A")
        competitor_b = await create_competitor(context_b, display_name="Identidad De B")

        yield AuditData(
            tenant_a=tenant_a,
            tenant_b=tenant_b,
            tournament_a=tournament_a,
            actor=actor,
            collaborator=collaborator,
            outsider=outsider,
            actor_b=actor_b,
            competitor_a=competitor_a,
            competitor_b=competitor_b,
            context_a=context_a,
            context_collaborator=ActorContext(
                tenant_club_id=tenant_a.id, actor_user_id=collaborator.id
            ),
            context_outsider=ActorContext(tenant_club_id=tenant_a.id, actor_user_id=outsider.id),
            context_b=context_b,
        )

        # Limpieza FK-segura antes de que el AsyncExitStack retire clubes y usuarios:
        # inscripciones -> auditoria (el FK a clubs es RESTRICT) -> competidores.
        await database.execute(
            query=DELETE_REGISTRATIONS, values={"tournament_id": tournament_a.id}
        )
        await database.execute(
            query=DELETE_AUDIT_FOR_TENANTS, values={"tenant_ids": [tenant_a.id, tenant_b.id]}
        )
        await database.execute(
            query=DELETE_COMPETITORS_FOR_TENANTS,
            values={"tenant_ids": [tenant_a.id, tenant_b.id]},
        )


# --- atribucion del evento ---------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_each_event_carries_the_tenant_of_its_entity(audit_data: AuditData) -> None:
    events_a = await _events("competitor", audit_data.competitor_a.id)
    events_b = await _events("competitor", audit_data.competitor_b.id)

    assert [event["tenant_club_id"] for event in events_a] == [audit_data.tenant_a.id]
    assert [event["tenant_club_id"] for event in events_b] == [audit_data.tenant_b.id]
    assert events_a[0]["action"] == "CREATE"


@pytest.mark.asyncio(loop_scope="session")
async def test_events_are_readable_by_tenant_and_not_cross_tenant(audit_data: AuditData) -> None:
    assert await _event_count_for_tenant(
        "competitor", audit_data.competitor_a.id, audit_data.tenant_a.id
    ) == len(await _events("competitor", audit_data.competitor_a.id))
    assert (
        await _event_count_for_tenant(
            "competitor", audit_data.competitor_a.id, audit_data.tenant_b.id
        )
        == 0
    )
    assert (
        await _event_count_for_tenant(
            "competitor", audit_data.competitor_b.id, audit_data.tenant_a.id
        )
        == 0
    )


# --- coherencia entidad <-> tenant -------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_rejects_a_tenant_that_does_not_exist(audit_data: AuditData) -> None:
    before = len(await _events("competitor", audit_data.competitor_a.id))

    with pytest.raises(DomainWriteScopeError):
        await sql_insert_domain_change_log(
            entity="competitor",
            entity_id=audit_data.competitor_a.id,
            action="UPDATE",
            changed_fields=["display_name"],
            actor_user_id=audit_data.actor.id,
            actor_label=None,
            reason="prueba",
            reason_code="DATA_CORRECTION",
            reason_note=None,
            tenant_club_id=ClubId(999_999),
        )

    assert len(await _events("competitor", audit_data.competitor_a.id)) == before


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_rejects_a_valid_tenant_that_is_not_the_entity_tenant(
    audit_data: AuditData,
) -> None:
    """Los dos tenants existen: la clave ajena seria valida y aun asi el evento no se escribe."""
    before_a = len(await _events("competitor", audit_data.competitor_a.id))
    before_b = len(await _events("competitor", audit_data.competitor_b.id))

    with pytest.raises(DomainWriteScopeError):
        await sql_insert_domain_change_log(
            entity="competitor",
            entity_id=audit_data.competitor_b.id,
            action="UPDATE",
            changed_fields=["display_name"],
            actor_user_id=audit_data.actor.id,
            actor_label=None,
            reason="prueba",
            reason_code="DATA_CORRECTION",
            reason_note=None,
            tenant_club_id=audit_data.tenant_a.id,
        )

    assert len(await _events("competitor", audit_data.competitor_a.id)) == before_a
    assert len(await _events("competitor", audit_data.competitor_b.id)) == before_b


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_rejects_an_entity_that_does_not_exist(audit_data: AuditData) -> None:
    before = len(await _events("competitor", audit_data.competitor_a.id))

    with pytest.raises(DomainWriteScopeError):
        await sql_insert_domain_change_log(
            entity="competitor",
            entity_id=CompetitorId(999_999),
            action="UPDATE",
            changed_fields=["display_name"],
            actor_user_id=audit_data.actor.id,
            actor_label=None,
            reason="prueba",
            reason_code="DATA_CORRECTION",
            reason_note=None,
            tenant_club_id=audit_data.tenant_a.id,
        )

    assert len(await _events("competitor", audit_data.competitor_a.id)) == before


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_rejects_an_unsupported_entity(audit_data: AuditData) -> None:
    with pytest.raises(DomainWriteScopeError):
        await sql_insert_domain_change_log(
            entity="tournament",
            entity_id=audit_data.tournament_a.id,
            action="UPDATE",
            changed_fields=["name"],
            actor_user_id=audit_data.actor.id,
            actor_label=None,
            reason="prueba",
            reason_code=None,
            reason_note=None,
            tenant_club_id=audit_data.tenant_a.id,
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_platform_entities_have_no_authorized_audit_writer(audit_data: AuditData) -> None:
    """Una entidad de plataforma (tenant NULL) no puede recibir evento: ni con tenant, ni con NULL.

    No hay todavia operacion de producto que audite una entidad sin tenant, asi que la columna
    admite NULL (historico) pero ningun escritor lo produce.
    """
    record = await database.fetch_one(
        query=INSERT_PLATFORM_COMPETITOR, values={"display_name": "Identidad De Plataforma"}
    )
    assert record is not None
    platform_id = CompetitorId(int(record._mapping["id"]))

    for tenant_club_id in (audit_data.tenant_a.id, None):
        with pytest.raises(DomainWriteScopeError):
            await sql_insert_domain_change_log(
                entity="competitor",
                entity_id=platform_id,
                action="CREATE",
                changed_fields=["display_name"],
                actor_user_id=audit_data.actor.id,
                actor_label=None,
                reason="prueba",
                reason_code="PLANNED_ENTRY",
                reason_note=None,
                tenant_club_id=tenant_club_id,  # type: ignore[arg-type]
            )

    assert await _events("competitor", platform_id) == []

    await database.execute(
        query=DELETE_ANY_AUDIT_FOR_COMPETITOR, values={"competitor_id": platform_id}
    )
    await database.execute(
        query="DELETE FROM competitors WHERE id = :competitor_id",
        values={"competitor_id": platform_id},
    )


# --- atomicidad --------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_failed_audit_reverts_the_whole_operation(audit_data: AuditData) -> None:
    """La auditoria y la operacion van en la misma transaccion: si falla una, no queda la otra."""
    tenant_ids = [audit_data.tenant_a.id]
    before = await database.fetch_val(
        query="SELECT count(*) FROM competitors WHERE managed_by_club_id = ANY(:ids)",
        values={"ids": tenant_ids},
    )

    with pytest.raises(DomainWriteScopeError):
        async with database.transaction():
            competitor = await sql_insert_competitor(
                display_name="No Debe Persistir", managed_by_club_id=audit_data.tenant_a.id
            )
            await sql_insert_domain_change_log(
                entity="competitor",
                entity_id=competitor.id,
                action="CREATE",
                changed_fields=["display_name"],
                actor_user_id=audit_data.actor.id,
                actor_label=None,
                reason="prueba",
                reason_code="PLANNED_ENTRY",
                reason_note=None,
                tenant_club_id=audit_data.tenant_b.id,
            )

    after = await database.fetch_val(
        query="SELECT count(*) FROM competitors WHERE managed_by_club_id = ANY(:ids)",
        values={"ids": tenant_ids},
    )
    assert after == before
    assert await _events("competitor", CompetitorId(999_999)) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_actor_without_access_writes_nothing(audit_data: AuditData) -> None:
    before = len(await _events("competitor", audit_data.competitor_a.id))

    with pytest.raises(TenantNotAuthorizedError):
        await create_competitor(audit_data.context_outsider, display_name="Sin Acceso")

    assert len(await _events("competitor", audit_data.competitor_a.id)) == before


# --- historial de nombres: ambito por tenant ---------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_name_history_helpers_cannot_touch_another_tenant(audit_data: AuditData) -> None:
    closed = await sql_close_open_competitor_name_history(
        competitor_id=audit_data.competitor_b.id, tenant_club_id=audit_data.tenant_a.id
    )
    assert closed == 0, "el cierre no alcanza al competidor de otro tenant"

    open_b = await database.fetch_all(
        query=SELECT_OPEN_HISTORY, values={"competitor_id": audit_data.competitor_b.id}
    )
    assert len(open_b) == 1, "el historial vigente del otro tenant sigue abierto"

    with pytest.raises(DomainWriteScopeError):
        await sql_insert_competitor_name_history(
            competitor_id=audit_data.competitor_b.id,
            tenant_club_id=audit_data.tenant_a.id,
            display_name="Nombre Ajeno",
            changed_by_user_id=audit_data.actor.id,
        )

    open_b_after = await database.fetch_all(
        query=SELECT_OPEN_HISTORY, values={"competitor_id": audit_data.competitor_b.id}
    )
    assert [row._mapping["display_name"] for row in open_b_after] == [
        row._mapping["display_name"] for row in open_b
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_legitimate_rename_still_closes_and_opens_the_history(audit_data: AuditData) -> None:
    updated = await update_competitor_display_name(
        audit_data.context_b,
        audit_data.competitor_b.id,
        CompetitorBasicDataUpdate(display_name="Identidad De B Renombrada"),
        reason_code="DATA_CORRECTION",
    )

    assert updated.display_name == "Identidad De B Renombrada"
    open_rows = await database.fetch_all(
        query=SELECT_OPEN_HISTORY, values={"competitor_id": audit_data.competitor_b.id}
    )
    assert [row._mapping["display_name"] for row in open_rows] == ["Identidad De B Renombrada"]
    events = await _events("competitor", audit_data.competitor_b.id)
    assert [event["action"] for event in events] == ["CREATE", "UPDATE"]
    assert [event["tenant_club_id"] for event in events] == [audit_data.tenant_b.id] * 2


# --- una operacion efectiva, un evento; no-op sin evento ---------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_effective_operations_write_one_event_and_no_ops_write_none(
    audit_data: AuditData,
) -> None:
    competitor = await create_competitor(audit_data.context_a, display_name="Ciclo De Vida")
    assert len(await _events("competitor", competitor.id)) == 1

    await update_competitor_display_name(
        audit_data.context_a,
        competitor.id,
        CompetitorBasicDataUpdate(display_name="Ciclo De Vida Renombrado"),
        reason_code="DATA_CORRECTION",
    )
    assert len(await _events("competitor", competitor.id)) == 2

    await update_competitor_display_name(
        audit_data.context_a,
        competitor.id,
        CompetitorBasicDataUpdate(display_name="Ciclo De Vida Renombrado"),
    )
    assert len(await _events("competitor", competitor.id)) == 2, "no-op sin evento"

    await deactivate_competitor(audit_data.context_a, competitor.id, reason_code="ADMINISTRATIVE")
    assert len(await _events("competitor", competitor.id)) == 3

    await deactivate_competitor(audit_data.context_a, competitor.id, reason_code="ADMINISTRATIVE")
    assert len(await _events("competitor", competitor.id)) == 3, "no-op sin evento"

    events = await _events("competitor", competitor.id)
    assert all(
        not isinstance(field, int) for event in events for field in event["changed_fields"]
    ), "changed_fields solo guarda nombres de campo"


# --- inscripciones: tenant en ambos eventos y snapshots intactos -------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_registration_events_carry_the_tenant_without_touching_snapshots(
    audit_data: AuditData,
) -> None:
    registration = await create_registration(
        audit_data.context_collaborator,
        audit_data.tournament_a.id,
        build_draft(competitor_id=audit_data.competitor_a.id),
        reason_code="PLANNED_ENTRY",
    )
    before = await database.fetch_one(
        query=SELECT_SNAPSHOTS, values={"registration_id": registration.id}
    )
    assert before is not None

    await withdraw_registration(
        audit_data.context_collaborator, registration.id, reason_code="ADMINISTRATIVE"
    )

    after = await database.fetch_one(
        query=SELECT_SNAPSHOTS, values={"registration_id": registration.id}
    )
    assert after is not None
    assert dict(after._mapping) == dict(before._mapping), "la retirada no toca los snapshots"

    events = await _events("tournament_registration", registration.id)
    assert [event["action"] for event in events] == ["CREATE", "WITHDRAW"]
    assert [event["tenant_club_id"] for event in events] == [audit_data.tenant_a.id] * 2


@pytest.mark.asyncio(loop_scope="session")
async def test_registration_into_another_tenants_tournament_is_rejected(
    audit_data: AuditData,
) -> None:
    """El tenant B no puede inscribir en el torneo de A: el error es el de torneo inexistente."""
    with pytest.raises(TournamentNotFoundError):
        await create_registration(
            audit_data.context_b,
            audit_data.tournament_a.id,
            build_draft(competitor_id=audit_data.competitor_b.id),
            reason_code="PLANNED_ENTRY",
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_competitor_and_registration_of_another_tenant_are_not_readable(
    audit_data: AuditData,
) -> None:
    registration = await create_registration(
        audit_data.context_a,
        audit_data.tournament_a.id,
        build_draft(competitor_id=audit_data.competitor_a.id),
        reason_code="PLANNED_ENTRY",
    )

    assert (
        await get_competitor(audit_data.competitor_b.id, tenant_club_id=audit_data.tenant_a.id)
        is None
    )
    assert (
        await get_competitor(audit_data.competitor_a.id, tenant_club_id=audit_data.tenant_b.id)
        is None
    )
    assert await get_registration(registration.id, tenant_club_id=audit_data.tenant_b.id) is None
    assert (
        await get_competitor(audit_data.competitor_a.id, tenant_club_id=audit_data.tenant_a.id)
        is not None
    )
