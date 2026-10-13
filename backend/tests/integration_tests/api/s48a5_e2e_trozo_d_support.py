# pylint: disable=redefined-outer-name
"""Soporte del E2E realista de S4.8a-5 (TROZO D §17-§21): privacidad, idempotencia, auditoria,
compatibilidad de categoria legacy y contrato OpenAPI, siempre por el API HTTP real.

No contiene pruebas (el nombre no termina en ``_test``). Reutiliza el escenario del TROZO C2
(``trozo_c2_scenario``: dataset A + dos superficies de clasificacion + torneo de cuadros) y anade
solo lo que §17-§21 necesitan:

* la **matriz de privacidad** campo -> (publico / organizador) con el ``archivo:linea`` real que lo
  garantiza (``PRIVACY_MATRIX``);
* lecturas de auditoria sobre ``domain_change_log`` y de claves sobre ``domain_idempotency_keys``,
  acotadas al tenant del escenario;
* un **torneo de calendario publico** aislado con un combate ya publicado (dos equipos con el nombre
  de dos competidores del escenario), para comprobar por HTTP que la proyeccion publica no lleva
  datos personales;
* el volcado de evidencia de §25 (``write_evidence_d``).

No hay mocks del motor, del protocolo ni del RBAC: se ejercitan las rutas reales y se lee lo que el
backend escribe.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from bracket.database import database
from tests.integration_tests.api.s48a5_e2e_trozo_a_support import (
    CREATED,
    START_TIME,
    E2EDataset,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url

#: Ruta de la evidencia congelable de §25 para este trozo.
EVIDENCE_D_PATH: Final = Path("/home/ubuntu/s48a5-evidence/trozo-d.json")

#: Matriz de privacidad: cada campo sensible, si viaja (``True``) o no (``False``) en la proyeccion
#: **publica** y en la del **organizador**, y el ``archivo:linea`` real que lo garantiza. Las lineas
#: son las del HEAD auditado; un cambio de contrato que mueva esa garantia invalida la matriz.
PRIVACY_MATRIX: Final[tuple[dict[str, Any], ...]] = (
    {
        "field": "date_of_birth",
        "public": False,
        "organizer": True,
        "organizer_source": (
            "backend/bracket/models/db/competitor_api.py:113 (CompetitorResponse.date_of_birth), "
            ":126 (from_competitor)"
        ),
        "public_source": (
            "backend/bracket/logic/scheduling/public_schedule.py:34 (PUBLIC_MATCH_KEYS, ausente), "
            ":250-262 (forbidden incluye date_of_birth); "
            "frontend/src/components/utils/f3_privacy.ts:15-31 (whitelist id+display_name)"
        ),
    },
    {
        "field": "sex",
        "public": False,
        "organizer": True,
        "organizer_source": "backend/bracket/models/db/competitor_api.py:114 (CompetitorResponse.sex)",
        "public_source": "backend/bracket/logic/scheduling/public_schedule.py:34 (PUBLIC_MATCH_KEYS)",
    },
    {
        "field": "belt_id",
        "public": False,
        "organizer": True,
        "organizer_source": (
            "backend/bracket/models/db/competitor_api.py:115 (CompetitorResponse.belt_id)"
        ),
        "public_source": "backend/bracket/logic/scheduling/public_schedule.py:34 (PUBLIC_MATCH_KEYS)",
    },
    {
        "field": "declared_weight_kg",
        "public": False,
        "organizer": True,
        "organizer_source": (
            "backend/bracket/models/db/registration_api.py:134 "
            "(RegistrationResponse.declared_weight_kg), :155 (from_registration)"
        ),
        "public_source": (
            "backend/bracket/logic/scheduling/public_schedule.py:34 (PUBLIC_MATCH_KEYS, ausente); "
            "frontend/src/components/utils/f3_privacy.ts:35-41 (PublicRegistrationSource sin peso)"
        ),
    },
)

#: Validaciones, matriz y evidencias acumuladas por el modulo de pruebas (se vuelcan al cerrar).
EVIDENCE_D: dict[str, Any] = {
    "trozo": "D",
    "privacy": {
        "matrix": [dict(row) for row in PRIVACY_MATRIX],
        "organizer_http": {},
        "public_http": {},
    },
    "idempotency": {"writes": [], "findings": []},
    "audit": {"events": {}, "reads_do_not_audit": {}},
    "legacy": {},
    "openapi": {},
    "validations": [],
}


def record_d(name: str, passed: bool, detail: dict[str, Any] | None = None) -> None:
    """Anota el resultado de una validacion del trozo D para el resumen de §25."""
    EVIDENCE_D["validations"].append({"name": name, "passed": bool(passed), "detail": detail or {}})


def write_evidence_d() -> Path:
    """Vuelca la evidencia de §25 del trozo D (una vez, al cerrar el modulo)."""
    EVIDENCE_D_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_D_PATH.write_text(
        json.dumps(EVIDENCE_D, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    return EVIDENCE_D_PATH


# ---------------------------------------------------------------- URLs


def public_schedule_url(tournament_id: int) -> str:
    """Calendario publico (§11/§17): la pantalla de la grada, sin sesion obligatoria."""
    return f"{get_root_uvicorn_url()}public/tournaments/{tournament_id}/schedule"


# ---------------------------------------------------------------- SQL (lecturas de auditoria/claves)

_SELECT_AUDIT = """
    SELECT id, entity, entity_id, action, changed_fields, actor_user_id, actor_label,
           reason, reason_code, reason_note, tenant_club_id, created
    FROM domain_change_log
    WHERE tenant_club_id = :club
    ORDER BY id
