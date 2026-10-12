# pylint: disable=redefined-outer-name
"""E2E de S4.5 (F3C): planificador de torneo y pantalla publica sobre CUATRO tatamis.

Un torneo de laboratorio con cuatro tatamis y seis cuadros recorre el contrato completo por las
rutas reales, en el orden del guion del E2E:

1. El torneo tiene exactamente cuatro tatamis activos: ni el 5 ni el 6 existen aqui.
2. ``POST /tournaments/{tid}/schedule/generate`` con sesion OWNER y ``Idempotency-Key`` planifica
   todo el torneo, reparte el primer turno entre los cuatro tatamis y deja horas y duraciones
   reales (``duration_minutes + margin_minutes``) en pasos del slot.
3. Una asignacion manual previa no se mueve al planificar, y solo la mueve un ``recalculate`` que
   lo pide expresamente.
4. Un combate futuro se planifica a partir del final de su alimentador, sin inventar participantes.
5. El descanso minimo (15 min) evita que un competidor vuelva antes de tiempo: sin descanso el plan
   se apila y aparece el conflicto; con 15 el mismo plan se estira.
6. La vista del organizador y 7. el cuerpo publico sirven los cuatro tatamis con AHORA, SIGUIENTE,
   los siguientes, los terminados y el retraso, y el guardia de privacidad pasa sobre el cuerpo
   real.
8. La pantalla publica se sirve en su ruta congelada y se valida por HTTP y por su derivacion pura
   sobre el cuerpo real.
9. Un combate que termina con retraso no cambia su hora planificada, mueve el ``estimated_start``
   de los siguientes de SU tatami y no toca los demas.
10. ``POST /schedule/recalculate`` replanifica y su idempotencia reenvia el mismo resumen.
11. Un resultado avanza el cuadro y la pantalla publica lo refleja.
12. Dos generaciones del mismo estado dan el mismo plan logico.

Solo LAB: la base es ``bracket-ci-pg`` (``ci.env``), nunca produccion; todo lo que crea este fichero
lo borra su propio teardown.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast
from uuid import uuid4

import aiohttp
import pytest
import pytest_asyncio
from heliclockter import datetime_utc
from starlette import status as http_status

from bracket.database import database
from bracket.logic.scheduling.builder import build_matches_for_stage_item
from bracket.logic.scheduling.public_schedule import (
    PUBLIC_MATCH_KEYS,
    STATE_COMPLETED,
    STATE_DELAYED,
    STATE_NEXT,
    STATE_NOW,
    STATE_UPCOMING,
    STATE_WAITING,
    assert_public_payload_is_safe,
)
from bracket.models.db.stage_item import StageItemWithInputsCreate, StageType
from bracket.models.db.stage_item_inputs import StageItemInputCreateBodyFinal
from bracket.sql.shared import sql_delete_stage_item_with_foreign_keys
from bracket.sql.stage_items import sql_create_stage_item_with_inputs
from bracket.utils.dummy_records import DUMMY_COURT1, DUMMY_RANKING1, DUMMY_STAGE2, DUMMY_TEAM1
from bracket.utils.id_types import CourtId, MatchId, StageItemId, TournamentId
from tests.integration_tests.api.generate_bracket_http_support import generation_lab_context
from tests.integration_tests.api.public_schedule_http_test import (  # noqa: F401
    PUBLIC_TOURNAMENT_FIELDS,
    idempotency_secret,
    public_url,
    read_public,
    walk_public_payload,
)
from tests.integration_tests.api.schedule_lab_test import (
    COURT_DISPLAY_FIELDS,
    REST_MINUTES,
    SCHEDULE_SUMMARY_FIELDS,
    SCHEDULE_VIEW_FIELDS,
    SLOT_MINUTES,
    START,
    START_TIME,
    ScheduleLab,
    court_display,
    generate_ok,
    generate_url,
    match_rows,
    post_operation,
    read_view,
    recalculate,
    recalculate_url,
    view_url,
)
from tests.integration_tests.api.shared import get_root_uvicorn_url
from tests.integration_tests.sql import (
    inserted_court,
    inserted_ranking,
    inserted_stage,
    inserted_team,
)

#: Los cuatro tatamis del torneo del E2E. El guion exige que el 5 y el 6 no existan.
TATAMI_NAMES: Final = ("Tatami 1", "Tatami 2", "Tatami 3", "Tatami 4")

#: Los cuadros del torneo: cinco eliminaciones directas y una liguilla (§1).
DEFAULT_COMPOSITION: Final = (StageType.SINGLE_ELIMINATION,) * 5 + (StageType.ROUND_ROBIN,)

#: Equipos por cuadro: cuatro en cada uno, todos distintos entre cuadros.
TEAMS_PER_CATEGORY: Final = 4

#: Combates del torneo completo: cinco cuadros de 3 combates y una liguilla de 6.
TOURNAMENT_MATCH_COUNT: Final = 21

#: Retraso de LAB del combate que termina tarde: fin real = fin planificado + 30 min (§9, > 10 min).
LATE_FINISH_MINUTES: Final = 30

#: Estados publicos posibles; la pantalla solo puede recibir uno de estos.
PUBLIC_STATES: Final = frozenset(
    {STATE_NOW, STATE_NEXT, STATE_UPCOMING, STATE_WAITING, STATE_COMPLETED, STATE_DELAYED}
)


@dataclass(frozen=True)
class CategoryLab:
    """Un cuadro del torneo del E2E con sus combates reales."""

    name: str
    stage_item_id: StageItemId
    type: StageType
    match_ids: tuple[MatchId, ...]


@dataclass(frozen=True)
class E2ELab(ScheduleLab):
    """Torneo del E2E: cuatro tatamis y los cuadros que pida el test.

    Hereda la forma del laboratorio del planificador para reutilizar sus ayudas HTTP
    (``generate_ok``, ``recalculate``, ``read_view``): el primer cuadro es el ``stage_item_id``
    heredado y ``match_ids`` es el torneo entero.
    """

    categories: tuple[CategoryLab, ...] = ()
    #: Tatami de OTRO torneo del mismo club: pedirlo no planifica nada (§1).
    foreign_court: CourtId | None = None

    @property
    def category(self) -> CategoryLab:
        """El primer cuadro del torneo."""
        return self.categories[0]

    def category_of(self, category_type: StageType) -> CategoryLab:
        """El primer cuadro de un tipo dado."""
        return next(category for category in self.categories if category.type == category_type)

    @property
    def single_elimination(self) -> CategoryLab:
        return self.category_of(StageType.SINGLE_ELIMINATION)

    @property
    def round_robin(self) -> CategoryLab:
        return self.category_of(StageType.ROUND_ROBIN)


@asynccontextmanager
async def e2e_lab_context(
    *,
    categories: Sequence[StageType] = DEFAULT_COMPOSITION,
    court_count: int = len(TATAMI_NAMES),
) -> AsyncIterator[E2ELab]:
    """Torneo de E2E: tatamis, cuadros y combates reales; la limpieza va de hojas a raices."""
    async with generation_lab_context() as lab:
        async with AsyncExitStack() as stack:
            now = datetime_utc.now()
            courts = [
                await stack.enter_async_context(
                    inserted_court(
                        DUMMY_COURT1.model_copy(
                            update={"tournament_id": lab.tournament, "name": name, "created": now}
                        )
                    )
                )
                for name in TATAMI_NAMES[:court_count]
            ]
            stage = await stack.enter_async_context(
                inserted_stage(
                    DUMMY_STAGE2.model_copy(
                        update={"tournament_id": lab.tournament, "name": "E2E S4.5"}
                    )
                )
            )
            ranking = await stack.enter_async_context(
                inserted_ranking(
                    DUMMY_RANKING1.model_copy(update={"tournament_id": lab.tournament})
                )
            )

            built: list[CategoryLab] = []
            for index, category_type in enumerate(categories, start=1):
                teams = [
                    await stack.enter_async_context(
                        inserted_team(
                            DUMMY_TEAM1.model_copy(
                                update={
                                    "tournament_id": lab.tournament,
                                    "name": f"Equipo {index}-{slot}",
                                    "created": now,
                                }
                            )
                        )
                    )
                    for slot in range(TEAMS_PER_CATEGORY)
                ]
                stage_item = await sql_create_stage_item_with_inputs(
                    lab.tournament,
                    StageItemWithInputsCreate(
                        stage_id=stage.id,
                        name=f"Categoria {index}",
                        team_count=TEAMS_PER_CATEGORY,
                        type=category_type,
                        ranking_id=ranking.id,
                        inputs=[
                            StageItemInputCreateBodyFinal(slot=slot + 1, team_id=team.id)
                            for slot, team in enumerate(teams)
                        ],
                    ),
                )
                stack.push_async_callback(sql_delete_stage_item_with_foreign_keys, stage_item.id)
                await build_matches_for_stage_item(stage_item, lab.tournament)
                rows = await match_rows(stage_item_id=stage_item.id)
                built.append(
                    CategoryLab(
                        name=stage_item.name,
                        stage_item_id=stage_item.id,
                        type=category_type,
                        match_ids=tuple(MatchId(row["id"]) for row in rows),
                    )
                )

            foreign_court = await stack.enter_async_context(
                inserted_court(
                    DUMMY_COURT1.model_copy(
                        update={
                            "tournament_id": lab.other_tournament,
                            "name": "Tatami 5 ajeno",
                            "created": now,
                        }
                    )
                )
            )

            match_ids = tuple(match_id for category in built for match_id in category.match_ids)

            yield E2ELab(
                lab=lab,
                tournament=lab.tournament,
                stage_item_id=built[0].stage_item_id,
                courts=tuple(CourtId(court.id) for court in courts),
                match_ids=match_ids,
                categories=tuple(built),
                foreign_court=CourtId(foreign_court.id),
            )


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def e2e_lab() -> AsyncIterator[E2ELab]:
    """Torneo del E2E: cuatro tatamis, cinco eliminaciones directas y una liguilla."""
    async with e2e_lab_context() as lab:
        yield lab


@pytest_asyncio.fixture(loop_scope="session", scope="function")
async def round_robin_lab() -> AsyncIterator[E2ELab]:
    """Solo la liguilla: el cuadro donde el mismo competidor repite y el descanso aprieta."""
    async with e2e_lab_context(categories=(StageType.ROUND_ROBIN,)) as lab:
        yield lab


# --- lecturas y escrituras de laboratorio -------------------------------------------------------


def moment(value: Any) -> datetime:
    """Marca temporal de la base, admite datetime o texto."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


