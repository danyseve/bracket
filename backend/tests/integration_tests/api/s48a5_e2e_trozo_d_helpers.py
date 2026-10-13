# pylint: disable=redefined-outer-name
"""Utilidades de los casos §17-§21 del TROZO D (S4.8a-5).

Viven fuera del modulo de pruebas porque el conjunto §17-§19 ya roza el limite de
``max-module-lines`` de pylint y §20/§21 (independientes) las necesitan igual. No hay
estado propio: todo se apoya en los apoyos ``s48a5_e2e_trozo_{b,c1,c2,d}_support``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tests.integration_tests.api.s48a5_e2e_trozo_b_support import create_registration_http
from tests.integration_tests.api.s48a5_e2e_trozo_c1_support import confirm_registration_http
from tests.integration_tests.api.s48a5_e2e_trozo_c2_support import C2Scenario
from tests.integration_tests.api.s48a5_e2e_trozo_d_support import EVIDENCE_D, record_d

IDEMPOTENCY_REUSED = "IDEMPOTENCY_KEY_REUSED"


def compat_keys(body: dict[str, Any]) -> set[str]:
    return {item["category_key"] for item in body["compatible_categories"]}


def verdict_of(body: dict[str, Any], category_key: str) -> dict[str, Any]:
    return next(
        item for item in body["compatible_categories"] if item["category_key"] == category_key
    )


def warnings_of(verdict: dict[str, Any]) -> list[str]:
    return [str(item) for item in verdict.get("warnings", [])]


async def assert_replay_same_body(
    post: Any, *, key: str, payload: dict[str, Any] | None, expected: int
) -> tuple[Any, Any]:
    """Primera llamada + replay (misma clave, mismo cuerpo): mismo estatus y cuerpo identico."""
    status1, body1 = await post(key, payload)
    assert status1 == expected, (status1, body1)
    status2, body2 = await post(key, payload)
    assert status2 == expected, (status2, body2)
    assert body2 == body1, (body1, body2)
    return status1, body1


async def assert_conflict(
    post: Any, *, key: str, other_payload: dict[str, Any] | None, expected: int
) -> None:
    """Misma clave + cuerpo distinto: 409 con el codigo real del protocolo."""
    status_code, body = await post(key, other_payload)
    assert status_code == 409, (status_code, body)
    assert body.get("detail") == IDEMPOTENCY_REUSED, body


async def dedicated_registration(
    scenario: C2Scenario, competitor_key: str, weight: str, modality: str = "GI"
) -> int:
    """Crea y CONFIRMA una inscripcion propia de un competidor libre, para aislar el test de cate
    goria.
    """
    dataset = scenario.dataset
    body = await create_registration_http(
        tournament_id=int(dataset.tournament),
        token=dataset.owner_token,
        competitor_id=int(dataset.competitors[competitor_key]),
        representation="INDEPENDENT",
        sports_club_id=None,
        affiliation_id=None,
        declared_weight_kg=Decimal(weight),
        modality=modality,
    )
    await confirm_registration_http(
        tournament_id=int(dataset.tournament),
        registration_id=int(body["id"]),
        token=dataset.owner_token,
    )
    return int(body["id"])


def record_write(
    name: str,
    *,
    endpoint: str,
    expected_status: int,
    replay_same_body: bool,
    conflict_409: bool,
    keys_delta: int,
    effect: dict[str, Any],
    note: str | None = None,
) -> None:
    EVIDENCE_D["idempotency"]["writes"].append(
        {
            "write": name,
            "endpoint": endpoint,
            "expected_status": expected_status,
            "replay_mismo_cuerpo": replay_same_body,
            "conflicto_409": conflict_409,
            "idempotency_keys_delta": keys_delta,
            "efecto": effect,
            "nota": note,
        }
    )
    record_d(f"idempotencia_{name}", replay_same_body and (conflict_409 or note is not None))
