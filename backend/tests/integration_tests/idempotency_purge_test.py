"""S3.8c — purga fisica de claves de idempotencia sobre PostgreSQL real (LAB ONLY).

Comprueba el criterio completo contra la base: que la fila viva no se toque, que la caducada con
margen si se borre por lotes, que el dry-run no escriba nada, que la segunda purga sea idempotente y
que la carrera con operaciones reales (consumidor que sostiene la fila, operacion sin confirmar,
finalizacion en vuelo, dos purgas a la vez) no deje a nadie sin su fila ni duplique borrados.

La carrera se ejercita con transacciones y bloqueos reales (dos backends del pool), nunca con
``sleep``. Los conteos se toman con SQL propio de la prueba, no con el de la purga, para que lo que
se mida sea el efecto y no la implementacion.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from heliclockter import datetime_utc

from bracket.database import database
from bracket.logic.idempotency import (
    IDEMPOTENCY_STATE_COMPLETED,
    IDEMPOTENCY_STATE_IN_PROGRESS,
    IdempotencyDecision,
    classify_reservation,
    compute_request_fingerprint,
)
from bracket.logic.idempotency_purge import (
    PurgePolicy,
    purge_expired_idempotency_reservations,
)
from bracket.models.db.idempotency import IdempotencyReservationInsert
from bracket.sql.idempotency import (
    sql_complete_idempotency_reservation,
    sql_insert_idempotency_reservation,
)
from bracket.utils.id_types import ClubId, UserId
from tests.integration_tests.idempotency_fixtures import (
    LAB_PATH,
    LAB_PAYLOAD,
    IdempotencyLab,
    lab_completion,
    lab_reservation,
    reservations_in_tenant,
    stored_reservation,
)

# Las pruebas usan la base y las fixtures del laboratorio, atadas al bucle de sesion.
pytestmark = pytest.mark.asyncio(loop_scope="session")

WAITING_TIMEOUT_SECONDS = 10
LIFETIME = timedelta(hours=1)
#: Bien pasada la ventana protegida (retencion 24 h + margen 24 h).
EXPIRED_HOURS = 48
#: Caducada, pero todavia dentro del margen: la ventana protegida sigue viva.
INSIDE_GRACE_HOURS = 2
BATCH_POLICY = PurgePolicy(batch_size=10, max_batches=5)

COUNT_ROWS_IN_TENANT = """
    SELECT count(*) FROM domain_idempotency_keys WHERE tenant_club_id = :tenant_club_id
"""
COUNT_ELIGIBLE_IN_TENANT = f"""
    SELECT count(*) FROM domain_idempotency_keys
    WHERE tenant_club_id = :tenant_club_id AND state = '{IDEMPOTENCY_STATE_COMPLETED}'
        AND expires_at < :cutoff
"""
SELECT_ROWS_IN_TENANT = """
    SELECT id, state, expires_at, idempotency_key, request_fingerprint, resource_id
    FROM domain_idempotency_keys
    WHERE tenant_club_id = :tenant_club_id
    ORDER BY id
"""
SELECT_ROW_FOR_UPDATE = """
    SELECT id FROM domain_idempotency_keys
    WHERE tenant_club_id = :tenant_club_id AND actor_user_id = :actor_user_id
        AND idempotency_key = :idempotency_key
    FOR UPDATE