EVIDENCE_DIR: Final = Path("/tmp/s45_e2e_evidence")


def dump_evidence(name: str, body: dict[str, Any]) -> None:
    """Deja en disco el cuerpo real que sirve el backend (LAB): con el se valida la pantalla."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / f"{name}.json").write_text(
        json.dumps(body, sort_keys=True, indent=1), encoding="utf-8"
    )


async def tournament_matches(tournament_id: TournamentId) -> list[dict[str, Any]]:
    """Estado persistido de TODOS los combates del torneo, en orden determinista (por id)."""
    rows = await database.fetch_all(
        query=(
            "SELECT matches.id AS id, matches.round_id AS round_id,"
            " matches.court_id AS court_id, matches.start_time AS start_time,"
            " matches.position_in_schedule AS position_in_schedule,"
            " matches.finished_at AS finished_at,"
            " matches.stage_item_input1_id AS input1_id,"
            " matches.duration_minutes AS duration_minutes,"
            " matches.margin_minutes AS margin_minutes"
            " FROM matches JOIN rounds ON matches.round_id = rounds.id"
            " JOIN stage_items ON rounds.stage_item_id = stage_items.id"
            " JOIN stages ON stage_items.stage_id = stages.id"
            " WHERE stages.tournament_id = :tournament_id ORDER BY matches.id"
        ),
        values={"tournament_id": int(tournament_id)},
    )
    return [dict(row) for row in rows]


async def tournament_court_names(tournament_id: TournamentId) -> list[str]:
    rows = await database.fetch_all(
        query="SELECT name FROM courts WHERE tournament_id = :tournament_id ORDER BY id",
        values={"tournament_id": int(tournament_id)},
    )
    return [cast("str", row["name"]) for row in rows]


async def participants_by_match(tournament_id: TournamentId) -> dict[int, tuple[int, ...]]:
    """Compania real de cada combate: los equipos de sus dos entradas, si ya se conocen."""
    rows = await database.fetch_all(
        query=(
            "SELECT matches.id AS id, input1.team_id AS team_a, input2.team_id AS team_b"
            " FROM matches JOIN rounds ON matches.round_id = rounds.id"
            " JOIN stage_items ON rounds.stage_item_id = stage_items.id"
            " JOIN stages ON stage_items.stage_id = stages.id"
            " LEFT JOIN stage_item_inputs AS input1 ON input1.id = matches.stage_item_input1_id"
            " LEFT JOIN stage_item_inputs AS input2 ON input2.id = matches.stage_item_input2_id"
            " WHERE stages.tournament_id = :tournament_id ORDER BY matches.id"
        ),
        values={"tournament_id": int(tournament_id)},
    )
    return {
        int(row["id"]): tuple(
            int(value) for value in (row["team_a"], row["team_b"]) if value is not None
        )
        for row in rows
    }


def planned_window(row: dict[str, Any]) -> tuple[datetime, datetime]:
    """Ventana real de un combate: la hora planificada mas su duracion y su margen."""
    start = moment(row["start_time"])
    length = int(row["duration_minutes"]) + int(row["margin_minutes"])
    return start, start + timedelta(minutes=length)


def plan_fingerprint(
    rows: Sequence[dict[str, Any]],
) -> list[tuple[int, int | None, str | None, int | None]]:
    """Huella del plan logico: identidad, tatami, hora y posicion de cada combate."""
    return [
        (
            int(row["id"]),
            None if row["court_id"] is None else int(row["court_id"]),
            None if row["start_time"] is None else moment(row["start_time"]).isoformat(),
            None if row["position_in_schedule"] is None else int(row["position_in_schedule"]),
        )
        for row in rows
    ]


def planned_starts(rows: Sequence[dict[str, Any]]) -> dict[int, datetime]:
    return {int(row["id"]): moment(row["start_time"]) for row in rows}


async def clear_plan(tournament_id: TournamentId) -> None:
    """Deshace el plan sin tocar nada mas: el estado vuelve a ser 'sin planificar'."""
    await database.execute(
        query=(
            "UPDATE matches SET court_id = NULL, start_time = NULL, position_in_schedule = NULL"
            " WHERE id IN ("
            "  SELECT matches.id FROM matches JOIN rounds ON matches.round_id = rounds.id"
            "  JOIN stage_items ON rounds.stage_item_id = stage_items.id"
            "  JOIN stages ON stage_items.stage_id = stages.id"
            "  WHERE stages.tournament_id = :tournament_id)"
        ),
        values={"tournament_id": int(tournament_id)},
    )


async def assign_manually(match_id: MatchId, *, court_id: CourtId, start_time: datetime) -> None:
    """Asignacion explicita del organizador: tatami y hora en el combate, sin posicion de plan."""
    await database.execute(
        query=(
            "UPDATE matches SET court_id = :court_id, start_time = :start_time WHERE id = :match_id"
        ),
        values={"match_id": int(match_id), "court_id": int(court_id), "start_time": start_time},
    )


async def set_finished_at(match_id: MatchId, finished_at: datetime) -> None:
    """Cierra el combate a una hora concreta.

    La mesa sella ``finished_at`` con el reloj; en el laboratorio el reloj no se puede mover, asi
    que la hora del cierre se fija aqui para poder exigir el retraso sin esperarlo de verdad.
    """
    await database.execute(
        query="UPDATE matches SET finished_at = :finished_at WHERE id = :match_id",
        values={"match_id": int(match_id), "finished_at": finished_at},
    )


async def finish_match(lab: E2ELab, match_id: MatchId) -> tuple[int, dict[str, Any]]:
    """Declara ganador por la via real (el ``PUT`` de combate) con la sesion del organizador."""
    row = next(
        item
        for item in await tournament_matches(lab.tournament)
        if int(item["id"]) == int(match_id)
    )
    payload: dict[str, Any] = {
        "round_id": int(row["round_id"]),
        "stage_item_input1_score": 2,
        "stage_item_input2_score": 0,
        "court_id": None if row["court_id"] is None else int(row["court_id"]),
        "winner_stage_item_input_id": int(row["input1_id"]),
        "result_method": "submission",
    }
    url = f"{get_root_uvicorn_url()}tournaments/{int(lab.tournament)}/matches/{int(match_id)}"
    headers = {"Authorization": f"Bearer {lab.lab.owner_token}"}
    async with aiohttp.ClientSession() as session:
        async with session.put(url, headers=headers, json=payload) as response:
            text = await response.text()
            if response.status != http_status.HTTP_200_OK:
                return response.status, {"detail": text}
            return response.status, cast("dict[str, Any]", json.loads(text))


async def generate_plan(
    lab: E2ELab,
    *,
    start: str = START_TIME,
    rest: int = REST_MINUTES,
    courts: Sequence[CourtId] | None = None,
) -> dict[str, Any]:
    """Generacion con la sesion OWNER: planifica lo que no tiene tatami ni hora."""
    return await generate_ok(lab, body=plan_body(start=start, rest=rest, courts=courts))


async def recalculate_plan(
    lab: E2ELab,
    *,
    start: str = START_TIME,
    rest: int = REST_MINUTES,
    courts: Sequence[CourtId] | None = None,
    preserve: bool = True,
    key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Replanificacion explicita; la clave se puede fijar para probar la idempotencia."""
    body = plan_body(start=start, rest=rest, courts=courts)
    body["preserve_assigned"] = preserve
    return await recalculate(
        lab, key=uuid4().hex if key is None else key, token=lab.lab.owner_token, body=body
    )


