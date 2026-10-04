# pylint: disable=redefined-outer-name  # `registration_data` es el fixture de este modulo.
"""S2-bis de F3B — concurrencia de la reactivacion de competidor y su interaccion con RS-9.

TDD: estas pruebas se escribieron y ejecutaron **antes** de implementar
``sql_activate_competitor`` / ``activate_competitor`` (rojo: el import falla) y despues en
verde. La evidencia literal de rojo/verde esta en el informe de la fase.

Que se demuestra aqui, y con que evidencia (sin *sleeps* como mecanismo):

* **Dos reactivaciones concurrentes** de la misma identidad dejan **un unico** evento
  ``ACTIVATE``: la fila del competidor las serializa y la perdedora se relee y devuelve el
  estado ya reactivado (idempotencia real, no un ``SELECT`` previo).
* **La reactivacion espera a un lector con ``FOR SHARE``** sobre la fila del competidor, que
  es exactamente el bloqueo que toma la confirmacion de una inscripcion (RS-9). Es decir, RS-9
  sigue vigente con la operacion nueva sin tocarla.
* **La confirmacion espera a una reactivacion en vuelo**: con la reactivacion detenida dentro
  de su transaccion (ya con la fila bloqueada, esperando para insertar su auditoria), la
  confirmacion se queda en su ``FOR SHARE`` y, al commitear la reactivacion, confirma.
* **Ningun ciclo de bloqueo**: la reactivacion **no** toma bloqueo sobre ninguna fila de
  ``tournament_registrations``; completa mientras otra transaccion tiene bloqueada la fila de
  la inscripcion. Con el orden competidor -> inscripcion ya fijado por RS-9, la reactivacion
  solo participa del primer eslabon.
* **El borrador se recupera**: un borrador inmovilizado por la baja (A9) vuelve a confirmarse
  despues de reactivar, sin perder snapshots ni auditoria.

Validacion exclusivamente contra la base de laboratorio (``bracket_ci`` con ``ENVIRONMENT=CI``,
o ``bracket_test``); ``bracket_dev`` no se usa para escrituras.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from databases import Database

from bracket.database import database
from bracket.logic.competitors import activate_competitor, deactivate_competitor
from bracket.logic.registrations import (
    CompetitorNotSelectableError,
    confirm_registration,
    create_registration,
    update_registration_draft,
)
from bracket.models.db.domain import Competitor, TournamentRegistration
from bracket.sql.domain_reads import get_competitor, get_registration
from tests.integration_tests.registration_fixtures import (
    WAITING_TIMEOUT_SECONDS,
    RegistrationData,
    audit_rows,
    build_draft,
    competitor_audit_rows,
    registration_data_context,
    wait_for_tuple_contention,
    wait_for_waiting_backends,
)

# Pax (solo pruebas) sobre la fila del competidor: reproduce el bloqueo que ya tiene la
# confirmacion (FOR SHARE) o cualquiera de las dos operaciones de estado (FOR NO KEY UPDATE).
SELECT_COMPETITOR_ROW_FOR_UPDATE = """
    SELECT id FROM competitors WHERE id = :competitor_id FOR UPDATE
"""

SELECT_COMPETITOR_ROW_FOR_SHARE = """
    SELECT id FROM competitors WHERE id = :competitor_id FOR SHARE
"""

# Pax sobre la fila de una inscripcion: sirve para demostrar que la reactivacion no la toca.
SELECT_REGISTRATION_ROW_FOR_UPDATE = """
    SELECT id FROM tournament_registrations WHERE id = :registration_id FOR UPDATE
