# pylint: disable=redefined-outer-name  # `registration_data` es un fixture de modulo.
"""S3.1 de F3B - nucleo de inscripciones: ciclo de escritura (alta, borrador, confirmacion).

TDD: estas pruebas se escribieron y se ejecutaron **antes** de
``bracket/logic/registrations.py`` y ``bracket/sql/registration_writes.py`` (rojo con
``ModuleNotFoundError``) y despues en verde con la implementacion.

Cubren el ciclo de vida: alta y snapshots derivados, multicategoria y unicidad por
categoria (indice unico parcial, no un SELECT previo), borrador editable y sin cambios
fantasma, confirmacion idempotente con snapshot congelado, concurrencia real, rollback
integral si falla la auditoria y ausencia de PII.

Datos, *fixture* y utilidades en ``registration_fixtures.py``. Base de datos
exclusivamente ``bracket_test``.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio
from databases import Database
from heliclockter import datetime_utc

import bracket.logic.registrations as registrations_module
from bracket.database import database
from bracket.logic.competitors import (
    update_competitor_display_name,
)
from bracket.logic.registrations import (
    CompetitorNotSelectableError,
    DuplicateRegistrationError,
    InvalidRegistrationStateError,
    RegistrationNotFoundError,
    SportsClubNotSelectableError,
    confirm_registration,
    create_registration,
    update_registration_draft,
)
from bracket.models.db.domain import (
    CompetitorBasicDataUpdate,
    RegistrationDraftData,
    TournamentRegistration,
)
from bracket.sql import registration_writes
from bracket.sql.domain_reads import get_registration, get_tournament_registrations
from bracket.utils.id_types import (
    TournamentRegistrationId,
)
from tests.integration_tests.registration_fixtures import (
    INSERT_RAW_REGISTRATION,
    RegistrationData,
    audit_rows,
    build_draft,
    count_current_registrations,
    fetch_all_audit_rows_as_text,
    registration_data_context,
    set_competitor_active,
    set_sports_club_active,
)


@pytest_asyncio.fixture(loop_scope="session")
async def registration_data(reinit_database: Database) -> AsyncIterator[RegistrationData]:
    """Datos de S3.1: envuelve ``registration_data_context`` y garantiza la limpieza."""
    async with registration_data_context(reinit_database) as data:
        yield data


@pytest.mark.asyncio(loop_scope="session")
async def test_create_registration_with_represented_academy_derives_snapshots(
    registration_data: RegistrationData,
) -> None:
    """Alta valida: academia explicita, afiliacion coherente y snapshots derivados."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            representation="CLUB",
            sports_club_id=registration_data.sports_club_a,
            affiliation_id=registration_data.affiliation_a,
            category_key="gi-peso-83",
            category_label="Gi Peso -83 kg",
        ),
    )

    assert registration.status == "DRAFT"
    assert registration.revision == 1
    assert registration.identity_status == "VERIFIED"
    assert registration.competitor_id == registration_data.competitor_a
    # Snapshot derivado de la identidad, no del payload.
    assert registration.competitor_name_snapshot == "Ana Gomez"
    assert registration.sports_club_id == registration_data.sports_club_a
    assert registration.sports_club_name_snapshot == "Academia Propia A"
    assert registration.category_key == "gi-peso-83"
    assert registration.category_label == "Gi Peso -83 kg"
    assert registration.corrects_registration_id is None
    assert registration.superseded_by_registration_id is None

    audit = await audit_rows(registration.id)
    assert len(audit) == 1
    assert audit[0]["action"] == "CREATE"
    assert audit[0]["entity"] == "tournament_registration"
    assert audit[0]["entity_id"] == registration.id
    assert audit[0]["actor_user_id"] == registration_data.context_owner_a.actor_user_id
    assert audit[0]["reason"] == "alta de inscripcion"
    changed_fields = audit[0]["changed_fields"]
    assert isinstance(changed_fields, list)
    assert "status" in changed_fields
    assert "competitor_name_snapshot" in changed_fields
    assert "sports_club_name_snapshot" in changed_fields


