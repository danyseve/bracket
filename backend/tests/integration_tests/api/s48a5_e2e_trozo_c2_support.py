# pylint: disable=redefined-outer-name
"""Soporte del E2E realista de S4.8a-5 (TROZO C2, §13-§15): historico de club, readiness y
generacion del cuadro de eliminacion directa, siempre por el API HTTP real.

Que monta sobre el escenario del TROZO A (``e2e_dataset_context``):

* **§13 HISTORICO DE CLUB**: reutiliza los competidores afiliados y la infraestructura de
  afiliaciones del escenario A; la inscripcion que se crea y cambia de club viaja por las rutas
  reales de inscripcion y afiliacion;
* **dos superficies de clasificacion limpias** (``classification_surface_context`` del TROZO B) para
  demostrar que representar un club o competir por libre **no** cambia la clasificacion deportiva:
  el mismo competidor se inscribe como ``CLUB`` en una y como ``INDEPENDENT`` en la otra, con los
  mismos datos declarados y el mismo perfil;
* un **torneo hermano** cuyas dos categorias son cuadros reales de eliminacion directa
  (``stage_items`` con ``type=SINGLE_ELIMINATION`` y las reglas reales de las categorias del
  escenario A) con los equipos de sus entrantes ya colocados en las ranuras. Es el objeto de §14
  (readiness antes/despues de resolver) y §15 (generacion del cuadro con su reparto real).

No hay mocks del motor, del protocolo ni del RBAC: se ejercitan las rutas reales y se lee lo que el
backend escribe (``tournament_registrations``, ``competitors_x_sports_clubs``,
``stage_item_inputs`` y ``domain_change_log``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast
from uuid import uuid4

from bracket.config import config  # noqa: F401  (reexportado: el modulo de pruebas lo usa)
from bracket.database import database
from bracket.logic.scheduling.generation import MINIMUM_ENTRANTS
from bracket.utils.id_types import RankingId, StageId, TournamentId
from tests.integration_tests.api.s48a5_e2e_paths import evidence_path
from tests.integration_tests.api.s48a5_e2e_trozo_a_support import (
    CATEGORY_SPECS,
    CREATED,
    START_TIME,
    E2EDataset,
    call,
    e2e_dataset_context,
    fresh_key,
)
from tests.integration_tests.api.s48a5_e2e_trozo_b_support import (
    ClassificationSurface,
    classification_surface_context,
    classify_http,
    create_registration_http,
    representation_of,
)
from tests.integration_tests.api.s48a5_e2e_trozo_c1_support import (
    assign_category_http,
    category_group,
    confirm_registration_http,
    get_registration_http,
    issue_codes,
    readiness_http,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url

#: Resumen congelable de §25 para este trozo.
EVIDENCE_C2_PATH: Final = evidence_path("trozo-c2.json")

#: Competidor del caso historico de §13 (afiliado a la academia A) y academia destino del cambio.
HISTORICAL_COMPETITOR: Final = "a2"
HISTORICAL_TARGET_ACADEMY: Final = "B"

#: Las dos categorias del torneo de §14/§15, tomadas con sus reglas reales del escenario A.
CATEGORY_LIGHT: Final = "cat-male-gi-light"
CATEGORY_HEAVY: Final = "cat-male-gi-heavy"
C2_CATEGORY_NAMES: Final[tuple[str, ...]] = (CATEGORY_LIGHT, CATEGORY_HEAVY)

#: Ranuras de cada cuadro del torneo de §14/§15. Con dos entrantes deja dos despedidas repartibles.
BRACKET_TEAM_COUNT: Final = 4

#: Entrantes por categoria (clave de competidor del escenario A). ``LIGHT`` es club+club,
#: ``HEAVY`` mezcla un competidor de club y un **independiente**.
BRACKET_ENTRANTS: Final[dict[str, tuple[str, ...]]] = {
    CATEGORY_LIGHT: ("a1", "b2"),
    CATEGORY_HEAVY: ("a6", "i3"),
}

#: Datos declarados con los que se inscriben los entrantes: son los que el motor clasifica.
DECLARED_LIGHT: Final = (Decimal("74.50"), "GI")
DECLARED_HEAVY: Final = (Decimal("100.00"), "GI")

# ---------------------------------------------------------------- escenario


@dataclass(frozen=True)
class BracketScenario:
    """Torneo hermano con dos categorias-cuadro y los equipos de sus entrantes ya en las ranuras."""

    tournament: TournamentId
    stage: StageId
    ranking: RankingId
    categories: dict[str, int]
    teams: dict[str, int]
    entrants: dict[str, tuple[str, ...]]
    team_count: int = BRACKET_TEAM_COUNT

    def key(self, name: str) -> str:
        """``category_key`` real que el motor publica para esa categoria
        (``si:<stage_item_id>``)."""
        return f"si:{self.categories[name]}"


@dataclass
class C2Scenario:
    """Escenario A + dos superficies de clasificacion + el torneo de cuadros de §14/§15."""

    dataset: E2EDataset
    club_surface: ClassificationSurface
    independent_surface: ClassificationSurface
    bracket: BracketScenario


@asynccontextmanager
async def trozo_c2_scenario() -> AsyncIterator[C2Scenario]:
    """Escenario A + ``club`` + ``independent`` + torneo de cuadros (el A se limpia el ultimo)."""
    async with e2e_dataset_context() as dataset:
        async with classification_surface_context(dataset) as club_surface:
            async with classification_surface_context(dataset) as independent_surface:
                async with _bracket_scenario_context(dataset) as bracket:
                    yield C2Scenario(
                        dataset=dataset,
                        club_surface=club_surface,
                        independent_surface=independent_surface,
                        bracket=bracket,
                    )


def key_name_map(surface: ClassificationSurface) -> dict[str, str]:
    """``category_key`` -> nombre de categoria del escenario, para comparar entre torneos."""
    return {surface.key(name): name for name in surface.categories}


# ---------------------------------------------------------------- URLs de las rutas reales


def generate_url(tournament_id: int) -> str:
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/generate-bracket"


def close_url(tournament_id: int) -> str:
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/registrations/close"


def window_url(tournament_id: int) -> str:
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/registration-window"


# ---------------------------------------------------------------- HTTP helpers


async def generate_http(
    *, tournament_id: int, token: str, key: str | None = None
) -> tuple[int, Any]:
    """Generacion del cuadro por la ruta real (§15): cuerpo vacio a proposito, clave obligatoria."""
    return await call("POST", generate_url(tournament_id), token=token, key=key or fresh_key())


async def close_window_http(
    *, tournament_id: int, token: str, key: str | None = None
) -> tuple[int, Any]:
    """Cierre manual de la ventana de inscripcion (§15): deja de admitir altas nuevas."""
    return await call("POST", close_url(tournament_id), token=token, key=key or fresh_key())


async def window_http(*, tournament_id: int, token: str) -> dict[str, Any]:
    """Lectura de la ventana de inscripcion (200 o falla la prueba)."""
    status_code, body = await call("GET", window_url(tournament_id), token=token)
    assert status_code == 200, body
    return cast("dict[str, Any]", body)


def generate_blocking_issues(status_code: int, body: Any) -> list[str]:
    """Codigos literales del rechazo de generacion ``409 BRACKET_NOT_READY`` (o lista vacia)."""
    if status_code != 409:
        return []
    detail = body.get("detail") if isinstance(body, dict) else None
    issues = detail.get("blocking_issues") if isinstance(detail, dict) else None
    if not isinstance(issues, list):
        return []
    return [str(issue["code"]) for issue in issues]


def generate_not_ready_code(body: Any) -> str | None:
    """Codigo de error del rechazo de generacion (``BRACKET_NOT_READY``), o ``None``."""
    detail = body.get("detail") if isinstance(body, dict) else None
    if not isinstance(detail, dict):
        return None
    code = detail.get("code")
    return None if code is None else str(code)


# ---------------------------------------------------------------- DB helpers (lecturas)


_SELECT_SPORTS_CLUB_NAME = "SELECT name FROM sports_clubs WHERE id = :id"
_SELECT_TEAM_NAME = "SELECT name FROM teams WHERE id = :id"
_SELECT_INPUT_OCCUPANTS = """
    SELECT slot, team_id FROM stage_item_inputs
    WHERE stage_item_id = :stage_item_id ORDER BY slot