def plan_body(*, start: str, rest: int, courts: Sequence[CourtId] | None) -> dict[str, Any]:
    return {
        "start_time": start,
        "minimum_rest_minutes": rest,
        "court_ids": None if courts is None else [int(court) for court in courts],
    }


def entries_of(block: dict[str, Any]) -> list[dict[str, Any]]:
    """Todos los combates visibles de un tatami, en el orden de la pantalla."""
    current = [block["current"]] if block["current"] is not None else []
    following = [block["next"]] if block["next"] is not None else []
    return [*current, *following, *block["upcoming"], *block["completed"]]


def public_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for block in payload["courts"] for entry in entries_of(block)]


def rests_between_matches(
    plan: Sequence[dict[str, Any]], companies: dict[int, tuple[int, ...]]
) -> dict[int, list[timedelta]]:
    """Descanso real de cada competidor entre sus combates consecutivos del plan."""
    by_team: dict[int, list[tuple[datetime, datetime]]] = {}
    for row in plan:
        start, end = planned_window(row)
        for team in companies[int(row["id"])]:
            by_team.setdefault(team, []).append((start, end))
    return {
        team: [
            following[0] - previous[1]
            for previous, following in itertools.pairwise(sorted(windows))
        ]
        for team, windows in by_team.items()
        if len(windows) > 1
    }


