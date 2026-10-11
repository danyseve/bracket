# pylint: disable=redefined-outer-name
"""Contrato HTTP de la generacion explicita del cuadro del torneo (S3.5a, F3B, LAB ONLY).

TDD: este modulo se escribio **antes** de la implementacion y fallo en rojo (la ruta no existia:
404 en lugar del contrato descrito en ``docs/26`` §44).

Lo que se fija aqui es el contrato observable de una operacion que **escribe**: quien puede pedirla
(OWNER-only), que exige (``Idempotency-Key`` y ningun cuerpo), que revalida antes de tocar nada (el
readiness de S3.4d recalculado dentro de la transaccion, nunca el que el cliente pudo leer antes),
que reutiliza (la generacion certificada de F2C, sin duplicar su algoritmo) y que garantiza
(atomicidad, una sola generacion y un solo evento de auditoria por efecto, replay estable, y
desenlaces 4xx/5xx sin 500 para estados de dominio esperables).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from heliclockter import datetime_utc
from starlette import status

from bracket.config import config
from bracket.database import database
from bracket.logic.idempotency_protocol import RETRY_AFTER_SECONDS
from bracket.logic.scheduling.generation import BLOCKER_PREFIX
from tests.integration_tests.api.generate_bracket_http_support import (
    LOCK_TOURNAMENT_SHARE,
    LOCK_TOURNAMENT_UPDATE,
    NOT_READY_FIELDS,
    SYNTHETIC_SECRET,
    GenerationLab,
    audit_rows,
    bracket_structure,
    call_generation,
    call_readiness,
    close_registrations,
    generate,
    generation_lab_context,
    generation_reservations,
    insert_registration,
    issues_by_code,
    layout,
    pairs_of,
    ready_tournament,
    set_deadline,
    set_tournament_archived,
)
from tests.integration_tests.registration_fixtures import (
    set_competitor_active,
    wait_for_tuple_contention,
)


@pytest.fixture(scope="module", autouse=True)
def idempotency_secret() -> Iterator[None]:
    """Clave HMAC sintetica del laboratorio: el protocolo falla cerrado sin ella.

    La app corre en el mismo proceso, asi que parchear la configuracion alcanza al servidor de
    pruebas. El valor es sintetico y no sale de aqui.
    """
    patcher = pytest.MonkeyPatch()
    patcher.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    patcher.setattr(config, "idempotency_hmac_key_version", "v1")
    yield
    patcher.undo()


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def lab() -> AsyncIterator[GenerationLab]:
    async with generation_lab_context() as generation_lab:
        yield generation_lab


# --- 1. quien puede pedirla y con que forma ---------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_is_owner_only(lab: GenerationLab) -> None:
    """COLLAB revisa el readiness, pero no puede disparar la generacion: es OWNER-only."""
    await ready_tournament(lab)

    async with bracket_structure(lab):
        assert (await call_readiness(lab))["ready"] is True
        status_code, _, body = await generate(lab, key=uuid4().hex, token=lab.collaborator_token)

    assert status_code == status.HTTP_403_FORBIDDEN
    assert body == {"detail": "ROLE_NOT_ALLOWED"}
    assert await audit_rows() == []
    assert await generation_reservations() == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_rejects_an_inactive_actor(lab: GenerationLab) -> None:
    """Un actor dado de baja no dispara una escritura: 401 y ni una reserva."""
    await ready_tournament(lab)

    status_code, _, _ = await generate(lab, key=uuid4().hex, token=lab.inactive_token)

    assert status_code == status.HTTP_401_UNAUTHORIZED
    assert await audit_rows() == []
    assert await generation_reservations() == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_hides_a_foreign_tournament(lab: GenerationLab) -> None:
    """Un torneo de otro tenant responde como uno inexistente y no deja rastro."""
    status_code, _, _ = await generate(
        lab, key=uuid4().hex, tournament=lab.foreign_tournament, token=lab.owner_token
    )

    assert status_code == status.HTTP_404_NOT_FOUND
    assert await audit_rows() == []
    assert await generation_reservations() == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_hides_the_state_of_a_foreign_tournament(
    lab: GenerationLab,
) -> None:
    """Ni el estado del torneo ajeno se filtra: 404, no el 400 de \"archivado\"."""
    await set_tournament_archived(lab.foreign_tournament, archived=True)

    status_code, _, _ = await generate(
        lab, key=uuid4().hex, tournament=lab.foreign_tournament, token=lab.owner_token
    )

    assert status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_rejects_an_archived_tournament(lab: GenerationLab) -> None:
    """Regla que ya existia en la generacion de F2C: un torneo archivado no se genera."""
    await ready_tournament(lab)
    await set_tournament_archived(lab.tournament, archived=True)

    status_code, _, body = await generate(lab, key=uuid4().hex, token=lab.owner_token)

    assert status_code == status.HTTP_400_BAD_REQUEST
    assert body == {"detail": "Can't update archived tournament"}
    assert await audit_rows() == []
    assert await generation_reservations() == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_requires_an_idempotency_key(lab: GenerationLab) -> None:
    """Sin ``Idempotency-Key`` no hay operacion: no se reserva nada ni se escribe nada."""
    await ready_tournament(lab)

    status_code, _, body = await generate(lab, token=lab.owner_token)

    assert status_code == status.HTTP_400_BAD_REQUEST
    assert body == {"detail": "IDEMPOTENCY_REQUEST_INVALID"}
    assert await audit_rows() == []
    assert await generation_reservations() == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_rejects_an_unexpected_body(lab: GenerationLab) -> None:
    """La generacion no lleva datos: un campo de mas es un 422, no un campo ignorado."""
    await ready_tournament(lab)

    status_code, _, _ = await generate(
        lab, key=uuid4().hex, token=lab.owner_token, body={"reason_note": "porque si"}
    )

    assert status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert await generation_reservations() == 0


