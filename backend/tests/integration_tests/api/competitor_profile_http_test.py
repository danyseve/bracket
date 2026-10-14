# pylint: disable=redefined-outer-name
"""Contrato HTTP del perfil deportivo del competidor (S4.8a-3, LAB ONLY).

TDD: este modulo se escribe **antes** de la implementacion y falla en rojo (ni los campos ni la
ruta existen todavia). Fija el contrato de S4.8a-3 §2:

* **Alta y actualizacion** aceptan ``date_of_birth``, ``sex`` y ``belt_id``, los tres opcionales y
  anulables: un cliente anterior a S4.8a sigue funcionando sin enviarlos (quedan en ``NULL``);
* **Lectura**: la proyeccion publica expone el perfil deportivo. El club gestor
  (``managed_by_club_id``) **no** se publica: se mantiene la proyeccion certificada de S3.7a, que
  argumenta que el ambito ya esta en la ruta (ajuste declarado frente a S4.8a-3 §2). El tenant sale
  de la sesion y del club de la ruta y el cuerpo no puede declararlo (``extra="forbid"``);
* **Validacion**: el cinturon tiene que existir y estar activo, el sexo tiene que ser del
  vocabulario del dominio y la fecha de nacimiento no puede estar en el futuro. No hay valores por
  defecto deportivos: lo que no se informa es ``NULL``, no un cinturon ni un sexo inventado;
* **Actualizacion de perfil**: reemplazo explicito de los tres campos (la misma politica que la
  inscripcion en borrador), auditada como ``UPDATE`` del competidor con
  ``DATA_CORRECTION``. Sin cambios reales no hay evento;
* **Autorizacion**: la misma del dominio certificado (OWNER y COLLABORATOR pueden editar datos);
  un competidor de otro tenant responde como uno inexistente;
* **Puerta e idempotencia**: la escritura lleva la puerta de F3 y el protocolo de S3.3c-4.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import date, timedelta
from typing import Any, Final
from uuid import uuid4

import pytest
import pytest_asyncio
from starlette import status

from bracket.config import config
from bracket.database import database
from bracket.routes.f3_write_gate import F3_WRITE_DISABLED
from bracket.utils.id_types import BeltId, CompetitorId
from tests.integration_tests.api.competitor_http_support import (
    CompetitorLab,
    call,
    competitor_lab_context,
    competitor_url,
    competitors_url,
)
from tests.integration_tests.api.generate_bracket_http_support import SYNTHETIC_SECRET
from tests.integration_tests.registration_fixtures import competitor_audit_rows

#: Proyeccion publica del competidor: un campo de mas es un fallo y cierra el contrato.
COMPETITOR_FIELDS: Final = {
    "id",
    "display_name",
    "active",
    "created",
    "updated_at",
    "date_of_birth",
    "sex",
    "belt_id",
}
#: Afiliacion actual: la publica **solo** el listado (S4.8d §3). El detalle sigue cerrado.
AFFILIATION_FIELDS: Final = {"affiliation_mode", "sports_club_id", "sports_club_name"}
#: Proyeccion del listado: el detalle mas la afiliacion actual.
COMPETITOR_LISTING_FIELDS: Final = COMPETITOR_FIELDS | AFFILIATION_FIELDS
#: Detalle fijo del rechazo por datos invalidos (no repite el valor recibido).
INVALID_DETAIL: Final = "Competitor request is invalid"
NOT_FOUND_DETAIL: Final = "Not Found"
UNAUTHORISED_DETAIL: Final = "Could not validate credentials"
NOT_AUTHENTICATED_DETAIL: Final = "Not authenticated"
#: Motivos del catalogo cerrado que usan estas rutas.
CREATE_REASON: Final = "PLANNED_ENTRY"
UPDATE_REASON: Final = "DATA_CORRECTION"

_INSERT_BELT = """
    INSERT INTO belts (code, label, rank, scope, active)
    VALUES (:code, :label, :rank, :scope, :active)
    RETURNING id
"""

_SELECT_PROFILE = """
    SELECT date_of_birth, sex, belt_id, managed_by_club_id
    FROM competitors
    WHERE id = :competitor_id
"""


@pytest.fixture(scope="module", autouse=True)
def idempotency_secret() -> Iterator[None]:
    """Clave HMAC sintetica del laboratorio: sin ella el protocolo falla cerrado."""
    patcher = pytest.MonkeyPatch()
    patcher.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    patcher.setattr(config, "idempotency_hmac_key_version", "v1")
    yield
    patcher.undo()


@pytest.fixture()
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Escritura habilitada de forma explicita: el contrato no depende del entorno de CI."""
    monkeypatch.setattr(config, "f3_write_enabled", True)


