# pylint: disable=redefined-outer-name  # `migration_lab` es el fixture de este modulo.
"""S3.3a — migracion de la auditoria con tenant: upgrade, backfill y downgrade.

TDD: el esqueleto de estas pruebas se escribio **antes** de la migracion
(``b3f7a1c92e04_s33a_domain_change_log_tenant.py``) y fallo en rojo (la revision no existia: la
columna no se creaba y el ``downgrade`` caia una revision de mas); la migracion posterior las
lleva a verde.

Se ejecuta contra una base **propia y temporal** (``bracket_s33a_migration_test_<pid>``) creada y
eliminada por el propio modulo sobre el servidor PostgreSQL de pruebas: no se toca ``bracket_test``
ni ``bracket_dev``, no se crean usuarios ni contenedores y no se escribe en ``bracket_ci``.

Que se comprueba:

* la migracion es aditiva (columna NULLABLE, FK a ``clubs(id)`` ``ON DELETE RESTRICT``, indice
  compuesto ``(tenant_club_id, created)``) y deja la cadena en la revision esperada;
* el backfill con entidades existentes, entidades eliminadas y entidades sin tenant (NULL):
  determinista, sin inventar atribucion;
* el backfill es reejecutable sin cambios inesperados;
* ``downgrade`` retira exactamente lo que anadio y no pierde el historico;
* upgrade sobre tabla vacia y reaplicacion tras downgrade.

Nota de metodo: la cadena Alembic **no es reproducible desde una base vacia** — la revision raiz
``274385f2a757`` (``down_revision = None``) asume un esquema legado previo y su ``upgrade`` hace
``DROP INDEX ix_users_email``, que no existe en una base recien creada. Por eso el laboratorio
reconstruye el estado **posterior a F3A** con el ``metadata`` vivo (``metadata.create_all`` y
despues retirada de la columna nueva, que es exactamente el aspecto fisico de ``domain_change_log``
en F3A), marca esa revision con ``alembic stamp`` y aplica ``upgrade head``: lo unico que se
ejecuta es la migracion de S3.3a, que es la que se quiere probar.
"""

from __future__ import annotations

import importlib.util
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
MIGRATION_FILE = (
    BACKEND_DIR / "alembic" / "versions" / "b3f7a1c92e04_s33a_domain_change_log_tenant.py"
)

F3A_REVISION = "f3a0c1d2e3f4"
S33A_REVISION = "b3f7a1c92e04"
SCRATCH_DATABASE_PREFIX = "bracket_s33a_migration_test"
TENANT_COLUMN = "tenant_club_id"
TENANT_INDEX = "ix_domain_change_log_tenant_created"
TENANT_FOREIGN_KEY = "fk_domain_change_log_tenant_club_id"

COUNT_COLUMN = """
    SELECT count(*) FROM information_schema.columns
    WHERE table_name = 'domain_change_log' AND column_name = :column
"""

# Historial previo a la migracion: se inserta SIN la columna nueva (que todavia no existe).
SEED = (
    "INSERT INTO clubs (name) VALUES ('S3.3a Tenant A') RETURNING id",
    "INSERT INTO clubs (name) VALUES ('S3.3a Tenant B') RETURNING id",
    "INSERT INTO competitors (display_name, managed_by_club_id)"
    " VALUES (:who, :tenant) RETURNING id",
    "INSERT INTO sports_clubs (name, tenant_club_id) VALUES (:who, :tenant) RETURNING id",
    """
    INSERT INTO tournaments (name, start_time, club_id, dashboard_public)
    VALUES ('S3.3a Torneo', NOW(), :tenant, false) RETURNING id
    """,
    """
    INSERT INTO tournament_registrations (tournament_id, competitor_id, competitor_name_snapshot)
    VALUES (:tournament, NULL, 'Inscripcion Historica') RETURNING id
    """,
    """
    INSERT INTO domain_change_log (entity, entity_id, action, changed_fields, reason)
    VALUES (:entity, :entity_id, :action, '{}'::text[], 'historico')
    """,
)


def _scratch_database() -> str:
    """Base propia del proceso: dos ejecuciones en paralelo no comparten ni pisan estado."""
    return f"{SCRATCH_DATABASE_PREFIX}_{os.getpid()}"


