# pylint: disable=redefined-outer-name
"""Soporte del E2E realista de S4.8a-5 (TROZO B §8-§9): inscripciones y clasificacion por HTTP.

Reutiliza **tal cual** el escenario del TROZO A (``e2e_dataset_context``) y anade solo lo que §8-§9
necesitan: la capa de inscripciones (alta F3 real con ``declared_weight_kg``, ``modality``,
``representation``, afiliacion historica y ``category_key`` sin resolver) y la **inspeccion de la
clasificacion** contra el endpoint HTTP real ``GET .../registrations/{r}/classification``.

No contiene pruebas (el nombre no termina en ``_test``).

Superficie de clasificacion
---------------------------
El escenario del trozo A trae cinco categorias estructuradas con reglas reales **mas una categoria
legacy sin ninguna regla**. La categoria legacy es, por definicion del motor, compatible con
**cualquier** inscripcion (aviso ``CATEGORY_RULES_PARTIAL``), de modo que en el torneo A toda
inscripcion tiene **al menos una** compatible y los casos deterministas de §9 ("exactamente una" /
"ninguna" compatible) no pueden observarse. Por eso el trozo B construye una **superficie de
clasificacion limpia**: un torneo hermano del mismo tenant con las **mismas cinco categorias
estructuradas** (mismos rangos de sexo/edad/peso/modalidad/cinturon) y **sin** el cuadro legacy.

* Los casos deterministas (a, c, d, e, f, g, h) clasifican contra esa superficie limpia.
* El caso "multiples compatibles" (b) clasifica tambien contra esa superficie limpia: el titular
  ``b3`` cae a la vez en ``cat-male-gi-heavy`` y en ``cat-male-gi-master``, dos categorias
  **estructuradas** con reglas reales (el solape solo existe para el).
* La compatibilidad con la categoria legacy **sin reglas** (§20) se mide aparte, contra el torneo
del
  escenario A, y no sostiene el caso obligatorio (b).
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

from bracket.database import database
from bracket.logic.classification.vocabulary import ClassificationBlocker, ClassificationWarning
from bracket.utils.id_types import RankingId, StageId, TournamentId
from tests.integration_tests.api.s48a5_e2e_trozo_a_support import (
    CATEGORY_SPECS,
    CREATED,
    START_TIME,
    E2EDataset,
    call,
    e2e_dataset_context,
    fresh_key,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url

#: Resumen congelable de §25 para este trozo.
EVIDENCE_B_PATH: Final = Path("/home/ubuntu/s48a5-evidence/trozo-b.json")


def blocker(code: ClassificationBlocker) -> str:
    """Codigo literal de un blocker del vocabulario cerrado del motor (nunca uno inventado)."""
    return str(code.value)


def warning(code: ClassificationWarning) -> str:
    """Codigo literal de un aviso del vocabulario cerrado del motor."""
    return str(code.value)


#: Orden canonico del vocabulario de blockers (indice de declaracion): se usa para comparar listas.
BLOCKER_ORDER: Final[dict[str, int]] = {
    str(code.value): i for i, code in enumerate(ClassificationBlocker)
}
#: Orden canonico del vocabulario de avisos.
WARNING_ORDER: Final[dict[str, int]] = {
    str(code.value): i for i, code in enumerate(ClassificationWarning)
}


# ---------------------------------------------------------------- modelo de la superficie


@dataclass(frozen=True)
class ClassificationSurface:
    """Torneo contra el que se clasifica y el id de cada categoria por su nombre del escenario."""

    tournament: TournamentId
    stage: StageId
    ranking: RankingId
    categories: dict[str, int]

    def key(self, name: str) -> str:
        """``category_key`` real que el motor publica para esa categoria
        (``si:<stage_item_id>``)."""
        return f"si:{self.categories[name]}"


@dataclass
class ClassificationScenario:
    """Escenario del trozo A + la superficie de clasificacion del trozo B."""

    dataset: E2EDataset
    surface: ClassificationSurface


# ---------------------------------------------------------------- SQL de la superficie limpia

_INSERT_SURFACE_TOURNAMENT = """
    INSERT INTO tournaments (
        name, created, start_time, club_id, dashboard_public, dashboard_endpoint,
        players_can_be_in_multiple_teams, auto_assign_courts, duration_minutes, margin_minutes
    ) VALUES (
        :name, :created, :start_time, :club_id, true, NULL, false, false, 10, 5
    ) RETURNING id