@pytest.mark.asyncio(loop_scope="session")
async def test_create_registration_independent_without_academy(
    registration_data: RegistrationData,
) -> None:
    """Participacion independiente: sin academia, sin snapshot de academia."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            category_key="no-gi-absoluto",
            category_label="No-Gi Absoluto",
        ),
    )

    assert registration.representation == "INDEPENDENT"
    assert registration.sports_club_id is None
    assert registration.sports_club_name_snapshot is None
    assert registration.affiliation_id is None
    assert registration.competitor_name_snapshot == "Ana Gomez"


@pytest.mark.asyncio(loop_scope="session")
async def test_same_competitor_can_be_registered_in_two_categories(
    registration_data: RegistrationData,
) -> None:
    """Multicategoria: un competidor, varias categorias del mismo torneo."""
    first = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            representation="CLUB",
            sports_club_id=registration_data.sports_club_a,
            category_key="gi-peso-83",
            category_label="Gi Peso -83 kg",
        ),
    )
    second = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            representation="CLUB",
            sports_club_id=registration_data.sports_club_a,
            category_key="no-gi-absoluto",
            category_label="No-Gi Absoluto",
        ),
    )

    assert first.id != second.id
    assert (
        await count_current_registrations(
            tournament_id=registration_data.tournament_a,
            competitor_id=registration_data.competitor_a,
        )
        == 2
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_duplicate_current_registration_is_rejected_by_the_index(
    registration_data: RegistrationData,
) -> None:
    """Duplicado: la proteccion es el indice unico parcial, no un SELECT previo."""
    await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            category_key="gi-peso-83",
            category_label="Gi Peso -83 kg",
        ),
    )

    with pytest.raises(DuplicateRegistrationError) as exc_info:
        await create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            build_draft(
                competitor_id=registration_data.competitor_a,
                category_key="gi-peso-83",
                category_label="Gi Peso -83 kg",
            ),
        )

    # El error de dominio envuelve la violacion real de PostgreSQL.
    assert isinstance(exc_info.value.__cause__, asyncpg.exceptions.UniqueViolationError)
    assert (
        await count_current_registrations(
            tournament_id=registration_data.tournament_a,
            competitor_id=registration_data.competitor_a,
        )
        == 1
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_uncategorized_registration_is_unique_per_competitor(
    registration_data: RegistrationData,
) -> None:
    """Categoria NULL: el indice parcial propio del esquema F3A la sigue limitando a una."""
    first = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(competitor_id=registration_data.competitor_a),
    )
    assert first.category_key is None
    assert first.category_label is None

    with pytest.raises(DuplicateRegistrationError):
        await create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            build_draft(competitor_id=registration_data.competitor_a),
        )

    # Sin competidor verificado, la unicidad no aplica: dos desconocidos pueden coexistir.
    other = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(competitor_name_snapshot="Sin Ficha Historica"),
    )
    assert other.category_key is None
    assert other.competitor_id is None


@pytest.mark.asyncio(loop_scope="session")
async def test_draft_edit_replaces_state_and_is_audited(
    registration_data: RegistrationData,
) -> None:
    """Un borrador se edita en sitio: cambia la representacion y queda auditado."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            category_key="absoluto",
            category_label="Absoluto",
        ),
    )

    updated = await update_registration_draft(
        registration_data.context_owner_a,
        registration.id,
        build_draft(
            competitor_id=registration_data.competitor_a,
            representation="CLUB",
            sports_club_id=registration_data.sports_club_a,
            affiliation_id=registration_data.affiliation_a,
            category_key="gi-absoluto",
            category_label="Gi Absoluto",
        ),
    )

    assert updated.id == registration.id
    assert updated.revision == 1
    assert updated.status == "DRAFT"
    assert updated.representation == "CLUB"
    assert updated.sports_club_name_snapshot == "Academia Propia A"
    assert updated.category_key == "gi-absoluto"
    assert updated.affiliation_id == registration_data.affiliation_a

    audit = await audit_rows(registration.id)
    assert [row["action"] for row in audit] == ["CREATE", "UPDATE"]
    changed_fields = audit[1]["changed_fields"]
    assert isinstance(changed_fields, list)
    assert set(changed_fields) == {
        "representation",
        "sports_club_id",
        "sports_club_name_snapshot",
        "affiliation_id",
        "category_key",
        "category_label",
    }
    # Ni un valor en la auditoria: solo nombres de campo.
    assert "Absoluto" not in str(changed_fields)


