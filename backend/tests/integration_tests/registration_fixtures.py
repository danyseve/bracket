# pylint: disable=redefined-outer-name  # `registration_data` es un fixture de modulo.
"""Soporte de pruebas del nucleo de inscripciones (S3.1 de F3B).

Datos, *fixture* y utilidades compartidos por ``registration_write_test.py`` (ciclo de
escritura) y ``registration_validation_test.py`` (validacion de entrada y control de
acceso). No contiene pruebas: pytest no lo recoge por el nombre.

Todo se ejecuta contra ``bracket_test`` y cada fila creada deja su limpieza en el
``AsyncExitStack``, de hojas a raices: inscripciones -> afiliaciones -> competidores ->
academias -> torneos -> clubes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from databases import Database
from heliclockter import datetime_utc

from bracket.database import database
from bracket.models.db.club import ClubInsertable
from bracket.models.db.domain import (
    ActorContext,
    RegistrationDraftData,
)
from bracket.models.db.user_x_club import UserXClubInsertable, UserXClubRelation
from bracket.utils.dummy_records import DUMMY_TOURNAMENT
from bracket.utils.id_types import (
    ClubId,
    CompetitorId,
    CompetitorXSportsClubId,
    SportsClubId,
    TournamentId,
    TournamentRegistrationId,
)
from tests.integration_tests.mocks import get_mock_user
from tests.integration_tests.sql import (
    inserted_club,
    inserted_tournament,
    inserted_user,
    inserted_user_x_club,
)

INSERT_SPORTS_CLUB = """
    INSERT INTO sports_clubs (name, tenant_club_id, active, created)
    VALUES (:name, :tenant_club_id, :active, :created)
    RETURNING id
"""

# Desactivar es la unica forma de dejar de representar una academia: el FK de
# `sports_club_id` en las inscripciones es RESTRICT.
UPDATE_SPORTS_CLUB_ACTIVE = """
    UPDATE sports_clubs SET active = :active, updated_at = NOW() WHERE id = :sports_club_id
"""

INSERT_COMPETITOR = """
    INSERT INTO competitors (display_name, managed_by_club_id, active, created)
    VALUES (:display_name, :managed_by_club_id, :active, :created)
    RETURNING id
"""

# La baja logica de un competidor es un UPDATE de `active`: el FK de `competitor_id`
# en las inscripciones es RESTRICT, asi que no borra ni arrastra nada.
UPDATE_COMPETITOR_ACTIVE = """
    UPDATE competitors SET active = :active, updated_at = NOW() WHERE id = :competitor_id
"""

INSERT_AFFILIATION = """
    INSERT INTO competitors_x_sports_clubs
        (competitor_id, sports_club_id, valid_from, valid_to, is_primary, created)
    VALUES (:competitor_id, :sports_club_id, :valid_from, :valid_to, :is_primary, :created)
    RETURNING id
"""

INSERT_RAW_REGISTRATION = """
    INSERT INTO tournament_registrations (
        tournament_id, competitor_id, identity_status, representation,
        competitor_name_snapshot, status, revision, created
    ) VALUES (
        :tournament_id, :competitor_id,
        CAST(:identity_status AS registration_identity_status),
        CAST(:representation AS registration_representation),
        :competitor_name_snapshot, CAST(:status AS registration_status), 1, NOW()
    )
    RETURNING id
"""

COUNT_REGISTRATIONS = """
    SELECT count(*) FROM tournament_registrations
    WHERE tournament_id = :tournament_id AND competitor_id = :competitor_id
"""

SELECT_REGISTRATION_AUDIT = """
    SELECT entity, entity_id, action, changed_fields, actor_user_id, actor_label, reason
    FROM domain_change_log
    WHERE entity = 'tournament_registration' AND entity_id = :registration_id
    ORDER BY id
"""

SELECT_ALL_AUDIT = """
    SELECT entity, entity_id, action, changed_fields, actor_user_id, actor_label, reason
    FROM domain_change_log
"""

DELETE_REGISTRATION_AUDIT = """
    DELETE FROM domain_change_log
    WHERE entity = 'tournament_registration'
        AND entity_id IN (SELECT id FROM tournament_registrations WHERE tournament_id = :t)
"""

DELETE_COMPETITOR_AUDIT = """
    DELETE FROM domain_change_log
    WHERE entity = 'competitor'
        AND entity_id IN (SELECT id FROM competitors WHERE managed_by_club_id = :t)
"""

DELETE_REGISTRATION_LEAVES = """
    DELETE FROM tournament_registrations tr
    WHERE tr.tournament_id = :tournament_id
    AND NOT EXISTS (
        SELECT 1 FROM tournament_registrations child
        WHERE child.corrects_registration_id = tr.id
    )
