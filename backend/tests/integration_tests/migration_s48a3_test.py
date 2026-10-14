# pylint: disable=redefined-outer-name  # `migration_lab` es el fixture de este modulo.
"""Migracion de S4.8a-3 §3/§4: afiliacion del competidor y logo de la academia.

Se prueba contra una base temporal del servidor de pruebas, con el CLI real de Alembic: el esquema
vivo (``metadata.create_all``) no ejecuta la revision, asi que no sirve como evidencia.

Lo que fija este modulo:

* el upgrade admite en ``domain_change_log`` el par ``(competitor, UPDATE,
  AFFILIATION_CHANGE)`` y el par ``(sports_club, UPDATE, CLUB_LOGO_CHANGE)``, y **no** otros
  cruces;
* anade **solo** la columna nulable ``sports_clubs.logo_path``;
* el downgrade retira exactamente lo anadido y deja la cadena con **una sola cabeza**.
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
from sqlalchemy.exc import IntegrityError

from bracket.config import config
from bracket.schema import metadata

BACKEND_DIR = Path(__file__).resolve().parents[2]
SCRATCH_DATABASE = "bracket_test_migration_s48a3"
PREVIOUS_REVISION = "c1a4f7e92b08"
REVISION = "f4b8d2a91c05"

CATALOG_CHECK = "ck_domain_change_log_reason_code_catalog"
ACTION_CHECK = "ck_domain_change_log_reason_code_action"
LOGO_COLUMN = "logo_path"

#: Estado anterior a esta revision: la foto congelada que vive hoy en `bracket/schema.py`.
PREVIOUS_REASON_CODES: tuple[str, ...] = (
    "ADMINISTRATIVE",
    "CATEGORY_CHANGE",
    "DATA_CORRECTION",
    "DATA_ERROR",
    "DATA_VERIFIED",
    "DUPLICATE",
    "ELIGIBILITY",
    "ELIGIBILITY_LOST",
    "ELIGIBILITY_REGAINED",
    "MISTAKEN_WITHDRAWAL",
    "PLANNED_ENTRY",
    "READY",
    "REPRESENTATION_CHANGE",
    "REQUESTED_BY_ATHLETE",
    "RULE_VIOLATION",
    "SCHEDULING_NO_SHOW",
    "WITHDRAWAL_REQUEST",
)
PREVIOUS_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("competitor", "CREATE", "PLANNED_ENTRY"),
    ("competitor", "UPDATE", "DATA_CORRECTION"),
    ("competitor", "DEACTIVATE", "REQUESTED_BY_ATHLETE"),
    ("competitor", "DEACTIVATE", "DUPLICATE"),
    ("competitor", "DEACTIVATE", "DATA_ERROR"),
    ("competitor", "DEACTIVATE", "ADMINISTRATIVE"),
    ("competitor", "ACTIVATE", "REQUESTED_BY_ATHLETE"),
    ("competitor", "ACTIVATE", "DUPLICATE"),
    ("competitor", "ACTIVATE", "DATA_ERROR"),
    ("competitor", "ACTIVATE", "ADMINISTRATIVE"),
    ("tournament_registration", "CREATE", "PLANNED_ENTRY"),
    ("tournament_registration", "UPDATE", "DATA_CORRECTION"),
    ("tournament_registration", "UPDATE", "CATEGORY_CHANGE"),
    ("tournament_registration", "UPDATE", "REPRESENTATION_CHANGE"),
    ("tournament_registration", "CONFIRM", "READY"),
    ("tournament_registration", "CONFIRM", "DATA_VERIFIED"),
    ("tournament_registration", "WITHDRAW", "WITHDRAWAL_REQUEST"),
    ("tournament_registration", "WITHDRAW", "DUPLICATE"),
    ("tournament_registration", "WITHDRAW", "ELIGIBILITY_LOST"),
    ("tournament_registration", "WITHDRAW", "ADMINISTRATIVE"),
    ("tournament_registration", "REINSTATE", "MISTAKEN_WITHDRAWAL"),
    ("tournament_registration", "REINSTATE", "ELIGIBILITY_REGAINED"),
    ("tournament_registration", "REINSTATE", "ADMINISTRATIVE"),
    ("tournament_registration", "DISQUALIFY", "SCHEDULING_NO_SHOW"),
    ("tournament_registration", "DISQUALIFY", "RULE_VIOLATION"),
    ("tournament_registration", "DISQUALIFY", "ELIGIBILITY"),
    ("tournament_registration", "DISQUALIFY", "ADMINISTRATIVE"),
)

#: Compromiso que la revision anade al catalogo de auditoria (entidad, accion, codigo).
ADDED_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("competitor", "UPDATE", "AFFILIATION_CHANGE"),
    ("sports_club", "UPDATE", "CLUB_LOGO_CHANGE"),
)

INSERT_LOG = (
    "INSERT INTO domain_change_log "
    "(entity, entity_id, action, changed_fields, reason, reason_code) "
    "VALUES (:entity, 1, :action, '{}', 'lab', :code)"
)


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
    with engine.begin() as connection:
        return connection.execute(text(sql), params or {}).scalar_one()


def _rows(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(text(sql), params or {}).all())


def _execute(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params or {})


def _fails(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> None:
    with pytest.raises(IntegrityError):
        _execute(engine, sql, params)


def _columns(engine: Engine, table: str) -> dict[str, tuple[str, str, str]]:
    """Tipo (``data_type``, ``udt_name``) y nulabilidad de cada columna de la tabla."""
    return {
        str(row[0]): (str(row[1]), str(row[2]), str(row[3]))
        for row in _rows(
            engine,
            """
            SELECT column_name, data_type, udt_name, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :table
            """,
            {"table": table},
        )
    }


def _catalog_check_sql(codes: tuple[str, ...]) -> str:
    """Misma forma literal que ``bracket/schema.py``: la migracion no importa codigo vivo."""
    joined = ", ".join(f"'{code}'" for code in codes)
    return f"reason_code IS NULL OR reason_code IN ({joined})"


def _action_check_sql(pairs: tuple[tuple[str, str, str], ...]) -> str:
    joined = ", ".join(f"('{entity}', '{action}', '{code}')" for entity, action, code in pairs)
    return f"reason_code IS NULL OR (entity, action, reason_code) IN ({joined})"


def _remove_everything_this_revision_adds(engine: Engine) -> None:
    """Devuelve la base temporal al estado previo: sin ``logo_path`` y con los CHECK de antes."""
    _execute(engine, "ALTER TABLE sports_clubs DROP COLUMN IF EXISTS logo_path")
    _execute(engine, f"ALTER TABLE domain_change_log DROP CONSTRAINT IF EXISTS {CATALOG_CHECK}")
    _execute(engine, f"ALTER TABLE domain_change_log DROP CONSTRAINT IF EXISTS {ACTION_CHECK}")
    _execute(
        engine,
        f"ALTER TABLE domain_change_log ADD CONSTRAINT {CATALOG_CHECK} "
        f"CHECK ({_catalog_check_sql(PREVIOUS_REASON_CODES)})",
    )
    _execute(
        engine,
        f"ALTER TABLE domain_change_log ADD CONSTRAINT {ACTION_CHECK} "
        f"CHECK ({_action_check_sql(PREVIOUS_PAIRS)})",
    )


@pytest.fixture
def migration_lab() -> Iterator[Engine]:
    """Base temporal limpia en el servidor de pruebas, en el estado posterior a S4.8a-1."""
    admin = create_engine(_dsn("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{SCRATCH_DATABASE}"'))

    engine = create_engine(_dsn(SCRATCH_DATABASE))
    metadata.create_all(engine)
    _remove_everything_this_revision_adds(engine)
    _alembic("stamp", PREVIOUS_REVISION)

    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}" WITH (FORCE)'))
        admin.dispose()


def test_the_previous_state_lacks_the_new_column_and_the_new_pairs(migration_lab: Engine) -> None:
    """Punto de partida: sin ``sports_clubs.logo_path`` y con el catalogo de auditoria de antes."""
    assert _scalar(migration_lab, "SELECT version_num FROM alembic_version") == PREVIOUS_REVISION
    assert LOGO_COLUMN not in _columns(migration_lab, "sports_clubs")
    for entity, action, code in ADDED_PAIRS:
        _fails(migration_lab, INSERT_LOG, {"entity": entity, "action": action, "code": code})
    # El catalogo anterior sigue aceptando lo suyo.
    _execute(
        migration_lab,
        INSERT_LOG,
        {"entity": "competitor", "action": "UPDATE", "code": "DATA_CORRECTION"},
    )


def test_upgrade_adds_the_optional_logo_and_the_two_new_audit_pairs(migration_lab: Engine) -> None:
    """El upgrade habilita el logo nulable y los dos pares de auditoria nuevos, y nada mas."""
    _alembic("upgrade", REVISION)

    assert _columns(migration_lab, "sports_clubs")[LOGO_COLUMN] == (
        "character varying",
        "varchar",
        "YES",
    )
    for entity, action, code in ADDED_PAIRS:
        _execute(migration_lab, INSERT_LOG, {"entity": entity, "action": action, "code": code})
    # Cruces que NO autoriza esta revision: el codigo ama el par, no la entidad sola.
    _fails(
        migration_lab,
        INSERT_LOG,
        {"entity": "sports_club", "action": "UPDATE", "code": "DATA_CORRECTION"},
    )
    _fails(
        migration_lab,
        INSERT_LOG,
        {"entity": "competitor", "action": "UPDATE", "code": "CLUB_LOGO_CHANGE"},
    )


def test_upgrade_is_repeatable_and_leaves_a_single_head(migration_lab: Engine) -> None:
    """Reaplicar la revision es inocuo y la cadena queda con una sola cabeza.

    La cabeza puede ser posterior a esta fase: la prueba protege el invariante (una sola cabeza y la
    base en ella), no el nombre concreto de la revision de S4.8a-3.
    """
    _alembic("upgrade", REVISION)
    _alembic("upgrade", REVISION)
    _alembic("upgrade", "head")

    heads = [line.strip() for line in _alembic("heads").splitlines() if line.strip()]
    assert len(heads) == 1, heads
    head_revision = heads[0].split()[0]
    assert _scalar(migration_lab, "SELECT version_num FROM alembic_version") == head_revision


def test_downgrade_retires_exactly_what_the_upgrade_added(migration_lab: Engine) -> None:
    """El downgrade retira la columna y los dos pares, y vuelve a la revision anterior."""
    _alembic("upgrade", REVISION)
    _alembic("downgrade", PREVIOUS_REVISION)

    assert _scalar(migration_lab, "SELECT version_num FROM alembic_version") == PREVIOUS_REVISION
    assert LOGO_COLUMN not in _columns(migration_lab, "sports_clubs")
    for entity, action, code in ADDED_PAIRS:
        _fails(migration_lab, INSERT_LOG, {"entity": entity, "action": action, "code": code})
    _execute(
        migration_lab,
        INSERT_LOG,
        {"entity": "competitor", "action": "UPDATE", "code": "DATA_CORRECTION"},
    )