@pytest.mark.asyncio(loop_scope="session")
async def test_draft_edit_without_changes_is_a_noop(registration_data: RegistrationData) -> None:
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(competitor_id=registration_data.competitor_a),
    )

    same = await update_registration_draft(
        registration_data.context_owner_a,
        registration.id,
        build_draft(competitor_id=registration_data.competitor_a),
    )

    assert same.updated_at == registration.updated_at
    assert len(await audit_rows(registration.id)) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_draft_edit_can_link_an_unknown_registration_to_a_competitor(
    registration_data: RegistrationData,
) -> None:
    """Vinculacion manual posterior: la identidad se resuelve antes de confirmar."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(competitor_name_snapshot="Sin Ficha Historica"),
    )
    assert registration.competitor_id is None
    assert registration.identity_status == "UNVERIFIED"

    linked = await update_registration_draft(
        registration_data.context_owner_a,
        registration.id,
        build_draft(competitor_id=registration_data.competitor_a),
    )

    assert linked.competitor_id == registration_data.competitor_a
    assert linked.identity_status == "VERIFIED"
    assert linked.competitor_name_snapshot == "Ana Gomez"

    audit = await audit_rows(registration.id)
    linked_fields = audit[1]["changed_fields"]
    assert isinstance(linked_fields, list)
    assert set(linked_fields) == {
        "competitor_id",
        "identity_status",
        "competitor_name_snapshot",
    }


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_registration_cannot_be_edited_into_a_duplicate(
    registration_data: RegistrationData,
) -> None:
    """Vincular una identidad que ya ocupa la categoria choca con el indice unico."""
    await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            category_key="gi-absoluto",
            category_label="Gi Absoluto",
        ),
    )
    pending = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_name_snapshot="Sin Ficha Historica",
            category_key="gi-absoluto",
            category_label="Gi Absoluto",
        ),
    )

    with pytest.raises(DuplicateRegistrationError):
        await update_registration_draft(
            registration_data.context_owner_a,
            pending.id,
            build_draft(
                competitor_id=registration_data.competitor_a,
                category_key="gi-absoluto",
                category_label="Gi Absoluto",
            ),
        )

    # Y el borrador sigue como estaba: la transaccion entera se revirtio.
    untouched = await get_registration(pending.id, tenant_club_id=registration_data.tenant_a)
    assert untouched is not None
    assert untouched.competitor_id is None
    assert len(await audit_rows(pending.id)) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_freezes_snapshots_against_a_later_rename(
    registration_data: RegistrationData,
) -> None:
    """Al confirmar se congelan nombre, academia y categoria."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            representation="CLUB",
            sports_club_id=registration_data.sports_club_a,
            category_key="gi-peso-83",
            category_label="Gi Peso -83 kg",
        ),
    )
    confirmed = await confirm_registration(registration_data.context_owner_a, registration.id)
    assert confirmed.status == "CONFIRMED"

    await update_competitor_display_name(
        registration_data.context_owner_a,
        registration_data.competitor_a,
        CompetitorBasicDataUpdate(display_name="Ana Gomez Rebolledo"),
    )

    frozen = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert frozen is not None
    assert frozen.competitor_name_snapshot == "Ana Gomez"
    assert frozen.sports_club_name_snapshot == "Academia Propia A"
    assert frozen.category_key == "gi-peso-83"
    assert frozen.category_label == "Gi Peso -83 kg"


