# pylint: disable=redefined-outer-name  # `lab` es el fixture de este modulo.
"""Contrato HTTP de la **clasificacion previa** al alta (S4.8d §2, LAB).

Pruebas negras contra la aplicacion real con sesiones reales (OWNER y COLLABORATOR del tenant, un
usuario de otro club y una peticion anonima) y categorias reales sembradas en ``stage_items``. La
base es la de CI y la app se levanta en el mismo proceso de pruebas: ni mocks del motor ni del
protocolo de idempotencia.

Lo que se demuestra aqui:

* ``POST .../registrations/classification-preview`` responde **el mismo contrato** que la
  clasificacion de una inscripcion (compatibles, recomendada solo si es unica, bloqueos y avisos) y
  lo calcula el **mismo** motor: la respuesta y los codigos son los de a-2, no una copia. La unica
  diferencia es el veredicto de la **categoria elegida**: la previa no lo publica porque todavia no
  hay ninguna elegida (la clasificacion de una inscripcion existente sigue publicandolo igual).
* Funciona **antes** de que exista la inscripcion y **no la crea**: el laboratorio no tiene ninguna
  inscripcion en el torneo y sigue sin tenerla despues.
* Es una lectura aunque viaje como POST: no escribe la fila, no audita, no reserva clave de
  idempotencia y sigue respondiendo con la escritura F3 **apagada**. Tampoco lleva la puerta.
* El peso y la modalidad se validan **igual** que en el alta (misma regla, un solo dueno), y su
  ausencia es informacion para el formulario (``MISSING_WEIGHT``/``MISSING_MODALITY``), no un error.
* El ambito es el tenant del torneo: el competidor tiene que ser una identidad vigente **de ese**
  tenant; otro club responde igual que un competidor inexistente.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Final
from uuid import uuid4
from zoneinfo import ZoneInfo

import aiohttp
import pytest
import pytest_asyncio
from heliclockter import datetime_utc

from bracket.config import config
from bracket.database import database
from bracket.logic.classification.vocabulary import ClassificationBlocker, ClassificationWarning
from bracket.models.db.club import ClubInsertable
from bracket.models.db.user_x_club import UserXClubInsertable, UserXClubRelation
from bracket.utils.dummy_records import DUMMY_TOURNAMENT
from bracket.utils.http import HTTPMethod
from bracket.utils.id_types import (
    ClubId,
    CompetitorId,
    StageId,
    StageItemId,
    TournamentId,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.mocks import get_mock_token, get_mock_user
from tests.integration_tests.sql import (
    inserted_club,
    inserted_tournament,
    inserted_user,
    inserted_user_x_club,
)

JSON_CONTENT_TYPE = "application/json"
NOT_AUTHENTICATED = {"detail": "Not authenticated"}
NOT_FOUND = {"detail": "Not Found"}
REQUEST_INVALID = {"detail": "Registration request is invalid"}

_START_TIME = datetime_utc(2026, 10, 13, 9, 0, 0, tzinfo=ZoneInfo("UTC"))
_CREATED = datetime_utc(2026, 1, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
_DOB = date(1995, 5, 5)
_WEIGHT = Decimal("74.50")

#: Los cuatro campos del contrato de clasificacion (§6). La previa publica **estos**.
CLASSIFICATION_FIELDS: Final = {
    "compatible_categories",
    "recommended_category",
    "blockers",
    "warnings",
}
#: Vocabularios cerrados: la previa no puede inventarse codigos propios.
ENGINE_BLOCKERS: Final = {blocker.value for blocker in ClassificationBlocker}
ENGINE_WARNINGS: Final = {warning.value for warning in ClassificationWarning}

_INSERT_STAGE = """
    INSERT INTO stages (tournament_id, name, created, is_active)
    VALUES (:tournament_id, :name, :created, true) RETURNING id
"""
_INSERT_RANKING = """
    INSERT INTO rankings (
        created, tournament_id, position, win_points, draw_points, loss_points, add_score_points
    ) VALUES (:created, :tournament_id, 1, 3, 1, 0, false) RETURNING id