"""


@dataclass
class RegistrationData:  # pylint: disable=too-many-instance-attributes
    """Dos tenants con academia propia, academia inactiva, academia ajena y afiliaciones."""

    tenant_a: ClubId
    tenant_b: ClubId
    tournament_a: TournamentId
    tournament_b: TournamentId
    sports_club_a: SportsClubId
    sports_club_a_inactive: SportsClubId
    sports_club_b: SportsClubId
    competitor_a: CompetitorId
    competitor_b: CompetitorId
    affiliation_a: CompetitorXSportsClubId
    affiliation_other_club: CompetitorXSportsClubId
    context_owner_a: ActorContext
    context_collaborator_a: ActorContext
    context_owner_b: ActorContext
    context_outsider_a: ActorContext


async def insert_sports_club(
    name: str, tenant_club_id: ClubId | None, *, active: bool, created: datetime_utc
) -> SportsClubId:
    sports_club_id = await database.fetch_val(
        query=INSERT_SPORTS_CLUB,
        values={
            "name": name,
            "tenant_club_id": tenant_club_id,
            "active": active,
            "created": created,
        },
    )
    return SportsClubId(sports_club_id)


async def set_sports_club_active(sports_club_id: SportsClubId, *, active: bool) -> None:
    """Activa o desactiva una academia: sirve para probar la regla A5 al confirmar."""
    await database.execute(
        query=UPDATE_SPORTS_CLUB_ACTIVE,
        values={"sports_club_id": sports_club_id, "active": active},
    )


async def insert_competitor(
    display_name: str,
    managed_by_club_id: ClubId | None,
    *,
    created: datetime_utc,
    active: bool = True,
) -> CompetitorId:
    competitor_id = await database.fetch_val(
        query=INSERT_COMPETITOR,
        values={
            "display_name": display_name,
            "managed_by_club_id": managed_by_club_id,
            "active": active,
            "created": created,
        },
    )
    return CompetitorId(competitor_id)


async def set_competitor_active(competitor_id: CompetitorId, *, active: bool) -> None:
    """Baja logica o reactivacion directa en la BBDD (S2-bis aun no existe como API).

    Se usa para probar la elegibilidad sin simular una operacion que no esta implementada.
    """
    await database.execute(
        query=UPDATE_COMPETITOR_ACTIVE,
        values={"competitor_id": competitor_id, "active": active},
    )


async def insert_affiliation(
    *,
    competitor_id: CompetitorId,
    sports_club_id: SportsClubId,
    is_primary: bool,
    created: datetime_utc,
) -> CompetitorXSportsClubId:
    affiliation_id = await database.fetch_val(
        query=INSERT_AFFILIATION,
        values={
            "competitor_id": competitor_id,
            "sports_club_id": sports_club_id,
            "valid_from": created,
            "valid_to": None,
            "is_primary": is_primary,
            "created": created,
        },
    )
    return CompetitorXSportsClubId(affiliation_id)


async def delete_registrations(tournament_id: TournamentId) -> None:
    """Borra las inscripciones del torneo empezando por las hojas (FK RESTRICT)."""
    for _ in range(5):
        remaining = await database.fetch_val(
            query="SELECT count(*) FROM tournament_registrations WHERE tournament_id = :t",
            values={"t": tournament_id},
        )
        if not remaining:
            break
        await database.execute(
            query=DELETE_REGISTRATION_LEAVES, values={"tournament_id": tournament_id}
        )


async def delete_affiliations(competitor_id: CompetitorId) -> None:
    await database.execute(
        query="DELETE FROM competitors_x_sports_clubs WHERE competitor_id = :c",
        values={"c": competitor_id},
    )


async def delete_competitor(competitor_id: CompetitorId) -> None:
    await database.execute(
        query="DELETE FROM competitors WHERE id = :c", values={"c": competitor_id}
    )


async def delete_sports_club(sports_club_id: SportsClubId) -> None:
    await database.execute(
        query="DELETE FROM sports_clubs WHERE id = :s", values={"s": sports_club_id}
    )


async def delete_registration_audit(
    tournament_ids: tuple[TournamentId, ...], tenant_id: ClubId
) -> None:
    """Retira la auditoria propia: las inscripciones y los competidores del fixture."""
    for tournament_id in tournament_ids:
        await database.execute(query=DELETE_REGISTRATION_AUDIT, values={"t": tournament_id})
    await database.execute(query=DELETE_COMPETITOR_AUDIT, values={"t": tenant_id})
    for tournament_id in tournament_ids:
        await delete_registrations(tournament_id)


@asynccontextmanager
async def registration_data_context(reinit_database: Database) -> AsyncIterator[RegistrationData]:
    """Datos completos de S3.1 (dos tenants, torneos, usuarios, academias, competidores).

    Se expone como *context manager* para que cada modulo de pruebas declare su propio
    ``registration_data`` (pytest solo resuelve fixtures del modulo o del ``conftest``) sin
    duplicar los datos ni la limpieza.
    """
    now = datetime_utc.now()

    async with AsyncExitStack() as stack:
        club_a = await stack.enter_async_context(
            inserted_club(ClubInsertable(name="Tenant A (F3B-S3)", created=now))
        )
        club_b = await stack.enter_async_context(
            inserted_club(ClubInsertable(name="Tenant B (F3B-S3)", created=now))
        )
        tournament_a = await stack.enter_async_context(
            inserted_tournament(
                DUMMY_TOURNAMENT.model_copy(
                    update={"club_id": club_a.id, "dashboard_endpoint": "f3b-s3-tenant-a"}
                )
            )
        )
        tournament_b = await stack.enter_async_context(
            inserted_tournament(
                DUMMY_TOURNAMENT.model_copy(
                    update={"club_id": club_b.id, "dashboard_endpoint": "f3b-s3-tenant-b"}
                )
            )
        )

        owner_a = await stack.enter_async_context(inserted_user(get_mock_user()))
        collaborator_a = await stack.enter_async_context(inserted_user(get_mock_user()))
        owner_b = await stack.enter_async_context(inserted_user(get_mock_user()))
        await stack.enter_async_context(
            inserted_user_x_club(
                UserXClubInsertable(
                    user_id=owner_a.id, club_id=club_a.id, relation=UserXClubRelation.OWNER
                )
            )
        )
        await stack.enter_async_context(
            inserted_user_x_club(
                UserXClubInsertable(
                    user_id=collaborator_a.id,
                    club_id=club_a.id,
                    relation=UserXClubRelation.COLLABORATOR,
                )
            )
        )
        # `owner_b` solo tiene relacion con el otro tenant: sirve de actor ajeno.
        await stack.enter_async_context(
            inserted_user_x_club(
                UserXClubInsertable(
                    user_id=owner_b.id, club_id=club_b.id, relation=UserXClubRelation.OWNER
                )
            )
        )

        sports_club_a = await insert_sports_club(
            "Academia Propia A", club_a.id, active=True, created=now
        )
        sports_club_a_inactive = await insert_sports_club(
            "Academia Antigua A", club_a.id, active=False, created=now
        )
        sports_club_b = await insert_sports_club(
            "Academia Ajena B", club_b.id, active=True, created=now
        )
        for sports_club_id in (sports_club_a, sports_club_a_inactive, sports_club_b):
            stack.push_async_callback(delete_sports_club, sports_club_id)

        competitor_a = await insert_competitor("Ana Gomez", club_a.id, created=now)
        competitor_b = await insert_competitor("Bea Ruiz", club_b.id, created=now)
        for competitor_id in (competitor_a, competitor_b):
            stack.push_async_callback(delete_competitor, competitor_id)

        affiliation_a = await insert_affiliation(
            competitor_id=competitor_a,
            sports_club_id=sports_club_a,
            is_primary=True,
            created=now,
        )
        affiliation_other_club = await insert_affiliation(
            competitor_id=competitor_a,
            sports_club_id=sports_club_b,
            is_primary=False,
            created=now,
        )
        stack.push_async_callback(delete_affiliations, competitor_a)

        # Lo ultimo que se apila se deshace primero: inscripciones y su auditoria antes
        # de afiliaciones, competidores y academias.
        stack.push_async_callback(
            delete_registration_audit, (tournament_a.id, tournament_b.id), club_a.id
        )

        yield RegistrationData(
            tenant_a=club_a.id,
            tenant_b=club_b.id,
            tournament_a=tournament_a.id,
            tournament_b=tournament_b.id,
            sports_club_a=sports_club_a,
            sports_club_a_inactive=sports_club_a_inactive,
            sports_club_b=sports_club_b,
            competitor_a=competitor_a,
            competitor_b=competitor_b,
            affiliation_a=affiliation_a,
            affiliation_other_club=affiliation_other_club,
            context_owner_a=ActorContext(
                tenant_club_id=club_a.id, actor_user_id=owner_a.id, actor_label="owner-a"
            ),
            context_collaborator_a=ActorContext(
                tenant_club_id=club_a.id,
                actor_user_id=collaborator_a.id,
                actor_label="collaborator-a",
            ),
            context_owner_b=ActorContext(
                tenant_club_id=club_b.id, actor_user_id=owner_b.id, actor_label="owner-b"
            ),
            # `owner_b` reclamando el tenant A: el actor no pertenece a ese tenant.
            context_outsider_a=ActorContext(
                tenant_club_id=club_a.id, actor_user_id=owner_b.id, actor_label="outsider-a"
            ),
        )


def build_draft(**overrides: object) -> RegistrationDraftData:
    """Borrador valido por defecto: independiente, sin academia, con categoria."""
    values: dict[str, object] = {
        "competitor_id": None,
        "identity_status": None,
        "representation": "INDEPENDENT",
        "sports_club_id": None,
        "affiliation_id": None,
        "category_key": None,
        "category_label": None,
        "competitor_name_snapshot": None,
    }
    values.update(overrides)
    return RegistrationDraftData.model_validate(values)


async def audit_rows(registration_id: TournamentRegistrationId) -> list[dict[str, object]]:
    records = await database.fetch_all(
        query=SELECT_REGISTRATION_AUDIT, values={"registration_id": registration_id}
    )
    return [dict(record._mapping) for record in records]


async def count_current_registrations(
    *, tournament_id: TournamentId, competitor_id: CompetitorId
) -> int:
    count = await database.fetch_val(
        query=COUNT_REGISTRATIONS,
        values={"tournament_id": tournament_id, "competitor_id": competitor_id},
    )
    return int(count)


async def fetch_all_audit_rows_as_text() -> list[str]:
    """Volcado de **toda** la auditoria como texto, para comprobar que no hay PII.

    No filtra por entidad ni por actor a proposito: el objetivo es que ningun evento
    contenga valores, solo nombres de campo.
    """
    records = await database.fetch_all(query=SELECT_ALL_AUDIT)
    return [str(dict(record._mapping)) for record in records]


# --- S2-bis: auditoria de competidor y sondas de concurrencia compartidas -----------------------


SELECT_COMPETITOR_AUDIT = """
    SELECT entity, entity_id, action, changed_fields, actor_user_id, actor_label, reason
    FROM domain_change_log
    WHERE entity = 'competitor' AND entity_id = :competitor_id
    ORDER BY id