@pytest.mark.asyncio(loop_scope="session")
async def test_confirmed_registration_cannot_be_edited_in_place(
    registration_data: RegistrationData,
) -> None:
    """Nunca modificar en silencio una inscripcion confirmada."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(competitor_id=registration_data.competitor_a),
    )
    await confirm_registration(registration_data.context_owner_a, registration.id)

    with pytest.raises(InvalidRegistrationStateError):
        await update_registration_draft(
            registration_data.context_owner_a,
            registration.id,
            build_draft(
                competitor_id=registration_data.competitor_a,
                category_key="gi-absoluto",
                category_label="Gi Absoluto",
            ),
        )

    unchanged = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert unchanged is not None
    assert unchanged.category_key is None
    assert [row["action"] for row in await audit_rows(registration.id)] == [
        "CREATE",
        "CONFIRM",
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_is_idempotent_and_does_not_duplicate_audit(
    registration_data: RegistrationData,
) -> None:
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(competitor_id=registration_data.competitor_a),
    )
    first = await confirm_registration(registration_data.context_owner_a, registration.id)
    second = await confirm_registration(registration_data.context_owner_a, registration.id)

    assert first.status == second.status == "CONFIRMED"
    assert first.updated_at == second.updated_at
    assert [row["action"] for row in await audit_rows(registration.id)] == [
        "CREATE",
        "CONFIRM",
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_rejects_an_academy_deactivated_after_the_draft(
    registration_data: RegistrationData,
) -> None:
    """A5 tambien al confirmar: si la academia se desactiva despues del borrador, no se confirma.

    El borrador no se pierde ni queda a medias: sigue ``DRAFT``, sin evento ``CONFIRM``, y
    vuelve a ser confirmable en cuanto la academia se reactiva.
    """
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            representation="CLUB",
            sports_club_id=registration_data.sports_club_a,
        ),
    )
    await set_sports_club_active(registration_data.sports_club_a, active=False)

    with pytest.raises(SportsClubNotSelectableError):
        await confirm_registration(registration_data.context_owner_a, registration.id)

    pending = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert pending is not None
    assert pending.status == "DRAFT"
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE"]

    await set_sports_club_active(registration_data.sports_club_a, active=True)
    confirmed = await confirm_registration(registration_data.context_owner_a, registration.id)
    assert confirmed.status == "CONFIRMED"
    assert confirmed.sports_club_name_snapshot == "Academia Propia A"


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_statement_refuses_an_inactive_academy(
    registration_data: RegistrationData,
) -> None:
    """La regla A5 vive tambien en la sentencia: sin el chequeo de Python, la fila no cambia."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            representation="CLUB",
            sports_club_id=registration_data.sports_club_a,
        ),
    )
    await set_sports_club_active(registration_data.sports_club_a, active=False)

    statement_result = await registration_writes.sql_confirm_registration(
        registration_id=registration.id, tenant_club_id=registration_data.tenant_a
    )

    assert statement_result is None
    untouched = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert untouched is not None
    assert untouched.status == "DRAFT"

    await set_sports_club_active(registration_data.sports_club_a, active=True)


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_rejects_a_competitor_deactivated_after_the_draft(
    registration_data: RegistrationData,
) -> None:
    """S3.1a: un borrador cuyo competidor se ha dado de baja despues no puede confirmarse.

    La recuperacion del borrador se prueba con el *fixture* (``set_competitor_active``):
    S2-bis (``activate_competitor``) todavia no existe y no se simula como API.
    """
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(competitor_id=registration_data.competitor_a),
    )
    await set_competitor_active(registration_data.competitor_a, active=False)

    with pytest.raises(CompetitorNotSelectableError):
        await confirm_registration(registration_data.context_owner_a, registration.id)

    untouched = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert untouched is not None
    assert untouched.status == "DRAFT"
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE"]

    await set_competitor_active(registration_data.competitor_a, active=True)
    confirmed = await confirm_registration(registration_data.context_owner_a, registration.id)

    assert confirmed.status == "CONFIRMED"
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE", "CONFIRM"]


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_statement_refuses_an_inactive_competitor(
    registration_data: RegistrationData,
) -> None:
    """La elegibilidad vive tambien en la sentencia: sin el chequeo de Python la fila no cambia."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(competitor_id=registration_data.competitor_a),
    )
    await set_competitor_active(registration_data.competitor_a, active=False)

    statement_result = await registration_writes.sql_confirm_registration(
        registration_id=registration.id, tenant_club_id=registration_data.tenant_a
    )

    assert statement_result is None
    untouched = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert untouched is not None
    assert untouched.status == "DRAFT"

    await set_competitor_active(registration_data.competitor_a, active=True)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_confirmed_registration_survives_the_deactivation_of_its_competitor(
    registration_data: RegistrationData,
) -> None:
    """La baja logica no borra ni altera el historico: snapshots, auditoria y consulta intactos."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            representation="CLUB",
            sports_club_id=registration_data.sports_club_a,
            affiliation_id=registration_data.affiliation_a,
        ),
    )
    confirmed = await confirm_registration(registration_data.context_owner_a, registration.id)
    await set_competitor_active(registration_data.competitor_a, active=False)

    stored = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert stored is not None
    assert stored.status == "CONFIRMED"
    assert stored.competitor_id == registration_data.competitor_a
    assert stored.competitor_name_snapshot == confirmed.competitor_name_snapshot == "Ana Gomez"
    assert stored.sports_club_name_snapshot == "Academia Propia A"
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE", "CONFIRM"]

    listed = await get_tournament_registrations(
        tournament_id=registration_data.tournament_a, tenant_club_id=registration_data.tenant_a
    )
    assert [row.id for row in listed] == [registration.id]
    assert (
        await count_current_registrations(
            tournament_id=registration_data.tournament_a,
            competitor_id=registration_data.competitor_a,
        )
        == 1
    )

    await set_competitor_active(registration_data.competitor_a, active=True)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_deactivation_racing_a_confirmation_leaves_no_partial_state(
    registration_data: RegistrationData,
) -> None:
    """Carrera baja logica <-> confirmacion: gane quien gane, no puede quedar estado parcial.

    Ambos ordenes de commit son legales (confirmar y despues dar de baja conserva el
    historico). Lo que la prueba fija es que no existe un tercer resultado: CONFIRMED sin
    evento CONFIRM, DRAFT con evento CONFIRM, o una transicion a medias.
    """
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(competitor_id=registration_data.competitor_a),
    )

    confirmation, deactivation = await asyncio.gather(
        confirm_registration(registration_data.context_owner_a, registration.id),
        set_competitor_active(registration_data.competitor_a, active=False),
        return_exceptions=True,
    )

    outcome = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert outcome is not None
    actions = [row["action"] for row in await audit_rows(registration.id)]

    if isinstance(confirmation, CompetitorNotSelectableError):
        assert outcome.status == "DRAFT"
        assert actions == ["CREATE"]
    else:
        assert isinstance(confirmation, TournamentRegistration)
        assert outcome.status == "CONFIRMED"
        assert actions == ["CREATE", "CONFIRM"]
    assert deactivation is None
    assert (
        await count_current_registrations(
            tournament_id=registration_data.tournament_a,
            competitor_id=registration_data.competitor_a,
        )
        == 1
    )

    await set_competitor_active(registration_data.competitor_a, active=True)


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_of_an_independent_registration_ignores_deactivations(
    registration_data: RegistrationData,
) -> None:
    """Un independiente no representa academia: desactivar una no le afecta."""
    await set_sports_club_active(registration_data.sports_club_a, active=False)
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(competitor_id=registration_data.competitor_a),
    )

    confirmed = await confirm_registration(registration_data.context_owner_a, registration.id)

    assert confirmed.status == "CONFIRMED"
    assert confirmed.sports_club_id is None
    await set_sports_club_active(registration_data.sports_club_a, active=True)


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_rejects_a_registration_that_is_not_a_draft(
    registration_data: RegistrationData,
) -> None:
    """La transicion se valida: no basta con que la fila exista."""
    registration_id = await database.fetch_val(
        query=INSERT_RAW_REGISTRATION,
        values={
            "tournament_id": registration_data.tournament_a,
            "competitor_id": registration_data.competitor_a,
            "identity_status": "VERIFIED",
            "representation": "INDEPENDENT",
            "competitor_name_snapshot": "Ana Gomez",
            "status": "WITHDRAWN",
        },
    )

    with pytest.raises(InvalidRegistrationStateError):
        await confirm_registration(
            registration_data.context_owner_a, TournamentRegistrationId(registration_id)
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_registration_of_another_tenant_is_invisible(
    registration_data: RegistrationData,
) -> None:
    """Sin acceso cruzado: desde el tenant A, una inscripcion del tenant B no existe."""
    foreign = await create_registration(
        registration_data.context_owner_b,
        registration_data.tournament_b,
        build_draft(competitor_id=registration_data.competitor_b),
    )

    assert await get_registration(foreign.id, tenant_club_id=registration_data.tenant_a) is None
    with pytest.raises(RegistrationNotFoundError):
        await update_registration_draft(
            registration_data.context_owner_a,
            foreign.id,
            build_draft(competitor_id=registration_data.competitor_a),
        )
    with pytest.raises(RegistrationNotFoundError):
        await confirm_registration(registration_data.context_owner_a, foreign.id)

    ghost = TournamentRegistrationId(999_999_999)
    with pytest.raises(RegistrationNotFoundError):
        await confirm_registration(registration_data.context_owner_a, ghost)

    untouched = await get_registration(foreign.id, tenant_club_id=registration_data.tenant_b)
    assert untouched is not None
    assert untouched.status == "DRAFT"
    assert len(await audit_rows(foreign.id)) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_confirm_does_not_cross_tenants(
    registration_data: RegistrationData,
) -> None:
    """Desde otro tenant la inscripcion no existe: ni se lee ni se confirma."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(competitor_id=registration_data.competitor_a),
    )

    assert (
        await get_registration(registration.id, tenant_club_id=registration_data.tenant_b) is None
    )
    with pytest.raises(RegistrationNotFoundError):
        await confirm_registration(registration_data.context_owner_b, registration.id)

    untouched = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert untouched is not None
    assert untouched.status == "DRAFT"
    assert len(await audit_rows(registration.id)) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_creates_leave_exactly_one_current_registration(
    registration_data: RegistrationData,
) -> None:
    """Alta duplicada simultanea: gana una y la otra choca con el indice unico."""
    draft = build_draft(
        competitor_id=registration_data.competitor_a,
        category_key="gi-peso-83",
        category_label="Gi Peso -83 kg",
    )

    async def _create() -> TournamentRegistration:
        return await create_registration(
            registration_data.context_owner_a, registration_data.tournament_a, draft
        )

    results = await asyncio.gather(_create(), _create(), return_exceptions=True)

    created = [r for r in results if isinstance(r, TournamentRegistration)]
    rejected = [r for r in results if isinstance(r, DuplicateRegistrationError)]
    assert len(created) == 1
    assert len(rejected) == 1
    assert (
        await count_current_registrations(
            tournament_id=registration_data.tournament_a,
            competitor_id=registration_data.competitor_a,
        )
        == 1
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_concurrent_confirms_leave_a_single_audit_event(
    registration_data: RegistrationData,
) -> None:
    """Confirmacion repetida simultanea: un solo evento de auditoria."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(competitor_id=registration_data.competitor_a),
    )

    results = await asyncio.gather(
        confirm_registration(registration_data.context_owner_a, registration.id),
        confirm_registration(registration_data.context_owner_a, registration.id),
        return_exceptions=True,
    )

    confirmed = [result for result in results if isinstance(result, TournamentRegistration)]
    assert len(confirmed) == 2
    assert all(result.status == "CONFIRMED" for result in confirmed)
    actions = [row["action"] for row in await audit_rows(registration.id)]
    assert actions == ["CREATE", "CONFIRM"]


