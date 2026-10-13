# pylint: disable=redefined-outer-name
"""Soporte del E2E realista de S4.8a-5 (TROZO C1, §10-§12).

No contiene pruebas (el nombre no termina en ``_test``): pytest no lo recoge. Reutiliza **tal cual**
el escenario del TROZO A (``e2e_dataset_context``) y la **superficie de clasificacion** del TROZO B
(``classification_surface_context``), y anade el minimo imprescindible para el trozo C1:

* dos superficies de clasificacion limpias (mismo tenant, torneos hermanos): ``before_surface`` para
  observar el estado incompleto y ``after_surface`` para observar la correccion, de modo que cada
  correccion de blocker se mida **antes/despues** sin tocar la fila que se clasifica;
* envoltorios HTTP de las rutas reales que ejercita el trozo: perfil de competidor (§10), asignacion
  de categoria (§11/§12), relectura de la inscripcion (§11) y readiness del cuadro (§11/§12);
* lectura de la auditoria de categoria (``domain_change_log``, evento ``CATEGORY_CHANGE``) y de la
  relacion del actor, para validar el override OWNER (§12);
* el volcado de evidencia §25 en ``EVIDENCE_C1_PATH`` (directorio temporal del sistema,
  o el que fije ``S48A5_EVIDENCE_DIR``).

Nada aqui reimplementa el motor de clasificacion, las reglas deportivas ni el RBAC: solo ejerce el
contrato real por HTTP y lee lo que el backend escribe.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final, cast

from bracket.database import database
from tests.integration_tests.api.s48a5_e2e_paths import evidence_path
from tests.integration_tests.api.s48a5_e2e_trozo_a_support import (
    E2EDataset,
    call,
    competitor_url,
    e2e_dataset_context,
    fresh_key,
)
from tests.integration_tests.api.s48a5_e2e_trozo_b_support import (
    ClassificationSurface,
    classification_surface_context,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url

#: Salida de evidencia §25 del trozo C1.
EVIDENCE_C1_PATH: Final = evidence_path("trozo-c1.json")

#: Entidad real de una inscripcion en la auditoria de dominio.
_REGISTRATION_ENTITY: Final = "tournament_registration"


# ---------------------------------------------------------------- escenario (A + dos superficies)


@dataclass(frozen=True)
class C1Scenario:
    """Escenario A con **dos** superficies de clasificacion limpias: antes y despues de corregir."""

    dataset: E2EDataset
    before_surface: ClassificationSurface
    after_surface: ClassificationSurface


@asynccontextmanager
async def trozo_c1_scenario() -> AsyncIterator[C1Scenario]:
    """Escenario A + superficie ``before`` + superficie ``after`` (el A se limpia el ultimo).

    Dos torneos hermanos del mismo tenant: uno mide el estado incompleto de una inscripcion y el
    otro mide el estado corregido. Una inscripcion solo puede tener una fila vigente por torneo
    (``uq_tournament_registrations_current``), asi que la correccion de peso/modalidad se observa
    como un alta nueva en el torneo ``after``; la correccion de cinturon/DOB es del competidor y se
    refleja en cualquier torneo.
    """

    async with e2e_dataset_context() as dataset:
        async with classification_surface_context(dataset) as before_surface:
            async with classification_surface_context(dataset) as after_surface:
                yield C1Scenario(
                    dataset=dataset,
                    before_surface=before_surface,
                    after_surface=after_surface,
                )


def surface_of(scenario: C1Scenario, which: str) -> ClassificationSurface:
    """Superficie por nombre: ``"before"`` o ``"after"``."""
    if which == "before":
        return scenario.before_surface
    if which == "after":
        return scenario.after_surface
    raise ValueError(f"superficie desconocida: {which!r}")


# ---------------------------------------------------------------- URLs de las rutas reales


def registration_url(tournament_id: int, registration_id: int) -> str:
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/registrations/{registration_id}"


def category_url(tournament_id: int, registration_id: int) -> str:
    return f"{registration_url(tournament_id, registration_id)}/category"


def confirm_url(tournament_id: int, registration_id: int) -> str:
    return f"{registration_url(tournament_id, registration_id)}/confirm"


def readiness_url(tournament_id: int) -> str:
    return f"{get_root_uvicorn_url()}tournaments/{tournament_id}/bracket-readiness"


def profile_url(club: int, competitor_id: int) -> str:
    return f"{competitor_url(club, competitor_id)}/profile"


# ---------------------------------------------------------------- HTTP helpers del trozo C1


async def update_profile_http(
    *,
    club: int,
    competitor_id: int,
    token: str,
    date_of_birth: date | None,
    sex: str | None,
    belt_id: int | None,
    key: str | None = None,
    reason_code: str | None = "DATA_CORRECTION",
    reason_note: str | None = None,
) -> tuple[int, Any]:
    """Reemplazo del perfil deportivo de un competidor (ruta real §10, puerta F3 + idempotencia).

    Los tres campos viajan **siempre** (``NULL`` es valor legitimo): es el mismo contrato que la
    inscripcion en borrador.
    """
    payload: dict[str, Any] = {
        "date_of_birth": None if date_of_birth is None else date_of_birth.isoformat(),
        "sex": sex,
        "belt_id": belt_id,
        "reason_code": reason_code,
        "reason_note": reason_note,
    }
    return await call(
        "POST",
        profile_url(club, competitor_id),
        token=token,
        key=key or fresh_key(),
        payload=payload,
    )


async def assign_category_http(
    *,
    tournament_id: int,
    registration_id: int,
    token: str,
    key: str | None = None,
    category_key: str | None = None,
    override: bool = False,
    reason_code: str | None = None,
    reason_note: str | None = None,
) -> tuple[int, Any]:
    """Asignacion de categoria por la ruta real (§7/§8), con ``Idempotency-Key``."""
    payload: dict[str, Any] = {
        "category_key": category_key,
        "override": override,
        "reason_code": reason_code,
        "reason_note": reason_note,
    }
    return await call(
        "POST",
        category_url(tournament_id, registration_id),
        token=token,
        key=key or fresh_key(),
        payload=payload,
    )


async def get_registration_http(
    *, tournament_id: int, registration_id: int, token: str
) -> tuple[int, Any]:
    """Relectura de la inscripcion (proyeccion publica, solo lectura)."""
    return await call("GET", registration_url(tournament_id, registration_id), token=token)


async def confirm_registration_http(
    *,
    tournament_id: int,
    registration_id: int,
    token: str,
    key: str | None = None,
    reason_code: str | None = "READY",
) -> tuple[int, Any]:
    """``DRAFT -> CONFIRMED`` (la readiness solo cuenta confirmadas)."""
    return await call(
        "POST",
        confirm_url(tournament_id, registration_id),
        token=token,
        key=key or fresh_key(),
        payload={"reason_code": reason_code, "reason_note": None},
    )


async def readiness_http(*, tournament_id: int, token: str) -> dict[str, Any]:
    """Lectura de readiness del cuadro (200 o falla la prueba)."""
    status_code, body = await call("GET", readiness_url(tournament_id), token=token)
    assert status_code == 200, body
    return cast("dict[str, Any]", body)


def issue_codes(readiness: dict[str, Any]) -> list[str]:
    """Codigos literales (en orden) de los bloqueos de readiness de nivel superior."""
    return [str(issue["code"]) for issue in readiness["blocking_issues"]]


def category_group(readiness: dict[str, Any], category_key: str | None) -> dict[str, Any] | None:
    """Grupo de categoria del readiness por su clave (``None`` = sin clasificar)."""
    for category in readiness["categories"]:
        if category["category_key"] == category_key:
            return cast("dict[str, Any]", category)
    return None


# ---------------------------------------------------------------- DB helpers (auditoria)


_SELECT_CATEGORY_CHANGE = """
    SELECT id, entity, entity_id, action, changed_fields, actor_user_id, actor_label,
           reason, reason_code, reason_note, tenant_club_id, created
    FROM domain_change_log
    WHERE entity = 'tournament_registration' AND entity_id = :rid
      AND action = 'UPDATE' AND reason_code = 'CATEGORY_CHANGE'
    ORDER BY id DESC
    LIMIT 1
