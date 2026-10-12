# pylint: disable=redefined-outer-name  # los fixtures vienen del modulo de contrato de asignacion.
"""Contrato de repeticion del resultado confirmado de la asignacion de tatami (S4.3).

Vive aparte del modulo de contrato de asignacion para no engordarlo mas alla del limite del gate
`too-many-lines` de pylint; comparte sus fixtures y sus ayudantes.
"""

import json
from typing import Any
from uuid import uuid4

import pytest
from fastapi import status

from bracket.database import database
from bracket.logic.idempotency import IdempotencyError, validate_response_metadata
from tests.integration_tests.api.generate_bracket_http_support import GenerationLab
from tests.integration_tests.api.stage_item_court_lab_test import (  # noqa: F401
    TatamiLab,
    assign,
    assign_ok,
    assign_ok_key,
    assignment_reservations,
    idempotency_secret,
    lab,
    match_courts,
    stage_item_court,
    tatami,
    tatami_audit,
)


@pytest.mark.asyncio(loop_scope="session")
async def test_replay_of_a_confirmed_unassignment_repeats_the_original_answer(
    lab: GenerationLab,  # noqa: F811  (fixture importado del modulo de contrato)
    tatami: TatamiLab,  # noqa: F811  (fixtures importados del modulo de contrato)
) -> None:
    """CASO A en la desasignacion: la repeticion confirma lo mismo, con nulos incluidos.

    Es el caso que mas aprieta el almacen del resultado: la respuesta confirmada lleva
    ``court_id`` y ``court_name`` a ``null`` (el cuadro se queda sin tatami) y hay que poder
    devolverla tal cual.
    """
    await assign_ok(lab, stage_item_id=tatami.stage_item_id, court_id=tatami.court_a)
    key = uuid4().hex
    first_status, _, first_body = await assign(
        lab, stage_item_id=tatami.stage_item_id, court_id=None, key=key, token=lab.owner_token
    )
    second_status, _, second_body = await assign(
        lab, stage_item_id=tatami.stage_item_id, court_id=None, key=key, token=lab.owner_token
    )

    assert first_status == second_status == status.HTTP_200_OK
    assert first_body["court_id"] is None and first_body["court_name"] is None
    assert first_body["affected_match_count"] >= 1
    assert second_body == first_body
    assert await stage_item_court(tatami.stage_item_id) is None
    assert await match_courts(tatami.stage_item_id) == [None]
    assert [row["action"] for row in await tatami_audit()] == [
        "TATAMI_ASSIGNED",
        "TATAMI_UNASSIGNED",
    ]
    assert await assignment_reservations(tatami.stage_item_id) == 2


# --- 6b. el resultado confirmado se guarda con la lista cerrada de claves -------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_confirmed_response_is_stored_with_the_closed_key_set(
    lab: GenerationLab,  # noqa: F811  (fixture importado del modulo de contrato)
    tatami: TatamiLab,  # noqa: F811  (fixtures importados del modulo de contrato)
) -> None:
    """La reserva guarda el contrato de la operacion, y una clave de fuera no se admite.

    Es la evidencia de que el replay no inventa: lo que se reenvia es lo que quedo guardado. El
    rechazo de claves de fuera se comprueba en el validador del protocolo (la restriccion de la
    tabla usa la misma lista, y el caso positivo de este test es el que demuestra que la acepta).
    """
    key = uuid4().hex
    first = await assign_ok_key(lab, tatami, key=key, court_id=tatami.court_a)

    stored = await database.fetch_val(
        query=(
            "SELECT response_body FROM domain_idempotency_keys "
            "WHERE request_path LIKE :path ORDER BY id DESC LIMIT 1"
        ),
        values={"path": f"%/stage-items/{int(tatami.stage_item_id)}/court"},
    )

    # El driver devuelve ``jsonb`` como texto: el cuerpo se compara ya deserializado.
    stored_body: dict[str, Any] = json.loads(stored) if isinstance(stored, str) else stored

    assert stored_body == {
        "stage_item_id": int(tatami.stage_item_id),
        "court_id": int(tatami.court_a),
        "court_name": "Tatami 1",
        "affected_match_count": first["affected_match_count"],
    }
    with pytest.raises(IdempotencyError):
        validate_response_metadata({**stored_body, "sneaky": 1})
    with pytest.raises(IdempotencyError):
        validate_response_metadata({**stored_body, "court_id": 0})