"""
_INSERT_BELT = """
    INSERT INTO belts (code, label, rank, scope) VALUES (:code, :label, :rank, :scope)
    ON CONFLICT DO NOTHING
"""
_INSERT_STAGE_ITEM = """
    INSERT INTO stage_items (
        name, stage_id, team_count, ranking_id, type, sex_constraint, age_min, age_max,
        weight_min_kg, weight_max_kg, modality, belt_min_id, belt_max_id
    ) VALUES (
        :name, :stage_id, 2, :ranking_id, CAST('ROUND_ROBIN' AS stage_type),
        CAST(:sex_constraint AS sex_constraint), :age_min, :age_max,
        :weight_min_kg, :weight_max_kg, CAST(:modality AS modality), :belt_min_id, :belt_max_id
    ) RETURNING id
"""
_INSERT_COMPETITOR = """
    INSERT INTO competitors (
        display_name, managed_by_club_id, active, created, date_of_birth, sex, belt_id
    ) VALUES (
        :display_name, :managed_by_club_id, true, :created, :date_of_birth,
        CAST(:sex AS competitor_sex), :belt_id
    ) RETURNING id
"""
_DELETE_COMPETITORS = "DELETE FROM competitors WHERE id = ANY(:ids)"
_DELETE_STAGE_ITEMS = "DELETE FROM stage_items WHERE id = ANY(:ids)"
_DELETE_RANKINGS = "DELETE FROM rankings WHERE id = ANY(:ids)"
_DELETE_STAGES = "DELETE FROM stages WHERE id = ANY(:ids)"
_DELETE_AUDIT = "DELETE FROM domain_change_log WHERE tenant_club_id = :club"


@dataclass
class PreviewLab:  # pylint: disable=too-many-instance-attributes
    """Tenant con torneo, categorias y competidor **sin** inscripcion, mas sesiones reales."""

    tenant: ClubId
    tournament: TournamentId
    owner_id: int
    owner_token: str
    collaborator_token: str
    foreign_competitor: CompetitorId
    outsider_token: str
    competitor: CompetitorId
    blue_belt: int
    stage: StageId
    ranking: int
    categories: dict[str, StageItemId] = field(default_factory=dict)

    def key(self, name: str) -> str:
        """Clave de la categoria sembrada con ese nombre (contrato ``si:<id>``)."""
        return f"si:{self.categories[name]}"


async def _insert(query: str, **values: object) -> int:
    return int(await database.fetch_val(query=query, values=values))


async def _belt_id(scope: str, code: str, label: str, rank: int) -> int:
    await database.execute(
        query=_INSERT_BELT,
        values={"code": code, "label": label, "rank": rank, "scope": scope},
    )
    return int(
        await database.fetch_val(
            query="SELECT id FROM belts WHERE scope = :scope AND code = :code",
            values={"scope": scope, "code": code},
        )
    )


async def _add_category(
    lab: PreviewLab,
    name: str,
    *,
    sex_constraint: str | None = None,
    age_min: int | None = None,
    age_max: int | None = None,
    weight_min_kg: Decimal | None = None,
    weight_max_kg: Decimal | None = None,
    modality: str | None = None,
    belt_min_id: int | None = None,
    belt_max_id: int | None = None,
) -> StageItemId:
    """Siembra una categoria con sus reglas y devuelve su ``stage_item_id``."""
    stage_item_id = StageItemId(
        await _insert(
            _INSERT_STAGE_ITEM,
            name=name,
            stage_id=lab.stage,
            ranking_id=lab.ranking,
            sex_constraint=sex_constraint,
            age_min=age_min,
            age_max=age_max,
            weight_min_kg=weight_min_kg,
            weight_max_kg=weight_max_kg,
            modality=modality,
            belt_min_id=belt_min_id,
            belt_max_id=belt_max_id,
        )
    )
    lab.categories[name] = stage_item_id
    return stage_item_id


async def _add_legacy_category(lab: PreviewLab, name: str = "heredada") -> StageItemId:
    """Categoria sin reglas: admite con aviso, no bloquea (compatibilidad heredada)."""
    return await _add_category(lab, name)


@asynccontextmanager
async def _lab_context() -> AsyncIterator[PreviewLab]:
    now = datetime_utc.now()
    suffix = uuid4().hex[:12]
    async with AsyncExitStack() as stack:
        club = await stack.enter_async_context(
            inserted_club(ClubInsertable(name=f"s48d-pre-{suffix}", created=now))
        )
        # Segundo club, solo para probar que un competidor de otro tenant no se clasifica aqui.
        other_club = await stack.enter_async_context(
            inserted_club(ClubInsertable(name=f"s48d-pre-otro-{suffix}", created=now))
        )
        tournament = await stack.enter_async_context(
            inserted_tournament(
                DUMMY_TOURNAMENT.model_copy(
                    update={"club_id": club.id, "dashboard_endpoint": f"s48d-pre-{suffix}"}
                )
            )
        )
        owner = await stack.enter_async_context(inserted_user(get_mock_user()))
        collaborator = await stack.enter_async_context(inserted_user(get_mock_user()))
        outsider = await stack.enter_async_context(inserted_user(get_mock_user()))
        for user, relation, membership_club in (
            (owner, UserXClubRelation.OWNER, club.id),
            (collaborator, UserXClubRelation.COLLABORATOR, club.id),
            (outsider, UserXClubRelation.COLLABORATOR, other_club.id),
        ):
            await stack.enter_async_context(
                inserted_user_x_club(
                    UserXClubInsertable(user_id=user.id, club_id=membership_club, relation=relation)
                )
            )

        # Fecha de inicio fija: la edad se calcula contra ella, no contra el reloj.
        await database.execute(
            query="UPDATE tournaments SET start_time = :start WHERE id = :id",
            values={"start": _START_TIME, "id": tournament.id},
        )
        stage = StageId(
            await _insert(_INSERT_STAGE, tournament_id=tournament.id, name="fase", created=_CREATED)
        )
        ranking = await _insert(_INSERT_RANKING, tournament_id=tournament.id, created=_CREATED)
        blue = await _belt_id("ADULT", "BLUE", "Azul", 2)

        lab = PreviewLab(
            tenant=club.id,
            tournament=tournament.id,
            owner_id=owner.id,
            owner_token=get_mock_token(owner.email),
            collaborator_token=get_mock_token(collaborator.email),
            outsider_token=get_mock_token(outsider.email),
            foreign_competitor=CompetitorId(0),
            competitor=CompetitorId(0),
            blue_belt=blue,
            stage=stage,
            ranking=ranking,
        )
        lab.competitor = CompetitorId(
            await _insert(
                _INSERT_COMPETITOR,
                display_name=f"competidor-{suffix}",
                managed_by_club_id=club.id,
                created=_CREATED,
                date_of_birth=_DOB,
                sex="MALE",
                belt_id=blue,
            )
        )
        lab.foreign_competitor = CompetitorId(
            await _insert(
                _INSERT_COMPETITOR,
                display_name=f"competidor-otro-{suffix}",
                managed_by_club_id=other_club.id,
                created=_CREATED,
                date_of_birth=_DOB,
                sex="MALE",
                belt_id=blue,
            )
        )
        # Categoria de referencia: encaja exactamente con el competidor y con lo declarado.
        await _add_category(
            lab,
            "solo",
            sex_constraint="MALE",
            age_min=18,
            age_max=39,
            weight_min_kg=Decimal("71.00"),
            weight_max_kg=Decimal("77.00"),
            modality="GI",
            belt_min_id=blue,
            belt_max_id=blue,
        )
        try:
            yield lab
        finally:
            await database.execute(
                query=_DELETE_COMPETITORS,
                values={"ids": [lab.competitor, lab.foreign_competitor]},
            )
            await database.execute(
                query=_DELETE_STAGE_ITEMS, values={"ids": list(lab.categories.values())}
            )
            await database.execute(query=_DELETE_RANKINGS, values={"ids": [lab.ranking]})
            await database.execute(query=_DELETE_STAGES, values={"ids": [lab.stage]})
            # La auditoria referencia al club: se borra antes que el club.
            await database.execute(query=_DELETE_AUDIT, values={"club": lab.tenant})


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def lab() -> AsyncIterator[PreviewLab]:
    """Laboratorio por caso: filas propias, recuentos absolutos y **sin** inscripciones."""
    async with _lab_context() as preview_lab:
        yield preview_lab


@pytest.fixture(scope="module", autouse=True)
def idempotency_secret() -> Iterator[None]:
    patcher = pytest.MonkeyPatch()
    patcher.setattr(config, "idempotency_hmac_key", "synthetic-laboratory-secret-not-deployment")
    patcher.setattr(config, "idempotency_hmac_key_version", "v1")
    yield
    patcher.undo()


def _preview_url(lab: PreviewLab) -> str:
    return f"tournaments/{lab.tournament}/registrations/classification-preview"


async def _call_json(
    method: HTTPMethod,
    url: str,
    *,
    token: str | None = None,
    payload: bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = JSON_CONTENT_TYPE
    async with aiohttp.ClientSession() as session:
        async with session.request(
            method=str(method.value),
            url=get_root_uvicorn_url() + url,
            data=payload,
            headers=headers,
        ) as response:
            text = await response.text()
            return response.status, (json.loads(text) if text else {})


async def _preview(
    lab: PreviewLab,
    *,
    competitor_id: int | None = None,
    weight: Decimal | None = _WEIGHT,
    modality: str | None = "GI",
    token: str | None = None,
) -> tuple[int, dict[str, Any]]:
    body = {
        "competitor_id": lab.competitor if competitor_id is None else competitor_id,
        "declared_weight_kg": None if weight is None else str(weight),
        "modality": modality,
    }
    return await _call_json(
        HTTPMethod.POST,
        _preview_url(lab),
        token=token or lab.owner_token,
        payload=json.dumps(body).encode(),
    )


async def _counts(lab: PreviewLab) -> dict[str, int]:
    """Todo lo que la previa **no** debe tocar: filas, auditoria y reservas de idempotencia."""
    return {
        "registrations": int(
            await database.fetch_val(
                query="SELECT count(*) FROM tournament_registrations WHERE tournament_id = :id",
                values={"id": lab.tournament},
            )
        ),
        "audit": int(
            await database.fetch_val(
                query="SELECT count(*) FROM domain_change_log WHERE tenant_club_id = :club",
                values={"club": lab.tenant},
            )
        ),
        "reservations": int(
            await database.fetch_val(
                query=(
                    "SELECT count(*) FROM domain_idempotency_keys WHERE request_path LIKE :path"
                ),
                values={"path": f"%/tournaments/{lab.tournament}/registrations%"},
            )
        ),
    }


# --- §2: mismo contrato y mismo motor que la clasificacion de una inscripcion -----------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_preview_returns_the_classification_contract(lab: PreviewLab) -> None:
    """Los cuatro campos del contrato (§6), sin codigos que el motor no publique."""
    status_code, body = await _preview(lab)

    assert status_code == 200, body
    assert set(body) == CLASSIFICATION_FIELDS
    assert set(body["blockers"]) <= ENGINE_BLOCKERS
    assert set(body["warnings"]) <= ENGINE_WARNINGS


@pytest.mark.asyncio(loop_scope="session")
async def test_the_preview_does_not_publish_the_chosen_category_verdict(lab: PreviewLab) -> None:
    """Sin categoria elegida no hay veredicto de categoria elegida (S4.8d §2).

    ``CATEGORY_NOT_RESOLVED`` describe una categoria **declarada** que no resuelve. La previa corre
    antes de elegir, asi que no publica ese veredicto: si lo publicara, todo formulario arrancaria
    con un bloqueo que no describe ningun dato declarado.
    """
    _, body = await _preview(lab)

    assert "CATEGORY_NOT_RESOLVED" not in body["blockers"]
    assert body["blockers"] == []


@pytest.mark.asyncio(loop_scope="session")
async def test_the_preview_hides_no_other_blocker(lab: PreviewLab) -> None:
    """Excluir ese veredicto no oculta los bloqueos que si salen de los datos declarados."""
    _, body = await _preview(lab, modality="NO_GI")

    assert "CATEGORY_NOT_RESOLVED" not in body["blockers"]
    assert "NO_COMPATIBLE_CATEGORY" in body["blockers"]
    assert "CATEGORY_INCOMPATIBLE" in body["blockers"]


@pytest.mark.asyncio(loop_scope="session")
async def test_the_single_compatible_category_is_the_recommendation(lab: PreviewLab) -> None:
    """Con exactamente una compatible, esa es la recomendada y no hay bloqueos."""
    _, body = await _preview(lab)

    assert [verdict["category_key"] for verdict in body["compatible_categories"]] == [
        lab.key("solo")
    ]
    assert body["recommended_category"]["category_key"] == lab.key("solo")
    assert body["recommended_category"]["category_label"] == "solo"
    assert body["blockers"] == []
    assert body["warnings"] == []


@pytest.mark.asyncio(loop_scope="session")
async def test_several_compatible_categories_have_no_recommendation(lab: PreviewLab) -> None:
    """Con una heredada sin reglas hay dos compatibles y **nada** recomendado: decide el usuario."""
    await _add_legacy_category(lab)
    _, body = await _preview(lab)

    compatible = {verdict["category_key"] for verdict in body["compatible_categories"]}
    assert compatible == {lab.key("solo"), lab.key("heredada")}
    assert body["recommended_category"] is None
    assert "MULTIPLE_COMPATIBLE_CATEGORIES" in body["warnings"]
    assert body["blockers"] == []


@pytest.mark.asyncio(loop_scope="session")
async def test_no_compatible_category_blocks(lab: PreviewLab) -> None:
    """Ninguna categoria admite: ``NO_COMPATIBLE_CATEGORY`` mas el motivo concreto."""
    await database.execute(
        query="UPDATE stage_items SET weight_min_kg = 60, weight_max_kg = 65 WHERE id = :id",
        values={"id": lab.categories["solo"]},
    )
    _, body = await _preview(lab)

    assert body["compatible_categories"] == []
    assert body["recommended_category"] is None
    assert "NO_COMPATIBLE_CATEGORY" in body["blockers"]
    assert "CATEGORY_INCOMPATIBLE" in body["blockers"]


# --- §2: lo que falta es informacion, no un error --------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_missing_weight_is_reported_as_missing_not_incompatible(lab: PreviewLab) -> None:
    """Sin peso todavia, la categoria que lo exige bloquea con ``MISSING_WEIGHT``."""
    _, body = await _preview(lab, weight=None)

    assert "MISSING_WEIGHT" in body["blockers"]
    assert "CATEGORY_INCOMPATIBLE" not in body["blockers"]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_missing_modality_is_reported_as_missing(lab: PreviewLab) -> None:
    """Sin modalidad todavia, el bloqueo es ``MISSING_MODALITY`` (la categoria exige GI)."""
    _, body = await _preview(lab, modality=None)

    assert "MISSING_MODALITY" in body["blockers"]
    assert "CATEGORY_INCOMPATIBLE" not in body["blockers"]


@pytest.mark.asyncio(loop_scope="session")
async def test_an_incompatible_modality_blocks(lab: PreviewLab) -> None:
    """Con NO_GI declarado, la categoria GI no admite: bloqueo de incompatibilidad."""
    _, body = await _preview(lab, modality="NO_GI")

    assert body["compatible_categories"] == []
    assert "NO_COMPATIBLE_CATEGORY" in body["blockers"]


@pytest.mark.asyncio(loop_scope="session")
async def test_an_out_of_range_weight_blocks(lab: PreviewLab) -> None:
    """Un peso fuera del rango de la unica categoria no la hace compatible."""
    _, body = await _preview(lab, weight=Decimal("99.00"))

    assert body["compatible_categories"] == []
    assert "NO_COMPATIBLE_CATEGORY" in body["blockers"]


# --- §2: lectura de verdad --------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_preview_works_before_any_registration_exists(lab: PreviewLab) -> None:
    """El laboratorio no tiene inscripciones: la previa clasifica igual y no crea ninguna."""
    counts = await _counts(lab)
    assert counts["registrations"] == 0

    _, body = await _preview(lab)

    assert body["recommended_category"]["category_key"] == lab.key("solo")
    assert await _counts(lab) == counts


@pytest.mark.asyncio(loop_scope="session")
async def test_the_preview_does_not_write_audit_or_reserve(lab: PreviewLab) -> None:
    """Dos previas identicas no dejan rastro y devuelven exactamente lo mismo."""
    before = await _counts(lab)
    first = await _preview(lab)
    second = await _preview(lab)

    assert first == second
    assert await _counts(lab) == before


@pytest.mark.asyncio(loop_scope="session")
async def test_the_preview_still_answers_with_the_write_gate_off(
    lab: PreviewLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con F3_WRITE_ENABLED apagado la previa sigue respondiendo: no es una escritura."""
    monkeypatch.setattr(config, "f3_write_enabled", False)

    status_code, body = await _preview(lab)

    assert status_code == 200, body
    assert body["recommended_category"]["category_key"] == lab.key("solo")