"""
_COUNT_CATEGORY_CHANGE = """
    SELECT count(*) FROM domain_change_log
    WHERE entity = 'tournament_registration' AND entity_id = :rid
      AND action = 'UPDATE' AND reason_code = 'CATEGORY_CHANGE'
"""
_SELECT_ACTOR_RELATION = (
    "SELECT relation FROM users_x_clubs WHERE user_id = :uid AND club_id = :club"
)


def _as_dict(record: Any) -> dict[str, Any]:
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


async def latest_category_change_event(registration_id: int) -> dict[str, Any] | None:
    """Ultimo evento ``CATEGORY_CHANGE`` de la inscripcion (campos reales de
    ``domain_change_log``)."""
    record = await database.fetch_one(
        query=_SELECT_CATEGORY_CHANGE, values={"rid": registration_id}
    )
    return None if record is None else _as_dict(record)


async def count_category_change_events(registration_id: int) -> int:
    """Cuantos eventos ``CATEGORY_CHANGE`` tiene la inscripcion (para probar el replay)."""
    return int(
        await database.fetch_val(query=_COUNT_CATEGORY_CHANGE, values={"rid": registration_id})
    )


async def actor_relation(user_id: int, club_id: int) -> str | None:
    """Relacion del actor con el tenant (``OWNER``/``COLLABORATOR``/...), o ``None``."""
    value = await database.fetch_val(
        query=_SELECT_ACTOR_RELATION, values={"uid": user_id, "club": club_id}
    )
    return None if value is None else str(value)


# ---------------------------------------------------------------- §25 evidencia


#: Validaciones y evidencias acumuladas por el modulo de pruebas (se vuelcan al cerrar).
EVIDENCE_C1: dict[str, Any] = {
    "trozo": "C1",
    "corrections": [],
    "assignments": [],
    "validations": [],
    "override_event": None,
}


def record_validation(name: str, passed: bool, detail: dict[str, Any] | None = None) -> None:
    """Anota el resultado de una validacion del trozo C1."""
    EVIDENCE_C1["validations"].append(
        {"name": name, "passed": bool(passed), "detail": detail or {}}
    )


def record_correction(
    *,
    case: str,
    competitor_key: str,
    field: str,
    corrected_value: Any,
    before_registration_id: int,
    before_tournament_id: int,
    after_registration_id: int,
    after_tournament_id: int,
    blockers_before: list[str],
    blockers_after: list[str],
    removed: list[str],
    added: list[str],
    other_cases_unchanged: bool,
) -> None:
    """Congela el antes/despues de una correccion de blocker (§10)."""
    EVIDENCE_C1["corrections"].append(
        {
            "case": case,
            "competitor_key": competitor_key,
            "field": field,
            "corrected_value": corrected_value,
            "before_registration_id": before_registration_id,
            "before_tournament_id": before_tournament_id,
            "after_registration_id": after_registration_id,
            "after_tournament_id": after_tournament_id,
            "blockers_before": blockers_before,
            "blockers_after": blockers_after,
            "removed": removed,
            "added": added,
            "other_cases_unchanged": bool(other_cases_unchanged),
        }
    )


def record_assignment(
    *,
    name: str,
    registration_id: int,
    tournament_id: int,
    actor: str,
    request: dict[str, Any],
    status: int,
    detail: Any,
    category_key_after: str | None = None,
    category_label_after: str | None = None,
    note: str | None = None,
) -> None:
    """Congela el resultado de una asignacion/override (§11/§12)."""
    EVIDENCE_C1["assignments"].append(
        {
            "name": name,
            "registration_id": registration_id,
            "tournament_id": tournament_id,
            "actor": actor,
            "request": request,
            "status": status,
            "detail": detail,
            "category_key_after": category_key_after,
            "category_label_after": category_label_after,
            "note": note,
        }
    )


def record_override_event(event: dict[str, Any] | None) -> None:
    """Congela el evento de auditoria del override OWNER (§12)."""
    EVIDENCE_C1["override_event"] = event


def write_evidence_c1() -> Path:
    """Vuelca la evidencia §25 del trozo C1 en ``EVIDENCE_C1_PATH``."""
    EVIDENCE_C1_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_C1_PATH.write_text(
        json.dumps(EVIDENCE_C1, indent=2, sort_keys=True, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return EVIDENCE_C1_PATH


__all__ = [
    "C1Scenario",
    "EVIDENCE_C1",
    "EVIDENCE_C1_PATH",
    "actor_relation",
    "assign_category_http",
    "category_group",
    "category_url",
    "confirm_registration_http",
    "confirm_url",
    "count_category_change_events",
    "get_registration_http",
    "issue_codes",
    "latest_category_change_event",
    "profile_url",
    "readiness_http",
    "readiness_url",
    "record_assignment",
    "record_correction",
    "record_override_event",
    "record_validation",
    "registration_url",
    "surface_of",
    "trozo_c1_scenario",
    "update_profile_http",
    "write_evidence_c1",
]