# --- 1. el torneo del laboratorio ---------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_the_lab_tournament_has_four_tatamis_and_no_fifth_or_sixth(
    e2e_lab: E2ELab,
) -> None:
    """§1: cuatro tatamis activos; el 5 y el 6 no existen en este torneo."""
    names = await tournament_court_names(e2e_lab.tournament)
    assert names == list(TATAMI_NAMES), names
    assert not any(name.endswith((" 5", " 6")) for name in names), names

    rows = await tournament_matches(e2e_lab.tournament)
    assert len(rows) == len(e2e_lab.match_ids) == TOURNAMENT_MATCH_COUNT, len(rows)
    assert len(e2e_lab.categories) == 6, e2e_lab.categories
    by_category = {category.name: len(category.match_ids) for category in e2e_lab.categories}
    assert list(by_category.values()) == [3, 3, 3, 3, 3, 6], by_category


# --- 2. generar el plan -------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_generate_plans_the_tournament_on_the_four_courts(e2e_lab: E2ELab) -> None:
    """§2: OWNER y clave nueva; plan completo, repartido y con horas y duraciones reales."""
    # Sin sesion no se planifica, y un colaborador tampoco: la operacion es del OWNER.
    no_session = await post_operation(
        generate_url(e2e_lab.tournament),
        key=uuid4().hex,
        body=plan_body(start=START_TIME, rest=REST_MINUTES, courts=None),
    )
    assert no_session[0] == http_status.HTTP_401_UNAUTHORIZED, no_session
    collaborator = await post_operation(
        generate_url(e2e_lab.tournament),
        key=uuid4().hex,
        token=e2e_lab.lab.collaborator_token,
        body=plan_body(start=START_TIME, rest=REST_MINUTES, courts=None),
    )
    assert collaborator[0] == http_status.HTTP_403_FORBIDDEN, collaborator

    summary = await generate_plan(e2e_lab)
    assert summary == {
        "tournament_id": int(e2e_lab.tournament),
        "court_count": 4,
        "scheduled_match_count": TOURNAMENT_MATCH_COUNT,
        "moved_match_count": TOURNAMENT_MATCH_COUNT,
        "manual_match_count": 0,
        "unscheduled_match_count": 0,
        "start_time": START_TIME,
    }, summary

    rows = await tournament_matches(e2e_lab.tournament)
    active = {int(court) for court in e2e_lab.courts}
    assert all(row["court_id"] is not None for row in rows), rows
    # Ningun combate cae en un tatami que no sea uno de los cuatro (§2): ni el 5 ni el 6.
    assert {int(row["court_id"]) for row in rows} == active, rows

    # Horas desde las 09:00 y en pasos del slot real (10 de combate + 5 de margen).
    for row in rows:
        start, _ = planned_window(row)
        assert start >= START, row
        assert (start - START) % timedelta(minutes=SLOT_MINUTES) == timedelta(0), row
        assert int(row["duration_minutes"]) == 10, row
        assert int(row["margin_minutes"]) == 5, row

    # El primer turno se reparte entre los cuatro tatamis, uno por tatami.
    opening = [row for row in rows if planned_window(row)[0] == START]
    assert len(opening) == 4, opening
    assert len({int(row["court_id"]) for row in opening}) == 4, opening

    # Los combates de un mismo tatami no se solapan: cada uno empieza cuando acaba el anterior.
    for court in e2e_lab.courts:
        windows = sorted(planned_window(row) for row in rows if int(row["court_id"]) == int(court))
        for (_, previous_end), (start, _) in itertools.pairwise(windows):
            assert start >= previous_end, (court, windows)

    # La posicion en el plan es coherente: numerada de 1 a N, sin huecos ni repetidos.
    positions = [row["position_in_schedule"] for row in rows]
    assert None not in positions, positions
    assert sorted(int(position) for position in positions) == list(range(1, len(rows) + 1))

    # Un tatami que no es de este torneo (aunque exista en el club) no se planifica.
    assert e2e_lab.foreign_court is not None
    before = plan_fingerprint(await tournament_matches(e2e_lab.tournament))
    rejected = await post_operation(
        generate_url(e2e_lab.tournament),
        key=uuid4().hex,
        token=e2e_lab.lab.owner_token,
        body={
            "start_time": START_TIME,
            "minimum_rest_minutes": REST_MINUTES,
            "court_ids": [int(e2e_lab.foreign_court)],
        },
    )
    assert rejected[0] == http_status.HTTP_400_BAD_REQUEST, rejected
    assert "SCHEDULE_NOT_PLANNABLE" in json.dumps(rejected[1]), rejected
    assert plan_fingerprint(await tournament_matches(e2e_lab.tournament)) == before


