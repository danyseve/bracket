# pylint: disable=redefined-outer-name  # `lab` es el fixture de este modulo.
"""Contrato HTTP de clasificacion, asignacion y override de categoria (S4.8a-3 §6/§7/§8, LAB).

Pruebas **negras** contra la aplicacion real con sesiones reales (OWNER, COLLABORATOR, anonimo) y
categorias reales sembradas en ``stage_items``. La base es la de CI y la app se levanta en el mismo
proceso de pruebas: no hay mocks del motor ni del protocolo.

Lo que se demuestra aqui:

* §6: ``GET .../classification`` **reutiliza** el motor de a-2 (mismos codigos, misma recomendacion)
  y es una lectura: no escribe, no audita y no reserva idempotencia. La recomendacion solo existe
  cuando hay **exactamente una** categoria compatible.
* §7: ``POST .../category`` asigna la recomendada cuando es unica, exige ``category_key`` explicita
  cuando hay varias y rechaza una categoria no compatible sin override.
* §8: ``override=true`` es **OWNER-only**, exige ``reason_code`` y audita el cambio; sin cambio de
  categoria no hay evento, y el RBAC general no cambia.

El formato de ``category_key`` es ``si:<stage_item_id>`` (contrato del motor de a-2); la etiqueta
sale de ``stage_items.name``.
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
from bracket.logic.classification.vocabulary import ClassificationBlocker
from bracket.models.db.club import ClubInsertable
from bracket.models.db.user_x_club import UserXClubInsertable, UserXClubRelation
from bracket.routes.domain_idempotency import IDEMPOTENCY_KEY_HEADER
from bracket.utils.dummy_records import DUMMY_TOURNAMENT
from bracket.utils.http import HTTPMethod
from bracket.utils.id_types import (
    ClubId,
    CompetitorId,
    StageId,
    StageItemId,
    TournamentId,
    TournamentRegistrationId,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.mocks import get_mock_token, get_mock_user
from tests.integration_tests.sql import (
    inserted_club,
    inserted_tournament,
    inserted_user,
    inserted_user_x_club,
)

SYNTHETIC_SECRET = "synthetic-laboratory-secret-not-for-deployment"
JSON_CONTENT_TYPE = "application/json"
NOT_AUTHENTICATED = {"detail": "Not authenticated"}
NOT_FOUND = {"detail": "Not Found"}
ROLE_NOT_ALLOWED = {"detail": "ROLE_NOT_ALLOWED"}
REQUEST_INVALID = {"detail": "Registration request is invalid"}
F3_WRITE_DISABLED_BODY = {"detail": "F3_WRITE_DISABLED"}

_START_TIME = datetime_utc(2026, 10, 13, 9, 0, 0, tzinfo=ZoneInfo("UTC"))
_CREATED = datetime_utc(2026, 1, 1, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
_DOB = date(1995, 5, 5)
_WEIGHT = Decimal("74.50")

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
_INSERT_REGISTRATION = """
    INSERT INTO tournament_registrations (
        tournament_id, competitor_id, identity_status, representation, sports_club_id,
        affiliation_id, category_key, category_label, competitor_name_snapshot,
        sports_club_name_snapshot, status, revision, corrects_registration_id, created,
        declared_weight_kg, modality
    ) VALUES (
        :tournament_id, :competitor_id, CAST('VERIFIED' AS registration_identity_status),
        CAST('INDEPENDENT' AS registration_representation), NULL, NULL, :category_key,
        :category_label, :competitor_name_snapshot, NULL, CAST('CONFIRMED' AS registration_status),
        1, NULL, :created, :declared_weight_kg, CAST(:modality AS modality)
    ) RETURNING id