def test_the_preview_route_does_not_carry_the_write_gate() -> None:
    """La puerta de escritura alcanza exactamente las rutas de escritura: la previa no esta.

    Se busca en la **app** entera (no en un router concreto) para que el contrato no dependa de en
    que modulo viva la ruta: un ``POST`` nuevo bajo este camino no puede heredar la puerta por
    accidente ni colarse como escritura por parecerse al alta.
    """
    from fastapi.routing import APIRoute

    from bracket.app import app
    from bracket.routes.f3_write_gate import require_f3_write_enabled

    preview = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == "/tournaments/{tournament_id}/registrations/classification-preview"
    )

    assert preview.methods == {"POST"}
    assert require_f3_write_enabled not in [
        dependency.dependency for dependency in preview.dependencies
    ]


# --- §2: tenant y RBAC ------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_preview_is_available_to_a_collaborator(lab: PreviewLab) -> None:
    """Clasificar antes de inscribir es una lectura: no exige OWNER."""
    status_code, body = await _preview(lab, token=lab.collaborator_token)

    assert status_code == 200, body
    assert body["recommended_category"]["category_key"] == lab.key("solo")


@pytest.mark.asyncio(loop_scope="session")
async def test_a_foreign_tenant_gets_the_same_answer_as_an_unknown_tournament(
    lab: PreviewLab,
) -> None:
    """Torneo de otro club y torneo inexistente comparten el mismo 404, sin revelar existencia."""
    other_tournament = TournamentId(999_999_999)

    foreign_status, foreign_body = await _preview(lab, token=lab.outsider_token)
    unknown_status, unknown_body = await _call_json(
        HTTPMethod.POST,
        f"tournaments/{other_tournament}/registrations/classification-preview",
        token=lab.outsider_token,
        payload=json.dumps({"competitor_id": lab.competitor}).encode(),
    )

    assert (foreign_status, foreign_body) == (404, NOT_FOUND)
    assert (unknown_status, unknown_body) == (404, NOT_FOUND)