# --- 2. revalidacion del readiness --------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_is_rejected_while_the_window_is_open(lab: GenerationLab) -> None:
    """Con la ventana abierta no se genera y no se escribe ni una ranura."""
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1])

    async with bracket_structure(lab) as stage_item_id:
        before = await layout(stage_item_id)
        status_code, _, body = await generate(lab, key=uuid4().hex, token=lab.owner_token)
        assert status_code == status.HTTP_409_CONFLICT
        assert set(body["detail"]) == NOT_READY_FIELDS
        assert body["detail"]["code"] == "BRACKET_NOT_READY"
        assert body["detail"]["registration_window"] == "OPEN"
        assert list(issues_by_code(body)) == ["REGISTRATION_WINDOW_OPEN"]
        assert await layout(stage_item_id) == before

    assert await audit_rows() == []
    assert await generation_reservations() == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_is_rejected_with_a_pending_draft(lab: GenerationLab) -> None:
    """Una inscripcion en borrador bloquea aunque la ventana ya este cerrada."""
    await ready_tournament(lab)
    await insert_registration(
        lab.tournament,
        competitor_id=lab.competitors[2],
        registration_status="DRAFT",
        category_key="gi-peso-92",
        category_label="Gi -92 kg",
    )

    async with bracket_structure(lab):
        status_code, _, body = await generate(lab, key=uuid4().hex, token=lab.owner_token)

    assert status_code == status.HTTP_409_CONFLICT
    assert "REGISTRATIONS_PENDING" in issues_by_code(body)
    assert await audit_rows() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_is_rejected_with_a_category_below_the_minimum(
    lab: GenerationLab,
) -> None:
    """Una categoria con una sola confirmada bloquea: el minimo es el de F2C."""
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(
        lab.tournament,
        competitor_id=lab.competitors[1],
        category_key="gi-peso-92",
        category_label="Gi -92 kg",
    )
    await close_registrations(lab)

    status_code, _, body = await generate(lab, key=uuid4().hex, token=lab.owner_token)

    assert status_code == status.HTTP_409_CONFLICT
    issues = issues_by_code(body)
    assert set(issues) == {"CATEGORY_NOT_ENOUGH_COMPETITORS"}
    assert issues["CATEGORY_NOT_ENOUGH_COMPETITORS"]["count"] == 1
    assert issues["CATEGORY_NOT_ENOUGH_COMPETITORS"]["required_minimum"] == 2
    assert await audit_rows() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_is_rejected_with_an_inactive_confirmed_competitor(
    lab: GenerationLab,
) -> None:
    """Una confirmada cuyo competidor esta de baja bloquea: no se genera con quien no compite."""
    await ready_tournament(lab)
    await set_competitor_active(lab.competitors[1], active=False)

    async with bracket_structure(lab) as stage_item_id:
        before = await layout(stage_item_id)
        status_code, _, body = await generate(lab, key=uuid4().hex, token=lab.owner_token)
        assert status_code == status.HTTP_409_CONFLICT
        assert set(issues_by_code(body)) == {"COMPETITOR_INACTIVE"}
        assert await layout(stage_item_id) == before

    assert await audit_rows() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_lists_every_derived_blocker(lab: GenerationLab) -> None:
    """Cuando hay varios bloqueos, el rechazo los publica todos: no se queda en el primero."""
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(
        lab.tournament,
        competitor_id=lab.competitors[1],
        registration_status="DRAFT",
        category_key="gi-peso-92",
        category_label="Gi -92 kg",
    )

    status_code, _, body = await generate(lab, key=uuid4().hex, token=lab.owner_token)

    assert status_code == status.HTTP_409_CONFLICT
    assert set(issues_by_code(body)) == {
        "REGISTRATION_WINDOW_OPEN",
        "REGISTRATIONS_PENDING",
        "CATEGORY_NOT_ENOUGH_COMPETITORS",
    }