"""

# Evidencia de espera: al menos `expected` backends detenidos por un bloqueo en esa sentencia.
SELECT_WAITING_BACKENDS = """
    SELECT count(*) >= :expected FROM pg_stat_activity
    WHERE datname = current_database()
        AND wait_event_type = 'Lock'
        AND query LIKE :pattern
"""

# Evidencia de conflicto de fila: los bloqueos de fila solo aparecen en `pg_locks` mientras
# hay contencion (el que espera y el que ya lo tiene), asi que su existencia es la senal.
SELECT_TUPLE_CONTENTION = """
    SELECT count(*) FROM pg_locks
    WHERE locktype = 'tuple' AND relation = to_regclass(:relation)
"""

WAITING_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.01


async def competitor_audit_rows(competitor_id: CompetitorId) -> list[dict[str, object]]:
    """Eventos de auditoria de una identidad, en orden de insercion."""
    records = await database.fetch_all(
        query=SELECT_COMPETITOR_AUDIT, values={"competitor_id": competitor_id}
    )
    return [dict(record._mapping) for record in records]


async def wait_until_true(query: str, values: dict[str, object], *, message: str) -> None:
    """Espera acotada a que la consulta devuelva cierto; si no, falla con ese mensaje.

    El limite no es el mecanismo de sincronizacion (para eso estan los eventos): es la red
    que convierte "nunca llego a bloquearse" en un fallo explicito en vez de un cuelgue.
    """
    deadline = asyncio.get_running_loop().time() + WAITING_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if await database.fetch_val(query=query, values=values):
            return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(message)


async def wait_for_waiting_backends(pattern: str, *, expected: int = 1) -> None:
    """Espera a que al menos ``expected`` backends esperen un bloqueo en esa sentencia."""
    await wait_until_true(
        SELECT_WAITING_BACKENDS,
        {"pattern": pattern, "expected": expected},
        message=f"menos de {expected} backend(s) esperaban un bloqueo con la sentencia {pattern!r}",
    )


async def wait_for_tuple_contention(relation: str) -> None:
    """Espera a que haya contencion de bloqueo de fila (row lock) en esa tabla."""
    await wait_until_true(
        SELECT_TUPLE_CONTENTION,
        {"relation": relation},
        message=f"no aparecio contencion de bloqueo de fila en {relation!r}",
    )