"""
_DELETE_REGISTRATIONS = "DELETE FROM tournament_registrations WHERE id = ANY(:ids)"
_DELETE_COMPETITORS = "DELETE FROM competitors WHERE id = ANY(:ids)"
_DELETE_STAGE_ITEMS = "DELETE FROM stage_items WHERE id = ANY(:ids)"
_DELETE_RANKINGS = "DELETE FROM rankings WHERE id = ANY(:ids)"
_DELETE_STAGES = "DELETE FROM stages WHERE id = ANY(:ids)"
_DELETE_AUDIT = "DELETE FROM domain_change_log WHERE tenant_club_id = :club"

#: Codigos que publica el motor (§6) y que el readiness (§9) reutiliza.
ENGINE_BLOCKERS: Final = {blocker.value for blocker in ClassificationBlocker}


@dataclass
class CategoryLab:  # pylint: disable=too-many-instance-attributes
    """Tenant con torneo propio, categorias, competidor confirmado y sesiones reales."""

    tenant: ClubId
    tournament: TournamentId
    owner_id: int
    owner_token: str
    collaborator_token: str
    outsider_token: str
    competitor: CompetitorId
    registration: TournamentRegistrationId
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
    lab: CategoryLab,
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


async def _add_incompatible_sex_category(lab: CategoryLab, name: str = "otra") -> StageItemId:
    """Categoria resuelta pero **no** compatible: exige sexo FEMALE y el competidor es MALE."""
    return await _add_category(lab, name, sex_constraint="FEMALE")


@asynccontextmanager
async def _lab_context() -> AsyncIterator[CategoryLab]:
    now = datetime_utc.now()
    suffix = uuid4().hex[:12]
    async with AsyncExitStack() as stack:
        club = await stack.enter_async_context(
            inserted_club(ClubInsertable(name=f"s48a3-cls-{suffix}", created=now))
        )
        tournament = await stack.enter_async_context(
            inserted_tournament(
                DUMMY_TOURNAMENT.model_copy(
                    update={"club_id": club.id, "dashboard_endpoint": f"s48a3-cls-{suffix}"}
                )
            )
        )
        owner = await stack.enter_async_context(inserted_user(get_mock_user()))
        collaborator = await stack.enter_async_context(inserted_user(get_mock_user()))
        outsider = await stack.enter_async_context(inserted_user(get_mock_user()))
        for user, relation in (
            (owner, UserXClubRelation.OWNER),
            (collaborator, UserXClubRelation.COLLABORATOR),
            (outsider, UserXClubRelation.COLLABORATOR),
        ):
            await stack.enter_async_context(
                inserted_user_x_club(
                    UserXClubInsertable(user_id=user.id, club_id=club.id, relation=relation)
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

        lab = CategoryLab(
            tenant=club.id,
            tournament=tournament.id,
            owner_id=owner.id,
            owner_token=get_mock_token(owner.email),
            collaborator_token=get_mock_token(collaborator.email),
            outsider_token=get_mock_token(outsider.email),
            competitor=CompetitorId(0),
            registration=TournamentRegistrationId(0),
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
        # Categoria de referencia que encaja exactamente con el competidor y la inscripcion.
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
        lab.registration = TournamentRegistrationId(
            await _insert(
                _INSERT_REGISTRATION,
                tournament_id=tournament.id,
                competitor_id=lab.competitor,
                category_key=lab.key("solo"),
                category_label="solo",
                competitor_name_snapshot=f"competidor-{suffix}",
                created=_CREATED,
                declared_weight_kg=_WEIGHT,
                modality="GI",
            )
        )
        try:
            yield lab
        finally:
            await database.execute(query=_DELETE_REGISTRATIONS, values={"ids": [lab.registration]})
            await database.execute(query=_DELETE_COMPETITORS, values={"ids": [lab.competitor]})
            await database.execute(
                query=_DELETE_STAGE_ITEMS, values={"ids": list(lab.categories.values())}
            )
            await database.execute(query=_DELETE_RANKINGS, values={"ids": [lab.ranking]})
            await database.execute(query=_DELETE_STAGES, values={"ids": [lab.stage]})
            # La auditoria del cambio de categoria referencia al club: se borra antes que el club.
            await database.execute(query=_DELETE_AUDIT, values={"club": lab.tenant})


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def lab() -> AsyncIterator[CategoryLab]:
    """Laboratorio por caso: filas propias y recuentos absolutos."""
    async with _lab_context() as category_lab:
        yield category_lab


@pytest.fixture(scope="module", autouse=True)
def idempotency_secret() -> Iterator[None]:
    patcher = pytest.MonkeyPatch()
    patcher.setattr(config, "idempotency_hmac_key", SYNTHETIC_SECRET)
    patcher.setattr(config, "idempotency_hmac_key_version", "v1")
    yield
    patcher.undo()


def _new_key() -> str:
    return f"k-{uuid4().hex}"


def _classification_url(lab: CategoryLab) -> str:
    return f"tournaments/{lab.tournament}/registrations/{lab.registration}/classification"


def _category_url(lab: CategoryLab) -> str:
    return f"tournaments/{lab.tournament}/registrations/{lab.registration}/category"


async def _call_json(
    method: HTTPMethod,
    url: str,
    *,
    token: str | None = None,
    key: str | None = None,
    payload: bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if key is not None:
        headers[IDEMPOTENCY_KEY_HEADER] = key
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


async def _classify(lab: CategoryLab, token: str | None = None) -> dict[str, Any]:
    status_code, body = await _call_json(
        HTTPMethod.GET, _classification_url(lab), token=token or lab.owner_token
    )
    assert status_code == 200, body
    return body


async def _assign(
    lab: CategoryLab,
    *,
    body: dict[str, Any],
    token: str | None = None,
    key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    return await _call_json(
        HTTPMethod.POST,
        _category_url(lab),
        token=token or lab.owner_token,
        key=key or _new_key(),
        payload=json.dumps(body).encode(),
    )


async def _clear_category(lab: CategoryLab) -> None:
    """Deja la inscripcion sin categoria elegida, como antes de asignar."""
    await database.execute(
        query=(
            "UPDATE tournament_registrations SET category_key = NULL, category_label = NULL "
            "WHERE id = :id"
        ),
        values={"id": lab.registration},
    )


async def _registration_row(lab: CategoryLab) -> dict[str, Any]:
    record = await database.fetch_one(
        query="SELECT category_key, category_label FROM tournament_registrations WHERE id = :id",
        values={"id": lab.registration},
    )
    assert record is not None
    return dict(record._mapping)


async def _category_audit(lab: CategoryLab) -> list[dict[str, Any]]:
    records = await database.fetch_all(
        query=(
            "SELECT action, reason_code, actor_user_id, changed_fields FROM domain_change_log "
            "WHERE entity = 'tournament_registration' AND entity_id = :id ORDER BY id"
        ),
        values={"id": lab.registration},
    )
    return [dict(record._mapping) for record in records]


async def _reservations(lab: CategoryLab) -> int:
    return int(
        await database.fetch_val(
            query="SELECT count(*) FROM domain_idempotency_keys WHERE request_path LIKE :path",
            values={"path": f"%/registrations/{lab.registration}/category"},
        )
    )


# --- §6: lectura de clasificacion ----------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_one_compatible_category_is_the_recommendation(lab: CategoryLab) -> None:
    """Con exactamente una compatible, esa es la recomendada y no hay bloqueos."""
    body = await _classify(lab)

    assert [verdict["category_key"] for verdict in body["compatible_categories"]] == [
        lab.key("solo")
    ]
    assert body["recommended_category"]["category_key"] == lab.key("solo")
    assert body["blockers"] == []
    assert body["warnings"] == []


@pytest.mark.asyncio(loop_scope="session")
async def test_several_compatible_categories_have_no_recommendation(lab: CategoryLab) -> None:
    """La heredada sin reglas tambien admite: dos compatibles y **nada** recomendado."""
    await _add_category(lab, "heredada")
    body = await _classify(lab)

    compatible = {verdict["category_key"] for verdict in body["compatible_categories"]}
    assert compatible == {lab.key("solo"), lab.key("heredada")}
    assert body["recommended_category"] is None
    assert "MULTIPLE_COMPATIBLE_CATEGORIES" in body["warnings"]
    assert body["blockers"] == []


@pytest.mark.asyncio(loop_scope="session")
async def test_no_compatible_category_blocks(lab: CategoryLab) -> None:
    """Ninguna categoria admite: ``NO_COMPATIBLE_CATEGORY`` mas el motivo concreto."""
    await database.execute(
        query="UPDATE stage_items SET weight_min_kg = 60, weight_max_kg = 65 WHERE id = :id",
        values={"id": lab.categories["solo"]},
    )
    body = await _classify(lab)

    assert body["compatible_categories"] == []
    assert body["recommended_category"] is None
    assert "NO_COMPATIBLE_CATEGORY" in body["blockers"]
    assert "CATEGORY_INCOMPATIBLE" in body["blockers"]
    assert set(body["blockers"]) <= ENGINE_BLOCKERS


@pytest.mark.asyncio(loop_scope="session")
async def test_a_missing_datum_is_reported_as_missing_not_incompatible(lab: CategoryLab) -> None:
    """Sin peso declarado, la categoria que lo exige bloquea con ``MISSING_WEIGHT``."""
    await database.execute(
        query="UPDATE tournament_registrations SET declared_weight_kg = NULL WHERE id = :id",
        values={"id": lab.registration},
    )
    body = await _classify(lab)

    assert "MISSING_WEIGHT" in body["blockers"]
    assert "CATEGORY_INCOMPATIBLE" not in body["blockers"]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_stage_without_rules_is_legacy_and_does_not_block(lab: CategoryLab) -> None:
    """Una categoria sin reglas admite todo con aviso, no con bloqueo."""
    await _add_category(lab, "heredada")
    body = await _classify(lab)

    heredada = next(
        verdict
        for verdict in body["compatible_categories"]
        if verdict["category_key"] == lab.key("heredada")
    )
    assert heredada["blockers"] == []
    assert heredada["warnings"] == ["CATEGORY_RULES_PARTIAL"]
    assert body["blockers"] == []


@pytest.mark.asyncio(loop_scope="session")
async def test_the_reading_does_not_write_nor_audit_nor_reserve(lab: CategoryLab) -> None:
    """Dos lecturas identicas no dejan rastro: sin auditoria, sin reserva y sin tocar la fila."""
    before = await _registration_row(lab)
    first = await _classify(lab)
    second = await _classify(lab)

    assert first == second
    assert await _category_audit(lab) == []
    assert await _reservations(lab) == 0
    assert await _registration_row(lab) == before


# --- §7/§8: asignacion y override ----------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_unique_recommendation_is_assigned_and_audited(lab: CategoryLab) -> None:
    """Sin ``category_key`` se asigna la unica compatible y el cambio se audita."""
    await _clear_category(lab)
    status_code, body = await _assign(lab, body={})

    assert status_code == 200, body
    assert body["category_key"] == lab.key("solo")
    assert body["category_label"] == "solo"
    rows = await _category_audit(lab)
    assert [row["action"] for row in rows] == ["UPDATE"]
    assert rows[0]["reason_code"] == "CATEGORY_CHANGE"
    assert str(rows[0]["actor_user_id"]) == str(lab.owner_id)
    assert set(rows[0]["changed_fields"]) == {"category_key", "category_label"}


@pytest.mark.asyncio(loop_scope="session")
async def test_with_several_compatible_an_explicit_key_is_required(lab: CategoryLab) -> None:
    """Con varias compatibles, sin ``category_key`` se rechaza sin elegir por el cliente."""
    await _clear_category(lab)
    await _add_category(lab, "heredada")
    status_code, body = await _assign(lab, body={})

    assert status_code == 400, body
    assert body == REQUEST_INVALID
    assert await _registration_row(lab) == {"category_key": None, "category_label": None}
    assert await _category_audit(lab) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_an_incompatible_key_without_override_is_rejected(lab: CategoryLab) -> None:
    """Una clave resuelta pero no compatible se rechaza si no hay override explicito."""
    await _clear_category(lab)
    await _add_incompatible_sex_category(lab)
    status_code, body = await _assign(lab, body={"category_key": lab.key("otra")})

    assert status_code == 400, body
    assert body == REQUEST_INVALID
    assert await _registration_row(lab) == {"category_key": None, "category_label": None}


@pytest.mark.asyncio(loop_scope="session")
async def test_a_key_that_does_not_exist_is_not_resolved(lab: CategoryLab) -> None:
    """Una clave que no existe en el torneo no resuelve: se rechaza igual."""
    status_code, body = await _assign(lab, body={"category_key": "si:999999999"})

    assert status_code == 400, body
    assert body == REQUEST_INVALID


@pytest.mark.asyncio(loop_scope="session")
async def test_owner_override_sets_an_incompatible_category_and_audits_it(lab: CategoryLab) -> None:
    """``override=true`` con motivo fija la categoria no compatible y audita el cambio."""
    await _add_incompatible_sex_category(lab)
    status_code, body = await _assign(
        lab,
        body={
            "category_key": lab.key("otra"),
            "override": True,
            "reason_code": "CATEGORY_CHANGE",
        },
    )

    assert status_code == 200, body
    assert body["category_key"] == lab.key("otra")
    assert body["category_label"] == "otra"
    rows = await _category_audit(lab)
    assert rows[-1]["action"] == "UPDATE"
    assert rows[-1]["reason_code"] == "CATEGORY_CHANGE"
    assert str(rows[-1]["actor_user_id"]) == str(lab.owner_id)


@pytest.mark.asyncio(loop_scope="session")
async def test_collaborator_override_is_forbidden(lab: CategoryLab) -> None:
    """El override es OWNER-only: un COLLABORATOR recibe 403 y no se reserva nada."""
    await _add_incompatible_sex_category(lab)
    status_code, body = await _assign(
        lab,
        token=lab.collaborator_token,
        body={
            "category_key": lab.key("otra"),
            "override": True,
            "reason_code": "CATEGORY_CHANGE",
        },
    )

    assert status_code == 403, body
    assert body == ROLE_NOT_ALLOWED
    assert await _registration_row(lab) == {
        "category_key": lab.key("solo"),
        "category_label": "solo",
    }
    assert await _reservations(lab) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_anonymous_override_is_unauthenticated(lab: CategoryLab) -> None:
    status_code, body = await _call_json(
        HTTPMethod.POST,
        _category_url(lab),
        key=_new_key(),
        payload=json.dumps({"category_key": "si:1", "override": True}).encode(),
    )

    assert status_code == 401
    assert body == NOT_AUTHENTICATED


@pytest.mark.asyncio(loop_scope="session")
async def test_an_override_without_a_reason_is_rejected(lab: CategoryLab) -> None:
    """El motivo es obligatorio: un override sin ``reason_code`` no es un override."""
    await _add_incompatible_sex_category(lab)
    status_code, body = await _assign(lab, body={"category_key": lab.key("otra"), "override": True})

    assert status_code == 400, body
    assert body == REQUEST_INVALID


@pytest.mark.asyncio(loop_scope="session")
async def test_a_collaborator_can_assign_the_unique_recommendation(lab: CategoryLab) -> None:
    """El RBAC general no cambia: la asignacion normal (sin override) no es OWNER-only."""
    await _clear_category(lab)
    status_code, body = await _assign(lab, token=lab.collaborator_token, body={})

    assert status_code == 200, body
    assert body["category_key"] == lab.key("solo")


@pytest.mark.asyncio(loop_scope="session")
async def test_reassigning_the_same_category_writes_no_event(lab: CategoryLab) -> None:
    """Sin cambio de categoria no hay evento de auditoria."""
    await _clear_category(lab)
    assert (await _assign(lab, body={}))[0] == 200
    assert (await _assign(lab, body={}))[0] == 200

    assert len(await _category_audit(lab)) == 1


# --- idempotencia de la escritura ---------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_the_same_key_and_body_is_a_replay(lab: CategoryLab) -> None:
    """Replay: mismo cuerpo con la misma clave devuelve lo mismo y no duplica la auditoria."""
    await _clear_category(lab)
    key = _new_key()
    first_status, first = await _assign(lab, body={}, key=key)
    second_status, second = await _assign(lab, body={}, key=key)

    assert first_status == second_status == 200
    assert first == second
    assert len(await _category_audit(lab)) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_the_same_key_with_another_body_is_a_conflict(lab: CategoryLab) -> None:
    await _clear_category(lab)
    key = _new_key()
    assert (await _assign(lab, body={}, key=key))[0] == 200
    status_code, body = await _assign(lab, body={"category_key": lab.key("solo")}, key=key)

    assert status_code == 409, body


@pytest.mark.asyncio(loop_scope="session")
async def test_a_disabled_write_gate_is_503_before_reserving(
    lab: CategoryLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La puerta apagada responde 503 antes de reservar la clave de idempotencia."""
    monkeypatch.setattr(config, "f3_write_enabled", False)
    status_code, body = await _assign(lab, body={}, key=_new_key())

    assert status_code == 503, body
    assert body == F3_WRITE_DISABLED_BODY
    assert await _reservations(lab) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_the_reading_is_available_with_the_write_gate_off(
    lab: CategoryLab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La clasificacion es lectura: sigue publicandose con la puerta de escritura apagada."""
    monkeypatch.setattr(config, "f3_write_enabled", False)
    body = await _classify(lab)

    assert body["recommended_category"]["category_key"] == lab.key("solo")


@pytest.mark.asyncio(loop_scope="session")
async def test_a_registration_of_another_tournament_is_not_found(lab: CategoryLab) -> None:
    """Una inscripcion inexistente para este torneo responde 404, no un error interno."""
    url = f"tournaments/{lab.tournament}/registrations/999999999/category"
    status_code, body = await _call_json(
        HTTPMethod.POST, url, token=lab.owner_token, key=_new_key(), payload=b"{}"
    )

    assert status_code == 404, body
    assert body == NOT_FOUND


# --- §9: clasificacion integrada en el readiness -------------------------------------------------


def _readiness_url(lab: CategoryLab) -> str:
    return f"tournaments/{lab.tournament}/bracket-readiness"


async def _readiness(lab: CategoryLab) -> dict[str, Any]:
    status_code, body = await _call_json(HTTPMethod.GET, _readiness_url(lab), token=lab.owner_token)
    assert status_code == 200, body
    return body


def _blocking_codes(body: dict[str, Any]) -> set[str]:
    return {issue["code"] for issue in body["blocking_issues"]}


@pytest.mark.asyncio(loop_scope="session")
async def test_readiness_reports_a_category_that_does_not_resolve(lab: CategoryLab) -> None:
    """Sin categoria elegida la clasificacion no resuelve: el readiness lo dice y no cuenta."""
    await _clear_category(lab)

    body = await _readiness(lab)

    assert "CATEGORY_NOT_RESOLVED" in _blocking_codes(body)
    [uncategorized] = [item for item in body["categories"] if item["category_key"] is None]
    assert uncategorized["confirmed_count"] == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_readiness_does_not_block_a_classifiable_registration(lab: CategoryLab) -> None:
    """Con la categoria resuelta y compatible ningun codigo del motor aparece como bloqueo."""
    body = await _readiness(lab)

    assert not _blocking_codes(body) & ENGINE_BLOCKERS
    [category] = [item for item in body["categories"] if item["category_key"] == lab.key("solo")]
    assert category["confirmed_count"] == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_readiness_does_not_count_an_incompatible_category_without_override(
    lab: CategoryLab,
) -> None:
    """La categoria elegida deja de admitir la inscripcion: el bloqueo aparece y no se cuenta."""
    await database.execute(
        query="UPDATE stage_items SET weight_min_kg = 10, weight_max_kg = 20 WHERE id = :id",
        values={"id": lab.categories["solo"]},
    )

    body = await _readiness(lab)

    assert "CATEGORY_INCOMPATIBLE" in _blocking_codes(body)
    [category] = [item for item in body["categories"] if item["category_key"] == lab.key("solo")]
    assert category["confirmed_count"] == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_readiness_counts_an_owner_override_of_an_incompatible_category(
    lab: CategoryLab,
) -> None:
    """Un override OWNER valido y auditado legitima el bloqueo: la fila vuelve a contar."""
    await _add_incompatible_sex_category(lab)

    status_code, body = await _assign(
        lab,
        body={
            "category_key": lab.key("otra"),
            "override": True,
            "reason_code": "CATEGORY_CHANGE",
        },
    )
    assert status_code == 200, body

    readiness = await _readiness(lab)
    assert "CATEGORY_INCOMPATIBLE" not in _blocking_codes(readiness)
    [category] = [
        item for item in readiness["categories"] if item["category_key"] == lab.key("otra")
    ]
    assert category["confirmed_count"] == 1