# --- 3. generacion ------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_generates_the_bye_aware_layout_once(lab: GenerationLab) -> None:
    """El caso normal: reparto con despedidas repartidas y un solo evento de auditoria."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        before = await layout(stage_item_id)
        body = await call_generation(lab)
        after = await layout(stage_item_id)

        assert sorted(value for value in after if value is not None) == sorted(
            value for value in before if value is not None
        )
        assert after != before
        assert all(not (left is None and right is None) for left, right in pairs_of(after)), after

    assert body["tournament_id"] == lab.tournament
    assert body["generated"] is True
    assert body["skipped_stage_items"] == 0
    (stage_item,) = body["stage_items"]
    assert stage_item["stage_item_id"] == stage_item_id
    assert stage_item["entrant_count"] == 2
    assert stage_item["bracket_size"] == 4
    assert stage_item["bye_count"] == 2

    rows = await audit_rows()
    assert len(rows) == 1
    assert rows[0]["entity"] == "tournament"
    assert rows[0]["entity_id"] == lab.tournament
    assert rows[0]["reason_code"] is None
    assert rows[0]["reason_note"] is None
    assert rows[0]["changed_fields"] == ["stage_item_inputs.team_id"]
    assert await generation_reservations() == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_generates_with_a_passed_deadline(lab: GenerationLab) -> None:
    """DEADLINE_PASSED permite generar si la revision global esta lista, y no toca el plazo."""
    await set_deadline(lab, datetime_utc.now() - timedelta(hours=1))
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1])

    async with bracket_structure(lab):
        body = await call_generation(lab)
        after = await call_readiness(lab)

    assert body["generated"] is True
    assert after["registration_window"] == "DEADLINE_PASSED"
    assert len(await audit_rows()) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_generates_with_closed_registrations(lab: GenerationLab) -> None:
    """CLOSED permite generar si la revision global esta lista, y sigue cerrada despues."""
    await ready_tournament(lab)

    async with bracket_structure(lab):
        body = await call_generation(lab)
        after = await call_readiness(lab)

    assert body["generated"] is True
    assert after["registration_window"] == "CLOSED"
    assert after["ready"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_without_brackets_is_an_empty_success(lab: GenerationLab) -> None:
    """Sin cuadros de eliminacion directa no hay nada que repartir: 200 vacio y sin evento."""
    await ready_tournament(lab)

    body = await call_generation(lab)

    assert body == {
        "tournament_id": lab.tournament,
        "generated": False,
        "stage_items": [],
        "skipped_stage_items": 0,
    }
    assert await audit_rows() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_skips_a_bracket_that_f2c_blocks(lab: GenerationLab) -> None:
    """Un cuadro que F2C bloquea no se toca ni se inventa un error: se cuenta aparte."""
    await ready_tournament(lab)

    async with bracket_structure(lab, team_count=4, entrant_slots=()) as empty_item:
        body = await call_generation(lab)
        assert await layout(empty_item) == [None, None, None, None]

    assert body["stage_items"] == []
    assert body["skipped_stage_items"] == 1
    assert body["generated"] is False
    assert await audit_rows() == []


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_rolls_everything_back_when_a_bracket_cannot_be_planned(
    lab: GenerationLab,
) -> None:
    """Atomicidad: un cuadro imposible de planificar deshace los anteriores y no deja reserva."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as first_item:
        async with bracket_structure(lab, slot_override=3) as inconsistent_item:
            before_first = await layout(first_item)
            before_inconsistent = await layout(inconsistent_item)
            key = uuid4().hex

            status_code, _, body = await generate(lab, key=key, token=lab.owner_token)

            assert status_code == status.HTTP_409_CONFLICT
            assert BLOCKER_PREFIX in body["detail"]
            assert await layout(first_item) == before_first
            assert await layout(inconsistent_item) == before_inconsistent
            assert await audit_rows() == []
            assert await generation_reservations() == 0

            again, _, _ = await generate(lab, key=key, token=lab.owner_token)
            assert again == status.HTTP_409_CONFLICT


