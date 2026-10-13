# pylint: disable=redefined-outer-name
"""Soporte del E2E realista de S4.8a-5 (TROZO B §8-§9): inscripciones y clasificacion por HTTP.

Reutiliza **tal cual** el escenario del TROZO A (``e2e_dataset_context``) y anade solo lo que §8-§9
necesitan: la capa de inscripciones (alta F3 real con ``declared_weight_kg``, ``modality``,
``representation``, afiliacion historica y ``category_key`` sin resolver) y la **inspeccion de la
clasificacion** contra el endpoint HTTP real ``GET .../registrations/{r}/classification``.

No contiene pruebas (el nombre no termina en ``_test``).

Superficie de clasificacion
---------------------------
El escenario del trozo A **ya** trae cuatro categorias estructuradas con reglas reales **mas una
categoria legacy sin ninguna regla**. La categoria legacy es, por definicion del motor, compatible
con **cualquier** inscripcion (aviso ``CATEGORY_RULES_PARTIAL``). Los casos deterministas de §9
(exactamente una compatible / ninguna compatible) necesitan por tanto una superficie de clasificacion
**sin** el cuadro legacy; el torneo del escenario A se reserva para el caso "multiples compatibles",
que es justamente lo que provoca el cuadro legacy. La superficie se expone aqui bajo
``ClassificationSurface`` para que las pruebas no dependan de como se materializa.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from bracket.database import database
from bracket.logic.classification.vocabulary import ClassificationBlocker, ClassificationWarning
from bracket.utils.id_types import RankingId, StageId, TournamentId
from tests.integration_tests.api.s48a5_e2e_trozo_a_support import (
    CATEGORY_SPECS,
    CREATED,
    E2EDataset,
    START_TIME,
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
BLOCKER_ORDER: Final[dict[str, int]] = {str(code.value): i for i, code in enumerate(ClassificationBlocker)}
#: Orden canonico del vocabulario de avisos.
WARNING_ORDER: Final[dict[str, int]] = {str(code.value): i for i, code in enumerate(ClassificationWarning)}


# ---------------------------------------------------------------- modelo de la superficie


@dataclass(frozen=True)
class ClassificationSurface:
    """Torneo contra el que se clasifica y el id de cada categoria por su nombre del escenario."""

    tournament: TournamentId
    stage: StageId
    ranking: RankingId
    categories: dict[str, int]

    def key(self, name: str) -> str:
        """``category_key`` real que el motor publica para esa categoria (``si:<stage_item_id>``)."""
        return f"si:{self.categories[name]}"


@dataclass
class ClassificationScenario:
    """Escenario del trozo A + la superficie de clasificacion del trozo B."""

    dataset: E2EDataset
    surface: ClassificationSurface


# ---------------------------------------------------------------- superficie de clasificacion


@asynccontextmanager
async def classification_surface_context(dataset: E2EDataset) -> AsyncIterator[ClassificationSurface]:
    """Superficie de clasificacion del trozo B.

    RED: reutiliza el torneo del escenario A **tal cual**, con sus cuatro categorias estructuradas y
    la legacy. Sirve para dejar en negro que el cuadro legacy (siempre compatible) rompe los casos
    deterministas de §9: ``exactamente una`` y ``ninguna`` compatible no pueden observarse.
    """
    yield ClassificationSurface(
        tournament=dataset.tournament,
        stage=dataset.stage,
        ranking=dataset.ranking,
        categories={name: int(sid) for name, sid in dataset.categories.items()},
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
        "POST", registrations_url(tournament_id), token=token, key=key or fresh_key(), payload=payload
    )
    assert status_code == 201, body
    return body


async def classify_http(
    *, tournament_id: int, registration_id: int, token: str
) -> dict[str, Any]:
    """Lee la clasificacion de una inscripcion (GET real, sin cuerpo ni clave de idempotencia)."""
    status_code, body = await call(
        "GET", classification_url(tournament_id, registration_id), token=token
    )
    assert status_code == 200, body
    return body


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


async def set_registration_category_sql(registration_id: int, category_key: str, label: str) -> None:
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
    EVIDENCE_B["validations"].append(
        {"name": name, "passed": bool(passed), "detail": detail or {}}
    )


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


#: Superficie usada por los casos deterministas (un solo punto de verdad para el informe).
EXPECTED_STRUCTURED: Final = tuple(spec["name"] for spec in CATEGORY_SPECS)


def surface_names() -> tuple[str, ...]:
    """Nombres de las cuatro categorias estructuradas del escenario A."""
    return EXPECTED_STRUCTURED
