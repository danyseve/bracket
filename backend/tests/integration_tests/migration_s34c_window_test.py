# pylint: disable=redefined-outer-name  # `migration_lab` es el fixture de este modulo.
"""S3.4c - migracion de la ventana de inscripcion: columnas nuevas, downgrade y cabeza unica.

TDD: el esqueleto se escribio **antes** de la revision (``a4e8d1c60f37_s34c_registration_window``)
y fallo en rojo (la revision no existia y las columnas tampoco); la migracion posterior lo lleva a
verde.

Se ejecuta contra una base **propia y temporal** (``bracket_s34c_window_test_<pid>``) creada y
eliminada por el propio modulo sobre el servidor PostgreSQL de pruebas: no se toca ``bracket_test``
ni ``bracket_dev``, no se crean usuarios ni contenedores y no se escribe en ``bracket_ci``.

Que se comprueba:

* la migracion es **aditiva**: anade dos columnas a ``tournaments`` y no toca ninguna otra tabla (se
  verifica tambien leyendo el propio fichero de revision: sin ``UPDATE``, ``DELETE``, ``INSERT`` ni
  ``op.execute``, sin relleno hacia atras y sin tocar tablas legadas);
* la forma fisica de las dos columnas (``timestamp with time zone``, anulables) es la declarada;
* el resto de columnas de ``tournaments`` sigue exactamente igual;
* ni restricciones, ni indices, ni claves ajenas nuevas: el estado de la ventana es derivado y el
  actor del cierre vive en la auditoria;
* ``downgrade`` retira exactamente las dos columnas y deja el resto intacto, sin perder el dato de
  los torneos;
* tras el downgrade, ``upgrade`` vuelve a funcionar;
* ``alembic heads`` sigue teniendo **una sola** cabeza.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from bracket.config import config
from bracket.schema import metadata
from tests.source_scan import code_without_documentation

BACKEND_DIR = Path(__file__).resolve().parents[2]
MIGRATION_FILE = (
    BACKEND_DIR / "alembic" / "versions" / "a4e8d1c60f37_s34c_registration_window.py"
)

S33C3_REVISION = "9c3e7a1d5b28"
S34C_REVISION = "a4e8d1c60f37"
SCRATCH_DATABASE_PREFIX = "bracket_s34c_window_test"
TABLE = "tournaments"

NEW_COLUMNS: dict[str, tuple[str, str]] = {
    "registration_deadline": ("timestamp with time zone", "YES"),
    "registrations_closed_at": ("timestamp with time zone", "YES"),
}

#: Columnas del torneo que **no** toca esta revision (las que ya estaban).
UNTOUCHED_COLUMNS = (
    "id",
    "name",
    "created",
    "start_time",
    "club_id",
    "dashboard_public",
    "logo_path",
    "dashboard_endpoint",
    "players_can_be_in_multiple_teams",
    "auto_assign_courts",
    "duration_minutes",
    "margin_minutes",
    "status",
)


def _scratch_database() -> str:
    """Base propia del proceso: dos ejecuciones en paralelo no comparten ni pisan estado."""
    return f"{SCRATCH_DATABASE_PREFIX}_{os.getpid()}"


def _dsn(database: str) -> str:
    """DSN de laboratorio apuntando a otra base (el DSN de pruebas no lleva parametros)."""
    base = str(config.pg_dsn)
    return f"{base.rsplit('/', 1)[0]}/{database}"


def _alembic(*arguments: str) -> str:
    """Ejecuta el CLI real de Alembic contra la base temporal, con el DSN en el entorno."""
    environment = {**os.environ, "ENVIRONMENT": "CI", "PG_DSN": _dsn(_scratch_database())}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=BACKEND_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"alembic {' '.join(arguments)}: {result.stderr.strip()}"
    return result.stdout


def _scalar(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> Any:
    with engine.connect() as connection:
        return connection.execute(text(sql), params or {}).scalar_one()


def _execute(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params or {})


def _columns(engine: Engine) -> dict[str, tuple[str, str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns WHERE table_name = :table
                """
            ),
            {"table": TABLE},
        ).all()
    return {str(row[0]): (str(row[1]), str(row[2])) for row in rows}


def _constraints(engine: Engine, contype: str) -> list[str]:
    with engine.connect() as connection:
        return [
            str(row[0])
            for row in connection.execute(
                text(
                    """
                    SELECT conname FROM pg_constraint
                    WHERE conrelid = to_regclass(:table) AND contype = :contype
                    ORDER BY conname
                    """
                ),
                {"table": TABLE, "contype": contype},
            ).all()
        ]


def _seed_tournament(engine: Engine) -> int:
    """Un club y un torneo reales: el dato tienen que sobrevivir al upgrade y al downgrade."""
    with engine.begin() as connection:
        club_id = connection.execute(
            text("INSERT INTO clubs (name) VALUES ('lab-club-s34c') RETURNING id")
        ).scalar_one()
        tournament_id = connection.execute(
            text(
                """
                INSERT INTO tournaments (name, start_time, club_id, dashboard_public)
                VALUES ('lab-tournament-s34c', now(), :club_id, true) RETURNING id
                """
            ),
            {"club_id": club_id},
        ).scalar_one()
    return int(tournament_id)


