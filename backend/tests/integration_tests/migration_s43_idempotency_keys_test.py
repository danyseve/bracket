# pylint: disable=redefined-outer-name  # `migration_lab` es el fixture de este modulo.
"""Migracion de S4.3: claves permitidas del resultado confirmado en el almacen de idempotencia.

Se prueba contra una base temporal del servidor de pruebas, con el CLI real de Alembic: el esquema
vivo (``metadata.create_all``) no ejecuta la revision, asi que no sirve como evidencia de la
migracion.

Lo que fija este modulo:

* el upgrade deja la restriccion cerrada con **exactamente** las nueve claves: las cinco de S3.3c-3
  mas las cuatro del contrato de respuesta de la asignacion de tatami;
* el downgrade restaura exactamente la lista anterior, de modo que el contrato nuevo no puede
  colarse en una base que no ha aplicado esta revision.
"""

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from bracket.config import config
from bracket.schema import metadata

BACKEND_DIR = Path(__file__).resolve().parents[2]
SCRATCH_DATABASE = "bracket_test_migration_s43"
TABLE = "domain_idempotency_keys"
CONSTRAINT = "ck_domain_idempotency_keys_response_body"
PREVIOUS_REVISION = "b7f1c9d4e2a6"
REVISION = "c1e7a5b39d84"
S3_4C_KEYS = frozenset(
    {"audit_event_id", "competitor_id", "registration_status", "resource_id", "resource_type"}
)
TATAMI_KEYS = frozenset({"stage_item_id", "court_id", "court_name", "affected_match_count"})
CURRENT_KEYS = S3_4C_KEYS | TATAMI_KEYS


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


def _declared_keys(engine: Engine) -> frozenset[str]:
    """Claves que la restriccion admite, leidas de la definicion viva en el catalogo."""
    with engine.connect() as connection:
        definition = connection.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"),
            {"name": CONSTRAINT},
        ).scalar_one()

    segment = str(definition).split("ARRAY[", 1)[1].split("]::text[]", 1)[0]
    return frozenset(re.findall(r"'([a-z_]+)'", segment))


def _constraint_exists(engine: Engine) -> bool:
    with engine.connect() as connection:
        return bool(
            connection.execute(
                text("SELECT count(*) FROM pg_constraint WHERE conname = :name"),
                {"name": CONSTRAINT},
            ).scalar_one()
        )


def _scratch_database(admin_dsn: str, action: str) -> None:
    admin = create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}" WITH (FORCE)'))
        if action == "create":
            connection.execute(text(f'CREATE DATABASE "{SCRATCH_DATABASE}"'))
    admin.dispose()


def _body_check(keys: tuple[str, ...]) -> str:
    """Misma expresion que declara el modelo, con la lista de claves que se le pase."""
    return (
        "response_body IS NULL OR (jsonb_typeof(response_body) = 'object'"
        " AND response_body - ARRAY["
        + ", ".join(f"'{key}'" for key in keys)
        + "]::text[] = '{}'::jsonb"
        " AND octet_length(response_body::text) <= 2048"
        ' AND NOT jsonb_path_exists(response_body, \'$.* ? (@.type() == "object"'
        ' || @.type() == "array")\'))'
    )


def _execute(engine: Engine, sql: str) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql))


@pytest.fixture
def migration_lab() -> Iterator[Engine]:
    """Base temporal con el aspecto fisico previo a esta revision (restriccion de S3.3c-3)."""
    _scratch_database(_dsn("postgres"), "create")
    engine = create_engine(_dsn(SCRATCH_DATABASE))
    metadata.create_all(engine)
    # El esquema vivo ya declara las nueve claves, asi que se sustituye la restriccion por la de
    # S3.3c-3: es el estado que tenia la tabla antes de esta revision.
    _execute(engine, f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    _execute(
        engine,
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT} "
        f"CHECK ({_body_check(tuple(sorted(S3_4C_KEYS)))})",
    )
    # S4.4b: el metadata vivo declara ademas las columnas del resultado de combate (revision
    # e4b8d1a70c52, posterior a la que prueba este modulo); se retiran para dejar el estado
    # fisico previo de verdad, o el ``upgrade head`` del test chocaria con DuplicateColumn.
    for column in ("winner_stage_item_input_id", "result_method", "finished_at"):
        _execute(engine, f"ALTER TABLE matches DROP COLUMN IF EXISTS {column}")

    _alembic("stamp", PREVIOUS_REVISION)

    try:
        yield engine
    finally:
        engine.dispose()
        _scratch_database(_dsn("postgres"), "drop")


def test_the_upgrade_closes_the_response_body_to_the_new_key_set(
    migration_lab: Engine,
) -> None:
    """Tras el upgrade la restriccion admite las nueve claves, y solo esas."""
    _alembic("upgrade", REVISION)
    with migration_lab.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert revision == REVISION
    assert _declared_keys(migration_lab) == CURRENT_KEYS


def test_the_downgrade_restores_the_previous_key_set(migration_lab: Engine) -> None:
    """El downgrade vuelve a cerrar la lista de S3.3c-3 y el upgrade la amplia otra vez."""
    _alembic("upgrade", REVISION)
    _alembic("downgrade", PREVIOUS_REVISION)

    assert _declared_keys(migration_lab) == S3_4C_KEYS
    assert _constraint_exists(migration_lab)

    _alembic("upgrade", "head")

    # Una revision posterior (S4.5) amplia la lista; las de S4.3 siguen siendo subconjunto.
    assert CURRENT_KEYS <= _declared_keys(migration_lab)
    assert _constraint_exists(migration_lab)
