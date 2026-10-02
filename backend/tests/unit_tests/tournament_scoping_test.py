"""Tests unitarios de DEF-01: alcance por torneo en las dependencias de recursos.

No necesitan PostgreSQL: se sustituye `fetch_one_parsed` por una version que
ejecuta la sentencia SQL real (tal como la construye el codigo de produccion)
contra una base SQLite en memoria con las mismas tablas. Asi el test comprueba
comportamiento (se permite / se rechaza), no solo la forma del SQL.

El alcance por torneo de matches y rounds no se puede comprobar con una columna
directa: `matches` y `rounds` no tienen `tournament_id` (ver `bracket/schema.py`),
asi que la pertenencia se valida con la cadena
`matches.round_id -> rounds.stage_item_id -> stage_items.stage_id -> stages.tournament_id`.

Cubre:
A. match del torneo correcto -> permitido
B. match existente de otro torneo -> rechazado (404)
C. round de otro torneo -> rechazado (404)
D. el SQL conserva AMBAS condiciones (id y tournament_id del torneo correcto)
E. equipo de otro torneo -> rechazado (404)
"""

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, text

from bracket import schema
from bracket.routes import util as routes_util
from bracket.utils.id_types import MatchId, RoundId, TeamId, TournamentId

TOURNAMENT_1 = TournamentId(1)
TOURNAMENT_2 = TournamentId(2)


class SqliteProbe:
    """Ejecuta sentencias SELECT reales contra SQLite con las tablas del schema."""

    instances: list["SqliteProbe"] = []

    def __init__(self, tables: tuple[Table, ...]) -> None:
        metadata = MetaData()
        copies = [
            Table(table.name, metadata, *[Column(column.name, Integer) for column in table.columns])
            for table in tables
        ]
        self.engine = create_engine("sqlite://")
        metadata.create_all(self.engine)
        self.copies = {copy.name: copy for copy in copies}
        self.statements: list[str] = []
        SqliteProbe.instances.append(self)

    def seed(self, table_name: str, row: dict[str, object]) -> None:
        columns = ", ".join(row.keys())
        placeholders = ", ".join(f":{key}" for key in row)
        with self.engine.begin() as connection:
            connection.execute(
                text(f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})"), row
            )

    def fetch_one(self, statement: object) -> SimpleNamespace | None:
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))  # type: ignore[attr-defined]
        self.statements.append(sql)
        with self.engine.connect() as connection:
            rows = connection.execute(text(sql)).mappings().all()
        return SimpleNamespace(**rows[0]) if rows else None

    @property
    def last_statement(self) -> str:
        assert self.statements, "no se ejecuto ninguna sentencia"
        return self.statements[-1]


@pytest.fixture(autouse=True)
def dispose_probes() -> Iterator[None]:
    yield
    for probe in SqliteProbe.instances:
        probe.engine.dispose()
    SqliteProbe.instances.clear()


def install_fake_fetch(monkeypatch: pytest.MonkeyPatch, probe: SqliteProbe) -> None:
    async def fake_fetch_one_parsed(database: object, model: object, query: object) -> object:
        return probe.fetch_one(query)

    monkeypatch.setattr(routes_util, "fetch_one_parsed", fake_fetch_one_parsed)


def make_match_probe() -> SqliteProbe:
    probe = SqliteProbe((schema.matches, schema.rounds, schema.stage_items, schema.stages))
    probe.seed("stages", {"id": 1, "tournament_id": 1})
    probe.seed("stages", {"id": 2, "tournament_id": 2})
    probe.seed("stage_items", {"id": 10, "stage_id": 1})
    probe.seed("stage_items", {"id": 20, "stage_id": 2})
    probe.seed("rounds", {"id": 3, "stage_item_id": 10})
    probe.seed("rounds", {"id": 4, "stage_item_id": 20})
    probe.seed("matches", {"id": 41, "round_id": 3})
    probe.seed("matches", {"id": 42, "round_id": 4})
    return probe