"""
_SELECT_IDEMPOTENCY = """
    SELECT idempotency_key, request_path, request_method, state, resource_type, resource_id,
           response_status, actor_user_id
    FROM domain_idempotency_keys
    WHERE tenant_club_id = :club
    ORDER BY id
"""
_SELECT_REGISTRATION_COUNT = (
    "SELECT count(*) FROM tournament_registrations WHERE tournament_id = :tournament"
)
_SELECT_AFFILIATION_COUNT = (
    "SELECT count(*) FROM competitors_x_sports_clubs WHERE competitor_id = :competitor"
)
_SELECT_OPEN_AFFILIATION_COUNT = (
    "SELECT count(*) FROM competitors_x_sports_clubs "
    "WHERE competitor_id = :competitor AND valid_to IS NULL"
)
_SELECT_SPORTS_CLUB = "SELECT id, name, logo_path FROM sports_clubs WHERE id = :id"


def _row_to_dict(record: Any) -> dict[str, Any]:
    """Fila de DB a ``dict`` plano, serializando ``datetime``/``date`` a ISO."""
    out: dict[str, Any] = {}
    for key, value in dict(record._mapping).items():
        if isinstance(value, (date,)):
            out[key] = value.isoformat()
        elif hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


async def audit_events(
    tenant_club_id: int,
    *,
    entity: str | None = None,
    action: str | None = None,
    reason_code: str | None = None,
    entity_id: int | None = None,
) -> list[dict[str, Any]]:
    """Eventos reales de ``domain_change_log`` del tenant, filtrados por los campos que se pidan.

    El filtro por ``reason_code`` admite ``None`` explicito (no filtra) frente a un codigo real:
    para exigir "sin motivo" se compara el campo en el propio test.
    """
    rows = await database.fetch_all(query=_SELECT_AUDIT, values={"club": tenant_club_id})
    events = [_row_to_dict(row) for row in rows]
    if entity is not None:
        events = [event for event in events if event["entity"] == entity]
    if action is not None:
        events = [event for event in events if event["action"] == action]
    if reason_code is not None:
        events = [event for event in events if event["reason_code"] == reason_code]
    if entity_id is not None:
        events = [event for event in events if int(event["entity_id"]) == int(entity_id)]
    return events


async def count_audit_events(tenant_club_id: int, **filters: Any) -> int:
    """Cuantos eventos de auditoria cumple el filtro (para antes/despues y "leer no audita")."""
    return len(await audit_events(tenant_club_id, **filters))


async def count_idempotency_keys(tenant_club_id: int) -> int:
    """Claves de idempotencia reservadas por el tenant."""
    rows = await database.fetch_all(
        query=_SELECT_IDEMPOTENCY, values={"club": tenant_club_id}
    )
    return len(rows)


async def idempotency_rows_for_key(tenant_club_id: int, key: str) -> list[dict[str, Any]]:
    """Filas de idempotencia del tenant con esa clave (deberia haber exactamente una por reserva)."""
    rows = await database.fetch_all(
        query=_SELECT_IDEMPOTENCY, values={"club": tenant_club_id}
    )
    return [
        _row_to_dict(row)
        for row in rows
        if str(row["idempotency_key"]) == key
    ]


async def count_registrations(tournament_id: int) -> int:
    """Inscripciones del torneo (todas, incluidas retiradas): efecto observable de un alta."""
    return int(
        await database.fetch_val(
            query=_SELECT_REGISTRATION_COUNT, values={"tournament": tournament_id}
        )
    )


async def count_affiliations(competitor_id: int, *, open_only: bool = False) -> int:
    """Afiliaciones del competidor (todas o solo las vigentes): efecto de alta/cierre/independencia."""
    query = _SELECT_OPEN_AFFILIATION_COUNT if open_only else _SELECT_AFFILIATION_COUNT
    return int(await database.fetch_val(query=query, values={"competitor": competitor_id}))


async def sports_club_row(sports_club_id: int) -> dict[str, Any]:
    """Fila minima de la academia (``logo_path``) para comprobar el efecto del logo."""
    record = await database.fetch_one(query=_SELECT_SPORTS_CLUB, values={"id": sports_club_id})
    assert record is not None
    return _row_to_dict(record)


# ---------------------------------------------------------------- torneo de calendario publico

_INSERT_PUBLIC_TOURNAMENT = """
    INSERT INTO tournaments (
        name, created, start_time, club_id, dashboard_public, dashboard_endpoint,
        players_can_be_in_multiple_teams, auto_assign_courts, duration_minutes, margin_minutes
    ) VALUES (
        :name, :created, :start_time, :club_id, true, NULL, false, false, 10, 5
    ) RETURNING id