"""
_COUNT_GENERATE_AUDIT = """
    SELECT count(*) FROM domain_change_log
    WHERE entity = 'tournament' AND entity_id = :tid AND action = 'GENERATE_BRACKET'
"""
_SELECT_COMPETITOR_CLUB = """
    SELECT display_name, managed_by_club_id FROM competitors WHERE id = :id
"""
_SELECT_CURRENT_REGISTRATION = """
    SELECT id, competitor_id, representation, sports_club_id, affiliation_id,
           competitor_name_snapshot, sports_club_name_snapshot,
           category_key, category_label, status
    FROM tournament_registrations
    WHERE tournament_id = :tid AND competitor_id = :cid
      AND superseded_by_registration_id IS NULL
"""


async def sports_club_name(sports_club_id: int) -> str:
    """Nombre real de la academia (para asertar el snapshot historico exacto)."""
    value = await database.fetch_val(query=_SELECT_SPORTS_CLUB_NAME, values={"id": sports_club_id})
    assert value is not None
    return str(value)


async def team_name(team_id: int) -> str:
    """Nombre real del equipo de un entrante del cuadro (lleva la clave del competidor)."""
    value = await database.fetch_val(query=_SELECT_TEAM_NAME, values={"id": team_id})
    assert value is not None
    return str(value)


async def bracket_layout(stage_item_id: int) -> list[dict[str, Any]]:
    """Ocupantes de las ranuras de un cuadro, en orden de ``slot`` (``team_id`` nulo = vacia)."""
    records = await database.fetch_all(
        query=_SELECT_INPUT_OCCUPANTS, values={"stage_item_id": stage_item_id}
    )
    return [
        {"slot": int(record._mapping["slot"]), "team_id": record._mapping["team_id"]}
        for record in records
    ]


async def occupied_team_ids(stage_item_id: int) -> list[int]:
    """Ids de equipo que ocupan las ranuras del cuadro, en orden de ``slot``."""
    layout = await bracket_layout(stage_item_id)
    return [int(row["team_id"]) for row in layout if row["team_id"] is not None]


async def count_generate_audit(tournament_id: int) -> int:
    """Eventos de auditoria ``GENERATE_BRACKET`` del torneo (para probar el congelado)."""
    return int(await database.fetch_val(query=_COUNT_GENERATE_AUDIT, values={"tid": tournament_id}))


async def current_registration(tournament_id: int, competitor_id: int) -> dict[str, Any]:
    """Inscripcion vigente (no sustituida) de un competidor en un torneo, o falla la prueba."""
    record = await database.fetch_one(
        query=_SELECT_CURRENT_REGISTRATION,
        values={"tid": tournament_id, "cid": competitor_id},
    )
    assert record is not None
    return dict(record._mapping)


async def competitor_row(competitor_id: int) -> dict[str, Any]:
    """Fila minima del competidor: nombre y club gestor (``None`` = independiente)."""
    record = await database.fetch_one(query=_SELECT_COMPETITOR_CLUB, values={"id": competitor_id})
    assert record is not None
    return {
        "display_name": str(record._mapping["display_name"]),
        "managed_by_club_id": record._mapping["managed_by_club_id"],
    }


# ---------------------------------------------------------------- constructor del torneo de cuadros


_INSERT_BRACKET_TOURNAMENT = """
    INSERT INTO tournaments (
        name, created, start_time, club_id, dashboard_public, dashboard_endpoint,
        players_can_be_in_multiple_teams, auto_assign_courts, duration_minutes, margin_minutes
    ) VALUES (
        :name, :created, :start_time, :club_id, true, NULL, false, false, 10, 5
    ) RETURNING id