async def insert_belt(*, active: bool = True, scope: str = "ADULT") -> BeltId:
    """Cinturon del laboratorio, unico por ejecucion en ``(scope, code)`` y en ``(scope, rank)``.

    El rango tambien tiene que ser unico: la tabla declara ``uq_belts_scope_rank`` y el rango es
    la clave de orden de la escala. Se usa un tramo alto para no chocar con el catalogo sembrado.
    """
    suffix = uuid4().hex[:10]
    belt_id = await database.fetch_val(
        query=_INSERT_BELT,
        values={
            "code": f"LAB-{suffix}",
            "label": f"Lab {suffix}",
            "rank": 1000 + int(uuid4().hex[:6], 16) % 8000,
            "scope": scope,
            "active": active,
        },
    )
    return BeltId(int(belt_id))


#: Cinturones del modulo pendientes de retirada (ver ``delete_belts``).
_PENDING_BELTS: list[int] = []


async def delete_belts(ids: tuple[int, ...]) -> None:
    """**Difiere** la retirada de los cinturones del laboratorio.

    No se borran en el ``finally`` del test a proposito: la fila del competidor que los referencia
    la confirma la API con su propia transaccion y sigue viva hasta que el laboratorio cierra, de
    modo que un ``DELETE`` ahi chocaria con ``competitors_belt_id_fkey``. La retirada efectiva la
    hace el fixture de modulo ``release_lab_belts``, con todos los laboratorios ya cerrados.
    """
    _PENDING_BELTS.extend(int(belt_id) for belt_id in ids)


@pytest_asyncio.fixture(scope="module", loop_scope="session", autouse=True)
async def release_lab_belts() -> AsyncIterator[None]:
    """Retira al cerrar el modulo los cinturones que el modulo creo para las pruebas."""
    yield
    if not _PENDING_BELTS:
        return
    ids = list(_PENDING_BELTS)
    # Ninguna identidad legitima apunta a estos cinturones: son del laboratorio de este modulo.
    await database.execute(
        query="UPDATE competitors SET belt_id = NULL WHERE belt_id = ANY(:ids)",
        values={"ids": ids},
    )
    await database.execute(query="DELETE FROM belts WHERE id = ANY(:ids)", values={"ids": ids})
    _PENDING_BELTS.clear()


async def profile_row(competitor_id: int) -> dict[str, Any]:
    """Perfil deportivo tal como quedo en la tabla."""
    record = await database.fetch_one(
        query=_SELECT_PROFILE, values={"competitor_id": competitor_id}
    )
    assert record is not None
    return dict(record._mapping)


def profile_url(lab: CompetitorLab, competitor_id: int) -> str:
    """Ruta de actualizacion del perfil deportivo."""
    return f"{competitor_url(lab, competitor_id)}/profile"


def profile_body(
    *,
    date_of_birth: str | None = None,
    sex: str | None = None,
    belt_id: int | None = None,
    reason_code: str | None = UPDATE_REASON,
    reason_note: str | None = None,
) -> dict[str, Any]:
    """Cuerpo de reemplazo explicito: los tres campos viajan siempre, aunque sean ``NULL``."""
    return {
        "date_of_birth": date_of_birth,
        "sex": sex,
        "belt_id": belt_id,
        "reason_code": reason_code,
        "reason_note": reason_note,
    }


def tomorrow() -> str:
    return (date.today() + timedelta(days=1)).isoformat()


@pytest.mark.asyncio(loop_scope="session")
async def test_creating_a_competitor_with_a_sports_profile_round_trips() -> None:
    """El alta acepta el perfil, lo persiste y lo publica."""
    async with competitor_lab_context() as lab:
        belt = await insert_belt()
        try:
            status_code, body = await call(
                "POST",
                competitors_url(lab),
                token=lab.owner_token,
                key=f"k-{uuid4().hex}",
                payload={
                    "display_name": f"{lab.root_name}-perfil",
                    "date_of_birth": "1990-05-17",
                    "sex": "FEMALE",
                    "belt_id": belt,
                    "reason_code": CREATE_REASON,
                },
            )
            assert status_code == status.HTTP_201_CREATED, body
            assert set(body) == COMPETITOR_FIELDS, set(body)
            assert body["date_of_birth"] == "1990-05-17"
            assert body["sex"] == "FEMALE"
            assert body["belt_id"] == belt
            assert "managed_by_club_id" not in body

            row = await profile_row(body["id"])
            # El club gestor es el de la sesion aunque la respuesta no lo publique.
            assert row["managed_by_club_id"] == lab.club
            assert row["sex"] == "FEMALE"
            assert row["belt_id"] == belt
            assert str(row["date_of_birth"]) == "1990-05-17"

            events = await competitor_audit_rows(CompetitorId(body["id"]))
            assert [event["action"] for event in events] == ["CREATE"]
            assert {"date_of_birth", "sex", "belt_id"} <= set(events[0]["changed_fields"])
        finally:
            await delete_belts((belt,))