# --- 3. asignaciones manuales -------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_a_manual_assignment_is_kept_and_only_moved_on_request(e2e_lab: E2ELab) -> None:
    """§3: la asignacion explicita sobrevive al plan y solo se replanifica si se pide."""
    manual_match = e2e_lab.category.match_ids[0]
    manual_start = START + timedelta(hours=9)
    await assign_manually(manual_match, court_id=e2e_lab.courts[-1], start_time=manual_start)

    summary = await generate_plan(e2e_lab)
    assert summary["manual_match_count"] == 1, summary
    assert summary["scheduled_match_count"] == TOURNAMENT_MATCH_COUNT, summary

    rows = await tournament_matches(e2e_lab.tournament)
    kept = next(row for row in rows if int(row["id"]) == int(manual_match))
    assert int(kept["court_id"]) == int(e2e_lab.courts[-1]), kept
    assert moment(kept["start_time"]) == manual_start, kept

    # Recalcular sin permiso explicito tampoco la mueve.
    status_code, preserved = await recalculate_plan(e2e_lab, preserve=True)
    assert status_code == http_status.HTTP_200_OK, preserved
    # Con la preservacion por defecto todo lo ya asignado se respeta y nada se mueve.
    assert preserved["manual_match_count"] == TOURNAMENT_MATCH_COUNT, preserved
    assert preserved["moved_match_count"] == 0, preserved
    rows = await tournament_matches(e2e_lab.tournament)
    kept = next(row for row in rows if int(row["id"]) == int(manual_match))
    assert int(kept["court_id"]) == int(e2e_lab.courts[-1]), kept
    assert moment(kept["start_time"]) == manual_start, kept

    # Recalcular pidiendolo expresamente si la replanifica: vuelve al plan del sistema.
    status_code, moved = await recalculate_plan(e2e_lab, preserve=False)
    assert status_code == http_status.HTTP_200_OK, moved
    assert moved["manual_match_count"] == 0, moved
    assert moved["moved_match_count"] >= 1, moved
    rows = await tournament_matches(e2e_lab.tournament)
    replaced = next(row for row in rows if int(row["id"]) == int(manual_match))
    assert moment(replaced["start_time"]) != manual_start, replaced