"""
_INSERT_PUBLIC_STAGE = """
    INSERT INTO stages (tournament_id, name, created, is_active)
    VALUES (:tournament_id, :name, :created, true) RETURNING id
"""
_INSERT_PUBLIC_RANKING = """
    INSERT INTO rankings (
        created, tournament_id, position, win_points, draw_points, loss_points, add_score_points
    ) VALUES (:created, :tournament_id, 1, 3, 1, 0, false) RETURNING id
"""
_INSERT_PUBLIC_STAGE_ITEM = """
    INSERT INTO stage_items (
        name, stage_id, team_count, ranking_id, type, sex_constraint, age_min, age_max,
        weight_min_kg, weight_max_kg, modality, belt_min_id, belt_max_id
    ) VALUES (
        :name, :stage_id, 2, :ranking_id, CAST('ROUND_ROBIN' AS stage_type),
        NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
    ) RETURNING id
"""
_INSERT_PUBLIC_COURT = (
    "INSERT INTO courts (name, created, tournament_id) VALUES (:name, :created, :t) RETURNING id"
)
_INSERT_PUBLIC_TEAM = (
    "INSERT INTO teams (name, created, tournament_id) VALUES (:name, :created, :t) RETURNING id"
)
_INSERT_PUBLIC_INPUT = """
    INSERT INTO stage_item_inputs (slot, tournament_id, stage_item_id, team_id)
    VALUES (:slot, :t, :stage_item_id, :team_id) RETURNING id
"""
_INSERT_PUBLIC_ROUND = """
    INSERT INTO rounds (name, created, is_draft, stage_item_id)
    VALUES (:name, :created, false, :stage_item_id) RETURNING id
"""
_INSERT_PUBLIC_MATCH = """
    INSERT INTO matches (
        created, start_time, duration_minutes, margin_minutes, round_id,
        stage_item_input1_id, stage_item_input2_id,
        stage_item_input1_conflict, stage_item_input2_conflict, court_id,
        stage_item_input1_score, stage_item_input2_score, position_in_schedule
    ) VALUES (
        :created, :start_time, 10, 5, :round_id,
        :input1, :input2, false, false, :court_id, 0, 0, 1
    ) RETURNING id