@pytest.mark.asyncio(loop_scope="session")
async def test_a_client_without_the_sports_profile_keeps_working() -> None:
    """Compatibilidad: el alta anterior a S4.8a no envia perfil y no se inventa nada."""
    async with competitor_lab_context() as lab:
        status_code, body = await call(
            "POST",
            competitors_url(lab),
            token=lab.owner_token,
            key=f"k-{uuid4().hex}",
            payload={"display_name": f"{lab.root_name}-antiguo", "reason_code": CREATE_REASON},
        )
        assert status_code == status.HTTP_201_CREATED, body
        assert body["date_of_birth"] is None
        assert body["sex"] is None
        assert body["belt_id"] is None
        assert "managed_by_club_id" not in body
        # El club gestor sigue saliendo del contexto autorizado, no del cuerpo.
        assert (await profile_row(body["id"]))["managed_by_club_id"] == lab.club

        row = await profile_row(body["id"])
        assert row == {
            "date_of_birth": None,
            "sex": None,
            "belt_id": None,
            "managed_by_club_id": lab.club,
        }


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unknown_belt_is_rejected() -> None:
    """Un cinturon que no existe es un dato invalido, no un 500."""
    async with competitor_lab_context() as lab:
        status_code, body = await call(
            "POST",
            competitors_url(lab),
            token=lab.owner_token,
            key=f"k-{uuid4().hex}",
            payload={
                "display_name": f"{lab.root_name}-cinturon-malo",
                "belt_id": 999_999_999,
                "reason_code": CREATE_REASON,
            },
        )
        assert status_code == status.HTTP_400_BAD_REQUEST, body
        assert body["detail"] == INVALID_DETAIL


@pytest.mark.asyncio(loop_scope="session")
async def test_an_inactive_belt_is_rejected() -> None:
    """Un cinturon retirado del catalogo no se puede asignar."""
    async with competitor_lab_context() as lab:
        belt = await insert_belt(active=False)
        try:
            status_code, body = await call(
                "POST",
                competitors_url(lab),
                token=lab.owner_token,
                key=f"k-{uuid4().hex}",
                payload={
                    "display_name": f"{lab.root_name}-cinturon-baja",
                    "belt_id": belt,
                    "reason_code": CREATE_REASON,
                },
            )
            assert status_code == status.HTTP_400_BAD_REQUEST, body
            assert body["detail"] == INVALID_DETAIL
        finally:
            await delete_belts((belt,))


@pytest.mark.asyncio(loop_scope="session")
async def test_a_date_of_birth_in_the_future_is_rejected() -> None:
    """Una fecha de nacimiento futura no es un dato valido."""
    async with competitor_lab_context() as lab:
        status_code, body = await call(
            "POST",
            competitors_url(lab),
            token=lab.owner_token,
            key=f"k-{uuid4().hex}",
            payload={
                "display_name": f"{lab.root_name}-fecha-futura",
                "date_of_birth": tomorrow(),
                "reason_code": CREATE_REASON,
            },
        )
        assert status_code == status.HTTP_400_BAD_REQUEST, body
        assert body["detail"] == INVALID_DETAIL


@pytest.mark.asyncio(loop_scope="session")
async def test_a_sex_outside_the_vocabulary_is_rejected() -> None:
    """``MIXED`` es una restriccion de categoria, nunca un sexo de persona."""
    async with competitor_lab_context() as lab:
        status_code, _ = await call(
            "POST",
            competitors_url(lab),
            token=lab.owner_token,
            key=f"k-{uuid4().hex}",
            payload={
                "display_name": f"{lab.root_name}-sexo-malo",
                "sex": "MIXED",
                "reason_code": CREATE_REASON,
            },
        )
        assert status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.asyncio(loop_scope="session")
