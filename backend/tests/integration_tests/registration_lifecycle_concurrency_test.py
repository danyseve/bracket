# pylint: disable=redefined-outer-name  # `registration_data` es el fixture de este modulo.
"""S3.2 de F3B - concurrencia del ciclo de vida de la inscripcion (O5, O5b, O6).

Las operaciones de ciclo de vida comparten dos recursos con la confirmacion: la fila de la
inscripcion (que la sentencia de transicion actualiza) y las filas de elegibilidad (competidor y
academia). RS-9 fijo el orden ``competidor -> academia -> inscripcion``; aqui se demuestra que la
retirada y la readmision lo respetan y que la carrera se resuelve en la propia sentencia.

Que se demuestra, y con que evidencia (sin *sleeps* como mecanismo):

* **Dos retiradas concurrentes** dejan **un unico** evento ``WITHDRAW``: la fila de la
  inscripcion las serializa y la perdedora se relee y devuelve el estado ya retirado.
* **Dos readmisiones concurrentes** dejan **un unico** evento ``REINSTATE``, con una sola escritura.
* **Retirada y readmision simultaneas**: la retirada de algo ya retirado es un no-op que **no toma
  bloqueo** sobre la fila (completa mientras la readmision la tiene bloqueada), asi que no hay
  bloqueo cruzado ni estado intermedio.
* **Readmision y baja de competidor**, en los dos ordenes: si la baja gana, la readmision espera y
  rechaza sin escritura parcial; si gana la readmision, la baja espera al ``FOR SHARE``.
* **Readmision y baja de academia**, en los dos ordenes, con la misma evidencia.
* **Orden de bloqueo competidor -> academia**: con el competidor retenido, la academia todavia no
  esta bloqueada por la readmision (la baja de academia completa sin esperar).

Validacion exclusivamente contra la base de laboratorio (``bracket_ci`` con ``ENVIRONMENT=CI``, o
``bracket_test``); ``bracket_dev`` no se usa para escrituras.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from databases import Database

from bracket.database import database
from bracket.logic.registrations import (
    CompetitorNotSelectableError,
    SportsClubNotSelectableError,
    confirm_registration,
    create_registration,
    reinstate_registration,
    withdraw_registration,
)
from bracket.models.db.domain import RegistrationDraftData, TournamentRegistration
from bracket.sql.domain_reads import get_registration
from tests.integration_tests.registration_fixtures import (
    UPDATE_COMPETITOR_ACTIVE,
    UPDATE_SPORTS_CLUB_ACTIVE,
    WAITING_TIMEOUT_SECONDS,
    RegistrationData,
    audit_rows,
    build_draft,
    registration_data_context,
    set_competitor_active,
    set_sports_club_active,
    wait_for_tuple_contention,
    wait_for_waiting_backends,
)

# La baja logica real de una academia/competidor es exactamente este ``UPDATE`` de ``active``: toma
# ``FOR NO KEY UPDATE`` sobre la fila, y ese es el bloqueo con el que conflictua el ``FOR SHARE``
# de la readmision. No existe todavia operacion de dominio de baja de academia (no se crea aqui).
DEACTIVATE_SPORTS_CLUB = UPDATE_SPORTS_CLUB_ACTIVE
DEACTIVATE_COMPETITOR = UPDATE_COMPETITOR_ACTIVE

# Pax (solo pruebas) sobre la fila de la inscripcion: detiene la readmision **despues** de sus dos
# bloqueos compartidos (competidor y academia) y antes de su ``UPDATE``.
SELECT_REGISTRATION_ROW_FOR_UPDATE = """
    SELECT id FROM tournament_registrations WHERE id = :registration_id FOR UPDATE
"""

# Pax sobre la fila del competidor, para reproducir una baja en vuelo sin escribir en la fila.
SELECT_COMPETITOR_ROW_FOR_UPDATE = """
    SELECT id FROM competitors WHERE id = :competitor_id FOR UPDATE
