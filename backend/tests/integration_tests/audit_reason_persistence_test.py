# pylint: disable=redefined-outer-name  # `reason_data` es el fixture de este modulo.
"""S3.3c-2 — persistencia del motivo cerrado de auditoria (LAB ONLY).

TDD: estas pruebas se escribieron **antes** de la integracion y fallaron en rojo (las operaciones
no aceptaban ``reason_code``/``reason_note`` y la auditoria no guardaba esas columnas); la
integracion posterior las lleva a verde.

Contrato verificado:

* el evento guarda el **codigo** cerrado y, si procede, la **nota**; el texto que se conserva es el
  canonico del catalogo, nunca el que envia el llamador;
* una entrada invalida (codigo desconocido, codigo de otra accion, nota no admitida, nota con datos
  personales evidentes, nota fuera de forma) **no deja evento escrito**;
* las acciones sin codigo por defecto exigen codigo explicito;
* el escritor sigue admitiendo el aspecto previo a la migracion (``reason_code IS NULL``) para las
  entidades que no tienen catalogo, y el historico con codigo nulo sigue siendo legible.

Validacion exclusivamente contra ``bracket_test``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from databases import Database

from bracket.database import database
from bracket.logic.competitors import (
    InvalidCompetitorDataError,
    activate_competitor,
    create_competitor,
    deactivate_competitor,
)
from bracket.logic.registrations import (
    InvalidRegistrationDataError,
    confirm_registration,
    create_registration,
    disqualify_registration,
    withdraw_registration,
)
from bracket.models.db.domain import RegistrationDraftData
from bracket.sql.domain_writes import sql_insert_domain_change_log
from tests.integration_tests.registration_fixtures import (
    RegistrationData,
    build_draft,
    registration_data_context,
)

SELECT_EVENTS = """
    SELECT action, reason, reason_code, reason_note
    FROM domain_change_log
    WHERE entity = :entity AND entity_id = :entity_id
    ORDER BY id
"""

INSERT_LEGACY_EVENT = """
    INSERT INTO domain_change_log (entity, entity_id, action, changed_fields, reason)
    VALUES ('competitor', :entity_id, 'CREATE', '{}'::text[], 'alta historica sin codigo')
