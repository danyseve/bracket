"""Tests unitarios de DEF-02: NULLs legitimos al actualizar un match.

`sql_update_match` construia los parametros de bind con `match.model_dump()`, que
en `BaseModelORM` excluye los valores `None`, mientras la sentencia SQL sigue
enlazando `:court_id`, `:custom_duration_minutes` y `:custom_margin_minutes`.
Resultado: `StatementError: A value is required for bind parameter`.

Estos tests ejecutan la sentencia SQL real contra SQLite y comprueban el efecto:
None -> SQL NULL, valor -> valor, sin inventar ceros.

Cubre:
A. court_id=None
B. custom_duration_minutes=None
C. custom_margin_minutes=None
D. los tres None
E. valores no-null
F. scores actualizados
G. round_id preservado
"""

import asyncio
from collections.abc import Iterator

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, text
from sqlalchemy.engine import Engine

from bracket import schema
from bracket.models.db.match import MatchBody
from bracket.models.db.tournament import Tournament
from bracket.sql import matches as sql_matches
from bracket.utils.id_types import CourtId, MatchId, RoundId

ENGINES: list[Engine] = []

MATCH_ID = MatchId(41)
ROUND_ID = RoundId(3)
COURT_ID = CourtId(7)
TOURNAMENT_DURATION = 12
TOURNAMENT_MARGIN = 6
COLUMNS = (
    "round_id, stage_item_input1_score, stage_item_input2_score, court_id,"
    " custom_duration_minutes, custom_margin_minutes, duration_minutes, margin_minutes"
)


class RecordingDatabase:
    """Sustituye a `database` y ejecuta la sentencia real contra SQLite."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.calls: list[dict[str, object]] = []

    async def execute(self, query: str, values: dict[str, object]) -> None:
        self.calls.append(dict(values))
        with self.engine.begin() as connection:
            result = connection.execute(text(query), dict(values))
            if result.returns_rows:  # consumir RETURNING antes del commit
                result.mappings().all()
        return None


def make_database() -> tuple[RecordingDatabase, Engine]:
    metadata = MetaData()
    Table("matches", metadata, *[Column(column.name, Integer) for column in schema.matches.columns])
    engine = create_engine("sqlite://")
    ENGINES.append(engine)
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO matches (id, round_id, stage_item_input1_score,"
                " stage_item_input2_score, court_id, custom_duration_minutes,"
                " custom_margin_minutes, duration_minutes, margin_minutes)"
                " VALUES (:id, :round_id, 0, 0, 7, 5, 2, 5, 2)"
            ),
            {"id": MATCH_ID, "round_id": ROUND_ID},
        )
    return RecordingDatabase(engine), engine


def read_row(engine: Engine) -> dict[str, object]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(f"SELECT {COLUMNS} FROM matches WHERE id = :id"), {"id": MATCH_ID}
            )
            .mappings()
            .one()
        )


def tournament() -> Tournament:
    return Tournament.model_construct(
        duration_minutes=TOURNAMENT_DURATION, margin_minutes=TOURNAMENT_MARGIN
    )


def body(**overrides: object) -> MatchBody:
    values: dict[str, object] = {
        "round_id": ROUND_ID,
        "stage_item_input1_score": 2,
        "stage_item_input2_score": 0,
        "court_id": COURT_ID,
        "custom_duration_minutes": 5,
        "custom_margin_minutes": 2,
    }
    values.update(overrides)
    return MatchBody(**values)  # type: ignore[arg-type]


def run_update(
    monkeypatch: pytest.MonkeyPatch, match_body: MatchBody
) -> tuple[RecordingDatabase, Engine]:
    database, engine = make_database()
    monkeypatch.setattr(sql_matches, "database", database)
    asyncio.run(sql_matches.sql_update_match(MATCH_ID, match_body, tournament()))
    return database, engine


@pytest.fixture(autouse=True)
def dispose_engines() -> Iterator[None]:
    yield
    for engine in ENGINES:
        engine.dispose()
    ENGINES.clear()


@pytest.mark.parametrize(
    "overrides,expected",
    [
        pytest.param(
            {"court_id": None},
            {"court_id": None, "custom_duration_minutes": 5, "custom_margin_minutes": 2},
            id="A-court-id-none",
        ),
        pytest.param(
            {"custom_duration_minutes": None},
            {"court_id": 7, "custom_duration_minutes": None, "custom_margin_minutes": 2},
            id="B-custom-duration-none",
        ),
        pytest.param(
            {"custom_margin_minutes": None},
            {"court_id": 7, "custom_duration_minutes": 5, "custom_margin_minutes": None},
            id="C-custom-margin-none",
        ),
        pytest.param(
            {"court_id": None, "custom_duration_minutes": None, "custom_margin_minutes": None},
            {"court_id": None, "custom_duration_minutes": None, "custom_margin_minutes": None},
            id="D-todos-none",
        ),
    ],
)
def test_nulls_are_written_as_sql_null_and_others_preserved(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object], expected: dict[str, object]
) -> None:
    _, engine = run_update(monkeypatch, body(**overrides))

    row = read_row(engine)
    for column, value in expected.items():
        if value is None:
            assert row[column] is None, f"{column} deberia quedar como NULL"
        else:
            assert row[column] == value, f"{column} deberia conservar {value}"
    assert row["stage_item_input1_score"] == 2, "los scores del body deben escribirse"
    assert row["stage_item_input2_score"] == 0
    assert row["round_id"] == ROUND_ID, "round_id debe preservarse"


def test_all_bind_parameters_are_always_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    database, _ = run_update(
        monkeypatch,
        body(court_id=None, custom_duration_minutes=None, custom_margin_minutes=None),
    )

    values = database.calls[0]
    for parameter in (
        "court_id",
        "custom_duration_minutes",
        "custom_margin_minutes",
        "round_id",
        "stage_item_input1_score",
        "stage_item_input2_score",
        "duration_minutes",
        "margin_minutes",
    ):
        assert parameter in values, f"falta el parametro de bind {parameter}"
    assert values["court_id"] is None
    assert values["custom_duration_minutes"] is None
    assert values["custom_margin_minutes"] is None


def test_non_null_values_are_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    _, engine = run_update(
        monkeypatch,
        body(
            court_id=9,
            custom_duration_minutes=8,
            custom_margin_minutes=3,
            stage_item_input1_score=4,
            stage_item_input2_score=6,
        ),
    )

    row = read_row(engine)
    assert row["court_id"] == 9
    assert row["custom_duration_minutes"] == 8
    assert row["custom_margin_minutes"] == 3
    assert row["duration_minutes"] == 8, "custom_duration_minutes manda sobre el torneo"
    assert row["margin_minutes"] == 3
    assert row["stage_item_input1_score"] == 4
    assert row["stage_item_input2_score"] == 6
    assert row["round_id"] == ROUND_ID


def test_duration_falls_back_to_tournament_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _, engine = run_update(
        monkeypatch, body(custom_duration_minutes=None, custom_margin_minutes=None)
    )

    row = read_row(engine)
    assert row["duration_minutes"] == TOURNAMENT_DURATION
    assert row["margin_minutes"] == TOURNAMENT_MARGIN
    assert row["custom_duration_minutes"] is None


def test_scores_and_round_id_are_updated(monkeypatch: pytest.MonkeyPatch) -> None:
    _, engine = run_update(
        monkeypatch, body(round_id=8, stage_item_input1_score=0, stage_item_input2_score=1)
    )

    row = read_row(engine)
    assert row["stage_item_input1_score"] == 0
    assert row["stage_item_input2_score"] == 1
    assert row["round_id"] == 8