"""
_INSERT_SURFACE_STAGE = """
    INSERT INTO stages (tournament_id, name, created, is_active)
    VALUES (:tournament_id, :name, :created, true) RETURNING id
"""
_INSERT_SURFACE_RANKING = """
    INSERT INTO rankings (
        created, tournament_id, position, win_points, draw_points, loss_points, add_score_points
    ) VALUES (:created, :tournament_id, 1, 3, 1, 0, false) RETURNING id
"""
_INSERT_SURFACE_STAGE_ITEM = """
    INSERT INTO stage_items (
        name, stage_id, team_count, ranking_id, type, sex_constraint, age_min, age_max,
        weight_min_kg, weight_max_kg, modality, belt_min_id, belt_max_id
    ) VALUES (
        :name, :stage_id, 2, :ranking_id, CAST('ROUND_ROBIN' AS stage_type),
        CAST(:sex_constraint AS sex_constraint), :age_min, :age_max,
        :weight_min_kg, :weight_max_kg, CAST(:modality AS modality), :belt_min_id, :belt_max_id
    ) RETURNING id
"""
_DELETE_SURFACE_REGISTRATIONS = "DELETE FROM tournament_registrations WHERE tournament_id = :t"
_DELETE_SURFACE_STAGE_ITEMS = "DELETE FROM stage_items WHERE id = ANY(:ids)"
_DELETE_SURFACE_RANKING = "DELETE FROM rankings WHERE id = :id"
_DELETE_SURFACE_STAGE = "DELETE FROM stages WHERE id = :id"
_DELETE_SURFACE_TOURNAMENT = "DELETE FROM tournaments WHERE id = :id"


async def _insert_surface_category(
    *, stage: int, ranking: int, belts: dict[str, int], spec: dict[str, Any]
) -> int:
    """Siembra, en la superficie limpia, una categoria con las mismas reglas reales del escenario
    A."""
    belt_min = None if spec["belt_min"] is None else belts[spec["belt_min"]]
    belt_max = None if spec["belt_max"] is None else belts[spec["belt_max"]]
    stage_item_id = await database.fetch_val(
        query=_INSERT_SURFACE_STAGE_ITEM,
        values={
            "name": spec["name"],
            "stage_id": stage,
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


# ---------------------------------------------------------------- superficie de clasificacion


@asynccontextmanager
async def classification_surface_context(
    dataset: E2EDataset,
) -> AsyncIterator[ClassificationSurface]:
    """Superficie de clasificacion limpia: torneo hermano con las cinco categorias estructuradas.

    Es un torneo propio del mismo tenant, sin cuadro legacy, para que las categorias estructuradas
    produzcan resultados deterministas (salvo el solape deliberado heavy/master del caso (b)). Se
    deshace de hojas a raices: inscripciones -> stage_items -> ranking -> stage -> torneo.
    """
    async with AsyncExitStack() as stack:
        suffix = uuid4().hex[:8]
        tournament_id = int(
            await database.fetch_val(
                query=_INSERT_SURFACE_TOURNAMENT,
                values={
                    "name": f"{dataset.name_prefix}-classification-{suffix}",
                    "created": CREATED,
                    "start_time": START_TIME,
                    "club_id": dataset.club,
                },
            )
        )

        created: dict[str, Any] = {"stage": None, "ranking": None, "items": []}

        async def _teardown() -> None:
            await database.execute(query=_DELETE_SURFACE_REGISTRATIONS, values={"t": tournament_id})
            if created["items"]:
                await database.execute(
                    query=_DELETE_SURFACE_STAGE_ITEMS, values={"ids": list(created["items"])}
                )
            if created["ranking"] is not None:
                await database.execute(
                    query=_DELETE_SURFACE_RANKING, values={"id": created["ranking"]}
                )
            if created["stage"] is not None:
                await database.execute(query=_DELETE_SURFACE_STAGE, values={"id": created["stage"]})
            await database.execute(query=_DELETE_SURFACE_TOURNAMENT, values={"id": tournament_id})

        stack.push_async_callback(_teardown)

        created["stage"] = int(
            await database.fetch_val(
                query=_INSERT_SURFACE_STAGE,
                values={
                    "tournament_id": tournament_id,
                    "name": f"{dataset.name_prefix}-classification-stage",
                    "created": CREATED,
                },
            )
        )
        created["ranking"] = int(
            await database.fetch_val(
                query=_INSERT_SURFACE_RANKING,
                values={"tournament_id": tournament_id, "created": CREATED},
            )
        )

        categories: dict[str, int] = {}
        for spec in CATEGORY_SPECS:
            item_id = await _insert_surface_category(
                stage=created["stage"], ranking=created["ranking"], belts=dataset.belts, spec=spec
            )
            categories[str(spec["name"])] = item_id
            created["items"].append(item_id)

        yield ClassificationSurface(
            tournament=TournamentId(tournament_id),
            stage=StageId(created["stage"]),
            ranking=RankingId(created["ranking"]),
            categories=categories,
        )


@asynccontextmanager
async def trozo_b_scenario() -> AsyncIterator[ClassificationScenario]:
    """Escenario A + superficie de clasificacion, limpiando la superficie antes de cerrar el A."""
    async with e2e_dataset_context() as dataset:
        async with classification_surface_context(dataset) as surface:
            yield ClassificationScenario(dataset=dataset, surface=surface)


# ---------------------------------------------------------------- HTTP helpers


def registrations_url(tournament_id: int) -> str:
    """Coleccion de inscripciones del torneo (alta F3, §8)."""
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/registrations"


def registration_url(tournament_id: int, registration_id: int) -> str:
    return f"{registrations_url(tournament_id)}/{registration_id}"


def classification_url(tournament_id: int, registration_id: int) -> str:
    """Lectura de clasificacion (§9): solo lectura, no exige ``Idempotency-Key``."""
    return f"{registration_url(tournament_id, registration_id)}/classification"


async def create_registration_http(
    *,
    tournament_id: int,
    token: str,
    competitor_id: int,
    representation: str,
    sports_club_id: int | None = None,
    affiliation_id: int | None = None,
    declared_weight_kg: Decimal | None = None,
    modality: str | None = None,
    category_key: str | None = None,
    category_label: str | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    """Alta de una inscripcion en borrador por el camino HTTP real (puerta F3 + idempotencia)."""
    payload: dict[str, Any] = {
        "competitor_id": competitor_id,
        "representation": representation,
        "sports_club_id": sports_club_id,
        "affiliation_id": affiliation_id,
        "declared_weight_kg": None if declared_weight_kg is None else str(declared_weight_kg),
        "modality": modality,
        "category_key": category_key,
        "category_label": category_label,
        "reason_code": "PLANNED_ENTRY",
    }
    status_code, body = await call(
        "POST",
        registrations_url(tournament_id),
        token=token,
        key=key or fresh_key(),
        payload=payload,
    )
    assert status_code == 201, body
    return cast("dict[str, Any]", body)


async def classify_http(*, tournament_id: int, registration_id: int, token: str) -> dict[str, Any]:
    """Lee la clasificacion de una inscripcion (GET real, sin cuerpo ni clave de idempotencia)."""
    status_code, body = await call(
        "GET", classification_url(tournament_id, registration_id), token=token
    )
    assert status_code == 200, body
    return cast("dict[str, Any]", body)


def representation_of(dataset: E2EDataset, competitor_key: str) -> dict[str, Any]:
    """Representacion real del competidor: ``CLUB`` con su academia y afiliacion, o ``INDEPENDENT``.

    Reutiliza la **afiliacion historica** dada de alta por el trozo A (``dataset.affiliations``).
    """
    academy = dataset.academy_of[competitor_key]
    if academy is None:
        return {"representation": "INDEPENDENT", "sports_club_id": None, "affiliation_id": None}
    return {
        "representation": "CLUB",
        "sports_club_id": int(dataset.clubs[academy]),
        "affiliation_id": int(dataset.affiliations[competitor_key]),
    }


# ---------------------------------------------------------------- DB helpers (fixture)


_SELECT_IDEMPOTENCY_COUNT = (
    "SELECT count(*) FROM domain_idempotency_keys WHERE tenant_club_id = :club"
)
_SELECT_AUDIT_COUNT = "SELECT count(*) FROM domain_change_log WHERE tenant_club_id = :club"
_SET_REGISTRATION_CATEGORY = """
    UPDATE tournament_registrations
    SET category_key = :category_key, category_label = :category_label, updated_at = NOW()
    WHERE id = :id