"""


async def _events(entity: str, entity_id: int) -> list[dict[str, Any]]:
    records = await database.fetch_all(
        query=SELECT_EVENTS, values={"entity": entity, "entity_id": entity_id}
    )
    return [dict(record._mapping) for record in records]


def _categorized(data: RegistrationData, key: str, **overrides: object) -> RegistrationDraftData:
    return build_draft(
        competitor_id=data.competitor_a,
        category_key=key,
        category_label=key.replace("-", " ").title(),
        sports_club_id=data.sports_club_a,
        representation="CLUB",
        **overrides,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def reason_data(reinit_database: Database) -> AsyncIterator[RegistrationData]:
    async with registration_data_context(reinit_database) as data:
        yield data


# --- competidores -------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_create_competitor_writes_the_default_code_and_canonical_text(
    reason_data: RegistrationData,
) -> None:
    competitor = await create_competitor(reason_data.context_owner_a, display_name="Motivo Cerrado")

    events = await _events("competitor", int(competitor.id))
    assert len(events) == 1
    assert events[0]["action"] == "CREATE"
    assert events[0]["reason_code"] == "PLANNED_ENTRY"
    assert events[0]["reason"] == "alta de competidor"
    assert events[0]["reason_note"] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_deactivate_competitor_persists_code_and_note(
    reason_data: RegistrationData,
) -> None:
    await deactivate_competitor(
        reason_data.context_owner_a,
        reason_data.competitor_a,
        reason_code="DATA_ERROR",
        reason_note="  duplicado del competidor 12  ",
    )

    events = await _events("competitor", int(reason_data.competitor_a))
    assert [event["action"] for event in events] == ["DEACTIVATE"]
    assert events[0]["reason_code"] == "DATA_ERROR"
    assert events[0]["reason_note"] == "duplicado del competidor 12", "la nota se normaliza"
    assert events[0]["reason"] == "baja por error de datos"


@pytest.mark.asyncio(loop_scope="session")
async def test_activate_competitor_reports_the_requested_code(
    reason_data: RegistrationData,
) -> None:
    await deactivate_competitor(reason_data.context_owner_a, reason_data.competitor_a)

    await activate_competitor(
        reason_data.context_owner_a, reason_data.competitor_a, reason_code="REQUESTED_BY_ATHLETE"
    )

    events = await _events("competitor", int(reason_data.competitor_a))
    assert [event["reason_code"] for event in events] == ["ADMINISTRATIVE", "REQUESTED_BY_ATHLETE"]
    assert events[1]["reason"] == "reactivacion solicitada por el competidor"


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    ("code", "note"),
    [
        ("NO_EXISTE", None),
        ("READY", None),
        ("REQUESTED_BY_ATHLETE", "nota no admitida"),
        ("ADMINISTRATIVE", "aviso a persona@example.com"),
        ("ADMINISTRATIVE", "a" * 201),
        ("ADMINISTRATIVE", "   "),
        ("administrative", None),
        (None, "nota sin codigo"),
    ],
    ids=[
        "codigo-desconocido",
        "codigo-de-otra-accion",
        "nota-no-admitida",
        "nota-con-datos-evidentes",
        "nota-demasiado-larga",
        "nota-vacia",
        "codigo-en-minusculas",
        "nota-sin-codigo",
    ],
)
async def test_invalid_reason_never_writes_the_event(
    reason_data: RegistrationData, code: object, note: object
) -> None:
    before = await _events("competitor", int(reason_data.competitor_a))

    with pytest.raises(InvalidCompetitorDataError):
        await deactivate_competitor(
            reason_data.context_owner_a, reason_data.competitor_a, reason_code=code, reason_note=note
        )

    assert await _events("competitor", int(reason_data.competitor_a)) == before
    assert note is None or "persona@example.com" not in str(note)


# --- inscripciones ------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_registration_operations_persist_the_closed_reason(
    reason_data: RegistrationData,
) -> None:
    registration = await create_registration(
        reason_data.context_owner_a, reason_data.tournament_a, _categorized(reason_data, "gi-cerrado")
    )
    await confirm_registration(
        reason_data.context_owner_a, registration.id, reason_code="DATA_VERIFIED"
    )
    await withdraw_registration(
        reason_data.context_owner_a,
        registration.id,
        reason_code="ADMINISTRATIVE",
        reason_note="retirada acordada con la organizacion",
    )

    events = await _events("tournament_registration", int(registration.id))
    assert [event["reason_code"] for event in events] == [
        "PLANNED_ENTRY",
        "DATA_VERIFIED",
        "ADMINISTRATIVE",
    ]
    assert [event["reason"] for event in events] == [
        "alta de inscripcion",
        "confirmacion con datos verificados",
        "retirada administrativa",
    ]
    assert events[2]["reason_note"] == "retirada acordada con la organizacion"


@pytest.mark.asyncio(loop_scope="session")
async def test_withdraw_requires_an_explicit_code(reason_data: RegistrationData) -> None:
    registration = await create_registration(
        reason_data.context_owner_a,
        reason_data.tournament_a,
        _categorized(reason_data, "gi-sin-motivo"),
    )

    with pytest.raises(InvalidRegistrationDataError):
        await withdraw_registration(reason_data.context_owner_a, registration.id)

    events = await _events("tournament_registration", int(registration.id))
    assert [event["action"] for event in events] == ["CREATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_disqualify_rejects_a_code_of_another_action(reason_data: RegistrationData) -> None:
    registration = await create_registration(
        reason_data.context_owner_a,
        reason_data.tournament_a,
        _categorized(reason_data, "gi-codigo-cruzado"),
    )

    with pytest.raises(InvalidRegistrationDataError):
        await disqualify_registration(
            reason_data.context_owner_a, registration.id, reason_code="WITHDRAWAL_REQUEST"
        )


# --- compatibilidad con el historico ------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_historical_events_without_code_are_still_valid_and_readable(
    reason_data: RegistrationData,
) -> None:
    await database.execute(
        query=INSERT_LEGACY_EVENT, values={"entity_id": int(reason_data.competitor_a)}
    )

    events = await _events("competitor", int(reason_data.competitor_a))
    assert events[0]["reason_code"] is None
    assert events[0]["reason"] == "alta historica sin codigo"

    competitor = await create_competitor(
        reason_data.context_owner_a, display_name="Posterior Al Historico"
    )
    fresh = await _events("competitor", int(competitor.id))
    assert fresh[0]["reason_code"] == "PLANNED_ENTRY"


@pytest.mark.asyncio(loop_scope="session")
async def test_writer_keeps_the_pre_migration_shape_for_uncatalogued_entities(
    reason_data: RegistrationData,
) -> None:
    await sql_insert_domain_change_log(
        entity="sports_club",
        entity_id=int(reason_data.sports_club_a),
        action="CREATE",
        changed_fields=["name"],
        actor_user_id=reason_data.context_owner_a.actor_user_id,
        actor_label=reason_data.context_owner_a.actor_label,
        reason="alta de academia",
        reason_code=None,
        reason_note=None,
        tenant_club_id=reason_data.context_owner_a.tenant_club_id,
    )

    events = await _events("sports_club", int(reason_data.sports_club_a))
    assert len(events) == 1
    assert events[0]["reason_code"] is None
