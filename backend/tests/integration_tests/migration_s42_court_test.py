# pylint: disable=redefined-outer-name  # `migration_lab` es el fixture de este modulo.
"""Migracion de S4.2: ``stage_items.court_id`` (aditiva, nullable, con SET NULL al borrar tatami).

Se prueba contra una base temporal del servidor de pruebas (como la migracion de S3.4c), con el CLI
real de Alembic: el esquema vivo ([...]) no basta como evidencia porque ``metadata.create_all`` no
ejecuta la revision.

Lo que fija este modulo:
* el upgrade anade **una** columna nulable ``bigint`` a ``stage_items`` y su clave foranea a
  ``courts`` con ``ON DELETE SET NULL``;
* nada mas cambia: las columnas preexistentes siguen igual y una fila real sobrevive al upgrade;
* el downgrade retira exactamente lo anadido y deja la fila intacta;
* borrar un tatami deja al cuadro sin tatami en lugar de romper el borrado (el SET NULL de verdad).
"""

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

BACKEND_DIR = Path(__file__).resolve().parents[2]
SCRATCH_DATABASE = "bracket_test_migration_s42"
TABLE = "stage_items"
COURT_COLUMN = "court_id"
PREVIOUS_REVISION = "a4e8d1c60f37"
REVISION = "b7f1c9d4e2a6"
#: La columna nueva es lo unico que el esquema vivo no puede declarar ya retirado.
NEW_COLUMN_SHAPE = ("bigint", "YES")


def _dsn(database: str) -> str:
    """DSN de laboratorio apuntando a otra base (el DSN de pruebas no lleva parametros)."""
    base = str(config.pg_dsn)
    return f"{base.rsplit('/', 1)[0]}/{database}"


def _alembic(*arguments: str) -> str:
    """Ejecuta el CLI real de Alembic contra la base temporal, con el DSN en el entorno."""
    environment = {**os.environ, "ENVIRONMENT": "CI", "PG_DSN": _dsn(SCRATCH_DATABASE)}
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


def _column(engine: Engine) -> tuple[str, str] | None:
    """Tipo y nulabilidad declarados de la columna nueva, o ``None`` si no existe."""
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT data_type, is_nullable FROM information_schema.columns
                WHERE table_name = :table AND column_name = :column
                """
            ),
            {"table": TABLE, "column": COURT_COLUMN},
        ).first()
    return None if row is None else (str(row[0]), str(row[1]))


def _foreign_key_delete_action(engine: Engine) -> str | None:
    """Accion al borrar del destino (``n`` = SET NULL) de la clave foranea del tatami."""
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT confdeltype FROM pg_constraint
                WHERE conrelid = to_regclass(:table) AND contype = 'f'
                  AND conname = :constraint
                """
            ),
            {"table": TABLE, "constraint": f"{TABLE}_{COURT_COLUMN}_fkey"},
        ).first()
    return None if row is None else str(row[0])


def _seed_stage_item(engine: Engine) -> int:
    """Un club, un torneo, una etapa y un cuadro reales: el dato debe sobrevivir a los dos pasos."""
    with engine.begin() as connection:
        club_id = connection.execute(
            text("INSERT INTO clubs (name) VALUES ('lab-club-s42') RETURNING id")
        ).scalar_one()
        tournament_id = connection.execute(
            text(
                """
                INSERT INTO tournaments (name, start_time, club_id, dashboard_public)
                VALUES ('lab-tournament-s42', now(), :club_id, true) RETURNING id
                """
            ),
            {"club_id": club_id},
        ).scalar_one()
        stage_id = connection.execute(
            text(
                """
                INSERT INTO stages (name, tournament_id) VALUES ('lab-stage-s42', :tournament_id)
                RETURNING id
                """
            ),
            {"tournament_id": tournament_id},
        ).scalar_one()
        # El cuadro real exige un ranking del torneo (columna NOT NULL en el esquema vivo).
        ranking_id = connection.execute(
            text(
                """
                INSERT INTO rankings
                    (tournament_id, position, win_points, draw_points, loss_points,
                     add_score_points)
                VALUES (:tournament_id, 1, 1, 0, 0, false) RETURNING id
                """
            ),
            {"tournament_id": tournament_id},
        ).scalar_one()
        return int(
            connection.execute(
                text(
                    """
                    INSERT INTO stage_items (name, stage_id, team_count, ranking_id, type)
                    VALUES ('lab-item-s42', :stage_id, 2, :ranking_id, 'SINGLE_ELIMINATION')
                    RETURNING id
                    """
                ),
                {"stage_id": stage_id, "ranking_id": ranking_id},
            ).scalar_one()
        )