"""


async def count_idempotency_keys(tenant_club_id: int) -> int:
    """Claves de idempotencia reservadas por el tenant (para probar que leer no reserva)."""
    return int(
        await database.fetch_val(query=_SELECT_IDEMPOTENCY_COUNT, values={"club": tenant_club_id})
    )


async def count_audit_rows(tenant_club_id: int) -> int:
    """Eventos de auditoria del tenant (para probar que leer no audita)."""
    return int(await database.fetch_val(query=_SELECT_AUDIT_COUNT, values={"club": tenant_club_id}))


async def set_registration_category_sql(
    registration_id: int, category_key: str, label: str
) -> None:
    """Fija la categoria asignada de una inscripcion (estado de partida del caso (h)).

    Se hace por SQL a proposito: la asignacion por API (§7) es otra unidad; aqui solo se prepara el
    estado "categoria asignada incompatible" que §8 exige para ese caso.
    """
    await database.execute(
        query=_SET_REGISTRATION_CATEGORY,
        values={"id": registration_id, "category_key": category_key, "category_label": label},
    )


# ---------------------------------------------------------------- §25 evidence

#: Validaciones acumuladas por el modulo de pruebas (se vuelcan al cerrar).
EVIDENCE_B: dict[str, Any] = {"trozo": "B", "cases": [], "validations": []}


def record_b(name: str, passed: bool, detail: dict[str, Any] | None = None) -> None:
    """Anota el resultado de una validacion del trozo B."""
    EVIDENCE_B["validations"].append({"name": name, "passed": bool(passed), "detail": detail or {}})


def record_case(
    *,
    case: str,
    label: str,
    competitor_key: str,
    registration_id: int,
    tournament_id: int,
    declared_weight_kg: str | None,
    modality: str | None,
    representation: str,
    classification: dict[str, Any],
) -> None:
    """Congela la inscripcion y el resultado **literal** de su clasificacion."""
    EVIDENCE_B["cases"].append(
        {
            "case": case,
            "label": label,
            "competitor_key": competitor_key,
            "registration_id": registration_id,
            "tournament_id": tournament_id,
            "declared_weight_kg": declared_weight_kg,
            "modality": modality,
            "representation": representation,
            "classification": classification,
        }
    )


def write_evidence_b() -> Path:
    """Vuelca el resumen de §25 (inscripciones + clasificaciones) en ``EVIDENCE_B_PATH``."""
    EVIDENCE_B_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_B_PATH.write_text(
        json.dumps(EVIDENCE_B, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )
    return EVIDENCE_B_PATH