@pytest.mark.asyncio(loop_scope="session")
async def test_an_anonymous_request_is_unauthenticated(lab: PreviewLab) -> None:
    status_code, body = await _call_json(
        HTTPMethod.POST,
        _preview_url(lab),
        payload=json.dumps({"competitor_id": lab.competitor, "modality": "GI"}).encode(),
    )

    assert status_code == 401
    assert body == NOT_AUTHENTICATED


@pytest.mark.asyncio(loop_scope="session")
async def test_a_competitor_of_another_club_is_not_classified(lab: PreviewLab) -> None:
    """El competidor tiene que ser una identidad vigente del tenant: otro club responde 404."""
    status_code, body = await _preview(lab, competitor_id=lab.foreign_competitor)

    assert status_code == 404, body
    assert body == NOT_FOUND


@pytest.mark.asyncio(loop_scope="session")
async def test_the_contract_does_not_accept_tenant_actor_or_state(lab: PreviewLab) -> None:
    """El cuerpo son tres datos: cualquier intento de colar tenant/estado se rechaza."""
    status_code, _ = await _call_json(
        HTTPMethod.POST,
        _preview_url(lab),
        token=lab.owner_token,
        payload=json.dumps(
            {
                "competitor_id": lab.competitor,
                "declared_weight_kg": str(_WEIGHT),
                "modality": "GI",
                "tenant_club_id": int(lab.tenant),
                "status": "CONFIRMED",
            }
        ).encode(),
    )

    assert status_code == 422