"""


@dataclass
class StoredReservationView:
    """Vista minima de la fila persistida, tal y como la mira el clasificador del protocolo.

    Mutable a proposito: el ``Protocol`` de la vista no declara atributos de solo lectura, asi que
    una version congelada no satisface la firma.
    """

    state: str
    request_fingerprint: str
    fingerprint_key_version: str
    expires_at: datetime_utc


def lab_purge_key(suffix: str) -> str:
    """Clave con forma valida y distinta por caso."""
    return f"purge-lab-key-{suffix}"


def reservation(
    lab: IdempotencyLab,
    *,
    key: str,
    age_hours: int,
    tenant_club_id: ClubId | None = None,
    actor_user_id: UserId | None = None,
) -> IdempotencyReservationInsert:
    """Reserva cuya ``expires_at`` cae en el pasado (o en el futuro) segun la antiguedad pedida."""
    return lab_reservation(
        tenant_club_id if tenant_club_id is not None else lab.club_a,
        actor_user_id if actor_user_id is not None else lab.user_a,
        key=key,
        created=datetime_utc.now() - timedelta(hours=age_hours),
        lifetime=LIFETIME,
    )


async def insert_completed(
    lab: IdempotencyLab,
    *,
    key: str,
    age_hours: int,
    tenant_club_id: ClubId | None = None,
    actor_user_id: UserId | None = None,
) -> None:
    """Reserva **finalizada**: es la unica forma en que una fila caducada puede quedar elegible."""
    tenant = tenant_club_id if tenant_club_id is not None else lab.club_a
    actor = actor_user_id if actor_user_id is not None else lab.user_a
    await sql_insert_idempotency_reservation(
        reservation(lab, key=key, age_hours=age_hours, tenant_club_id=tenant, actor_user_id=actor)
    )
    completed = await sql_complete_idempotency_reservation(
        tenant_club_id=tenant,
        actor_user_id=actor,
        idempotency_key=key,
        completion=lab_completion(),
    )
    assert completed is not None


async def insert_in_progress(lab: IdempotencyLab, *, key: str, age_hours: int) -> None:
    """Reserva pendiente (patologico: pendiente y ya caducada)."""
    await sql_insert_idempotency_reservation(reservation(lab, key=key, age_hours=age_hours))


async def rows_in_tenant(tenant_club_id: ClubId) -> list[dict[str, Any]]:
    records = await database.fetch_all(
        query=SELECT_ROWS_IN_TENANT, values={"tenant_club_id": tenant_club_id}
    )
    return [dict(record._mapping) for record in records]


async def count_in_tenant(tenant_club_id: ClubId) -> int:
    return int(
        await database.fetch_val(
            query=COUNT_ROWS_IN_TENANT, values={"tenant_club_id": tenant_club_id}
        )
    )


async def count_eligible_in_tenant(
    tenant_club_id: ClubId, *, policy: PurgePolicy = BATCH_POLICY
) -> int:
    """Elegibles segun el ``WHERE`` de la politica, con SQL propio de la prueba."""
    return int(
        await database.fetch_val(
            query=COUNT_ELIGIBLE_IN_TENANT,
            values={"tenant_club_id": tenant_club_id, "cutoff": policy.cutoff(datetime_utc.now())},
        )
    )


async def drain(policy: PurgePolicy = BATCH_POLICY) -> None:
    """Barre residuos de la sesion para que los conteos globales sean exactos."""
    await purge_expired_idempotency_reservations(policy)


async def hold_row(
    lab: IdempotencyLab, *, key: str, taken: asyncio.Event, release: asyncio.Event
) -> None:
    """Toma el bloqueo de fila (como un consumidor real) y lo mantiene hasta que se libere."""
    async with database.transaction():
        row = await database.fetch_one(
            query=SELECT_ROW_FOR_UPDATE,
            values={
                "tenant_club_id": lab.club_a,
                "actor_user_id": lab.user_a,
                "idempotency_key": key,
            },
        )
        assert row is not None
        taken.set()
        await release.wait()


async def test_a_live_completed_reservation_is_not_purged(idempotency_lab: IdempotencyLab) -> None:
    lab = idempotency_lab
    key = lab_purge_key("viva-000000000001")
    await drain()
    await insert_completed(lab, key=key, age_hours=0)

    report = await purge_expired_idempotency_reservations(BATCH_POLICY)

    assert report.deleted == 0
    assert report.candidates == 0
    assert await stored_reservation(lab.club_a, lab.user_a, key) is not None


async def test_an_expired_completed_reservation_past_the_grace_is_purged(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    key = lab_purge_key("caducada-000000001")
    await drain()
    await insert_completed(lab, key=key, age_hours=EXPIRED_HOURS)

    report = await purge_expired_idempotency_reservations(BATCH_POLICY)

    assert report.deleted == 1
    assert report.batches == 1
    assert report.stopped_at_batch_limit is False
    assert report.duration_seconds >= 0
    assert await stored_reservation(lab.club_a, lab.user_a, key) is None
    assert await count_in_tenant(lab.club_a) == 0


async def test_an_expired_reservation_inside_the_grace_is_not_purged(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    key = lab_purge_key("margen-00000000001")
    await drain()
    # Caducada hace una hora, pero el margen por defecto es de 24: la ventana protegida sigue viva.
    await insert_completed(lab, key=key, age_hours=INSIDE_GRACE_HOURS)

    report = await purge_expired_idempotency_reservations(BATCH_POLICY)

    assert report.deleted == 0
    assert await stored_reservation(lab.club_a, lab.user_a, key) is not None


async def test_an_in_progress_reservation_is_never_purged(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    key = lab_purge_key("encurso-00000000001")
    await drain()
    await insert_in_progress(lab, key=key, age_hours=EXPIRED_HOURS)

    report = await purge_expired_idempotency_reservations(BATCH_POLICY)

    assert report.deleted == 0
    row = await stored_reservation(lab.club_a, lab.user_a, key)
    assert row is not None
    assert row["state"] == IDEMPOTENCY_STATE_IN_PROGRESS


async def test_purge_deletes_in_batches_of_the_configured_size(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    await drain()
    for index in range(5):
        await insert_completed(
            lab, key=lab_purge_key(f"lote-{index:011d}"), age_hours=EXPIRED_HOURS
        )

    policy = PurgePolicy(batch_size=2, max_batches=10)
    report = await purge_expired_idempotency_reservations(policy)

    assert report.deleted == 5
    assert report.batches == 3, "cinco filas con lotes de dos son tres sentencias"
    assert report.stopped_at_batch_limit is False
    assert await count_in_tenant(lab.club_a) == 0


async def test_the_batch_limit_leaves_the_rest_for_the_next_run(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    await drain()
    for index in range(5):
        await insert_completed(
            lab, key=lab_purge_key(f"tope-{index:011d}"), age_hours=EXPIRED_HOURS
        )

    policy = PurgePolicy(batch_size=2, max_batches=1)
    limited = await purge_expired_idempotency_reservations(policy)

    assert limited.deleted == 2
    assert limited.batches == 1
    assert limited.stopped_at_batch_limit is True
    assert await count_eligible_in_tenant(lab.club_a, policy=BATCH_POLICY) == 3

    rest = await purge_expired_idempotency_reservations(BATCH_POLICY)

    assert rest.deleted == 3
    assert rest.stopped_at_batch_limit is False
    assert await count_in_tenant(lab.club_a) == 0


async def test_dry_run_counts_without_deleting_and_leaves_the_rows_identical(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    await drain()
    await insert_completed(lab, key=lab_purge_key("dryrun-caducada-01"), age_hours=EXPIRED_HOURS)
    await insert_completed(lab, key=lab_purge_key("dryrun-viva-000001"), age_hours=0)
    before = await rows_in_tenant(lab.club_a)

    report = await purge_expired_idempotency_reservations(PurgePolicy(dry_run=True))

    assert report.dry_run is True
    assert report.candidates == 1, "el dry-run usa el mismo criterio que el borrado"
    assert report.deleted == 0
    assert report.batches == 0
    after = await rows_in_tenant(lab.club_a)
    assert after == before, "el dry-run no puede tocar una sola fila"
    assert await count_in_tenant(lab.club_a) == 2


async def test_a_second_purge_is_idempotent(idempotency_lab: IdempotencyLab) -> None:
    lab = idempotency_lab
    await drain()
    await insert_completed(lab, key=lab_purge_key("idempotente-00001"), age_hours=EXPIRED_HOURS)

    first = await purge_expired_idempotency_reservations(BATCH_POLICY)
    second = await purge_expired_idempotency_reservations(BATCH_POLICY)

    assert first.deleted == 1
    assert second.deleted == 0
    assert second.candidates == 0
    still_present = await stored_reservation(
        lab.club_a, lab.user_a, lab_purge_key("idempotente-00001")
    )
    assert still_present is None


async def test_purge_skips_a_row_held_by_an_active_consumer(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    key = lab_purge_key("tomada-00000000001")
    await drain()
    await insert_completed(lab, key=key, age_hours=EXPIRED_HOURS)
    taken = asyncio.Event()
    release = asyncio.Event()
    consumer = asyncio.create_task(hold_row(lab, key=key, taken=taken, release=release))
    await asyncio.wait_for(taken.wait(), WAITING_TIMEOUT_SECONDS)
    try:
        report = await purge_expired_idempotency_reservations(BATCH_POLICY)

        assert report.deleted == 0, "SKIP LOCKED: la fila tomada se salta, no se espera ni se borra"
        assert await stored_reservation(lab.club_a, lab.user_a, key) is not None
    finally:
        release.set()
        await asyncio.gather(consumer, return_exceptions=True)

    after = await purge_expired_idempotency_reservations(BATCH_POLICY)

    assert after.deleted == 1, "liberada la fila, el siguiente barrido la recoge"


async def test_purge_does_not_touch_a_reservation_being_written(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    key = lab_purge_key("encurso-escritura-01")
    await drain()
    written = asyncio.Event()
    release = asyncio.Event()

    async def operation() -> None:
        async with database.transaction():
            await sql_insert_idempotency_reservation(
                reservation(lab, key=key, age_hours=EXPIRED_HOURS)
            )
            await sql_complete_idempotency_reservation(
                tenant_club_id=lab.club_a,
                actor_user_id=lab.user_a,
                idempotency_key=key,
                completion=lab_completion(),
            )
            written.set()
            await release.wait()

    in_flight = asyncio.create_task(operation())
    await asyncio.wait_for(written.wait(), WAITING_TIMEOUT_SECONDS)
    try:
        report = await purge_expired_idempotency_reservations(BATCH_POLICY)

        assert report.deleted == 0, "una operacion sin confirmar no es visible para la purga"
        assert await reservations_in_tenant(lab.club_a, lab.user_a) == 0
    finally:
        release.set()
        await asyncio.gather(in_flight, return_exceptions=True)

    row = await stored_reservation(lab.club_a, lab.user_a, key)
    assert row is not None, "la operacion no puede quedarse sin su fila"
    assert row["state"] == IDEMPOTENCY_STATE_COMPLETED


async def test_a_completion_in_flight_keeps_its_row_until_it_finishes(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    key = lab_purge_key("finalizando-000001")
    await drain()
    await insert_in_progress(lab, key=key, age_hours=EXPIRED_HOURS)
    completed = asyncio.Event()
    release = asyncio.Event()

    async def finish() -> None:
        async with database.transaction():
            result = await sql_complete_idempotency_reservation(
                tenant_club_id=lab.club_a,
                actor_user_id=lab.user_a,
                idempotency_key=key,
                completion=lab_completion(),
            )
            assert result is not None
            completed.set()
            await release.wait()

    finishing = asyncio.create_task(finish())
    await asyncio.wait_for(completed.wait(), WAITING_TIMEOUT_SECONDS)
    try:
        report = await purge_expired_idempotency_reservations(BATCH_POLICY)

        assert report.deleted == 0
    finally:
        release.set()
        await asyncio.gather(finishing, return_exceptions=True)

    row = await stored_reservation(lab.club_a, lab.user_a, key)
    assert row is not None, "el resultado de la operacion no se puede perder"
    assert row["state"] == IDEMPOTENCY_STATE_COMPLETED

    after = await purge_expired_idempotency_reservations(BATCH_POLICY)

    assert after.deleted == 1, "terminada y fuera de la ventana, la fila ya si es elegible"


async def test_two_concurrent_purges_delete_each_row_at_most_once(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    await drain()
    for index in range(6):
        await insert_completed(
            lab, key=lab_purge_key(f"carrera-{index:011d}"), age_hours=EXPIRED_HOURS
        )
    policy = PurgePolicy(batch_size=2, max_batches=10)

    first, second = await asyncio.gather(
        purge_expired_idempotency_reservations(policy),
        purge_expired_idempotency_reservations(policy),
    )

    assert first.deleted + second.deleted == 6, "SKIP LOCKED: ninguna fila se borra dos veces"
    assert await count_in_tenant(lab.club_a) == 0


async def test_purge_keeps_foreign_keys_and_other_tenants_intact(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    neighbour_key = lab_purge_key("vecino-00000000001")
    await drain()
    await insert_completed(lab, key=lab_purge_key("propia-00000000001"), age_hours=EXPIRED_HOURS)
    await insert_completed(
        lab,
        key=neighbour_key,
        age_hours=EXPIRED_HOURS,
        tenant_club_id=lab.club_b,
        actor_user_id=lab.user_b,
    )

    report = await purge_expired_idempotency_reservations(BATCH_POLICY)

    assert report.deleted == 2
    assert await count_in_tenant(lab.club_a) == 0
    assert await count_in_tenant(lab.club_b) == 0
    assert await reservations_in_tenant(lab.club_a, lab.user_a) == 0

    absent_club = ClubId(999_999_999)
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        await sql_insert_idempotency_reservation(
            reservation(
                lab,
                key=lab_purge_key("sin-club-0000001"),
                age_hours=0,
                tenant_club_id=absent_club,
            )
        )


async def test_an_expired_key_is_rejected_while_present_and_fresh_once_purged(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    key = lab_purge_key("contrato-0000000001")
    await drain()
    await insert_completed(lab, key=key, age_hours=EXPIRED_HOURS)
    fingerprint = compute_request_fingerprint(method="POST", path=LAB_PATH, payload=LAB_PAYLOAD)
    stored = await stored_reservation(lab.club_a, lab.user_a, key)
    assert stored is not None

    decision = classify_reservation(
        StoredReservationView(
            state=stored["state"],
            request_fingerprint=stored["request_fingerprint"],
            fingerprint_key_version=stored["fingerprint_key_version"],
            expires_at=stored["expires_at"],
        ),
        fingerprint=fingerprint.fingerprint,
        fingerprint_key_version=fingerprint.key_version,
        now=datetime_utc.now(),
    )

    assert decision is IdempotencyDecision.EXPIRED, (
        "contrato vigente de S3.3c-4: rechazo determinista, sin reejecutar"
    )

    report = await purge_expired_idempotency_reservations(BATCH_POLICY)

    assert report.deleted == 1
    assert await stored_reservation(lab.club_a, lab.user_a, key) is None

    # Ya purgada, esa clave no tiene nada que verificar: se comporta como nueva.
    inserted = await sql_insert_idempotency_reservation(
        lab_reservation(lab.club_a, lab.user_a, key=key)
    )
    assert inserted.state == IDEMPOTENCY_STATE_IN_PROGRESS, (
        "documentado en docs/26 §56: la proteccion la da la ventana, no la permanencia de la fila"
    )
    assert len(await rows_in_tenant(lab.club_a)) == 1, "una sola fila: la clave no se duplica"