@pytest.fixture
def migration_lab() -> Iterator[Engine]:
    """Base temporal limpia en el servidor de pruebas, en el estado posterior a S3.3c-3."""
    admin = create_engine(_dsn("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{_scratch_database()}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{_scratch_database()}"'))

    engine = create_engine(_dsn(_scratch_database()))
    metadata.create_all(engine)
    # Aspecto fisico antes de esta revision: las dos columnas nuevas no existen. Si el metadata vivo
    # todavia no las declara, el esquema ya esta en el estado previo y no hay nada que retirar.
    for column in NEW_COLUMNS:
        _execute(engine, f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS {column}")
    _alembic("stamp", S33C3_REVISION)

    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{_scratch_database()}" WITH (FORCE)')
            )
        admin.dispose()


def test_upgrade_adds_the_two_columns_with_the_declared_shape(migration_lab: Engine) -> None:
    _alembic("upgrade", S34C_REVISION)

    columns = _columns(migration_lab)
    for name, shape in NEW_COLUMNS.items():
        assert columns[name] == shape, f"{name} no tiene la forma declarada"
    assert _scalar(migration_lab, "SELECT version_num FROM alembic_version") == S34C_REVISION


def test_upgrade_leaves_the_rest_of_the_tournament_columns_untouched(
    migration_lab: Engine,
) -> None:
    before = {name: shape for name, shape in _columns(migration_lab).items()}

    _alembic("upgrade", S34C_REVISION)

    after = _columns(migration_lab)
    assert set(after) - set(before) == set(NEW_COLUMNS)
    for column in UNTOUCHED_COLUMNS:
        assert after[column] == before[column], f"{column} cambio de forma"


def test_upgrade_declares_no_new_constraint_index_or_foreign_key(migration_lab: Engine) -> None:
    before = {
        contype: _constraints(migration_lab, contype) for contype in ("c", "u", "f", "p")
    }

    _alembic("upgrade", S34C_REVISION)

    for contype, names in before.items():
        assert _constraints(migration_lab, contype) == names, f"restricciones {contype} nuevas"


def test_the_migration_only_touches_the_two_columns(migration_lab: Engine) -> None:
    """La revision no escribe datos ni toca nada mas. Se lee **sin su cabecera**: explicar la
    nulabilidad en la documentacion de la migracion no es ejecutar nada."""
    code = code_without_documentation(MIGRATION_FILE.read_text(encoding="utf-8"))

    assert "op.add_column(" in code
    assert "op.drop_column(" in code
    for forbidden in ("op.execute", "UPDATE ", "DELETE ", "INSERT ", "bulk_insert"):
        assert forbidden not in code, f"la migracion no debe contener {forbidden!r}"
    assert "op.create_table(" not in code, "no hay tabla nueva en esta fase"


def test_upgrade_leaves_the_tournaments_data_untouched(migration_lab: Engine) -> None:
    tournament_id = _seed_tournament(migration_lab)

    _alembic("upgrade", S34C_REVISION)

    assert (
        _scalar(
            migration_lab,
            f"SELECT count(*) FROM {TABLE} WHERE id = :id",
            {"id": tournament_id},
        )
        == 1
    ), "el torneo sigue ahi"
    assert (
        _scalar(
            migration_lab,
            f"SELECT count(*) FROM {TABLE} WHERE registration_deadline IS NOT NULL"
            " OR registrations_closed_at IS NOT NULL",
        )
        == 0
    ), "no se rellena nada hacia atras"


def test_downgrade_removes_exactly_the_two_columns(migration_lab: Engine) -> None:
    tournament_id = _seed_tournament(migration_lab)
    _alembic("upgrade", S34C_REVISION)
    _execute(
        migration_lab,
        f"UPDATE {TABLE} SET registration_deadline = now(),"
        " registrations_closed_at = now() WHERE id = :id",
        {"id": tournament_id},
    )

    _alembic("downgrade", S33C3_REVISION)

    assert set(_columns(migration_lab)).isdisjoint(NEW_COLUMNS)
    assert _scalar(migration_lab, "SELECT version_num FROM alembic_version") == S33C3_REVISION
    assert (
        _scalar(
            migration_lab,
            "SELECT count(*) FROM information_schema.tables WHERE table_name = :name",
            {"name": "tournament_registrations"},
        )
        == 1
    ), "el resto del esquema sigue intacto"


def test_upgrade_can_be_reapplied_after_a_downgrade(migration_lab: Engine) -> None:
    tournament_id = _seed_tournament(migration_lab)

    _alembic("upgrade", S34C_REVISION)
    _alembic("downgrade", S33C3_REVISION)
    _alembic("upgrade", S34C_REVISION)

    columns = _columns(migration_lab)
    for name, shape in NEW_COLUMNS.items():
        assert columns[name] == shape
    assert _scalar(migration_lab, "SELECT version_num FROM alembic_version") == S34C_REVISION
    assert (
        _scalar(
            migration_lab,
            f"SELECT count(*) FROM {TABLE} WHERE id = :id",
            {"id": tournament_id},
        )
        == 1
    )


def test_the_revision_graph_still_has_a_single_head(migration_lab: Engine) -> None:
    heads = [line for line in _alembic("heads").splitlines() if line.strip()]

    assert len(heads) == 1, f"cabeza unica: {heads}"
    assert S34C_REVISION in heads[0]