# --- 4. dependencias ----------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_future_fights_wait_for_their_feeder_without_inventing_participants(
    e2e_lab: E2ELab,
) -> None:
    """§4: la final espera al ganador de su alimentador y aun no tiene rivales inventados."""
    await generate_plan(e2e_lab)
    semis = e2e_lab.single_elimination.match_ids[:2]
    final_id = int(e2e_lab.single_elimination.match_ids[2])

    status_code, payload = await read_public(e2e_lab.tournament)
    assert status_code == http_status.HTTP_200_OK, payload
    final_entries = [
        entry for entry in public_entries(payload) if int(entry["match_id"]) == final_id
    ]
    assert len(final_entries) == 1, payload
    final_entry = final_entries[0]
    # Sin rivales todavia y con la espera declarada: nada de nombres inventados.
    assert final_entry["state"] == STATE_WAITING, final_entry
    assert final_entry["fighter_a"] is None, final_entry
    assert final_entry["fighter_b"] is None, final_entry
    assert final_entry["waiting_for"], final_entry
    assert final_entry["scheduled_start"] is not None, final_entry

    # Y se planifica detras del final de su alimentador, no antes.
    rows = await tournament_matches(e2e_lab.tournament)
    final_row = next(row for row in rows if int(row["id"]) == final_id)
    final_start, _ = planned_window(final_row)
    for semi in semis:
        semi_row = next(row for row in rows if int(row["id"]) == int(semi))
        _, semi_end = planned_window(semi_row)
        assert final_start >= semi_end, (final_row, semi_row)


# --- 5. descanso --------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_rest_minutes_prevent_a_too_early_return(round_robin_lab: E2ELab) -> None:
    """§5: sin descanso el plan se apila y el mismo competidor vuelve de inmediato; con 15 no."""
    one_court = (round_robin_lab.courts[0],)
    await generate_plan(round_robin_lab, rest=0, courts=one_court)

    companies = await participants_by_match(round_robin_lab.tournament)
    rows = await tournament_matches(round_robin_lab.tournament)
    assert len(rows) == 6, rows
    assert {int(row["court_id"]) for row in rows} == {int(one_court[0])}, rows

    packed = rests_between_matches(rows, companies)
    assert packed, companies
    packed_span = max(end for _, end in map(planned_window, rows)) - min(
        start for start, _ in map(planned_window, rows)
    )
    # Sin descanso los seis combates van pegados: seis slots exactos.
    assert packed_span == timedelta(minutes=SLOT_MINUTES * len(rows)), packed_span
    # Y ahi esta el conflicto: alguien volveria antes de los 15 minutos.
    conflicts = {
        team: rests
        for team, rests in packed.items()
        if min(rests) < timedelta(minutes=REST_MINUTES)
    }
    assert conflicts, packed

    # Con el descanso minimo del torneo, el mismo plan se estira y nadie vuelve antes de tiempo.
    status_code, summary = await recalculate_plan(
        round_robin_lab, rest=REST_MINUTES, courts=one_court, preserve=False
    )
    assert status_code == http_status.HTTP_200_OK, summary
    assert summary["scheduled_match_count"] == 6, summary
    assert summary["moved_match_count"] >= 1, summary

    rows = await tournament_matches(round_robin_lab.tournament)
    rested = rests_between_matches(rows, companies)
    assert rested, companies
    for team, rests in rested.items():
        assert min(rests) >= timedelta(minutes=REST_MINUTES), (team, rests)
    rested_span = max(end for _, end in map(planned_window, rows)) - min(
        start for start, _ in map(planned_window, rows)
    )
    assert rested_span > packed_span, (rested_span, packed_span)
    # El descanso obliga a estirar el plan: al menos un slot mas que el plan apilado.
    assert rested_span >= packed_span + timedelta(minutes=SLOT_MINUTES), (rested_span, packed_span)
    for rests in rested.values():
        for rest in rests:
            assert rest % timedelta(minutes=SLOT_MINUTES) == timedelta(0), rests


# --- 6, 7 y 8. vista del organizador, cuerpo publico y pantalla ---------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_the_organizer_view_and_the_public_screen_serve_the_four_tatamis(
    e2e_lab: E2ELab,
) -> None:
    """§6, §7 y §8: misma verdad por las dos puertas, con el guardia de privacidad en verde."""
    await generate_plan(e2e_lab)

    status_code, view = await read_view(e2e_lab, token=e2e_lab.lab.owner_token)
    assert status_code == http_status.HTTP_200_OK, view
    assert set(view) == SCHEDULE_VIEW_FIELDS, view
    assert len(view["courts"]) == 4, view
    categories = {category.name for category in e2e_lab.categories}

    for court in e2e_lab.courts:
        block = court_display(view, court)
        assert set(block) == COURT_DISPLAY_FIELDS, block
        assert block["court_name"] in TATAMI_NAMES, block
        assert block["lag_minutes"] == 0, block
        assert block["current"] is not None, block
        assert block["current"]["state"] in {STATE_NOW, STATE_WAITING, STATE_DELAYED}, block
        assert block["next"] is not None, block
        assert block["next"]["state"] in {STATE_NEXT, STATE_WAITING, STATE_DELAYED}, block
        assert all(
            entry["state"] in {STATE_UPCOMING, STATE_WAITING, STATE_DELAYED}
            for entry in block["upcoming"]
        ), block
        for entry in entries_of(block):
            assert entry["scheduled_start"] is not None, entry
            assert entry["estimated_start"] == entry["scheduled_start"], entry
            assert entry["state"] in PUBLIC_STATES, entry
            assert entry["category"] in categories, entry
            assert entry["round_name"], entry
            assert entry["position_in_schedule"] is not None, entry
    # Los cuatro estados de papel salen en la pantalla: AHORA, SIGUIENTE, los siguientes y las
    # esperas de las finales que aun no tienen rivales.
    observed = {entry["state"] for block in view["courts"] for entry in entries_of(block)}
    assert {STATE_NOW, STATE_NEXT, STATE_UPCOMING, STATE_WAITING} <= observed, observed

    status_code, payload = await read_public(e2e_lab.tournament)
    assert status_code == http_status.HTTP_200_OK, payload
    assert set(payload["tournament"]) == PUBLIC_TOURNAMENT_FIELDS, payload["tournament"]
    assert len(payload["courts"]) == 4, payload
    assert {block["court_name"] for block in payload["courts"]} == set(TATAMI_NAMES), payload
    assert_public_payload_is_safe(payload)
    walk_public_payload(payload, "$")
    for entry in public_entries(payload):
        assert set(entry) == PUBLIC_MATCH_KEYS, entry
        assert entry["scheduled_start"] is not None, entry
        assert entry["state"] in PUBLIC_STATES, entry
    # El cuerpo publico no lleva nada de cuenta, de sesion ni de auditoria: solo deporte.
    body = json.dumps(payload).lower()
    for forbidden in ("token", "email", "created", "user_id", "idempotency", "audit", "password"):
        assert forbidden not in body, forbidden

    # La pantalla se valida tambien con su derivacion pura sobre este mismo cuerpo real.
    dump_evidence("public_payload", payload)
    dump_evidence("organizer_view", view)


