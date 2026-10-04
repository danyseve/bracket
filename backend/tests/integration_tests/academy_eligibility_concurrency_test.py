# pylint: disable=redefined-outer-name  # `registration_data` es el fixture de este modulo.
"""S3.1b de F3B - RS-10: serializacion de la elegibilidad de la academia al confirmar.

RS-9 cerro la carrera entre la baja logica de un competidor y la confirmacion. RS-10 es la
misma carrera con la otra pieza de la elegibilidad: la academia representada
(``sports_clubs.active``). Hasta ahora la confirmacion **comprobaba** la academia sin
bloquearla, asi que una desactivacion que comitease entre la comprobacion y el ``UPDATE``
dejaba una inscripcion ``CONFIRMED`` representando una academia inactiva.

Que se demuestra aqui, y con que evidencia (sin *sleeps* como mecanismo):

* La lectura de la academia de la confirmacion toma ``FOR SHARE`` (RS-10): una desactivacion
  en vuelo la hace **esperar** y, al commitear, la fila deja de cumplir el filtro y la
  confirmacion rechaza sin escritura parcial. La direccion contraria —la confirmacion toma
  el bloqueo primero— obliga a la desactivacion a esperar.
* El orden de adquisicion es **competidor -> academia -> inscripcion**: con el competidor
  retenido por un tercero, la confirmacion espera en el primer eslabon y la desactivacion de
  la academia **completa** (no hay ciclo, no hay bloqueo cruzado).
* La participacion independiente (``sports_club_id IS NULL``) no toma bloqueo sobre ninguna
  academia: confirma mientras la fila de la academia esta retenida.
* Las rutas que **solo comprueban** (alta y edicion de borrador) siguen sin bloquear: el
  limite queda declarado, no oculto.
* El historico no se toca: desactivar la academia deja la inscripcion confirmada y sus
  snapshots exactamente igual, y no escribe auditoria propia.
* Sin auditoria parcial y sin PII: un rechazo no deja evento ``CONFIRM`` ni valores de fila
  en el registro de cambios.

TDD: estas pruebas se escribieron y ejecutaron **antes** de implementar el parametro
``for_share`` de ``sql_selectable_sports_club`` (rojo: ``TypeError`` por palabra clave
inesperada en la prueba unitaria; en rojo tambien las de espera, porque sin el bloqueo la
confirmacion no esperaba a nadie). La evidencia literal de rojo/verde esta en el informe.

Validacion exclusivamente contra la base de laboratorio (``bracket_ci`` con
``ENVIRONMENT=CI``, o ``bracket_test``); ``bracket_dev`` no se usa para escrituras.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from databases import Database

from bracket.database import database
from bracket.logic.registrations import (
    SportsClubNotSelectableError,
    confirm_registration,
    create_registration,
)
from bracket.models.db.domain import RegistrationDraftData, TournamentRegistration
from bracket.sql.domain_reads import get_registration
from bracket.sql.registration_writes import sql_selectable_sports_club
from bracket.utils.id_types import SportsClubId
from tests.integration_tests.registration_fixtures import (
    UPDATE_SPORTS_CLUB_ACTIVE,
    WAITING_TIMEOUT_SECONDS,
    RegistrationData,
    audit_rows,
    build_draft,
    fetch_all_audit_rows_as_text,
    registration_data_context,
    set_sports_club_active,
    wait_for_tuple_contention,
    wait_for_waiting_backends,
)

# La desactivacion real de una academia es exactamente esta sentencia: ``UPDATE`` de ``active``
# toma ``FOR NO KEY UPDATE`` sobre la fila, y ese es el bloqueo con el que conflictua el
# ``FOR SHARE`` de la confirmacion. No hay operacion de dominio todavia (no se crea aqui).
DEACTIVATE_SPORTS_CLUB = UPDATE_SPORTS_CLUB_ACTIVE

# Pax (solo pruebas) sobre la fila de la academia, para retenerla sin escribir nada.
SELECT_SPORTS_CLUB_ROW_FOR_UPDATE = """
    SELECT id FROM sports_clubs WHERE id = :sports_club_id FOR UPDATE
"""

# Pax sobre la fila de la inscripcion: detiene la confirmacion **despues** de sus dos bloqueos
# compartidos (competidor y academia) y antes de su ``UPDATE``.
SELECT_REGISTRATION_ROW_FOR_UPDATE = """
    SELECT id FROM tournament_registrations WHERE id = :registration_id FOR UPDATE