def _dsn(database: str) -> str:
    """DSN de laboratorio apuntando a otra base (el DSN de pruebas no lleva parametros)."""
    base = str(config.pg_dsn)
    return f"{base.rsplit('/', 1)[0]}/{database}"


def _alembic(*arguments: str) -> None:
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


def _migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("s33a_migration_module", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scalar(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> Any:
    with engine.connect() as connection:
        return connection.execute(text(sql), params or {}).scalar_one()


def _execute(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params or {})


def _tenant_columns(engine: Engine) -> int:
    return int(_scalar(engine, COUNT_COLUMN, {"column": TENANT_COLUMN}))


@pytest.fixture
def migration_lab() -> Iterator[Engine]:
    """Base temporal limpia en el servidor de pruebas, en el estado posterior a F3A."""
    admin = create_engine(_dsn("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{_scratch_database()}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{_scratch_database()}"'))

    engine = create_engine(_dsn(_scratch_database()))
    metadata.create_all(engine)
    # Aspecto fisico de domain_change_log en F3A: sin la columna nueva (con ella caen su indice y
    # su FK), que es lo unico que anadio la revision de S3.3a. Si el metadata vivo todavia no la
    # declara, el esquema ya esta en el estado previo y no hay nada que retirar.
    if _tenant_columns(engine):
        _execute(engine, f"ALTER TABLE domain_change_log DROP COLUMN {TENANT_COLUMN}")
    _alembic("stamp", F3A_REVISION)

    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{_scratch_database()}" WITH (FORCE)')
            )
        admin.dispose()


def _seed_history(engine: Engine) -> dict[str, int]:
    """Historial tipico de F3B: entidades vivas, una eliminada y dos entidades de plataforma."""
    with engine.begin() as connection:
        tenant_a = int(connection.execute(text(SEED[0])).scalar_one())
        tenant_b = int(connection.execute(text(SEED[1])).scalar_one())
        competitor_a = int(
            connection.execute(
                text(SEED[2]), {"who": "Historico De A", "tenant": tenant_a}
            ).scalar_one()
        )
        competitor_platform = int(
            connection.execute(
                text(SEED[2]), {"who": "Historico De Plataforma", "tenant": None}
            ).scalar_one()
        )
        sports_club_a = int(
            connection.execute(
                text(SEED[3]), {"who": "Academia De A", "tenant": tenant_a}
            ).scalar_one()
        )
        sports_club_platform = int(
            connection.execute(
                text(SEED[3]), {"who": "Academia De Plataforma", "tenant": None}
            ).scalar_one()
        )
        tournament_a = int(connection.execute(text(SEED[4]), {"tenant": tenant_a}).scalar_one())
        registration = int(
            connection.execute(text(SEED[5]), {"tournament": tournament_a}).scalar_one()
        )

        events = {
            "competitor_existing": ("competitor", competitor_a),
            "competitor_deleted": ("competitor", 999_999),
            "competitor_platform": ("competitor", competitor_platform),
            "sports_club_existing": ("sports_club", sports_club_a),
            "sports_club_platform": ("sports_club", sports_club_platform),
            "registration_existing": ("tournament_registration", registration),
            "registration_deleted": ("tournament_registration", 999_998),
        }
        for entity, entity_id in events.values():
            connection.execute(
                text(SEED[6]),
                {"entity": entity, "entity_id": entity_id, "action": "CREATE"},
            )

    return {
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "competitor_existing": competitor_a,
        "competitor_deleted": 999_999,
        "competitor_platform": competitor_platform,
        "sports_club_existing": sports_club_a,
        "sports_club_platform": sports_club_platform,
        "registration_existing": registration,
        "registration_deleted": 999_998,
    }


def _attribution(engine: Engine) -> dict[tuple[str, int], Any]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT entity, entity_id, tenant_club_id FROM domain_change_log")
        ).all()
    return {(str(row[0]), int(row[1])): row[2] for row in rows}


def test_upgrade_adds_the_tenant_column_with_fk_index_and_backfill(migration_lab: Engine) -> None:
    seeded = _seed_history(migration_lab)

    _alembic("upgrade", "head")

    assert _tenant_columns(migration_lab) == 1
    assert (
        _scalar(
            migration_lab,
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name = 'domain_change_log' AND column_name = :column
            """,
            {"column": TENANT_COLUMN},
        )
        == "YES"
    ), "la columna es NULLABLE: el historico y las entidades de plataforma no tienen tenant"

    assert (
        _scalar(
            migration_lab,
            "SELECT confdeltype FROM pg_constraint WHERE conname = :name",
            {"name": TENANT_FOREIGN_KEY},
        )
        == "r"
    ), "la FK es ON DELETE RESTRICT, como el resto de claves de tenant de F3A"

    index_definition = str(
        _scalar(
            migration_lab,
            "SELECT indexdef FROM pg_indexes WHERE indexname = :name",
            {"name": TENANT_INDEX},
        )
    )
    assert "tenant_club_id" in index_definition and "created" in index_definition

    attribution = _attribution(migration_lab)
    assert attribution[("competitor", seeded["competitor_existing"])] == seeded["tenant_a"]
    assert attribution[("sports_club", seeded["sports_club_existing"])] == seeded["tenant_a"]
    assert (
        attribution[("tournament_registration", seeded["registration_existing"])]
        == (seeded["tenant_a"])
    )
    assert attribution[("competitor", seeded["competitor_deleted"])] is None, "entidad eliminada"
    assert attribution[("competitor", seeded["competitor_platform"])] is None, "sin tenant"
    assert attribution[("sports_club", seeded["sports_club_platform"])] is None
    assert attribution[("tournament_registration", seeded["registration_deleted"])] is None

    with pytest.raises(IntegrityError):
        _execute(
            migration_lab,
            "UPDATE domain_change_log SET tenant_club_id = 999999 WHERE entity = 'competitor'",
        )

    with pytest.raises(IntegrityError):
        # El historico no puede quedarse apuntando a un club inexistente.
        _execute(
            migration_lab,
            "DELETE FROM clubs WHERE id = :club_id",
            {"club_id": seeded["tenant_a"]},
        )


def test_backfill_is_idempotent(migration_lab: Engine) -> None:
    seeded = _seed_history(migration_lab)
    _alembic("upgrade", "head")

    first = _attribution(migration_lab)
    statements = _migration_module().BACKFILL_STATEMENTS

    for _ in range(2):
        for statement in statements:
            _execute(migration_lab, statement)

    assert _attribution(migration_lab) == first, (
        "reejecutar el backfill no cambia nada: no reatribuye, no borra y no inventa tenant"
    )
    assert first[("competitor", seeded["competitor_existing"])] == seeded["tenant_a"]


def test_downgrade_removes_only_what_it_added(migration_lab: Engine) -> None:
    seeded = _seed_history(migration_lab)
    _alembic("upgrade", "head")

    _alembic("downgrade", "-1")

    assert _tenant_columns(migration_lab) == 0
    assert (
        _scalar(
            migration_lab,
            "SELECT count(*) FROM pg_indexes WHERE indexname = :name",
            {"name": TENANT_INDEX},
        )
        == 0
    )
    assert (
        _scalar(
            migration_lab,
            "SELECT count(*) FROM pg_constraint WHERE conname = :name",
            {"name": TENANT_FOREIGN_KEY},
        )
        == 0
    )
    assert _scalar(migration_lab, "SELECT count(*) FROM domain_change_log") == 7, (
        "el historico sobrevive al downgrade"
    )
    assert (
        _scalar(
            migration_lab,
            "SELECT count(*) FROM competitors WHERE id = :competitor_id",
            {"competitor_id": seeded["competitor_existing"]},
        )
        == 1
    )


def test_upgrade_is_safe_on_an_empty_table_and_repeatable(migration_lab: Engine) -> None:
    _alembic("upgrade", "head")

    assert _scalar(migration_lab, "SELECT count(*) FROM domain_change_log") == 0
    assert _tenant_columns(migration_lab) == 1

    _alembic("downgrade", "-1")
    _alembic("upgrade", "head")

    assert _tenant_columns(migration_lab) == 1
    assert _scalar(migration_lab, "SELECT version_num FROM alembic_version") == S33A_REVISION
