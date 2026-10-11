# pylint: disable=redefined-outer-name
"""Concurrencia del contrato HTTP de competidores (S3.7a, F3B, LAB ONLY).

Cubre lo que **aporta la capa HTTP**: la clave de idempotencia en vuelo, dos renombrados en
paralelo, renombrado contra baja y dos altas con el mismo nombre. Sin ``sleep``: la unica espera
posible es la de un bloqueo real y su limite esta acotado por el protocolo (``Retry-After``).

El entrecruzamiento de bloqueos de la identidad con la confirmacion de inscripciones (lista de
espera RS-9: la confirmacion lee la fila del competidor con ``FOR SHARE`` y la baja toma el
bloqueo de fila al escribir) esta certificado en la suite de dominio
(``competitor_reactivation_concurrency_test.py``: ``test_reactivation_waits_for_a_share_lock_on_
the_competitor_row``, ``test_a_confirmation_never_sees_an_uncommitted_reactivation``). Aqui no se
duplica: la ruta HTTP invoca las mismas funciones de dominio bajo los mismos bloqueos.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from starlette import status

from bracket.config import config
from bracket.database import database
from bracket.utils.id_types import CompetitorId
from tests.integration_tests.api.competitor_http_support import (
    CompetitorLab,
    action_url,
    assert_history_is_coherent,
    call,
    competitor_lab_context,
    competitors_url,
    history_rows,
)
from tests.integration_tests.api.generate_bracket_http_support import SYNTHETIC_SECRET
from tests.integration_tests.registration_fixtures import competitor_audit_rows

CREATE_REASON = "PLANNED_ENTRY"
RENAME_REASON = "DATA_CORRECTION"
DEACTIVATE_REASON = "DUPLICATE"


@pytest.fixture(scope="module", autouse=True)
def idempotency_secret() -> Iterator[None]:
    patcher = pytest.MonkeyPatch()
    patcher.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    patcher.setattr(config, "idempotency_hmac_key_version", "v1")
    yield
    patcher.undo()


@pytest.fixture(autouse=True)
def write_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """La concurrencia se prueba sobre escrituras habilitadas, sin depender del entorno."""
    monkeypatch.setattr(config, "f3_write_enabled", True)


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def lab() -> AsyncIterator[CompetitorLab]:
    async with competitor_lab_context() as competitor_lab:
        yield competitor_lab


def new_key() -> str:
    return uuid4().hex


async def count_by_name(club_id: int, name: str) -> int:
    count = await database.fetch_val(
        query="SELECT count(*) FROM competitors WHERE managed_by_club_id = :club"
        " AND display_name = :name",
        values={"club": club_id, "name": name},
    )
    return int(count or 0)


async def open_history_names(competitor_id: int) -> list[str]:
    records = await database.fetch_all(
        query="""
            SELECT display_name
            FROM competitors_name_history
            WHERE competitor_id = :competitor_id AND valid_to IS NULL
        """,
        values={"competitor_id": competitor_id},
    )
    return [str(record._mapping["display_name"]) for record in records]


async def current_row(competitor_id: int) -> dict[str, Any]:
    record = await database.fetch_one(
        query="SELECT display_name, active FROM competitors WHERE id = :id",
        values={"id": competitor_id},
    )
    assert record is not None
    return dict(record._mapping)


@pytest.mark.asyncio(loop_scope="session")
async def test_the_same_key_in_flight_writes_a_single_identity(lab: CompetitorLab) -> None:
    key = new_key()
    payload = {"display_name": f"{lab.root_name}-en-vuelo", "reason_code": CREATE_REASON}

    results = await asyncio.gather(
        call("POST", competitors_url(lab), token=lab.owner_token, key=key, payload=payload),
        call("POST", competitors_url(lab), token=lab.owner_token, key=key, payload=payload),
    )

    statuses = sorted(response_status for response_status, _ in results)
    assert statuses[0] == status.HTTP_201_CREATED
    assert statuses[1] in (status.HTTP_201_CREATED, status.HTTP_409_CONFLICT)
    for response_status, body in results:
        if response_status == status.HTTP_409_CONFLICT:
            assert body["detail"] == "OPERATION_IN_PROGRESS"

    assert await count_by_name(int(lab.club), f"{lab.root_name}-en-vuelo") == 1
    created_id = next(body["id"] for response_status, body in results if "id" in body)
    assert [row["action"] for row in await competitor_audit_rows(created_id)] == ["CREATE"]


# Hallazgo S3.7a (documentado en docs/26 §50) y corregido en S3.7b: dos renombrados simultaneos del
# mismo competidor chocaban con ck_competitors_name_history_range porque el cierre del historial y
# la apertura de la fila nueva tomaban NOW() (inicio de transaccion) en vez de un reloj leido en el
# momento efectivo de la mutacion. La prueba ya no se marca: exige el contrato completo.
@pytest.mark.asyncio(loop_scope="session")
async def test_two_renames_in_parallel_keep_the_history_coherent(lab: CompetitorLab) -> None:
    competitor_id = int(lab.active_competitors[0])
    target_names = {f"{lab.root_name}-par-a", f"{lab.root_name}-par-b"}

    results = await asyncio.gather(
        *(
            call(
                "POST",
                action_url(lab, competitor_id, "rename"),
                token=lab.owner_token,
                key=new_key(),
                payload={"display_name": name, "reason_code": RENAME_REASON},
            )
            for name in sorted(target_names)
        )
    )

    assert [response_status for response_status, _ in results] == [
        status.HTTP_200_OK,
        status.HTTP_200_OK,
    ]
    # Cada renombrado abre una entrada y cierra la anterior: nunca queda mas de una abierta y la
    # abierta es el nombre que quedo guardado.
    open_names = await open_history_names(competitor_id)
    assert len(open_names) == 1
    assert open_names[0] == (await current_row(competitor_id))["display_name"]
    assert set(open_names) <= target_names
    # Ninguno de los dos estados comprometidos se pierde y los rangos siguen siendo validos.
    history = await assert_history_is_coherent(lab, competitor_id)
    assert target_names <= {str(row["display_name"]) for row in history}
    audit = await competitor_audit_rows(CompetitorId(competitor_id))
    assert [row["action"] for row in audit] == ["UPDATE", "UPDATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_rename_and_a_deactivation_converge(lab: CompetitorLab) -> None:
    competitor_id = int(lab.active_competitors[1])
    new_name = f"{lab.root_name}-renombrada-a-la-vez"

    rename_result, deactivate_result = await asyncio.gather(
        call(
            "POST",
            action_url(lab, competitor_id, "rename"),
            token=lab.owner_token,
            key=new_key(),
            payload={"display_name": new_name, "reason_code": RENAME_REASON},
        ),
        call(
            "POST",
            action_url(lab, competitor_id, "deactivate"),
            token=lab.owner_token,
            key=new_key(),
            payload={"reason_code": DEACTIVATE_REASON},
        ),
    )

    assert rename_result[0] == status.HTTP_200_OK
    assert deactivate_result[0] == status.HTTP_200_OK
    # Los bloqueos de fila serializan las dos escrituras: no se pierde ninguna de las dos.
    row = await current_row(competitor_id)
    assert row["display_name"] == new_name
    assert row["active"] is False
    second_audit = await competitor_audit_rows(CompetitorId(competitor_id))
    actions = [str(item["action"]) for item in second_audit]
    assert sorted(actions) == ["DEACTIVATE", "UPDATE"]
    # El historial de nombres tambien queda coherente cuando compite con otra escritura.
    await assert_history_is_coherent(lab, competitor_id)


@pytest.mark.asyncio(loop_scope="session")
async def test_two_identities_can_share_a_name(lab: CompetitorLab) -> None:
    name = f"{lab.root_name}-homonima"

    results = await asyncio.gather(
        *(
            call(
                "POST",
                competitors_url(lab),
                token=lab.owner_token,
                key=new_key(),
                payload={"display_name": name, "reason_code": CREATE_REASON},
            )
            for _ in range(2)
        )
    )

    assert [response_status for response_status, _ in results] == [
        status.HTTP_201_CREATED,
        status.HTTP_201_CREATED,
    ]
    created_ids = {body["id"] for _, body in results}
    assert len(created_ids) == 2
    # Dos identidades distintas, deliberadamente: el nombre no identifica.
    assert await count_by_name(int(lab.club), name) == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_a_concurrent_burst_of_writes_never_breaks_the_contract(
    lab: CompetitorLab,
) -> None:
    burst = [
        (
            "POST",
            competitors_url(lab),
            {
                "display_name": f"{lab.root_name}-rafaga-{index}",
                "reason_code": CREATE_REASON,
            },
        )
        for index in range(3)
    ] + [
        (
            "POST",
            action_url(lab, int(lab.active_competitors[2]), "rename"),
            {
                "display_name": f"{lab.root_name}-rafaga-rename-{index}",
                "reason_code": RENAME_REASON,
            },
        )
        for index in range(2)
    ]

    results = await asyncio.gather(
        *(
            call(method, url, token=lab.owner_token, key=new_key(), payload=payload)
            for method, url, payload in burst
        )
    )

    for response_status, body in results:
        assert response_status in (
            status.HTTP_200_OK,
            status.HTTP_201_CREATED,
            status.HTTP_409_CONFLICT,
        )
        if response_status == status.HTTP_409_CONFLICT:
            assert body["detail"] in ("OPERATION_IN_PROGRESS", "LOCK_TIMEOUT")
    rafaga_counts = [
        await count_by_name(int(lab.club), f"{lab.root_name}-rafaga-{index}")
        for index in range(3)
    ]
    assert rafaga_counts == [1, 1, 1]
    competitor_id = int(lab.active_competitors[2])
    assert len(await open_history_names(competitor_id)) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_a_burst_of_renames_of_one_identity_leaves_one_open_entry(
    lab: CompetitorLab,
) -> None:
    """Cuatro renombrados del mismo competidor: serializan y dejan el historial coherente."""
    competitor_id = int(lab.active_competitors[0])
    names = [f"{lab.root_name}-renombrada-{index}" for index in range(4)]

    results = await asyncio.gather(
        *(
            call(
                "POST",
                action_url(lab, competitor_id, "rename"),
                token=lab.owner_token,
                key=new_key(),
                payload={"display_name": name, "reason_code": RENAME_REASON},
            )
            for name in names
        )
    )

    assert [response_status for response_status, _ in results] == [status.HTTP_200_OK] * 4
    history = await assert_history_is_coherent(lab, competitor_id)
    # Ninguna de las cuatro mutaciones comprometidas se pierde: todas estan en el historial.
    assert set(names) <= {str(row["display_name"]) for row in history}
    assert len(history) == len(names)
    audit = [row["action"] for row in await competitor_audit_rows(CompetitorId(competitor_id))]
    assert audit == ["UPDATE"] * 4


@pytest.mark.asyncio(loop_scope="session")
async def test_a_replayed_rename_is_a_single_effect(lab: CompetitorLab) -> None:
    """La misma clave reproduce la respuesta sin volver a cerrar ni abrir historial."""
    competitor_id = int(lab.active_competitors[1])
    key = new_key()
    renamed = f"{lab.root_name}-replay"
    payload = {"display_name": renamed, "reason_code": RENAME_REASON}
    url = action_url(lab, competitor_id, "rename")

    first = await call("POST", url, token=lab.owner_token, key=key, payload=payload)
    second = await call("POST", url, token=lab.owner_token, key=key, payload=payload)

    assert first[0] == status.HTTP_200_OK
    assert second == first
    history = await assert_history_is_coherent(lab, competitor_id)
    assert len(history) == 1
    assert history[0]["valid_to"] is None
    assert str(history[0]["display_name"]) == renamed

    # Renombrar al nombre que ya esta vigente no abre historial nuevo ni duplica auditoria.
    repeated = await call("POST", url, token=lab.owner_token, key=new_key(), payload=payload)
    assert repeated[0] == status.HTTP_200_OK
    assert len(await history_rows(competitor_id)) == 1
    audit = [row["action"] for row in await competitor_audit_rows(CompetitorId(competitor_id))]
    assert audit == ["UPDATE"]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_rename_rejected_by_validation_leaves_no_partial_history(
    lab: CompetitorLab,
) -> None:
    """Una carrera con una peticion invalida no deja historial a medias ni audita el rechazo."""
    competitor_id = int(lab.active_competitors[2])
    accepted_name = f"{lab.root_name}-valida"
    url = action_url(lab, competitor_id, "rename")

    results = await asyncio.gather(
        call(
            "POST",
            url,
            token=lab.owner_token,
            key=new_key(),
            payload={"display_name": accepted_name, "reason_code": RENAME_REASON},
        ),
        call(
            "POST",
            url,
            token=lab.owner_token,
            key=new_key(),
            payload={"display_name": "", "reason_code": RENAME_REASON},
        ),
    )

    by_status = {response_status: body for response_status, body in results}
    assert sorted(by_status) == [status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST]
    # El rechazo es un error de dominio estable, sin internals de base de datos.
    assert by_status[status.HTTP_400_BAD_REQUEST] == {"detail": "Competitor request is invalid"}
    history = await assert_history_is_coherent(lab, competitor_id)
    open_rows = [row for row in history if row["valid_to"] is None]
    assert [str(row["display_name"]) for row in open_rows] == [accepted_name]
    audit = [row["action"] for row in await competitor_audit_rows(CompetitorId(competitor_id))]
    assert audit == ["UPDATE"]