"""


@asynccontextmanager
async def public_schedule_tournament(
    dataset: E2EDataset, *, fighter_keys: tuple[str, str]
) -> AsyncIterator[int]:
    """Torneo **aislado** del mismo tenant con un combate ya publicado (§17).

    Monta un torneo con panel publico activado, un tatami, una categoria, una ronda **no** borrador
    y un combate cuyos dos equipos llevan el **nombre HTTP real** de dos competidores del escenario.
    Asi el cuerpo publico identifica a esos competidores por su nombre (no es una prueba vacia)
    mientras el propio guardin de privacidad y la matriz demuestran que ningun dato personal viaja.
    Se deshace de hojas a raices al cerrar.
    """
    async with AsyncExitStack() as stack:
        suffix = uuid4().hex[:8]
        tournament_id = int(
            await database.fetch_val(
                query=_INSERT_PUBLIC_TOURNAMENT,
                values={
                    "name": f"{dataset.name_prefix}-public-{suffix}",
                    "created": CREATED,
                    "start_time": START_TIME,
                    "club_id": dataset.club,
                },
            )
        )

        async def _teardown() -> None:
            await database.execute(
                query="DELETE FROM matches WHERE round_id IN "
                "(SELECT id FROM rounds WHERE stage_item_id IN "
                "(SELECT id FROM stage_items WHERE stage_id IN "
                "(SELECT id FROM stages WHERE tournament_id = :t)))",
                values={"t": tournament_id},
            )
            await database.execute(
                query="DELETE FROM rounds WHERE stage_item_id IN "
                "(SELECT id FROM stage_items WHERE stage_id IN "
                "(SELECT id FROM stages WHERE tournament_id = :t))",
                values={"t": tournament_id},
            )
            await database.execute(
                query="DELETE FROM stage_item_inputs WHERE stage_item_id IN "
                "(SELECT id FROM stage_items WHERE stage_id IN "
                "(SELECT id FROM stages WHERE tournament_id = :t))",
                values={"t": tournament_id},
            )
            await database.execute(
                query="DELETE FROM teams WHERE tournament_id = :t", values={"t": tournament_id}
            )
            await database.execute(
                query="DELETE FROM courts WHERE tournament_id = :t", values={"t": tournament_id}
            )
            await database.execute(
                query="DELETE FROM stage_items WHERE stage_id IN "
                "(SELECT id FROM stages WHERE tournament_id = :t)",
                values={"t": tournament_id},
            )
            await database.execute(
                query="DELETE FROM rankings WHERE tournament_id = :t", values={"t": tournament_id}
            )
            await database.execute(
                query="DELETE FROM stages WHERE tournament_id = :t", values={"t": tournament_id}
            )
            await database.execute(
                query="DELETE FROM tournaments WHERE id = :t", values={"t": tournament_id}
            )

        stack.push_async_callback(_teardown)

        stage_id = int(
            await database.fetch_val(
                query=_INSERT_PUBLIC_STAGE,
                values={
                    "tournament_id": tournament_id,
                    "name": f"{dataset.name_prefix}-public-stage",
                    "created": CREATED,
                },
            )
        )
        ranking_id = int(
            await database.fetch_val(
                query=_INSERT_PUBLIC_RANKING,
                values={"created": CREATED, "tournament_id": tournament_id},
            )
        )
        stage_item_id = int(
            await database.fetch_val(
                query=_INSERT_PUBLIC_STAGE_ITEM,
                values={
                    "name": f"{dataset.name_prefix}-public-category",
                    "stage_id": stage_id,
                    "ranking_id": ranking_id,
                },
            )
        )
        court_id = int(
            await database.fetch_val(
                query=_INSERT_PUBLIC_COURT,
                values={"name": f"{dataset.name_prefix}-public-court", "created": CREATED, "t": tournament_id},
            )
        )

        inputs: list[int] = []
        for slot, key in enumerate(fighter_keys):
            team_id = int(
                await database.fetch_val(
                    query=_INSERT_PUBLIC_TEAM,
                    values={"name": dataset.name(key), "created": CREATED, "t": tournament_id},
                )
            )
            inputs.append(
                int(
                    await database.fetch_val(
                        query=_INSERT_PUBLIC_INPUT,
                        values={
                            "slot": slot,
                            "t": tournament_id,
                            "stage_item_id": stage_item_id,
                            "team_id": team_id,
                        },
                    )
                )
            )

        round_id = int(
            await database.fetch_val(
                query=_INSERT_PUBLIC_ROUND,
                values={
                    "name": "Ronda 1",
                    "created": CREATED,
                    "stage_item_id": stage_item_id,
                },
            )
        )
        await database.execute(
            query=_INSERT_PUBLIC_MATCH,
            values={
                "created": CREATED,
                "start_time": START_TIME,
                "round_id": round_id,
                "input1": inputs[0],
                "input2": inputs[1],
                "court_id": court_id,
            },
        )

        yield tournament_id


#: Reexportados para que el modulo de pruebas los use sin conocer el arnes por dentro.
__all__ = [
    "EVIDENCE_D",
    "EVIDENCE_D_PATH",
    "PRIVACY_MATRIX",
    "audit_events",
    "count_affiliations",
    "count_audit_events",
    "count_idempotency_keys",
    "count_registrations",
    "idempotency_rows_for_key",
    "public_schedule_tournament",
    "public_schedule_url",
    "record_d",
    "sports_club_row",
    "write_evidence_d",
]