# --- 4. idempotencia ----------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_replays_the_same_key(lab: GenerationLab) -> None:
    """Misma clave: mismo cuerpo y mismo reparto, sin segunda generacion ni segundo evento."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        key = uuid4().hex
        first_status, _, first_body = await generate(lab, key=key, token=lab.owner_token)
        after_first = await layout(stage_item_id)
        second_status, _, second_body = await generate(lab, key=key, token=lab.owner_token)
        after_second = await layout(stage_item_id)

    assert first_status == second_status == status.HTTP_200_OK
    assert second_body == first_body
    assert after_second == after_first
    assert len(await audit_rows()) == 1
    assert await generation_reservations() == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_a_second_call_with_another_key_does_not_generate_twice(lab: GenerationLab) -> None:
    """Doble clic (dos claves): un solo reparto y un solo evento, sin doble auditoria."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        first = await call_generation(lab)
        after_first = await layout(stage_item_id)
        second = await call_generation(lab)
        after_second = await layout(stage_item_id)

    assert second == first
    assert after_second == after_first
    assert len(await audit_rows()) == 1
    assert await generation_reservations() == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_rejects_a_key_already_used_for_another_tournament(
    lab: GenerationLab,
) -> None:
    """La misma clave con otro contenido (otro torneo) es un conflicto, no un replay."""
    await ready_tournament(lab)

    key = uuid4().hex
    async with bracket_structure(lab):
        first_status, _, _ = await generate(lab, key=key, token=lab.owner_token)
    second_status, _, body = await generate(
        lab, key=key, tournament=lab.other_tournament, token=lab.owner_token
    )

    assert first_status == status.HTTP_200_OK
    assert second_status == status.HTTP_409_CONFLICT
    assert body["detail"] == "IDEMPOTENCY_KEY_REUSED"
    assert len(await audit_rows()) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_regenerates_when_the_layout_changed(lab: GenerationLab) -> None:
    """Regeneracion segun el contrato de F2C: si el reparto cambio, la generacion lo rehace."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        await call_generation(lab)
        generated_layout = await layout(stage_item_id)
        # El organizador mueve un participante de ranura a mano: el reparto queda fuera de su sitio,
        # pero los participantes son los mismos, asi que la generacion puede rehacerlo.
        await database.execute(
            query=(
                "UPDATE stage_item_inputs SET team_id = NULL WHERE stage_item_id = :stage_item_id "
                "AND slot = 0"
            ),
            values={"stage_item_id": stage_item_id},
        )
        await database.execute(
            query=(
                "UPDATE stage_item_inputs SET team_id = :team_id "
                "WHERE stage_item_id = :stage_item_id AND slot = 1"
            ),
            values={"stage_item_id": stage_item_id, "team_id": generated_layout[0]},
        )
        moved = await layout(stage_item_id)
        body = await call_generation(lab)

        assert moved != generated_layout
        assert await layout(stage_item_id) == generated_layout
        assert body["generated"] is True

    assert len(await audit_rows()) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_generate_bracket_is_blocked_when_a_bracket_has_activity(lab: GenerationLab) -> None:
    """Un cuadro ya repartido con actividad encima bloquea la operacion entera (regla de F2C)."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        await call_generation(lab)
        # Un resultado ya puntuado es actividad: F2C deja de admitir la regeneracion del cuadro.
        await database.execute(
            query=(
                "UPDATE matches SET stage_item_input1_score = 6 WHERE round_id IN "
                "(SELECT id FROM rounds WHERE stage_item_id = :stage_item_id)"
            ),
            values={"stage_item_id": stage_item_id},
        )

        status_code, _, body = await generate(lab, key=uuid4().hex, token=lab.owner_token)

        assert status_code == status.HTTP_409_CONFLICT
        assert body["detail"]["code"] == "BRACKET_NOT_READY"
        assert "BRACKET_ALREADY_EXISTS" in issues_by_code(body)

    assert len(await audit_rows()) == 1