# --- §2: el peso se valida igual que en el alta ----------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_an_invalid_weight_is_rejected_like_the_create(lab: PreviewLab) -> None:
    """Un peso imposible (0, negativo o fuera de escala) es 400, igual que al inscribir."""
    for weight in (Decimal("0"), Decimal("-3.00"), Decimal("5000.00")):
        status_code, body = await _preview(lab, weight=weight)

        assert status_code == 400, (weight, body)
        assert body == REQUEST_INVALID


@pytest.mark.asyncio(loop_scope="session")
async def test_a_non_finite_weight_does_not_reach_the_domain(lab: PreviewLab) -> None:
    """``NaN``/``Infinity`` los rechaza ya el esquema (422), igual que en el alta."""
    for weight in (Decimal("NaN"), Decimal("Infinity")):
        status_code, body = await _preview(lab, weight=weight)

        assert status_code == 422, (weight, body)
        assert body["detail"][0]["type"] == "finite_number"


@pytest.mark.asyncio(loop_scope="session")
async def test_a_weight_that_is_not_a_number_does_not_reach_the_domain(lab: PreviewLab) -> None:
    """Un peso que no es numero lo rechaza el esquema (422), igual que en el alta."""
    status_code, _ = await _call_json(
        HTTPMethod.POST,
        _preview_url(lab),
        token=lab.owner_token,
        payload=json.dumps(
            {
                "competitor_id": lab.competitor,
                "declared_weight_kg": "no-es-un-peso",
                "modality": "GI",
            }
        ).encode(),
    )

    assert status_code == 422