"""
_INSERT_BRACKET_STAGE = """
    INSERT INTO stages (tournament_id, name, created, is_active)
    VALUES (:tournament_id, :name, :created, true) RETURNING id
"""
_INSERT_BRACKET_RANKING = """
    INSERT INTO rankings (
        created, tournament_id, position, win_points, draw_points, loss_points, add_score_points
    ) VALUES (:created, :tournament_id, 1, 3, 1, 0, false) RETURNING id
"""
_INSERT_BRACKET_CATEGORY = """
    INSERT INTO stage_items (
        name, stage_id, team_count, ranking_id, type, sex_constraint, age_min, age_max,
        weight_min_kg, weight_max_kg, modality, belt_min_id, belt_max_id
    ) VALUES (
        :name, :stage_id, :team_count, :ranking_id, CAST('SINGLE_ELIMINATION' AS stage_type),
        CAST(:sex_constraint AS sex_constraint), :age_min, :age_max,
        :weight_min_kg, :weight_max_kg, CAST(:modality AS modality), :belt_min_id, :belt_max_id
    ) RETURNING id
"""
_INSERT_BRACKET_TEAM = """
    INSERT INTO teams (name, created, tournament_id, active)
    VALUES (:name, :created, :tournament_id, true) RETURNING id
"""
_INSERT_BRACKET_INPUT = """
    INSERT INTO stage_item_inputs (slot, tournament_id, stage_item_id, team_id)
    VALUES (:slot, :tournament_id, :stage_item_id, :team_id)