# --- 9. retraso real ----------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_a_late_finish_delays_only_its_own_court(e2e_lab: E2ELab) -> None:
    """§9: la hora planificada no cambia; el retraso solo lo hereda el tatami que se retraso."""
    await generate_plan(e2e_lab)
    rows = await tournament_matches(e2e_lab.tournament)
    late_court = e2e_lab.courts[0]

    def on_court(item: dict[str, Any]) -> bool:
        return int(item["court_id"]) == int(late_court)

    first_on_court = min(
        (row for row in rows if on_court(row)), key=lambda row: planned_window(row)[0]
    )
    planned_start, planned_end = planned_window(first_on_court)
    assert planned_start == START, first_on_court
    before = planned_starts(rows)

    # La mesa cierra el combate por la via real y despues se fecha el cierre 30 min tarde.
    status_code, response = await finish_match(e2e_lab, MatchId(first_on_court["id"]))
    assert status_code == http_status.HTTP_200_OK, response
    closed = next(
        row
        for row in await tournament_matches(e2e_lab.tournament)
        if int(row["id"]) == int(first_on_court["id"])
    )
    assert closed["finished_at"] is not None, closed
    await set_finished_at(
        MatchId(first_on_court["id"]), planned_end + timedelta(minutes=LATE_FINISH_MINUTES)
    )

    # La hora planificada no se toca: ni la del que termino ni la de los siguientes.
    after = planned_starts(await tournament_matches(e2e_lab.tournament))
    assert after == before, (before, after)
    assert after[int(first_on_court["id"])] == planned_start

    status_code, view = await read_view(e2e_lab, token=e2e_lab.lab.owner_token)
    assert status_code == http_status.HTTP_200_OK, view
    late_block = court_display(view, late_court)
    assert late_block["lag_minutes"] == LATE_FINISH_MINUTES, late_block
    for entry in [late_block["current"], late_block["next"], *late_block["upcoming"]]:
        if entry is None:
            continue
        assert moment(entry["estimated_start"]) > moment(entry["scheduled_start"]), entry
        lag = moment(entry["estimated_start"]) - moment(entry["scheduled_start"])
        assert lag == timedelta(minutes=LATE_FINISH_MINUTES), entry

    # El combate terminado sigue con su hora y aparece como terminado en la pantalla publica.
    status_code, payload = await read_public(e2e_lab.tournament)
    assert status_code == http_status.HTTP_200_OK, payload
    completed = [
        entry
        for block in payload["courts"]
        for entry in block["completed"]
        if int(entry["match_id"]) == int(first_on_court["id"])
    ]
    assert len(completed) == 1, payload
    assert completed[0]["state"] == STATE_COMPLETED, completed
    assert moment(completed[0]["scheduled_start"]) == planned_start, completed

    dump_evidence("late_payload", payload)
    dump_evidence("late_view", view)

    # Los otros tatamis no heredan nada: su retraso es cero y su estimacion sigue siendo la hora.
    for court in e2e_lab.courts[1:]:
        block = court_display(view, court)
        assert block["lag_minutes"] == 0, block
        for entry in entries_of(block):
            assert entry["estimated_start"] == entry["scheduled_start"], entry