async def test_the_profile_update_sets_and_clears_the_three_fields() -> None:
    """Reemplazo explicito: fija el perfil, lo audita y luego lo deja vacio."""
    async with competitor_lab_context() as lab:
        competitor = lab.active_competitors[0]
        belt = await insert_belt()
        try:
            status_code, body = await call(
                "POST",
                profile_url(lab, competitor),
                token=lab.owner_token,
                key=f"k-{uuid4().hex}",
                payload=profile_body(date_of_birth="1988-02-29", sex="MALE", belt_id=belt),
            )
            assert status_code == status.HTTP_200_OK, body
            assert set(body) == COMPETITOR_FIELDS, set(body)
            assert body["sex"] == "MALE" and body["belt_id"] == belt
            assert body["date_of_birth"] == "1988-02-29"

            events = await competitor_audit_rows(competitor)
            update_events = [event for event in events if event["action"] == "UPDATE"]
            assert len(update_events) == 1
            assert update_events[0]["reason_code"] == UPDATE_REASON
            assert set(update_events[0]["changed_fields"]) == {
                "date_of_birth",
                "sex",
                "belt_id",
            }

            status_code, body = await call(
                "POST",
                profile_url(lab, competitor),
                token=lab.owner_token,
                key=f"k-{uuid4().hex}",
                payload=profile_body(),
            )
            assert status_code == status.HTTP_200_OK, body
            assert body["date_of_birth"] is None and body["sex"] is None
            assert body["belt_id"] is None
            assert await profile_row(competitor) == {
                "date_of_birth": None,
                "sex": None,
                "belt_id": None,
                "managed_by_club_id": lab.club,
            }

            events = await competitor_audit_rows(competitor)
            assert len([event for event in events if event["action"] == "UPDATE"]) == 2
        finally:
            await delete_belts((belt,))


@pytest.mark.asyncio(loop_scope="session")
async def test_a_profile_update_without_real_changes_writes_no_event() -> None:
    """Sin cambios no hay evento: la auditoria no repite operaciones que no cambiaron nada."""
    async with competitor_lab_context() as lab:
        competitor = lab.active_competitors[1]
        belt = await insert_belt()
        try:
            first, _ = await call(
                "POST",
                profile_url(lab, competitor),
                token=lab.owner_token,
                key=f"k-{uuid4().hex}",
                payload=profile_body(date_of_birth="2000-01-01", sex="MALE", belt_id=belt),
            )
            assert first == status.HTTP_200_OK
            before = await competitor_audit_rows(competitor)

            second, body = await call(
                "POST",
                profile_url(lab, competitor),
                token=lab.owner_token,
                key=f"k-{uuid4().hex}",
                payload=profile_body(date_of_birth="2000-01-01", sex="MALE", belt_id=belt),
            )
            assert second == status.HTTP_200_OK, body
            assert await competitor_audit_rows(competitor) == before
        finally:
            await delete_belts((belt,))


@pytest.mark.asyncio(loop_scope="session")
async def test_an_invalid_belt_on_update_changes_nothing() -> None:
    """Un rechazo de datos no deja fila ni evento: la operacion es todo o nada."""
    async with competitor_lab_context() as lab:
        competitor = lab.active_competitors[2]
        before = await competitor_audit_rows(competitor)

        status_code, body = await call(
            "POST",
            profile_url(lab, competitor),
            token=lab.owner_token,
            key=f"k-{uuid4().hex}",
            payload=profile_body(date_of_birth="1990-01-01", sex="MALE", belt_id=999_999_999),
        )
        assert status_code == status.HTTP_400_BAD_REQUEST, body
        assert body["detail"] == INVALID_DETAIL
        assert await competitor_audit_rows(competitor) == before
        assert await profile_row(competitor) == {
            "date_of_birth": None,
            "sex": None,
            "belt_id": None,
            "managed_by_club_id": lab.club,
        }


@pytest.mark.asyncio(loop_scope="session")
async def test_a_foreign_competitor_is_not_found() -> None:
    """El identificador se busca acotado al tenant: ajeno e inexistente son indistinguibles."""
    async with competitor_lab_context() as lab:
        status_code, body = await call(
            "POST",
            profile_url(lab, lab.foreign_competitor),
            token=lab.owner_token,
            key=f"k-{uuid4().hex}",
            payload=profile_body(),
        )
        assert status_code == status.HTTP_404_NOT_FOUND, body
        assert body["detail"] == NOT_FOUND_DETAIL