@pytest.fixture
def migration_lab() -> Iterator[Engine]:
    """Base temporal limpia en el servidor de pruebas, en el estado posterior a S3.4c."""
    admin = create_engine(_dsn("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{SCRATCH_DATABASE}"'))

    engine = create_engine(_dsn(SCRATCH_DATABASE))
    metadata.create_all(engine)
    # Aspecto fisico previo a esta revision: la columna nueva no existe. Si el esquema vivo todavia
    # no la declara, el esquema ya esta en el estado previo y no hay nada que retirar.
    _execute(engine, f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS {COURT_COLUMN}")
    _alembic("stamp", PREVIOUS_REVISION)

    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}" WITH (FORCE)'))
        admin.dispose()


def test_upgrade_adds_exactly_one_nullable_column_with_set_null(
    migration_lab: Engine,
) -> None:
    """El upgrade anade la columna nulable, su clave foranea con SET NULL y nada mas."""
    stage_item_id = _seed_stage_item(migration_lab)
    assert _column(migration_lab) is None

    _alembic("upgrade", REVISION)

    assert _column(migration_lab) == NEW_COLUMN_SHAPE
    assert _foreign_key_delete_action(migration_lab) == "n"
    assert (
        _scalar(
            migration_lab,
            "SELECT court_id FROM stage_items WHERE id = :stage_item_id",
            {"stage_item_id": stage_item_id},
        )
        is None
    )


def test_upgrade_does_not_touch_a_stage_item_already_assigned(migration_lab: Engine) -> None:
    """Sin backfill: el upgrade no inventa asignaciones para cuadros que no las tenian."""
    stage_item_id = _seed_stage_item(migration_lab)
    _alembic("upgrade", REVISION)
    assert (
        _scalar(
            migration_lab,
            "SELECT count(*) FROM stage_items WHERE id = :stage_item_id AND court_id IS NOT NULL",
            {"stage_item_id": stage_item_id},
        )
        == 0
    )


def test_deleting_a_court_leaves_the_stage_item_without_a_court(migration_lab: Engine) -> None:
    """El SET NULL es real: borrar el tatami desasigna el cuadro, no rompe el borrado."""
    stage_item_id = _seed_stage_item(migration_lab)
    _alembic("upgrade", REVISION)
    with migration_lab.begin() as connection:
        tournament_id = connection.execute(
            text("SELECT id FROM tournaments WHERE name = 'lab-tournament-s42'")
        ).scalar_one()
        court_id = connection.execute(
            text(
                "INSERT INTO courts (name, tournament_id) "
                "VALUES ('lab-court-s42', :tid) RETURNING id"
            ),
            {"tid": tournament_id},
        ).scalar_one()
        connection.execute(
            text("UPDATE stage_items SET court_id = :court_id WHERE id = :stage_item_id"),
            {"court_id": court_id, "stage_item_id": stage_item_id},
        )
        connection.execute(text("DELETE FROM courts WHERE id = :court_id"), {"court_id": court_id})

    assert (
        _scalar(
            migration_lab,
            "SELECT court_id FROM stage_items WHERE id = :stage_item_id",
            {"stage_item_id": stage_item_id},
        )
        is None
    )


def test_downgrade_removes_only_the_new_column_and_keeps_the_data(migration_lab: Engine) -> None:
    """El downgrade retira lo anadido y deja la fila real intacta."""
    stage_item_id = _seed_stage_item(migration_lab)
    _alembic("upgrade", REVISION)
    _alembic("downgrade", PREVIOUS_REVISION)

    assert _column(migration_lab) is None
    assert (
        _scalar(
            migration_lab,
            "SELECT name FROM stage_items WHERE id = :stage_item_id",
            {"stage_item_id": stage_item_id},
        )
        == "lab-item-s42"
    )


def test_the_revision_is_a_single_head_on_top_of_the_registration_window(
    migration_lab: Engine,
) -> None:
    """La revision cuelga de S3.4c y es una sola cabeza: no hay ramas ni migraciones paralelas."""
    assert _scalar(migration_lab, "SELECT version_num FROM alembic_version") == PREVIOUS_REVISION
    output = _alembic("heads")
    assert REVISION in output
    assert output.count("(head)") == 1