@pytest.mark.asyncio(loop_scope="session")
async def test_an_invalid_modality_is_unprocessable(lab: PreviewLab) -> None:
    """La modalidad combinada no existe para el contrato: 422 del esquema."""
    status_code, _ = await _call_json(
        HTTPMethod.POST,
        _preview_url(lab),
        token=lab.owner_token,
        payload=json.dumps(
            {
                "competitor_id": lab.competitor,
                "declared_weight_kg": str(_WEIGHT),
                "modality": "BOTH",
            }
        ).encode(),
    )

    assert status_code == 422


@pytest.mark.asyncio(loop_scope="session")
async def test_an_unknown_competitor_is_not_found(lab: PreviewLab) -> None:
    status_code, body = await _preview(lab, competitor_id=999_999_999)

    assert status_code == 404, body
    assert body == NOT_FOUND


@pytest.mark.asyncio(loop_scope="session")
async def test_the_preview_does_not_touch_the_competitor_profile(lab: PreviewLab) -> None:
    """Clasificar no persiste lo declarado: el perfil del competidor queda intacto."""
    _, preview = await _preview(lab)

    assert preview["recommended_category"]["category_key"] == lab.key("solo")
    assert [verdict["category_key"] for verdict in preview["compatible_categories"]] == [
        lab.key("solo")
    ]
    row = await database.fetch_one(
        query="SELECT date_of_birth, sex, belt_id FROM competitors WHERE id = :id",
        values={"id": lab.competitor},
    )
    assert row is not None
    assert dict(row._mapping) == {"date_of_birth": _DOB, "sex": "MALE", "belt_id": lab.blue_belt}