"""
_DELETE_BRACKET_REGISTRATIONS = "DELETE FROM tournament_registrations WHERE tournament_id = :t"
_DELETE_BRACKET_INPUTS = "DELETE FROM stage_item_inputs WHERE tournament_id = :t"
_DELETE_BRACKET_TEAMS = "DELETE FROM teams WHERE tournament_id = :t"
_DELETE_BRACKET_STAGE_ITEMS = "DELETE FROM stage_items WHERE id = ANY(:ids)"
_DELETE_BRACKET_RANKING = "DELETE FROM rankings WHERE id = :id"
_DELETE_BRACKET_STAGE = "DELETE FROM stages WHERE id = :id"
_DELETE_BRACKET_TOURNAMENT = "DELETE FROM tournaments WHERE id = :id"

#: Reglas reales por nombre de categoria (tomadas del escenario A, sin redefinirlas).
_CATEGORY_BY_NAME: Final[dict[str, dict[str, Any]]] = {
    spec["name"]: spec for spec in CATEGORY_SPECS
}


async def _insert_bracket_category(
    *, stage: int, ranking: int, team_count: int, belts: dict[str, int], name: str
) -> int:
    """Siembra una categoria-cuadro de eliminacion directa con las reglas reales del escenario A."""
    spec = _CATEGORY_BY_NAME[name]
    belt_min = None if spec["belt_min"] is None else belts[spec["belt_min"]]
    belt_max = None if spec["belt_max"] is None else belts[spec["belt_max"]]
    stage_item_id = await database.fetch_val(
        query=_INSERT_BRACKET_CATEGORY,
        values={
            "name": name,
            "stage_id": stage,
            "team_count": team_count,
            "ranking_id": ranking,
            "sex_constraint": spec["sex_constraint"],
            "age_min": spec["age_min"],
            "age_max": spec["age_max"],
            "weight_min_kg": spec["weight_min_kg"],
            "weight_max_kg": spec["weight_max_kg"],
            "modality": spec["modality"],
            "belt_min_id": belt_min,
            "belt_max_id": belt_max,
        },
    )
    return int(stage_item_id)


@asynccontextmanager
async def _bracket_scenario_context(dataset: E2EDataset) -> AsyncIterator[BracketScenario]:
    """Torneo hermano con dos categorias-cuadro y los equipos de sus entrantes en las ranuras.

    Se deshace de hojas a raices: inscripciones -> ranuras -> equipos -> stage_items -> ranking ->
    stage -> torneo. El torneo es del mismo tenant que el escenario A y comparte su ``start_time``,
    de modo que la edad de clasificacion se calcula contra la misma fecha.
    """
    async with AsyncExitStack() as stack:
        suffix = uuid4().hex[:8]
        tournament_id = int(
            await database.fetch_val(
                query=_INSERT_BRACKET_TOURNAMENT,
                values={
                    "name": f"{dataset.name_prefix}-bracket-{suffix}",
                    "created": CREATED,
                    "start_time": START_TIME,
                    "club_id": dataset.club,
                },
            )
        )

        created: dict[str, Any] = {"items": []}

        async def _teardown() -> None:
            await database.execute(query=_DELETE_BRACKET_REGISTRATIONS, values={"t": tournament_id})
            await database.execute(query=_DELETE_BRACKET_INPUTS, values={"t": tournament_id})
            await database.execute(query=_DELETE_BRACKET_TEAMS, values={"t": tournament_id})
            await database.execute(
                query=_DELETE_BRACKET_STAGE_ITEMS, values={"ids": created["items"]}
            )
            await database.execute(query=_DELETE_BRACKET_RANKING, values={"id": created["ranking"]})
            await database.execute(query=_DELETE_BRACKET_STAGE, values={"id": created["stage"]})
            await database.execute(query=_DELETE_BRACKET_TOURNAMENT, values={"id": tournament_id})

        stack.push_async_callback(_teardown)

        stage_id = int(
            await database.fetch_val(
                query=_INSERT_BRACKET_STAGE,
                values={
                    "tournament_id": tournament_id,
                    "name": f"{dataset.name_prefix}-bracket-stage",
                    "created": CREATED,
                },
            )
        )
        created["stage"] = stage_id
        ranking_id = int(
            await database.fetch_val(
                query=_INSERT_BRACKET_RANKING,
                values={"tournament_id": tournament_id, "created": CREATED},
            )
        )
        created["ranking"] = ranking_id

        categories: dict[str, int] = {}
        for name in C2_CATEGORY_NAMES:
            stage_item_id = await _insert_bracket_category(
                stage=stage_id,
                ranking=ranking_id,
                team_count=BRACKET_TEAM_COUNT,
                belts=dataset.belts,
                name=name,
            )
            categories[name] = stage_item_id
            created["items"].append(stage_item_id)

        # Un equipo por entrante (nombre con la clave del competidor) y su ranura. Se dejan los
        # entrantes **juntos al principio**: la generacion de §15 tiene asi algo real que repartir.
        teams: dict[str, int] = {}
        for name, keys in BRACKET_ENTRANTS.items():
            stage_item_id = categories[name]
            for slot in range(BRACKET_TEAM_COUNT):
                team_id: int | None = None
                if slot < len(keys):
                    team_id = int(
                        await database.fetch_val(
                            query=_INSERT_BRACKET_TEAM,
                            values={
                                "name": f"{dataset.name_prefix}-team-{keys[slot]}",
                                "created": CREATED,
                                "tournament_id": tournament_id,
                            },
                        )
                    )
                    teams[keys[slot]] = team_id
                await database.execute(
                    query=_INSERT_BRACKET_INPUT,
                    values={
                        "slot": slot,
                        "tournament_id": tournament_id,
                        "stage_item_id": stage_item_id,
                        "team_id": team_id,
                    },
                )

        yield BracketScenario(
            tournament=TournamentId(tournament_id),
            stage=StageId(stage_id),
            ranking=RankingId(ranking_id),
            categories=categories,
            teams=teams,
            entrants=BRACKET_ENTRANTS,
        )


# ---------------------------------------------------------------- §25 evidencia


#: Validaciones y evidencias acumuladas por el modulo de pruebas (se vuelcan al cerrar).
EVIDENCE_C2: dict[str, Any] = {
    "trozo": "C2",
    "historical": {"before": None, "after": None},
    "readiness": {"before": None, "after": None},
    "generation": {"groups": [], "frozen": None},
    "validations": [],
}


def record_c2(name: str, passed: bool, detail: dict[str, Any] | None = None) -> None:
    """Anota el resultado de una validacion del trozo C2."""
    EVIDENCE_C2["validations"].append(
        {"name": name, "passed": bool(passed), "detail": detail or {}}
    )


def record_historical(*, before: dict[str, Any], after: dict[str, Any]) -> None:
    """Congela el antes/despues del caso historico de §13."""
    EVIDENCE_C2["historical"] = {"before": before, "after": after}


def record_readiness(phase: str, readiness: dict[str, Any]) -> None:
    """Congela la readiness de una fase (``before``/``after``) de §14."""
    if phase not in ("before", "after"):
        raise ValueError(f"fase desconocida: {phase!r}")
    EVIDENCE_C2["readiness"][phase] = readiness


def record_generation(*, groups: list[dict[str, Any]], frozen: dict[str, Any]) -> None:
    """Congela el reparto por grupo y la prueba de congelado de §15."""
    EVIDENCE_C2["generation"] = {"groups": groups, "frozen": frozen}


def write_evidence_c2() -> Path:
    """Vuelca la evidencia de §25 (historico + readiness + generacion) en ``EVIDENCE_C2_PATH``."""
    EVIDENCE_C2_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_C2_PATH.write_text(
        json.dumps(EVIDENCE_C2, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    return EVIDENCE_C2_PATH


#: Reexportados para que el modulo de pruebas los use sin conocer el arnes por dentro.
__all__ = [
    "BRACKET_ENTRANTS",
    "BRACKET_TEAM_COUNT",
    "C2Scenario",
    "CATEGORY_HEAVY",
    "CATEGORY_LIGHT",
    "C2_CATEGORY_NAMES",
    "DECLARED_HEAVY",
    "DECLARED_LIGHT",
    "EVIDENCE_C2",
    "EVIDENCE_C2_PATH",
    "HISTORICAL_COMPETITOR",
    "HISTORICAL_TARGET_ACADEMY",
    "BracketScenario",
    "MINIMUM_ENTRANTS",
    "assign_category_http",
    "bracket_layout",
    "category_group",
    "classify_http",
    "close_window_http",
    "competitor_row",
    "confirm_registration_http",
    "count_generate_audit",
    "create_registration_http",
    "current_registration",
    "generate_blocking_issues",
    "generate_http",
    "generate_not_ready_code",
    "get_registration_http",
    "issue_codes",
    "key_name_map",
    "occupied_team_ids",
    "readiness_http",
    "record_c2",
    "record_generation",
    "record_historical",
    "record_readiness",
    "representation_of",
    "sports_club_name",
    "team_name",
    "trozo_c2_scenario",
    "window_http",
    "write_evidence_c2",
]