# --- 10. recalcular -----------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_recalculate_replans_and_replays_the_same_summary(e2e_lab: E2ELab) -> None:
    """§10: solo la accion del organizador mueve el plan, y su clave reenvia el mismo resumen."""
    await generate_plan(e2e_lab)
    generated = plan_fingerprint(await tournament_matches(e2e_lab.tournament))

    # Con la preservacion por defecto, recalcular un plan ya completo no mueve nada: lo asignado
    # se respeta, y solo un recalculate que lo pida expresamente lo replanifica.
    key = uuid4().hex
    first = await recalculate_plan(e2e_lab, key=key)
    assert first[0] == http_status.HTTP_200_OK, first
    assert first[1]["moved_match_count"] == 0, first
    assert first[1]["manual_match_count"] == TOURNAMENT_MATCH_COUNT, first
    assert plan_fingerprint(await tournament_matches(e2e_lab.tournament)) == generated

    # La misma clave reenviada devuelve el mismo resumen: idempotencia de la operacion.
    again = await recalculate_plan(e2e_lab, key=key)
    assert again == first, (first, again)
    assert plan_fingerprint(await tournament_matches(e2e_lab.tournament)) == generated

    # Una replanificacion de verdad mueve el plan entero y vuelve a derivar las estimaciones.
    new_start = START + timedelta(hours=2)
    status_code, moved = await recalculate_plan(
        e2e_lab, start=new_start.isoformat(), rest=30, courts=None, preserve=False
    )
    assert status_code == http_status.HTTP_200_OK, moved
    assert moved["moved_match_count"] == TOURNAMENT_MATCH_COUNT, moved
    assert moved["manual_match_count"] == 0, moved
    assert moment(moved["start_time"]) == new_start, moved
    replanned = plan_fingerprint(await tournament_matches(e2e_lab.tournament))
    assert replanned != generated, replanned
    for row in await tournament_matches(e2e_lab.tournament):
        assert planned_window(row)[0] >= new_start, row

    status_code, view = await read_view(e2e_lab, token=e2e_lab.lab.owner_token)
    assert status_code == http_status.HTTP_200_OK, view
    for court in e2e_lab.courts:
        block = court_display(view, court)
        assert block["lag_minutes"] == 0, block
        for entry in entries_of(block):
            assert entry["estimated_start"] == entry["scheduled_start"], entry

    # Y la lectura no escribe: el plan sigue siendo el mismo despues de leerlo.
    assert plan_fingerprint(await tournament_matches(e2e_lab.tournament)) == replanned


# --- 11. resultado y avance del cuadro ----------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_a_result_advances_the_bracket_and_the_screen_follows(e2e_lab: E2ELab) -> None:
    """§11: cerrar un combate mete al ganador en la siguiente ronda y la pantalla lo refleja."""
    await generate_plan(e2e_lab)
    semis = e2e_lab.single_elimination.match_ids[:2]
    final_id = int(e2e_lab.single_elimination.match_ids[2])

    status_code, payload = await read_public(e2e_lab.tournament)
    assert status_code == http_status.HTTP_200_OK, payload
    before = [entry for entry in public_entries(payload) if int(entry["match_id"]) == final_id]
    assert len(before) == 1, before
    assert before[0]["state"] == STATE_WAITING, before
    assert before[0]["fighter_a"] is None and before[0]["fighter_b"] is None, before

    for semi in semis:
        status_code, response = await finish_match(e2e_lab, semi)
        assert status_code == http_status.HTTP_200_OK, response

    # El ganador de cada semifinal ya esta en la final, con su nombre.
    status_code, payload = await read_public(e2e_lab.tournament)
    assert status_code == http_status.HTTP_200_OK, payload
    final_entry = next(
        entry for entry in public_entries(payload) if int(entry["match_id"]) == final_id
    )
    assert final_entry["fighter_a"] is not None, final_entry
    assert final_entry["fighter_b"] is not None, final_entry
    assert final_entry["state"] != STATE_WAITING, final_entry
    assert not final_entry["waiting_for"], final_entry

    # Los combates cerrados salen como terminados en la pantalla publica.
    completed_ids = {
        int(entry["match_id"]) for block in payload["courts"] for entry in block["completed"]
    }
    assert {int(semi) for semi in semis} <= completed_ids, completed_ids
    for entry in public_entries(payload):
        if int(entry["match_id"]) in {int(semi) for semi in semis}:
            assert entry["state"] == STATE_COMPLETED, entry


# --- 12. determinismo ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_e2e_two_generations_of_the_same_state_produce_the_same_plan(e2e_lab: E2ELab) -> None:
    """§12: con el mismo estado y las mismas entradas, dos generaciones dan el mismo plan."""
    first_summary = await generate_plan(e2e_lab)
    first = plan_fingerprint(await tournament_matches(e2e_lab.tournament))

    second_summary = await generate_plan(e2e_lab)
    second = plan_fingerprint(await tournament_matches(e2e_lab.tournament))
    assert second == first, (first, second)
    # La segunda generacion no mueve nada y lo ya asignado queda preservado: mismo plan logico.
    assert second_summary["moved_match_count"] == 0, second_summary
    assert second_summary["manual_match_count"] == TOURNAMENT_MATCH_COUNT, second_summary
    assert second_summary["scheduled_match_count"] == TOURNAMENT_MATCH_COUNT, second_summary

    # El mismo plan se rehace igual partiendo de cero: planificar es funcion del estado.
    await clear_plan(e2e_lab.tournament)
    third_summary = await generate_plan(e2e_lab)
    third = plan_fingerprint(await tournament_matches(e2e_lab.tournament))
    assert third == first, (first, third)
    assert third_summary == first_summary, (first_summary, third_summary)


# --- 7 y 8. la ruta publica congelada ----------------------------------------------------------


def test_e2e_the_public_screen_url_is_the_documented_one() -> None:
    """§7 y §8: la ruta que sirve el display es la congelada en S4.5."""
    assert public_url(TournamentId(7)) == f"{get_root_uvicorn_url()}public/tournaments/7/schedule"
    assert view_url(TournamentId(7)).endswith("/tournaments/7/schedule")
    assert recalculate_url(TournamentId(7)).endswith("/tournaments/7/schedule/recalculate")
    assert set(SCHEDULE_SUMMARY_FIELDS) == {
        "tournament_id",
        "court_count",
        "scheduled_match_count",
        "moved_match_count",
        "manual_match_count",
        "unscheduled_match_count",
        "start_time",
    }