"""

# La auditoria bloqueada por completo detiene a la reactivacion **dentro** de su transaccion,
# despues de haber bloqueado la fila del competidor: es la forma de tenerla "en vuelo".
LOCK_DOMAIN_CHANGE_LOG = "LOCK TABLE domain_change_log IN ACCESS EXCLUSIVE MODE"


@pytest_asyncio.fixture(loop_scope="session")
async def registration_data(reinit_database: Database) -> AsyncIterator[RegistrationData]:
    """Datos de laboratorio (mismo soporte que S3.1/S3.1a), con limpieza garantizada."""
    async with registration_data_context(reinit_database) as data:
        yield data


@pytest.mark.asyncio(loop_scope="session")
async def test_two_concurrent_activations_leave_a_single_audit_event(
    registration_data: RegistrationData,
) -> None:
    """La fila del competidor serializa: una escribe, la otra se relee; un solo ``ACTIVATE``."""
    competitor_id = registration_data.competitor_a
    await deactivate_competitor(
        registration_data.context_owner_a, competitor_id, reason="baja previa"
    )
    assert [row["action"] for row in await competitor_audit_rows(competitor_id)] == ["DEACTIVATE"]

    row_is_held = asyncio.Event()
    release_row = asyncio.Event()

    async def _hold_the_competitor_row() -> None:
        async with database.transaction():
            await database.fetch_val(
                query=SELECT_COMPETITOR_ROW_FOR_UPDATE, values={"competitor_id": competitor_id}
            )
            row_is_held.set()
            await release_row.wait()

    holder = asyncio.create_task(_hold_the_competitor_row())
    activations: list[asyncio.Task[Competitor]] = []
    try:
        await asyncio.wait_for(row_is_held.wait(), WAITING_TIMEOUT_SECONDS)
        activations = [
            asyncio.create_task(
                activate_competitor(
                    registration_data.context_owner_a,
                    competitor_id,
                    reason="reactivacion concurrente",
                )
            )
            for _ in range(2)
        ]
        await wait_for_waiting_backends("%UPDATE competitors%", expected=2)
        await wait_for_tuple_contention("competitors")
        assert not any(task.done() for task in activations), "las dos reactivaciones esperan"
    finally:
        release_row.set()
        await asyncio.gather(holder, return_exceptions=True)

    results = await asyncio.gather(*activations)
    assert [result.active for result in results] == [True, True]
    assert results[0].updated_at == results[1].updated_at, "una sola escritura gano la carrera"

    events = await competitor_audit_rows(competitor_id)
    assert [row["action"] for row in events] == ["DEACTIVATE", "ACTIVATE"], (
        "dos reactivaciones concurrentes no duplican la auditoria"
    )
    assert events[1]["reason"] == "reactivacion concurrente"


@pytest.mark.asyncio(loop_scope="session")
async def test_reactivation_waits_for_a_share_lock_on_the_competitor_row(
    registration_data: RegistrationData,
) -> None:
    """RS-9 con la operacion nueva: el bloqueo de la confirmacion (``FOR SHARE``) la detiene."""
    competitor_id = registration_data.competitor_a
    await deactivate_competitor(registration_data.context_owner_a, competitor_id)

    row_is_held = asyncio.Event()
    release_row = asyncio.Event()

    async def _hold_for_share() -> None:
        async with database.transaction():
            await database.fetch_val(
                query=SELECT_COMPETITOR_ROW_FOR_SHARE, values={"competitor_id": competitor_id}
            )
            row_is_held.set()
            await release_row.wait()

    holder = asyncio.create_task(_hold_for_share())
    activation: asyncio.Task[Competitor] | None = None
    try:
        await asyncio.wait_for(row_is_held.wait(), WAITING_TIMEOUT_SECONDS)
        activation = asyncio.create_task(
            activate_competitor(
                registration_data.context_owner_a, competitor_id, reason="reactivacion serializada"
            )
        )
        await wait_for_waiting_backends("%UPDATE competitors%")
        await wait_for_tuple_contention("competitors")
        assert not activation.done(), "el FOR SHARE de la confirmacion detiene la reactivacion"
        blocked = await get_competitor(competitor_id, tenant_club_id=registration_data.tenant_a)
        assert blocked is not None and blocked.active is False, "la fila no cambia mientras espera"
    finally:
        release_row.set()
        await asyncio.gather(holder, return_exceptions=True)

    assert activation is not None
    activated = await activation
    assert activated.active is True
    assert [row["action"] for row in await competitor_audit_rows(competitor_id)] == [
        "DEACTIVATE",
        "ACTIVATE",
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_confirmation_waits_for_a_reactivation_in_flight(
    registration_data: RegistrationData,
) -> None:
    """Con la reactivacion en vuelo (fila bloqueada, auditoria pendiente), confirmar espera."""
    context = registration_data.context_owner_a
    competitor_id = registration_data.competitor_a
    registration = await create_registration(
        context,
        registration_data.tournament_a,
        build_draft(competitor_id=competitor_id),
    )
    await deactivate_competitor(context, competitor_id, reason="baja previa")

    audit_table_is_locked = asyncio.Event()
    release_audit_table = asyncio.Event()

    async def _hold_the_audit_table() -> None:
        async with database.transaction():
            await database.execute(query=LOCK_DOMAIN_CHANGE_LOG)
            audit_table_is_locked.set()
            await release_audit_table.wait()

    holder = asyncio.create_task(_hold_the_audit_table())
    activation: asyncio.Task[Competitor] | None = None
    confirmation: asyncio.Task[TournamentRegistration] | None = None
    try:
        await asyncio.wait_for(audit_table_is_locked.wait(), WAITING_TIMEOUT_SECONDS)
        activation = asyncio.create_task(
            activate_competitor(context, competitor_id, reason="reactivacion en vuelo")
        )
        # Ya tiene la fila del competidor bloqueada y espera para escribir su auditoria.
        await wait_for_waiting_backends("%INSERT INTO domain_change_log%")
        assert not activation.done()
        uncommitted = await get_competitor(competitor_id, tenant_club_id=registration_data.tenant_a)
        assert uncommitted is not None and uncommitted.active is False, "sin lectura sucia"

        confirmation = asyncio.create_task(confirm_registration(context, registration.id))
        await wait_for_waiting_backends("%FOR SHARE%")
        await wait_for_tuple_contention("competitors")
        assert not confirmation.done(), "la confirmacion espera a la reactivacion"
    finally:
        release_audit_table.set()
        await asyncio.gather(holder, return_exceptions=True)

    assert activation is not None and confirmation is not None
    activated, confirmed = await asyncio.gather(activation, confirmation)
    assert activated.active is True
    assert confirmed.status == "CONFIRMED"
    assert confirmed.competitor_id == competitor_id
    assert await competitor_audit_rows(competitor_id) != []
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE", "CONFIRM"]


@pytest.mark.asyncio(loop_scope="session")
async def test_reactivation_does_not_touch_a_locked_registration_row(
    registration_data: RegistrationData,
) -> None:
    """Sin ciclo posible: la reactivacion no toma bloqueo sobre la fila de la inscripcion."""
    context = registration_data.context_owner_a
    competitor_id = registration_data.competitor_a
    registration = await create_registration(
        context,
        registration_data.tournament_a,
        build_draft(competitor_id=competitor_id),
    )
    await deactivate_competitor(context, competitor_id)

    registration_row_is_held = asyncio.Event()
    release_registration_row = asyncio.Event()

    async def _hold_the_registration_row() -> None:
        async with database.transaction():
            await database.fetch_val(
                query=SELECT_REGISTRATION_ROW_FOR_UPDATE,
                values={"registration_id": registration.id},
            )
            registration_row_is_held.set()
            await release_registration_row.wait()

    holder = asyncio.create_task(_hold_the_registration_row())
    try:
        await asyncio.wait_for(registration_row_is_held.wait(), WAITING_TIMEOUT_SECONDS)
        # Se acota la espera: si la reactivacion tomara la fila de la inscripcion, no terminaria.
        activated = await asyncio.wait_for(
            activate_competitor(
                registration_data.context_owner_a, competitor_id, reason="reactivacion sin ciclo"
            ),
            WAITING_TIMEOUT_SECONDS,
        )
        assert activated.active is True
        assert not holder.done(), "la inscripcion seguia bloqueada mientras se reactivaba"
    finally:
        release_registration_row.set()
        await asyncio.gather(holder, return_exceptions=True)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_blocked_draft_becomes_confirmable_after_reactivation(
    registration_data: RegistrationData,
) -> None:
    """El borrador inmovilizado por la baja (A9) se recupera con la reactivacion."""
    context = registration_data.context_owner_a
    competitor_id = registration_data.competitor_a
    draft = build_draft(
        competitor_id=competitor_id, category_key="adulto-azul", category_label="Adulto Azul"
    )
    registration = await create_registration(context, registration_data.tournament_a, draft)

    await deactivate_competitor(context, competitor_id, reason="baja con borrador abierto")

    with pytest.raises(CompetitorNotSelectableError):
        await confirm_registration(context, registration.id)
    with pytest.raises(CompetitorNotSelectableError):
        await update_registration_draft(context, registration.id, draft)

    blocked = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert blocked is not None and blocked.status == "DRAFT"
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE"]

    await activate_competitor(context, competitor_id, reason="reactivacion para desbloquear")

    confirmed = await confirm_registration(context, registration.id)
    assert confirmed.status == "CONFIRMED"
    assert confirmed.competitor_id == competitor_id
    assert confirmed.category_key == "adulto-azul"
    assert blocked is not None
    assert confirmed.competitor_name_snapshot == blocked.competitor_name_snapshot
    assert confirmed.sports_club_name_snapshot == blocked.sports_club_name_snapshot
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE", "CONFIRM"]


@pytest.mark.asyncio(loop_scope="session")
async def test_reactivation_keeps_confirmed_registrations_and_snapshots_intact(
    registration_data: RegistrationData,
) -> None:
    """Ronda baja -> reactivacion: la inscripcion confirmada y sus snapshots no cambian."""
    context = registration_data.context_owner_a
    competitor_id = registration_data.competitor_a
    registration = await create_registration(
        context,
        registration_data.tournament_a,
        build_draft(competitor_id=competitor_id),
    )
    confirmed = await confirm_registration(context, registration.id)
    before = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)

    await deactivate_competitor(context, competitor_id)
    await activate_competitor(context, competitor_id)

    after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert after == before, "la ida y vuelta no toca la inscripcion confirmada"
    assert after is not None
    assert after.status == "CONFIRMED"
    assert after.revision == confirmed.revision
    assert after.competitor_name_snapshot == confirmed.competitor_name_snapshot
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE", "CONFIRM"]

    competitor = await get_competitor(competitor_id, tenant_club_id=registration_data.tenant_a)
    assert competitor is not None and competitor.active is True
    assert [row["action"] for row in await competitor_audit_rows(competitor_id)] == [
        "CREATE",
        "DEACTIVATE",
        "ACTIVATE",
    ]