@pytest.mark.asyncio(loop_scope="session")
async def test_draft_update_sql_refuses_a_row_that_is_not_a_draft(
    registration_data: RegistrationData,
) -> None:
    """La proteccion contra sobrescrituras vive en el ``WHERE``, no en la lectura previa.

    Se comprueba la sentencia directamente: aunque una lectura previa hubiera visto un
    borrador, la escritura no toca una fila que ya no lo es, ni la de otro tenant.
    """
    registration_id = TournamentRegistrationId(
        await database.fetch_val(
            query=INSERT_RAW_REGISTRATION,
            values={
                "tournament_id": registration_data.tournament_a,
                "competitor_id": registration_data.competitor_a,
                "identity_status": "VERIFIED",
                "representation": "INDEPENDENT",
                "competitor_name_snapshot": "Ana Gomez",
                "status": "WITHDRAWN",
            },
        )
    )

    not_a_draft = await registration_writes.sql_update_registration_draft(
        registration_id=registration_id,
        tenant_club_id=registration_data.tenant_a,
        competitor_id=registration_data.competitor_a,
        identity_status="VERIFIED",
        representation="INDEPENDENT",
        sports_club_id=None,
        affiliation_id=None,
        category_key="gi-absoluto",
        category_label="Gi Absoluto",
        competitor_name_snapshot="Ana Gomez",
        sports_club_name_snapshot=None,
        verified_by_user_id=registration_data.context_owner_a.actor_user_id,
        verified_at=datetime_utc.now(),
    )
    assert not_a_draft is None

    foreign_tenant = await registration_writes.sql_update_registration_draft(
        registration_id=registration_id,
        tenant_club_id=registration_data.tenant_b,
        competitor_id=registration_data.competitor_a,
        identity_status="VERIFIED",
        representation="INDEPENDENT",
        sports_club_id=None,
        affiliation_id=None,
        category_key="gi-absoluto",
        category_label="Gi Absoluto",
        competitor_name_snapshot="Ana Gomez",
        sports_club_name_snapshot=None,
        verified_by_user_id=registration_data.context_owner_a.actor_user_id,
        verified_at=datetime_utc.now(),
    )
    assert foreign_tenant is None

    # Y la fila sigue intacta: la sentencia no escribio nada.
    untouched = await get_registration(registration_id, tenant_club_id=registration_data.tenant_a)
    assert untouched is not None
    assert untouched.status == "WITHDRAWN"
    assert untouched.category_key is None