"""

# Pax sobre la fila del competidor: reproduce una baja logica en vuelo sin escribir en la fila.
SELECT_COMPETITOR_ROW_FOR_UPDATE = """
    SELECT id FROM competitors WHERE id = :competitor_id FOR UPDATE
"""

# La auditoria bloqueada por completo conserva en vuelo dos confirmaciones a la vez.
LOCK_DOMAIN_CHANGE_LOG = "LOCK TABLE domain_change_log IN ACCESS EXCLUSIVE MODE"


@pytest_asyncio.fixture(loop_scope="session")
async def registration_data(reinit_database: Database) -> AsyncIterator[RegistrationData]:
    """Datos de laboratorio (mismo soporte que S3.1/S3.1a/S2-bis), con limpieza garantizada."""
    async with registration_data_context(reinit_database) as data:
        yield data


def _club_draft(registration_data: RegistrationData) -> RegistrationDraftData:
    """Borrador valido que representa la academia propia del tenant A."""
    return build_draft(
        competitor_id=registration_data.competitor_a,
        representation="CLUB",
        sports_club_id=registration_data.sports_club_a,
        affiliation_id=registration_data.affiliation_a,
        category_key="gi-peso-83",
        category_label="Gi Peso -83 kg",
    )


async def _deactivate_in_flight(
    sports_club_id: SportsClubId, ready: asyncio.Event, release: asyncio.Event
) -> None:
    """Desactiva la academia dentro de una transaccion que queda abierta hasta ``release``."""
    async with database.transaction():
        await database.execute(
            query=DEACTIVATE_SPORTS_CLUB,
            values={"sports_club_id": sports_club_id, "active": False},
        )
        ready.set()
        await release.wait()


@pytest.mark.asyncio(loop_scope="session")
async def test_sql_selectable_sports_club_takes_a_share_lock_that_blocks_a_deactivation(
    registration_data: RegistrationData,
) -> None:
    """RS-10 en la propia guarda: ``for_share=True`` retiene la fila y la baja espera."""
    sports_club_id = registration_data.sports_club_a
    deactivation: asyncio.Task[None] | None = None
    async with database.transaction():
        held = await sql_selectable_sports_club(sports_club_id, for_share=True)
        assert held is not None and held.active is True
        deactivation = asyncio.create_task(
            database.execute(
                query=DEACTIVATE_SPORTS_CLUB,
                values={"sports_club_id": sports_club_id, "active": False},
            )
        )
        # Evidencia positiva del propio PostgreSQL: hay una sentencia de baja esperando.
        await wait_for_waiting_backends("%UPDATE sports_clubs%")
        await wait_for_tuple_contention("sports_clubs")
        assert not deactivation.done(), "el FOR SHARE retiene la fila de la academia"
    assert deactivation is not None
    await asyncio.wait_for(deactivation, WAITING_TIMEOUT_SECONDS)
    assert await sql_selectable_sports_club(sports_club_id) is None, "ya no es representable"


@pytest.mark.asyncio(loop_scope="session")
async def test_the_plain_read_does_not_lock_the_academy(
    registration_data: RegistrationData,
) -> None:
    """Alta y edicion de borrador solo comprueban: sin ``for_share`` la lectura no bloquea."""
    sports_club_id = registration_data.sports_club_a
    deactivation_ready = asyncio.Event()
    release_deactivation = asyncio.Event()

    holder = asyncio.create_task(
        _deactivate_in_flight(sports_club_id, deactivation_ready, release_deactivation)
    )
    try:
        await asyncio.wait_for(deactivation_ready.wait(), WAITING_TIMEOUT_SECONDS)
        # Sin bloqueo y sin lectura sucia: la lectura simple sigue viendo la version vigente.
        selectable = await asyncio.wait_for(
            sql_selectable_sports_club(sports_club_id), WAITING_TIMEOUT_SECONDS
        )
        assert selectable is not None and selectable.active is True
        # Se acota la espera: si la lectura tomara un bloqueo compartido, no terminaria.
        assert not holder.done(), "la desactivacion sigue en vuelo"
    finally:
        release_deactivation.set()
        await asyncio.gather(holder, return_exceptions=True)


@pytest.mark.asyncio(loop_scope="session")
async def test_an_inactive_academy_blocks_the_confirmation(
    registration_data: RegistrationData,
) -> None:
    """A5 / RS-10 orden directo: academia inactiva antes de confirmar, rechazo sin escritura."""
    context = registration_data.context_owner_a
    registration = await create_registration(
        context, registration_data.tournament_a, _club_draft(registration_data)
    )
    before = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)

    await set_sports_club_active(registration_data.sports_club_a, active=False)

    with pytest.raises(SportsClubNotSelectableError):
        await confirm_registration(context, registration.id)

    after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert after == before, "el rechazo no escribe nada en la inscripcion"
    assert after is not None and after.status == "DRAFT"
    assert after.sports_club_name_snapshot is not None, "el borrador conserva su snapshot"
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_deactivation_in_flight_makes_the_confirmation_wait(
    registration_data: RegistrationData,
) -> None:
    """RS-10 orden A: la baja tiene la fila; la confirmacion espera y luego rechaza."""
    context = registration_data.context_owner_a
    registration = await create_registration(
        context, registration_data.tournament_a, _club_draft(registration_data)
    )
    deactivation_ready = asyncio.Event()
    release_deactivation = asyncio.Event()

    holder = asyncio.create_task(
        _deactivate_in_flight(
            registration_data.sports_club_a, deactivation_ready, release_deactivation
        )
    )
    confirmation: asyncio.Task[TournamentRegistration] | None = None
    try:
        await asyncio.wait_for(deactivation_ready.wait(), WAITING_TIMEOUT_SECONDS)
        confirmation = asyncio.create_task(confirm_registration(context, registration.id))
        # En rojo (sin ``FOR SHARE``) la confirmacion no esperaba a nadie: el timeout lo delata.
        await wait_for_waiting_backends("%FROM sports_clubs%")
        await wait_for_tuple_contention("sports_clubs")
        assert not confirmation.done(), "la confirmacion espera al bloqueo de la baja"
    finally:
        release_deactivation.set()
        await asyncio.gather(holder, return_exceptions=True)

    assert confirmation is not None
    with pytest.raises(SportsClubNotSelectableError):
        await confirmation

    after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert after is not None and after.status == "DRAFT", "sin confirmacion parcial"
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_confirmation_in_flight_makes_the_deactivation_wait(
    registration_data: RegistrationData,
) -> None:
    """RS-10 orden B: la confirmacion tiene los dos ``FOR SHARE`` y la baja espera su turno."""
    context = registration_data.context_owner_a
    registration = await create_registration(
        context, registration_data.tournament_a, _club_draft(registration_data)
    )
    registration_ready = asyncio.Event()
    release_registration = asyncio.Event()

    async def _hold_registration_row() -> None:
        async with database.transaction():
            await database.fetch_val(
                query=SELECT_REGISTRATION_ROW_FOR_UPDATE,
                values={"registration_id": registration.id},
            )
            registration_ready.set()
            await release_registration.wait()

    holder = asyncio.create_task(_hold_registration_row())
    confirmation: asyncio.Task[TournamentRegistration] | None = None
    deactivation: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(registration_ready.wait(), WAITING_TIMEOUT_SECONDS)
        confirmation = asyncio.create_task(confirm_registration(context, registration.id))
        # La confirmacion esta dentro de su transaccion: ha tomado competidor y academia y
        # espera al pax sobre la fila de la inscripcion.
        await wait_for_waiting_backends("%UPDATE tournament_registrations%")
        deactivation = asyncio.create_task(
            database.execute(
                query=DEACTIVATE_SPORTS_CLUB,
                values={"sports_club_id": registration_data.sports_club_a, "active": False},
            )
        )
        # La baja espera al ``FOR SHARE`` que la confirmacion ya tiene.
        await wait_for_waiting_backends("%UPDATE sports_clubs%")
        await wait_for_tuple_contention("sports_clubs")
        assert not deactivation.done()
    finally:
        release_registration.set()
        await asyncio.gather(holder, return_exceptions=True)

    assert confirmation is not None and deactivation is not None
    confirmed = await confirmation
    assert confirmed.status == "CONFIRMED"
    await deactivation

    after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert after is not None and after.status == "CONFIRMED"
    assert after.sports_club_name_snapshot == confirmed.sports_club_name_snapshot
    assert await sql_selectable_sports_club(registration_data.sports_club_a) is None
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE", "CONFIRM"]


@pytest.mark.asyncio(loop_scope="session")
async def test_the_confirmation_locks_the_competitor_before_the_academy(
    registration_data: RegistrationData,
) -> None:
    """Orden competidor -> academia: con el competidor retenido, la baja de academia completa."""
    context = registration_data.context_owner_a
    registration = await create_registration(
        context, registration_data.tournament_a, _club_draft(registration_data)
    )
    competitor_is_held = asyncio.Event()
    release_competitor = asyncio.Event()

    async def _hold_competitor_row() -> None:
        async with database.transaction():
            await database.fetch_val(
                query=SELECT_COMPETITOR_ROW_FOR_UPDATE,
                values={"competitor_id": registration_data.competitor_a},
            )
            competitor_is_held.set()
            await release_competitor.wait()

    holder = asyncio.create_task(_hold_competitor_row())
    confirmation: asyncio.Task[TournamentRegistration] | None = None
    try:
        await asyncio.wait_for(competitor_is_held.wait(), WAITING_TIMEOUT_SECONDS)
        confirmation = asyncio.create_task(confirm_registration(context, registration.id))
        await wait_for_waiting_backends("%FOR SHARE%")
        # Primer eslabon retenido: la academia todavia no esta bloqueada por la confirmacion.
        await asyncio.wait_for(
            database.execute(
                query=DEACTIVATE_SPORTS_CLUB,
                values={"sports_club_id": registration_data.sports_club_a, "active": False},
            ),
            WAITING_TIMEOUT_SECONDS,
        )
        assert not confirmation.done(), "la confirmacion sigue esperando al competidor"
    finally:
        release_competitor.set()
        await asyncio.gather(holder, return_exceptions=True)

    assert confirmation is not None
    with pytest.raises(SportsClubNotSelectableError):
        await confirmation

    after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert after is not None and after.status == "DRAFT"
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_two_concurrent_confirmations_do_not_deadlock_and_audit_once(
    registration_data: RegistrationData,
) -> None:
    """Varias confirmaciones simultaneas: los ``FOR SHARE`` son compatibles, un solo ``CONFIRM``."""
    context = registration_data.context_owner_a
    registration = await create_registration(
        context, registration_data.tournament_a, _club_draft(registration_data)
    )
    audit_table_is_locked = asyncio.Event()
    release_audit_table = asyncio.Event()

    async def _hold_the_audit_table() -> None:
        async with database.transaction():
            await database.execute(query=LOCK_DOMAIN_CHANGE_LOG)
            audit_table_is_locked.set()
            await release_audit_table.wait()

    holder = asyncio.create_task(_hold_the_audit_table())
    confirmations: list[asyncio.Task[TournamentRegistration]] = []
    try:
        await asyncio.wait_for(audit_table_is_locked.wait(), WAITING_TIMEOUT_SECONDS)
        confirmations = [
            asyncio.create_task(confirm_registration(context, registration.id)) for _ in range(2)
        ]
        # La ganadora espera al INSERT de auditoria; la otra, al ``UPDATE`` ya bloqueado.
        await wait_for_waiting_backends("%INSERT INTO domain_change_log%")
        await wait_for_waiting_backends("%UPDATE tournament_registrations%")
        await wait_for_tuple_contention("tournament_registrations")
        assert not any(task.done() for task in confirmations)
    finally:
        release_audit_table.set()
        await asyncio.gather(holder, return_exceptions=True)

    results = await asyncio.gather(*confirmations)
    assert [result.status for result in results] == ["CONFIRMED", "CONFIRMED"]
    assert results[0].updated_at == results[1].updated_at, "una sola escritura gano la carrera"
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE", "CONFIRM"]


@pytest.mark.asyncio(loop_scope="session")
async def test_an_independent_registration_never_locks_an_academy(
    registration_data: RegistrationData,
) -> None:
    """Participacion independiente: sin academia no hay bloqueo que la haga depender de nadie."""
    context = registration_data.context_owner_a
    registration = await create_registration(
        context,
        registration_data.tournament_a,
        build_draft(competitor_id=registration_data.competitor_a),
    )
    assert registration.sports_club_id is None
    academy_is_held = asyncio.Event()
    release_academy = asyncio.Event()

    async def _hold_the_academy_row() -> None:
        async with database.transaction():
            await database.fetch_val(
                query=SELECT_SPORTS_CLUB_ROW_FOR_UPDATE,
                values={"sports_club_id": registration_data.sports_club_a},
            )
            academy_is_held.set()
            await release_academy.wait()

    holder = asyncio.create_task(_hold_the_academy_row())
    try:
        await asyncio.wait_for(academy_is_held.wait(), WAITING_TIMEOUT_SECONDS)
        confirmed = await asyncio.wait_for(
            confirm_registration(context, registration.id), WAITING_TIMEOUT_SECONDS
        )
        assert confirmed.status == "CONFIRMED"
        assert confirmed.sports_club_name_snapshot is None
        assert not holder.done(), "la academia seguia retenida mientras se confirmaba"
        assert [row["action"] for row in await audit_rows(registration.id)] == [
            "CREATE",
            "CONFIRM",
        ]
    finally:
        release_academy.set()
        await asyncio.gather(holder, return_exceptions=True)


@pytest.mark.asyncio(loop_scope="session")
async def test_deactivating_the_academy_does_not_touch_confirmed_history(
    registration_data: RegistrationData,
) -> None:
    """Historico inmutable: bajar la academia no reescribe inscripciones ni snapshots."""
    context = registration_data.context_owner_a
    registration = await create_registration(
        context, registration_data.tournament_a, _club_draft(registration_data)
    )
    confirmed = await confirm_registration(context, registration.id)
    before = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)

    await set_sports_club_active(registration_data.sports_club_a, active=False)

    after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert after == before, "la baja de la academia no toca la inscripcion confirmada"
    assert after is not None
    assert after.status == "CONFIRMED"
    assert after.revision == confirmed.revision
    assert after.sports_club_id == registration_data.sports_club_a
    assert after.sports_club_name_snapshot == confirmed.sports_club_name_snapshot
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE", "CONFIRM"]

    # Efecto real de la baja: una inscripcion nueva para esa academia ya no se admite.
    with pytest.raises(SportsClubNotSelectableError):
        await create_registration(
            context, registration_data.tournament_a, _club_draft(registration_data)
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_a_deactivation_never_touches_a_locked_registration_row(
    registration_data: RegistrationData,
) -> None:
    """Sin ciclo posible: la baja de la academia no toma bloqueo sobre la inscripcion."""
    context = registration_data.context_owner_a
    registration = await create_registration(
        context, registration_data.tournament_a, _club_draft(registration_data)
    )
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
        await asyncio.wait_for(
            database.execute(
                query=DEACTIVATE_SPORTS_CLUB,
                values={"sports_club_id": registration_data.sports_club_a, "active": False},
            ),
            WAITING_TIMEOUT_SECONDS,
        )
        assert not holder.done(), "la inscripcion seguia bloqueada mientras se daba de baja"
    finally:
        release_registration_row.set()
        await asyncio.gather(holder, return_exceptions=True)

    assert await sql_selectable_sports_club(registration_data.sports_club_a) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_a_rejected_confirmation_leaves_no_partial_audit_and_no_pii(
    registration_data: RegistrationData,
) -> None:
    """Sin auditoria parcial y sin PII: el rechazo no deja rastro de valores de fila."""
    context = registration_data.context_owner_a
    registration = await create_registration(
        context, registration_data.tournament_a, _club_draft(registration_data)
    )
    await set_sports_club_active(registration_data.sports_club_a, active=False)

    with pytest.raises(SportsClubNotSelectableError) as rejected:
        await confirm_registration(context, registration.id)

    message = str(rejected.value)
    assert "Academia Propia A" not in message, "el error no revela el nombre de la academia"
    assert "Ana Gomez" not in message, "el error no revela PII"

    rows = await audit_rows(registration.id)
    assert [row["action"] for row in rows] == ["CREATE"], "sin evento CONFIRM parcial"
    changed_fields = rows[0]["changed_fields"]
    assert isinstance(changed_fields, list)
    assert {"status", "sports_club_id", "sports_club_name_snapshot"} <= set(changed_fields)
    events = " ".join(await fetch_all_audit_rows_as_text())
    assert "Academia Propia A" not in events and "Ana Gomez" not in events, "auditoria sin PII"
