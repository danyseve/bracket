# pylint: disable=redefined-outer-name  # los fixtures de este modulo son los del laboratorio.
"""S3.3c-3 — almacenamiento transaccional de claves de idempotencia, contra PostgreSQL real.

TDD: escrito **antes** de la tabla, de la migracion y de los helpers; en rojo porque no existian
``bracket/sql/idempotency.py`` ni ``domain_idempotency_keys``.

Que se comprueba (S3.3c-3 §2, §3, §5, §6):

* reservar deja una fila **pendiente** y sin respuesta; el estado solo lo cambia la finalizacion;
* la lectura esta **acotada** al tenant y al actor: la misma clave en otro tenant u otro actor es
  otra reserva, y la clave ajena no se ve;
* un segundo intento con la misma clave choca con el **indice unico** (nombre exacto de la
  restriccion), y con huella distinta se clasifica como conflicto, no como replay;
* las claves ajenas al tenant o al actor inexistentes fallan; un ``rollback`` no deja rastro y
  libera la clave;
* finalizar es **atomico**: escribe operacion y respuesta juntas y no reescribe una finalizacion
  previa;
* el esquema rechaza estados abiertos, respuestas no permitidas, escalares fuera de forma y
  caducidad mal formada (``CHECK`` de PostgreSQL);
* una reserva **caducada no se borra ni se reutiliza** en silencio: sigue ocupando su clave.

Los casos de bloqueo real (dos transacciones independientes) estan en
``idempotency_concurrency_test.py``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from heliclockter import datetime_utc
from sqlalchemy import text

from bracket.database import database
from bracket.logic.idempotency import (
    IDEMPOTENCY_STATE_COMPLETED,
    IDEMPOTENCY_STATE_IN_PROGRESS,
    IdempotencyDecision,
    classify_reservation,
)
from bracket.models.db.idempotency import IdempotencyCompletion
from bracket.sql.idempotency import (
    is_idempotency_unique_violation,
    sql_complete_idempotency_reservation,
    sql_insert_idempotency_reservation,
    sql_read_idempotency_reservation,
    violation_constraint_name,
)
from bracket.utils.id_types import ClubId, UserId
from tests.integration_tests.idempotency_fixtures import (
    ABSENT_ID,
    LAB_KEY,
    LAB_PATH,
    IdempotencyLab,
    lab_completion,
    lab_reservation,
    reservations_in_tenant,
    reservations_total,
    stored_reservation,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

UNIQUE_KEY = "uq_domain_idempotency_keys_tenant_actor_key"
FK_TENANT = "fk_domain_idempotency_keys_tenant_club_id"
FK_ACTOR = "fk_domain_idempotency_keys_actor_user_id"
CK_STATE = "ck_domain_idempotency_keys_state"
CK_COMPLETED = "ck_domain_idempotency_keys_completed_shape"
CK_PENDING = "ck_domain_idempotency_keys_pending_shape"
CK_RESPONSE_BODY = "ck_domain_idempotency_keys_response_body"
CK_EXPIRY = "ck_domain_idempotency_keys_expiry"

TABLE = "domain_idempotency_keys"

# Insercion directa: se salta los validadores de Python a proposito, para probar los CHECK de
# PostgreSQL (que son la ultima linea de defensa frente a una escritura que no pase por ellos).
RAW_INSERT = text(
    """
    INSERT INTO domain_idempotency_keys
        (tenant_club_id, actor_user_id, idempotency_key, request_fingerprint,
         fingerprint_key_version, request_method, request_path, state, resource_type, resource_id,
         response_status, response_body, created, expires_at, completed_at)
    VALUES
        (:tenant_club_id, :actor_user_id, :idempotency_key, :request_fingerprint,
         :fingerprint_key_version, :request_method, :request_path, :state, :resource_type,
         :resource_id, :response_status, CAST(:response_body AS jsonb), :created, :expires_at,
         :completed_at)
    """
)


def _raw_values(**overrides: Any) -> dict[str, Any]:
    created = datetime_utc.now()
    values: dict[str, Any] = {
        "tenant_club_id": 1,
        "actor_user_id": 1,
        "idempotency_key": "raw-laboratory-key-0001",
        "request_fingerprint": "0" * 64,
        "fingerprint_key_version": "v1",
        "request_method": "POST",
        "request_path": LAB_PATH,
        "state": IDEMPOTENCY_STATE_IN_PROGRESS,
        "resource_type": None,
        "resource_id": None,
        "response_status": None,
        "response_body": None,
        "created": created,
        "expires_at": created + timedelta(hours=1),
        "completed_at": None,
    }
    values.update(overrides)
    return values


async def _raw_insert(**overrides: Any) -> None:
    await database.execute(query=RAW_INSERT, values=_raw_values(**overrides))


# --- Reserva y aislamiento ----------------------------------------------------------------------


async def test_reserving_records_a_pending_reservation_without_any_response(
    idempotency_lab: IdempotencyLab,
) -> None:
    reservation = lab_reservation(idempotency_lab.club_a, idempotency_lab.user_a)

    stored = await sql_insert_idempotency_reservation(reservation)

    assert stored.state == IDEMPOTENCY_STATE_IN_PROGRESS
    assert stored.completed_at is None
    assert stored.response_status is None
    assert stored.response_body is None
    assert stored.resource_id is None
    assert stored.request_fingerprint == reservation.request_fingerprint
    assert stored.expires_at == reservation.expires_at
    assert stored.request_method == "POST"

    row = await stored_reservation(idempotency_lab.club_a, idempotency_lab.user_a)
    assert row is not None
    assert row["state"] == IDEMPOTENCY_STATE_IN_PROGRESS
    assert await reservations_in_tenant(idempotency_lab.club_a, idempotency_lab.user_a) == 1


async def test_reading_a_reservation_is_scoped_by_tenant_and_actor(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    await sql_insert_idempotency_reservation(lab_reservation(lab.club_a, lab.user_a))

    own = await sql_read_idempotency_reservation(
        tenant_club_id=lab.club_a, actor_user_id=lab.user_a, idempotency_key=LAB_KEY
    )
    assert own is not None and own.idempotency_key == LAB_KEY

    other_tenant = await sql_read_idempotency_reservation(
        tenant_club_id=lab.club_b, actor_user_id=lab.user_a, idempotency_key=LAB_KEY
    )
    other_actor = await sql_read_idempotency_reservation(
        tenant_club_id=lab.club_a, actor_user_id=lab.user_b, idempotency_key=LAB_KEY
    )
    unknown_actor = await sql_read_idempotency_reservation(
        tenant_club_id=lab.club_a, actor_user_id=UserId(ABSENT_ID), idempotency_key=LAB_KEY
    )

    assert other_tenant is None, "la clave de otro tenant no se ve"
    assert other_actor is None, "la clave de otro actor no se ve"
    assert unknown_actor is None


async def test_the_same_key_is_a_different_reservation_for_each_tenant_and_actor(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab

    await sql_insert_idempotency_reservation(lab_reservation(lab.club_a, lab.user_a))
    await sql_insert_idempotency_reservation(lab_reservation(lab.club_a, lab.user_b))
    await sql_insert_idempotency_reservation(lab_reservation(lab.club_b, lab.user_a))

    assert await reservations_in_tenant(lab.club_a, lab.user_a) == 1
    assert await reservations_in_tenant(lab.club_a, lab.user_b) == 1
    assert await reservations_in_tenant(lab.club_b, lab.user_a) == 1
    assert await reservations_total() == 3


async def test_a_second_reservation_with_the_same_key_hits_the_unique_index(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    reservation = lab_reservation(lab.club_a, lab.user_a)
    await sql_insert_idempotency_reservation(reservation)

    with pytest.raises(asyncpg.exceptions.UniqueViolationError) as error:
        await sql_insert_idempotency_reservation(reservation)

    assert violation_constraint_name(error.value) == UNIQUE_KEY
    assert is_idempotency_unique_violation(error.value)
    assert await reservations_in_tenant(lab.club_a, lab.user_a) == 1


async def test_the_same_key_with_another_fingerprint_is_a_conflict_and_keeps_the_first(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    first = lab_reservation(lab.club_a, lab.user_a, payload=b'{"display_name":"Ana"}')
    second = lab_reservation(lab.club_a, lab.user_a, payload=b'{"display_name":"Otro"}')
    assert first.request_fingerprint != second.request_fingerprint

    stored_first = await sql_insert_idempotency_reservation(first)
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await sql_insert_idempotency_reservation(second)

    reread = await sql_read_idempotency_reservation(
        tenant_club_id=lab.club_a, actor_user_id=lab.user_a, idempotency_key=LAB_KEY
    )
    assert reread is not None
    assert reread.request_fingerprint == stored_first.request_fingerprint, (
        "la reserva original no se reescribe"
    )

    decision = classify_reservation(
        reread,
        fingerprint=second.request_fingerprint,
        fingerprint_key_version=second.fingerprint_key_version,
        now=datetime_utc.now(),
    )
    assert decision is IdempotencyDecision.FINGERPRINT_CONFLICT

    same = classify_reservation(
        reread,
        fingerprint=first.request_fingerprint,
        fingerprint_key_version=first.fingerprint_key_version,
        now=datetime_utc.now(),
    )
    assert same is IdempotencyDecision.IN_PROGRESS


async def test_an_unknown_tenant_is_rejected_by_the_foreign_key(
    idempotency_lab: IdempotencyLab,
) -> None:
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError) as error:
        await sql_insert_idempotency_reservation(
            lab_reservation(ClubId(ABSENT_ID), idempotency_lab.user_a)
        )

    assert violation_constraint_name(error.value) == FK_TENANT
    assert await reservations_total() == 0


async def test_an_unknown_actor_is_rejected_by_the_foreign_key(
    idempotency_lab: IdempotencyLab,
) -> None:
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError) as error:
        await sql_insert_idempotency_reservation(
            lab_reservation(idempotency_lab.club_a, UserId(ABSENT_ID))
        )

    assert violation_constraint_name(error.value) == FK_ACTOR
    assert await reservations_total() == 0


async def test_a_rolled_back_reservation_leaves_no_row_and_frees_the_key(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    reservation = lab_reservation(lab.club_a, lab.user_a)

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        async with database.transaction():
            await sql_insert_idempotency_reservation(reservation)
            raise _Boom

    assert await reservations_total() == 0, "el rollback no deja reserva"

    stored = await sql_insert_idempotency_reservation(reservation)
    assert stored.state == IDEMPOTENCY_STATE_IN_PROGRESS, "la clave vuelve a estar libre"


# --- Finalizacion atomica -----------------------------------------------------------------------


async def test_finishing_records_the_operation_and_its_allowed_response(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    await sql_insert_idempotency_reservation(lab_reservation(lab.club_a, lab.user_a))
    metadata = {"resource_type": "tournament_registration", "registration_status": "CONFIRMED"}

    completed = await sql_complete_idempotency_reservation(
        tenant_club_id=lab.club_a,
        actor_user_id=lab.user_a,
        idempotency_key=LAB_KEY,
        completion=lab_completion(status=201, metadata=metadata),
    )

    assert completed is not None
    assert completed.state == IDEMPOTENCY_STATE_COMPLETED
    assert completed.completed_at is not None
    assert completed.response_status == 201
    assert completed.response_body == metadata
    assert completed.resource_type == "tournament_registration"
    assert completed.resource_id == 7

    decision = classify_reservation(
        completed,
        fingerprint=completed.request_fingerprint,
        fingerprint_key_version=completed.fingerprint_key_version,
        now=datetime_utc.now(),
    )
    assert decision is IdempotencyDecision.REPLAY

    replayed = completed.replay_metadata()
    assert replayed.response_status == 201
    assert replayed.response_body == metadata
    assert await reservations_in_tenant(lab.club_a, lab.user_a) == 1


async def test_finishing_twice_does_not_rewrite_the_first_completion(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    await sql_insert_idempotency_reservation(lab_reservation(lab.club_a, lab.user_a))
    first = await sql_complete_idempotency_reservation(
        tenant_club_id=lab.club_a,
        actor_user_id=lab.user_a,
        idempotency_key=LAB_KEY,
        completion=lab_completion(status=201, metadata={"resource_id": 7}),
    )
    assert first is not None

    again = await sql_complete_idempotency_reservation(
        tenant_club_id=lab.club_a,
        actor_user_id=lab.user_a,
        idempotency_key=LAB_KEY,
        completion=lab_completion(status=500, metadata={"resource_id": 8}),
    )

    assert again is None, "una reserva ya confirmada no se vuelve a finalizar"
    row = await stored_reservation(lab.club_a, lab.user_a)
    assert row is not None
    assert row["response_status"] == 201
    assert row["response_body"] == {"resource_id": 7}


async def test_finishing_without_a_reservation_or_outside_its_scope_does_nothing(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    await sql_insert_idempotency_reservation(lab_reservation(lab.club_a, lab.user_a))
    completion = lab_completion()

    unknown_key = await sql_complete_idempotency_reservation(
        tenant_club_id=lab.club_a,
        actor_user_id=lab.user_a,
        idempotency_key="lab-idempotency-key-9999",
        completion=completion,
    )
    other_tenant = await sql_complete_idempotency_reservation(
        tenant_club_id=lab.club_b,
        actor_user_id=lab.user_a,
        idempotency_key=LAB_KEY,
        completion=completion,
    )
    other_actor = await sql_complete_idempotency_reservation(
        tenant_club_id=lab.club_a,
        actor_user_id=lab.user_b,
        idempotency_key=LAB_KEY,
        completion=completion,
    )

    assert unknown_key is None
    assert other_tenant is None
    assert other_actor is None
    row = await stored_reservation(lab.club_a, lab.user_a)
    assert row is not None and row["state"] == IDEMPOTENCY_STATE_IN_PROGRESS


# --- Lo que PostgreSQL rechaza aunque el llamador no pase por Python ----------------------------


async def test_a_committed_state_without_its_response_is_impossible(
    idempotency_lab: IdempotencyLab,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as error:
        await _raw_insert(state=IDEMPOTENCY_STATE_COMPLETED)

    assert violation_constraint_name(error.value) == CK_COMPLETED


async def test_a_pending_reservation_cannot_carry_response_data(
    idempotency_lab: IdempotencyLab,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as error:
        await _raw_insert(state=IDEMPOTENCY_STATE_IN_PROGRESS, response_status=200)

    assert violation_constraint_name(error.value) == CK_PENDING


async def test_an_ambiguous_state_is_rejected(idempotency_lab: IdempotencyLab) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as error:
        await _raw_insert(state="COMMITTED")

    assert violation_constraint_name(error.value) == CK_STATE


@pytest.mark.parametrize(
    "body",
    [
        '{"token": "abc"}',
        '{"headers": {"authorization": "Bearer x"}}',
        '{"resource_id": {"nested": 1}}',
        '{"registration_status": "' + "x" * 2048 + '"}',
        '[1, 2, 3]',
    ],
)
async def test_disallowed_response_bodies_are_rejected_by_postgresql(
    idempotency_lab: IdempotencyLab, body: str
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as error:
        await _raw_insert(
            state=IDEMPOTENCY_STATE_COMPLETED,
            response_status=200,
            response_body=body,
            completed_at=datetime_utc.now(),
        )

    assert violation_constraint_name(error.value) == CK_RESPONSE_BODY


async def test_scalars_out_of_shape_are_rejected(idempotency_lab: IdempotencyLab) -> None:
    created = datetime_utc.now()

    cases: list[tuple[str, dict[str, Any]]] = [
        ("ck_domain_idempotency_keys_request_method", {"request_method": "GET"}),
        ("ck_domain_idempotency_keys_fingerprint", {"request_fingerprint": "Z" * 64}),
        ("ck_domain_idempotency_keys_fingerprint", {"request_fingerprint": "0" * 63}),
        ("ck_domain_idempotency_keys_fingerprint_version", {"fingerprint_key_version": "1"}),
        ("ck_domain_idempotency_keys_key_shape", {"idempotency_key": "corta"}),
        ("ck_domain_idempotency_keys_key_shape", {"idempotency_key": "clave con espacios"}),
        ("ck_domain_idempotency_keys_key_shape", {"idempotency_key": "k" * 256}),
        ("ck_domain_idempotency_keys_request_path", {"request_path": "sin-barra"}),
        ("ck_domain_idempotency_keys_expiry", {"expires_at": created}),
        ("ck_domain_idempotency_keys_resource_coherence", {"resource_type": "tournament"}),
        (
            "ck_domain_idempotency_keys_response_status",
            {
                "state": IDEMPOTENCY_STATE_COMPLETED,
                "response_status": 99,
                "completed_at": created,
            },
        ),
    ]

    for expected_constraint, overrides in cases:
        with pytest.raises(asyncpg.exceptions.CheckViolationError) as error:
            await _raw_insert(**overrides)
        assert violation_constraint_name(error.value) == expected_constraint, overrides

    assert await reservations_total() == 0


async def test_an_expired_reservation_is_neither_deleted_nor_reused(
    idempotency_lab: IdempotencyLab,
) -> None:
    lab = idempotency_lab
    long_ago = datetime_utc.now() - timedelta(hours=48)
    expired = lab_reservation(
        lab.club_a, lab.user_a, created=long_ago, lifetime=timedelta(hours=1)
    )
    stored = await sql_insert_idempotency_reservation(expired)
    assert stored.expires_at <= datetime_utc.now()

    decision = classify_reservation(
        stored,
        fingerprint=expired.request_fingerprint,
        fingerprint_key_version=expired.fingerprint_key_version,
        now=datetime_utc.now(),
    )
    assert decision is IdempotencyDecision.EXPIRED, "caducada no se reutiliza en silencio"

    assert await reservations_total() == 1, "no se purga: la fila sigue ahi"

    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await sql_insert_idempotency_reservation(expired)

    assert await reservations_total() == 1, "la clave caducada sigue ocupada"


async def test_the_table_has_no_column_for_the_request_payload(
    idempotency_lab: IdempotencyLab,
) -> None:
    columns = {
        str(record._mapping["column_name"])
        for record in await database.fetch_all(
            query="""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = :table
            """,
            values={"table": TABLE},
        )
    }

    assert columns, "la tabla existe"
    assert columns & {"payload", "request_body", "body", "jwt", "token", "cookie"} == set()
    assert "response_body" in columns, "solo los metadatos permitidos"