@pytest.mark.asyncio(loop_scope="session")
async def test_the_profile_update_demands_a_session_and_a_relation_with_the_club() -> None:
    """Sin sesion no hay credencial; con sesion ajena, la denegacion no distingue el club."""
    async with competitor_lab_context() as lab:
        competitor = lab.active_competitors[0]
        anonymous, _ = await call(
            "POST",
            profile_url(lab, competitor),
            key=f"k-{uuid4().hex}",
            payload=profile_body(),
        )
        assert anonymous == status.HTTP_401_UNAUTHORIZED

        outsider, body = await call(
            "POST",
            profile_url(lab, competitor),
            token=lab.outsider_token,
            key=f"k-{uuid4().hex}",
            payload=profile_body(),
        )
        assert outsider == status.HTTP_401_UNAUTHORIZED, body


@pytest.mark.asyncio(loop_scope="session")
async def test_the_write_gate_rejects_before_consuming_the_key(
    flag_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con la bandera apagada el rechazo es anterior a la reserva de idempotencia."""
    async with competitor_lab_context() as lab:
        competitor = lab.active_competitors[0]
        key = f"k-{uuid4().hex}"
        monkeypatch.setattr(config, "f3_write_enabled", False)

        status_code, body = await call(
            "POST",
            profile_url(lab, competitor),
            token=lab.owner_token,
            key=key,
            payload=profile_body(sex="MALE"),
        )
        assert status_code == status.HTTP_503_SERVICE_UNAVAILABLE, body
        assert body["detail"] == F3_WRITE_DISABLED

        monkeypatch.setattr(config, "f3_write_enabled", True)
        retried, retried_body = await call(
            "POST",
            profile_url(lab, competitor),
            token=lab.owner_token,
            key=key,
            payload=profile_body(sex="MALE"),
        )
        assert retried == status.HTTP_200_OK, retried_body


@pytest.mark.asyncio(loop_scope="session")
async def test_the_same_key_repeats_the_operation_and_another_body_conflicts() -> None:
    """Misma clave y mismo cuerpo: un solo efecto. Misma clave y otro cuerpo: conflicto."""
    async with competitor_lab_context() as lab:
        competitor = lab.active_competitors[0]
        key = f"k-{uuid4().hex}"
        payload = profile_body(date_of_birth="1995-03-04", sex="FEMALE")

        first, body = await call(
            "POST",
            profile_url(lab, competitor),
            token=lab.owner_token,
            key=key,
            payload=payload,
        )
        assert first == status.HTTP_200_OK, body
        events_after_first = await competitor_audit_rows(competitor)

        replay, replay_body = await call(
            "POST",
            profile_url(lab, competitor),
            token=lab.owner_token,
            key=key,
            payload=payload,
        )
        assert replay == status.HTTP_200_OK, replay_body
        assert replay_body == body
        assert await competitor_audit_rows(competitor) == events_after_first

        conflict, conflict_body = await call(
            "POST",
            profile_url(lab, competitor),
            token=lab.owner_token,
            key=key,
            payload=profile_body(date_of_birth="1995-03-05", sex="FEMALE"),
        )
        assert conflict == status.HTTP_409_CONFLICT, conflict_body


@pytest.mark.asyncio(loop_scope="session")
async def test_the_read_routes_publish_the_sports_profile() -> None:
    """Listado y detalle publican el perfil y el club gestor, con proyeccion cerrada."""
    async with competitor_lab_context() as lab:
        belt = await insert_belt()
        try:
            created, body = await call(
                "POST",
                competitors_url(lab),
                token=lab.owner_token,
                key=f"k-{uuid4().hex}",
                payload={
                    "display_name": f"{lab.root_name}-lectura",
                    "date_of_birth": "1979-12-31",
                    "sex": "MALE",
                    "belt_id": belt,
                    "reason_code": CREATE_REASON,
                },
            )
            assert created == status.HTTP_201_CREATED, body

            detail_status, detail = await call(
                "GET", competitor_url(lab, body["id"]), token=lab.owner_token
            )
            assert detail_status == status.HTTP_200_OK, detail
            assert set(detail) == COMPETITOR_FIELDS, set(detail)
            assert detail["belt_id"] == belt and detail["sex"] == "MALE"

            list_status, listing = await call("GET", competitors_url(lab), token=lab.owner_token)
            assert list_status == status.HTTP_200_OK, listing
            published = [item for item in listing["competitors"] if item["id"] == body["id"]]
            assert len(published) == 1
            assert set(published[0]) == COMPETITOR_LISTING_FIELDS, set(published[0])
            assert published[0]["date_of_birth"] == "1979-12-31"
            # Sin afiliacion abierta el listado lo dice explicitamente: no se inventa club.
            assert published[0]["affiliation_mode"] == "INDEPENDENT", published[0]
            assert published[0]["sports_club_id"] is None
            assert published[0]["sports_club_name"] is None
        finally:
            await delete_belts((belt,))