# --- 5. concurrencia ----------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_generation_that_cannot_take_the_lock_answers_503(lab: GenerationLab) -> None:
    """Si el bloqueo del torneo no se consigue en el plazo: 503 con ``Retry-After``, no un 500."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        before = await layout(stage_item_id)
        async with database.transaction():
            # Otro actor (aqui, el propio test) mantiene la fila del torneo bloqueada.
            await database.execute(
                query="SELECT id FROM tournaments WHERE id = :tournament_id FOR NO KEY UPDATE",
                values={"tournament_id": lab.tournament},
            )
            status_code, headers, body = await generate(lab, key=uuid4().hex, token=lab.owner_token)

        retry_after = {key.lower(): value for key, value in headers.items()}.get("retry-after")
        assert status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert retry_after == str(RETRY_AFTER_SECONDS)
        assert body["detail"] == "LOCK_TIMEOUT"
        assert await layout(stage_item_id) == before

    assert await audit_rows() == []
    assert await generation_reservations() == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_a_generation_in_flight_makes_the_next_one_wait(lab: GenerationLab) -> None:
    """Generate vs Generate: el segundo espera el bloqueo del torneo y no reparte dos veces."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        async with database.transaction():
            # Otra generacion en vuelo (aqui, el propio test) mantiene el torneo en exclusivo.
            await database.execute(
                query=LOCK_TOURNAMENT_UPDATE, values={"tournament_id": lab.tournament}
            )
            first = asyncio.create_task(generate(lab, key=uuid4().hex, token=lab.owner_token))
            await wait_for_tuple_contention("tournaments")

            second = asyncio.create_task(generate(lab, key=uuid4().hex, token=lab.owner_token))
            await wait_for_tuple_contention("tournaments")

        first_status, _, first_body = await first
        second_status, _, second_body = await second
        after = await layout(stage_item_id)

    assert first_status == second_status == status.HTTP_200_OK
    assert second_body == first_body
    assert all(not (left is None and right is None) for left, right in pairs_of(after))
    assert len(await audit_rows()) == 1
    assert await generation_reservations() == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_a_generation_waits_for_a_registration_in_flight_and_revalidates(
    lab: GenerationLab,
) -> None:
    """Generate vs alta: la generacion espera y revisa **despues** del alta, no antes.

    El cliente ya habia visto ``ready: true``; mientras el alta se confirma, la generacion espera su
    bloqueo. Al desbloquearse recalcula la revision: aparece el borrador y se rechaza. Es la prueba
    de que el veredicto no viaja con la peticion.
    """
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        before = await layout(stage_item_id)
        assert (await call_readiness(lab))["ready"] is True

        async with database.transaction():
            await database.execute(
                query=LOCK_TOURNAMENT_SHARE, values={"tournament_id": lab.tournament}
            )
            await insert_registration(
                lab.tournament,
                competitor_id=lab.competitors[2],
                registration_status="DRAFT",
                category_key="gi-peso-92",
                category_label="Gi -92 kg",
            )
            pending = asyncio.create_task(generate(lab, key=uuid4().hex, token=lab.owner_token))
            await wait_for_tuple_contention("tournaments")

        status_code, _, body = await pending

        assert status_code == status.HTTP_409_CONFLICT
        assert "REGISTRATIONS_PENDING" in issues_by_code(body)
        assert await layout(stage_item_id) == before

    assert await audit_rows() == []
    assert await generation_reservations() == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_a_generation_waits_for_the_close_and_then_generates(lab: GenerationLab) -> None:
    """Generate vs cierre: se pide con la ventana abierta y sale bien porque revalida despues.

    La revision previa diria \"no\" (ventana ``OPEN``); la operacion espera su bloqueo, el cierre se
    confirma y la revision que decide es la de despues. Ni cierra la ventana ni la toca.
    """
    await insert_registration(lab.tournament, competitor_id=lab.competitors[0])
    await insert_registration(lab.tournament, competitor_id=lab.competitors[1])

    async with bracket_structure(lab) as stage_item_id:
        assert (await call_readiness(lab))["ready"] is False

        async with database.transaction():
            # El cierre: toma el torneo en exclusivo y sella la ventana. La generacion espera.
            await database.execute(
                query=LOCK_TOURNAMENT_UPDATE, values={"tournament_id": lab.tournament}
            )
            await database.execute(
                query=(
                    "UPDATE tournaments SET registrations_closed_at = NOW() "
                    "WHERE id = :tournament_id"
                ),
                values={"tournament_id": lab.tournament},
            )
            pending = asyncio.create_task(generate(lab, key=uuid4().hex, token=lab.owner_token))
            await wait_for_tuple_contention("tournaments")

        status_code, _, body = await pending
        after = await call_readiness(lab)
        after_layout = await layout(stage_item_id)

    assert status_code == status.HTTP_200_OK
    assert body["generated"] is True
    assert after["registration_window"] == "CLOSED"
    assert all(not (left is None and right is None) for left, right in pairs_of(after_layout))
    assert len(await audit_rows()) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_a_generation_waits_for_a_lifecycle_change_and_revalidates(
    lab: GenerationLab,
) -> None:
    """Generate vs ciclo de vida: la baja del competidor se ve porque se espera y se revalida."""
    await ready_tournament(lab)

    async with bracket_structure(lab) as stage_item_id:
        before = await layout(stage_item_id)
        assert (await call_readiness(lab))["ready"] is True

        async with database.transaction():
            await database.execute(
                query=LOCK_TOURNAMENT_SHARE, values={"tournament_id": lab.tournament}
            )
            await set_competitor_active(lab.competitors[1], active=False)
            pending = asyncio.create_task(generate(lab, key=uuid4().hex, token=lab.owner_token))
            await wait_for_tuple_contention("tournaments")

        status_code, _, body = await pending

        assert status_code == status.HTTP_409_CONFLICT
        assert set(issues_by_code(body)) == {"COMPETITOR_INACTIVE"}
        assert await layout(stage_item_id) == before

    assert await audit_rows() == []
    assert await generation_reservations() == 0