"""

# La auditoria bloqueada por completo conserva en vuelo dos operaciones a la vez.
LOCK_DOMAIN_CHANGE_LOG = "LOCK TABLE domain_change_log IN ACCESS EXCLUSIVE MODE"


@pytest_asyncio.fixture(loop_scope="session")
async def registration_data(reinit_database: Database) -> AsyncIterator[RegistrationData]:
    """Datos de laboratorio (mismo soporte que S3.1/S3.1b/S2-bis), con limpieza garantizada."""
    async with registration_data_context(reinit_database) as data:
        yield data


def _independent_draft(registration_data: RegistrationData, key: str) -> RegistrationDraftData:
    """Borrador valido sin academia, para las carreras que solo dependen del competidor."""
    return build_draft(
        competitor_id=registration_data.competitor_a,
        category_key=key,
        category_label=key.replace("-", " ").title(),
    )


def _club_draft(registration_data: RegistrationData, key: str) -> RegistrationDraftData:
    """Borrador valido que representa la academia propia del tenant A."""
    return build_draft(
        competitor_id=registration_data.competitor_a,
        representation="CLUB",
        sports_club_id=registration_data.sports_club_a,
        category_key=key,
        category_label=key.replace("-", " ").title(),
    )


async def _withdrawn(registration_data: RegistrationData, key: str) -> TournamentRegistration:
    """Inscripcion retirada partiendo de un borrador."""
    registration = await create_registration(
        registration_data.context_owner_a,
        registration_data.tournament_a,
        _independent_draft(registration_data, key),
    )
    return await withdraw_registration(
        registration_data.context_owner_a, registration.id, reason="retirada de partida"
    )


async def _deactivate_in_flight(
    competitor_id: int | None,
    sports_club_id: int | None,
    ready: asyncio.Event,
    release: asyncio.Event,
) -> None:
    """Baja logica dentro de una transaccion que queda abierta hasta ``release``."""
    async with database.transaction():
        if competitor_id is not None:
            await database.execute(
                query=DEACTIVATE_COMPETITOR,
                values={"competitor_id": competitor_id, "active": False},
            )
        else:
            await database.execute(
                query=DEACTIVATE_SPORTS_CLUB,
                values={"sports_club_id": sports_club_id, "active": False},
            )
        ready.set()
        await release.wait()


# --- Dos operaciones iguales --------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_two_concurrent_withdrawals_write_a_single_event(
    registration_data: RegistrationData,
) -> None:
    """Dos retiradas simultaneas: la fila serializa, una escribe y la otra se relee sin evento."""
    context = registration_data.context_owner_a
    registration = await create_registration(
        context,
        registration_data.tournament_a,
        _independent_draft(registration_data, "gi-doble-retirada"),
    )
    await confirm_registration(context, registration.id, reason="confirmada")
    audit_table_is_locked = asyncio.Event()
    release_audit_table = asyncio.Event()

    async def _hold_the_audit_table() -> None:
        async with database.transaction():
            await database.execute(query=LOCK_DOMAIN_CHANGE_LOG)
            audit_table_is_locked.set()
            await release_audit_table.wait()

    holder = asyncio.create_task(_hold_the_audit_table())
    withdrawals: list[asyncio.Task[TournamentRegistration]] = []
    try:
        await asyncio.wait_for(audit_table_is_locked.wait(), WAITING_TIMEOUT_SECONDS)
        withdrawals = [
            asyncio.create_task(
                withdraw_registration(context, registration.id, reason="retirada concurrente")
            )
            for _ in range(2)
        ]
        # La ganadora espera al INSERT de auditoria; la otra, al ``UPDATE`` ya bloqueado.
        await wait_for_waiting_backends("%INSERT INTO domain_change_log%")
        await wait_for_waiting_backends("%UPDATE tournament_registrations%")
        await wait_for_tuple_contention("tournament_registrations")
        assert not any(task.done() for task in withdrawals)
    finally:
        release_audit_table.set()
        await asyncio.gather(holder, return_exceptions=True)

    results = await asyncio.gather(*withdrawals)
    assert [result.status for result in results] == ["WITHDRAWN", "WITHDRAWN"]
    assert results[0].updated_at == results[1].updated_at, "una sola escritura gano la carrera"
    assert [row["action"] for row in await audit_rows(registration.id)] == [
        "CREATE",
        "CONFIRM",
        "WITHDRAW",
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_two_concurrent_reinstatements_write_a_single_event(
    registration_data: RegistrationData,
) -> None:
    """Dos readmisiones simultaneas: un unico ``REINSTATE`` y una unica escritura."""
    context = registration_data.context_owner_a
    registration = await _withdrawn(registration_data, "gi-doble-readmision")
    audit_table_is_locked = asyncio.Event()
    release_audit_table = asyncio.Event()

    async def _hold_the_audit_table() -> None:
        async with database.transaction():
            await database.execute(query=LOCK_DOMAIN_CHANGE_LOG)
            audit_table_is_locked.set()
            await release_audit_table.wait()

    holder = asyncio.create_task(_hold_the_audit_table())
    reinstatements: list[asyncio.Task[TournamentRegistration]] = []
    try:
        await asyncio.wait_for(audit_table_is_locked.wait(), WAITING_TIMEOUT_SECONDS)
        reinstatements = [
            asyncio.create_task(
                reinstate_registration(context, registration.id, reason="readmision concurrente")
            )
            for _ in range(2)
        ]
        await wait_for_waiting_backends("%INSERT INTO domain_change_log%")
        await wait_for_waiting_backends("%UPDATE tournament_registrations%")
        await wait_for_tuple_contention("tournament_registrations")
        assert not any(task.done() for task in reinstatements)
    finally:
        release_audit_table.set()
        await asyncio.gather(holder, return_exceptions=True)

    results = await asyncio.gather(*reinstatements)
    assert [result.status for result in results] == ["CONFIRMED", "CONFIRMED"]
    assert results[0].updated_at == results[1].updated_at, "una sola escritura gano la carrera"
    assert [row["action"] for row in await audit_rows(registration.id)] == [
        "CREATE",
        "WITHDRAW",
        "REINSTATE",
    ]


# --- Retirada y readmision ----------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_withdrawal_racing_a_reinstatement_does_not_take_the_row_lock(
    registration_data: RegistrationData,
) -> None:
    """Retirada no-op y readmision a la vez: la retirada no espera y no deja estado intermedio."""
    context = registration_data.context_owner_a
    withdrawn = await _withdrawn(registration_data, "gi-retirada-readmision")
    registration_row_is_held = asyncio.Event()
    release_registration_row = asyncio.Event()

    async def _hold_the_registration_row() -> None:
        async with database.transaction():
            await database.fetch_val(
                query=SELECT_REGISTRATION_ROW_FOR_UPDATE,
                values={"registration_id": withdrawn.id},
            )
            registration_row_is_held.set()
            await release_registration_row.wait()

    holder = asyncio.create_task(_hold_the_registration_row())
    reinstatement: asyncio.Task[TournamentRegistration] | None = None
    try:
        await asyncio.wait_for(registration_row_is_held.wait(), WAITING_TIMEOUT_SECONDS)
        reinstatement = asyncio.create_task(
            reinstate_registration(context, withdrawn.id, reason="readmision")
        )
        await wait_for_waiting_backends("%UPDATE tournament_registrations%")
        # La retirada de algo ya retirado es un no-op: lee (sin bloqueo) y completa.
        withdrawal = asyncio.create_task(
            withdraw_registration(context, withdrawn.id, reason="retirada concurrente")
        )
        no_op = await asyncio.wait_for(withdrawal, WAITING_TIMEOUT_SECONDS)
        assert no_op.status == "WITHDRAWN"
        assert no_op.updated_at == withdrawn.updated_at, "el no-op no reescribe la fila"
        assert not reinstatement.done(), "la readmision sigue esperando a la fila"
    finally:
        release_registration_row.set()
        await asyncio.gather(holder, return_exceptions=True)

    assert reinstatement is not None
    reinstated = await reinstatement
    assert reinstated.status == "CONFIRMED"
    assert [row["action"] for row in await audit_rows(withdrawn.id)] == [
        "CREATE",
        "WITHDRAW",
        "REINSTATE",
    ]


# --- Readmision contra la baja del competidor ---------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_competitor_deactivation_in_flight_makes_the_reinstatement_wait_and_reject(
    registration_data: RegistrationData,
) -> None:
    """Orden A: la baja del competidor tiene la fila; la readmision espera y falla sin escribir."""
    context = registration_data.context_owner_a
    withdrawn = await _withdrawn(registration_data, "gi-readmision-baja-competidor")
    deactivation_ready = asyncio.Event()
    release_deactivation = asyncio.Event()

    holder = asyncio.create_task(
        _deactivate_in_flight(
            registration_data.competitor_a, None, deactivation_ready, release_deactivation
        )
    )
    reinstatement: asyncio.Task[TournamentRegistration] | None = None
    try:
        await asyncio.wait_for(deactivation_ready.wait(), WAITING_TIMEOUT_SECONDS)
        reinstatement = asyncio.create_task(
            reinstate_registration(context, withdrawn.id, reason="readmision")
        )
        # Evidencia positiva: la readmision espera al ``FOR SHARE`` del competidor.
        await wait_for_waiting_backends("%FOR SHARE%")
        await wait_for_tuple_contention("competitors")
        assert not reinstatement.done(), "la readmision espera al bloqueo de la baja"
    finally:
        release_deactivation.set()
        await asyncio.gather(holder, return_exceptions=True)
        await set_competitor_active(registration_data.competitor_a, active=True)

    assert reinstatement is not None
    with pytest.raises(CompetitorNotSelectableError):
        await reinstatement

    after = await get_registration(withdrawn.id, tenant_club_id=registration_data.tenant_a)
    assert after is not None and after.status == "WITHDRAWN", "sin readmision parcial"
    assert [row["action"] for row in await audit_rows(withdrawn.id)] == ["CREATE", "WITHDRAW"]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_reinstatement_in_flight_makes_the_competitor_deactivation_wait(
    registration_data: RegistrationData,
) -> None:
    """Orden B: la readmision toma el ``FOR SHARE`` primero y la baja espera su turno."""
    context = registration_data.context_owner_a
    withdrawn = await _withdrawn(registration_data, "gi-readmision-gana-competidor")
    registration_row_is_held = asyncio.Event()
    release_registration_row = asyncio.Event()

    async def _hold_the_registration_row() -> None:
        async with database.transaction():
            await database.fetch_val(
                query=SELECT_REGISTRATION_ROW_FOR_UPDATE,
                values={"registration_id": withdrawn.id},
            )
            registration_row_is_held.set()
            await release_registration_row.wait()

    holder = asyncio.create_task(_hold_the_registration_row())
    reinstatement: asyncio.Task[TournamentRegistration] | None = None
    deactivation: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(registration_row_is_held.wait(), WAITING_TIMEOUT_SECONDS)
        reinstatement = asyncio.create_task(
            reinstate_registration(context, withdrawn.id, reason="readmision")
        )
        # Detenida despues de su ``FOR SHARE`` sobre el competidor y antes de su ``UPDATE``.
        await wait_for_waiting_backends("%UPDATE tournament_registrations%")
        deactivation = asyncio.create_task(
            database.execute(
                query=DEACTIVATE_COMPETITOR,
                values={"competitor_id": registration_data.competitor_a, "active": False},
            )
        )
        await wait_for_waiting_backends("%UPDATE competitors%")
        await wait_for_tuple_contention("competitors")
        assert not deactivation.done(), "la baja espera al FOR SHARE de la readmision"
    finally:
        release_registration_row.set()
        await asyncio.gather(holder, return_exceptions=True)

    assert reinstatement is not None and deactivation is not None
    reinstated = await reinstatement
    assert reinstated.status == "CONFIRMED"
    await deactivation
    try:
        assert [row["action"] for row in await audit_rows(withdrawn.id)] == [
            "CREATE",
            "WITHDRAW",
            "REINSTATE",
        ]
    finally:
        await set_competitor_active(registration_data.competitor_a, active=True)


# --- Readmision contra la baja de la academia ---------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_an_academy_deactivation_in_flight_makes_the_reinstatement_wait_and_reject(
    registration_data: RegistrationData,
) -> None:
    """Orden A: la baja de la academia tiene la fila; la readmision espera y falla sin escribir."""
    context = registration_data.context_owner_a
    registration = await create_registration(
        context,
        registration_data.tournament_a,
        _club_draft(registration_data, "gi-readmision-baja-academia"),
    )
    await withdraw_registration(context, registration.id, reason="retirada")
    deactivation_ready = asyncio.Event()
    release_deactivation = asyncio.Event()

    holder = asyncio.create_task(
        _deactivate_in_flight(
            None, registration_data.sports_club_a, deactivation_ready, release_deactivation
        )
    )
    reinstatement: asyncio.Task[TournamentRegistration] | None = None
    try:
        await asyncio.wait_for(deactivation_ready.wait(), WAITING_TIMEOUT_SECONDS)
        reinstatement = asyncio.create_task(
            reinstate_registration(context, registration.id, reason="readmision")
        )
        await wait_for_waiting_backends("%FOR SHARE%")
        await wait_for_tuple_contention("sports_clubs")
        assert not reinstatement.done()
    finally:
        release_deactivation.set()
        await asyncio.gather(holder, return_exceptions=True)
        await set_sports_club_active(registration_data.sports_club_a, active=True)

    assert reinstatement is not None
    with pytest.raises(SportsClubNotSelectableError):
        await reinstatement

    after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert after is not None and after.status == "WITHDRAWN", "sin readmision parcial"
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE", "WITHDRAW"]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_reinstatement_in_flight_makes_the_academy_deactivation_wait(
    registration_data: RegistrationData,
) -> None:
    """Orden B: la readmision retiene competidor y academia; la baja espera su ``FOR SHARE``."""
    context = registration_data.context_owner_a
    registration = await create_registration(
        context,
        registration_data.tournament_a,
        _club_draft(registration_data, "gi-readmision-gana-academia"),
    )
    await withdraw_registration(context, registration.id, reason="retirada")
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
    reinstatement: asyncio.Task[TournamentRegistration] | None = None
    deactivation: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(registration_row_is_held.wait(), WAITING_TIMEOUT_SECONDS)
        reinstatement = asyncio.create_task(
            reinstate_registration(context, registration.id, reason="readmision")
        )
        await wait_for_waiting_backends("%UPDATE tournament_registrations%")
        deactivation = asyncio.create_task(
            database.execute(
                query=DEACTIVATE_SPORTS_CLUB,
                values={"sports_club_id": registration_data.sports_club_a, "active": False},
            )
        )
        await wait_for_waiting_backends("%UPDATE sports_clubs%")
        await wait_for_tuple_contention("sports_clubs")
        assert not deactivation.done()
    finally:
        release_registration_row.set()
        await asyncio.gather(holder, return_exceptions=True)

    assert reinstatement is not None and deactivation is not None
    reinstated = await reinstatement
    assert reinstated.status == "CONFIRMED"
    await deactivation
    try:
        after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
        assert after is not None and after.status == "CONFIRMED"
        assert after.sports_club_name_snapshot == reinstated.sports_club_name_snapshot
        assert [row["action"] for row in await audit_rows(registration.id)] == [
            "CREATE",
            "WITHDRAW",
            "REINSTATE",
        ]
    finally:
        await set_sports_club_active(registration_data.sports_club_a, active=True)


@pytest.mark.asyncio(loop_scope="session")
async def test_the_reinstatement_locks_the_competitor_before_the_academy(
    registration_data: RegistrationData,
) -> None:
    """Orden competidor -> academia: con el competidor retenido, la baja de academia completa."""
    context = registration_data.context_owner_a
    registration = await create_registration(
        context, registration_data.tournament_a, _club_draft(registration_data, "gi-orden-bloqueo")
    )
    await withdraw_registration(context, registration.id, reason="retirada")
    competitor_is_held = asyncio.Event()
    release_competitor = asyncio.Event()

    async def _hold_the_competitor_row() -> None:
        async with database.transaction():
            await database.fetch_val(
                query=SELECT_COMPETITOR_ROW_FOR_UPDATE,
                values={"competitor_id": registration_data.competitor_a},
            )
            competitor_is_held.set()
            await release_competitor.wait()

    holder = asyncio.create_task(_hold_the_competitor_row())
    reinstatement: asyncio.Task[TournamentRegistration] | None = None
    try:
        await asyncio.wait_for(competitor_is_held.wait(), WAITING_TIMEOUT_SECONDS)
        reinstatement = asyncio.create_task(
            reinstate_registration(context, registration.id, reason="readmision")
        )
        await wait_for_waiting_backends("%FOR SHARE%")
        # Primer eslabon retenido: la academia todavia no esta bloqueada por la readmision.
        await asyncio.wait_for(
            database.execute(
                query=DEACTIVATE_SPORTS_CLUB,
                values={"sports_club_id": registration_data.sports_club_a, "active": False},
            ),
            WAITING_TIMEOUT_SECONDS,
        )
        assert not reinstatement.done(), "la readmision sigue esperando al competidor"
    finally:
        release_competitor.set()
        await asyncio.gather(holder, return_exceptions=True)
        await set_sports_club_active(registration_data.sports_club_a, active=True)

    assert reinstatement is not None
    with pytest.raises(SportsClubNotSelectableError):
        await reinstatement

    after = await get_registration(registration.id, tenant_club_id=registration_data.tenant_a)
    assert after is not None and after.status == "WITHDRAWN"
    assert [row["action"] for row in await audit_rows(registration.id)] == ["CREATE", "WITHDRAW"]