@pytest.mark.asyncio(loop_scope="session")
async def test_audit_failure_rolls_back_the_registration(
    registration_data: RegistrationData, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback integral: sin evento de auditoria no hay inscripcion."""

    async def _boom(**_kwargs: object) -> None:
        raise RuntimeError("auditoria no disponible")

    monkeypatch.setattr(registrations_module, "sql_insert_domain_change_log", _boom)

    with pytest.raises(RuntimeError):
        await create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            build_draft(competitor_id=registration_data.competitor_a),
        )

    assert (
        await count_current_registrations(
            tournament_id=registration_data.tournament_a,
            competitor_id=registration_data.competitor_a,
        )
        == 0
    )


@pytest.mark.asyncio(loop_scope="session")
async def test_payload_cannot_carry_tenant_state_or_revision() -> None:
    """El contrato de entrada no expone tenant, estado, revision ni enlaces de correccion."""
    forbidden = {
        "id",
        "tournament_id",
        "managed_by_club_id",
        "status",
        "revision",
        "corrects_registration_id",
        "superseded_by_registration_id",
        "created",
        "updated_at",
        "sports_club_name_snapshot",
    }
    assert forbidden.isdisjoint(RegistrationDraftData.model_fields)

    parameters = list(inspect.signature(create_registration).parameters)
    assert parameters[:3] == ["context", "tournament_id", "data"]
    assert list(inspect.signature(confirm_registration).parameters)[:1] == ["context"]
    assert list(inspect.signature(update_registration_draft).parameters)[:3] == [
        "context",
        "registration_id",
        "data",
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_no_pii_in_errors_or_audit(registration_data: RegistrationData) -> None:
    """Ni valores en la auditoria ni datos personales en los mensajes de error."""
    sensitive_tokens = (
        "Ana",
        "Gomez",
        "Bea",
        "Ruiz",
        "Academia Propia A",
        "Absoluto",
        "Secreto",
    )

    with pytest.raises(SportsClubNotSelectableError) as exc_info:
        await create_registration(
            registration_data.context_owner_a,
            registration_data.tournament_a,
            build_draft(
                competitor_id=registration_data.competitor_a,
                representation="CLUB",
                sports_club_id=registration_data.sports_club_a_inactive,
                category_key="gi-absoluto-secreto",
                category_label="Absoluto Peso Pesado Secreto",
            ),
        )
    message = str(exc_info.value)
    assert all(token not in message for token in sensitive_tokens)

    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        build_draft(
            competitor_id=registration_data.competitor_a,
            representation="CLUB",
            sports_club_id=registration_data.sports_club_a,
            category_key="gi-absoluto",
            category_label="Gi Absoluto",
        ),
    )
    audit_dump = str(await audit_rows(registration.id))
    assert all(token not in audit_dump for token in sensitive_tokens)

    dumped_rows = await fetch_all_audit_rows_as_text()
    assert all(token not in row for row in dumped_rows for token in sensitive_tokens)