def make_round_probe() -> SqliteProbe:
    probe = SqliteProbe((schema.rounds, schema.stage_items, schema.stages))
    probe.seed("stages", {"id": 1, "tournament_id": 1})
    probe.seed("stages", {"id": 2, "tournament_id": 2})
    probe.seed("stage_items", {"id": 10, "stage_id": 1})
    probe.seed("stage_items", {"id": 20, "stage_id": 2})
    probe.seed("rounds", {"id": 3, "stage_item_id": 10})
    probe.seed("rounds", {"id": 4, "stage_item_id": 20})
    return probe


def make_team_probe() -> SqliteProbe:
    probe = SqliteProbe((schema.teams,))
    probe.seed("teams", {"id": 5, "tournament_id": 1})
    probe.seed("teams", {"id": 6, "tournament_id": 2})
    return probe


def test_match_dependency_allows_match_of_own_tournament(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = make_match_probe()
    install_fake_fetch(monkeypatch, probe)

    match = asyncio.run(routes_util.match_dependency(TOURNAMENT_1, MatchId(41)))

    assert match.id == 41


def test_match_dependency_rejects_match_of_other_tournament(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = make_match_probe()
    install_fake_fetch(monkeypatch, probe)

    with pytest.raises(HTTPException) as error:
        asyncio.run(routes_util.match_dependency(TOURNAMENT_1, MatchId(42)))

    assert error.value.status_code == 404


def test_round_dependency_allows_round_of_own_tournament(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = make_round_probe()
    install_fake_fetch(monkeypatch, probe)

    round_ = asyncio.run(routes_util.round_dependency(TOURNAMENT_1, RoundId(3)))

    assert round_.id == 3


def test_round_dependency_rejects_round_of_other_tournament(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = make_round_probe()
    install_fake_fetch(monkeypatch, probe)

    with pytest.raises(HTTPException) as error:
        asyncio.run(routes_util.round_dependency(TOURNAMENT_1, RoundId(4)))

    assert error.value.status_code == 404


def test_team_dependency_rejects_team_of_other_tournament(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = make_team_probe()
    install_fake_fetch(monkeypatch, probe)

    assert asyncio.run(routes_util.team_dependency(TOURNAMENT_1, TeamId(5))).id == 5
    with pytest.raises(HTTPException) as error:
        asyncio.run(routes_util.team_dependency(TOURNAMENT_1, TeamId(6)))
    assert error.value.status_code == 404


def test_match_dependency_sql_keeps_both_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = make_match_probe()
    install_fake_fetch(monkeypatch, probe)

    asyncio.run(routes_util.match_dependency(TOURNAMENT_1, MatchId(41)))

    sql = probe.last_statement
    assert "matches.id = 41" in sql
    assert "stages.tournament_id = 1" in sql
    assert "matches.tournament_id" not in sql, "matches no tiene columna tournament_id"


def test_round_dependency_sql_keeps_both_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = make_round_probe()
    install_fake_fetch(monkeypatch, probe)

    asyncio.run(routes_util.round_dependency(TOURNAMENT_1, RoundId(3)))

    sql = probe.last_statement
    assert "rounds.id = 3" in sql
    assert "stages.tournament_id = 1" in sql
    assert "matches" not in sql, "la consulta de rounds no debe referenciar la tabla matches"


def test_team_dependency_sql_keeps_both_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = make_team_probe()
    install_fake_fetch(monkeypatch, probe)

    asyncio.run(routes_util.team_dependency(TOURNAMENT_1, TeamId(5)))

    sql = probe.last_statement
    assert "teams.id = 5" in sql
    assert "teams.tournament_id = 1" in sql


def test_other_tournament_id_cannot_reach_own_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El mismo match, pedido desde otro torneo, no se puede obtener."""
    probe = make_match_probe()
    install_fake_fetch(monkeypatch, probe)

    with pytest.raises(HTTPException) as error:
        asyncio.run(routes_util.match_dependency(TOURNAMENT_2, MatchId(41)))

    assert error.value.status_code == 404
    assert asyncio.run(routes_util.match_dependency(TOURNAMENT_2, MatchId(42))).id == 42
