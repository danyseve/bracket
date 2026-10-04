# pylint: disable=redefined-outer-name  # `migration_lab` es el fixture de este modulo.
"""S3.3c-3 — migracion de la tabla de idempotencia: upgrade, restricciones y downgrade.

TDD: el esqueleto se escribio **antes** de la revision (``9c3e7a1d5b28_s33c3_idempotency_keys``)
y fallo en rojo (la revision no existia y la tabla tampoco); la migracion posterior lo lleva a
verde.

Se ejecuta contra una base **propia y temporal** (``bracket_s33c3_migration_test_<pid>``) creada y
eliminada por el propio modulo sobre el servidor PostgreSQL de pruebas: no se toca ``bracket_test``
ni ``bracket_dev``, no se crean usuarios ni contenedores y no se escribe en ``bracket_ci``.

Que se comprueba:

* la migracion es **aditiva**: crea solo la tabla nueva y no toca ninguna tabla historica (se
  verifica tambien leyendo el propio fichero de revision: sin ``UPDATE``, ``DELETE`` ni ``INSERT``);
* la forma fisica de cada columna (tipo, nulabilidad) es la declarada;
* estan los tres CHECK de forma, el de estado, el de coherencia de recurso y el de caducidad;
* el indice unico esta acotado a (tenant, actor, clave) y el indice de caducidad no es unico;
* las claves ajenas apuntan a ``clubs`` y ``users``;
* ``downgrade`` retira exactamente lo que anadio y deja el resto intacto;
* tras el downgrade, ``upgrade`` vuelve a funcionar.

Metodo (igual que en S3.3c-1/S3.3c-2): la cadena Alembic no es reproducible desde una base vacia
(la revision raiz asume un esquema legado previo). El laboratorio reconstruye el estado posterior a
S3.3c-2 con el ``metadata`` vivo, retira la tabla nueva —que es exactamente el aspecto fisico antes
de esta revision—, marca ``d5c1a7e83f21`` con ``alembic stamp`` y aplica ``upgrade head``: lo unico
que se ejecuta es la migracion de S3.3c-3.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from bracket.config import config
from bracket.schema import metadata

BACKEND_DIR = Path(__file__).resolve().parents[2]
MIGRATION_FILE = BACKEND_DIR / "alembic" / "versions" / "9c3e7a1d5b28_s33c3_idempotency_keys.py"

S33C2_REVISION = "d5c1a7e83f21"
S33C3_REVISION = "9c3e7a1d5b28"
SCRATCH_DATABASE_PREFIX = "bracket_s33c3_migration_test"
TABLE = "domain_idempotency_keys"

EXPECTED_COLUMNS = {
    "id": ("bigint", "NO"),
    "tenant_club_id": ("bigint", "NO"),
    "actor_user_id": ("bigint", "NO"),
    "idempotency_key": ("character varying", "NO"),
    "request_fingerprint": ("character varying", "NO"),
    "fingerprint_key_version": ("character varying", "NO"),
    "request_method": ("character varying", "NO"),
    "request_path": ("character varying", "NO"),
    "state": ("character varying", "NO"),
    "resource_type": ("character varying", "YES"),
    "resource_id": ("bigint", "YES"),
    "response_status": ("smallint", "YES"),
    "response_body": ("jsonb", "YES"),
    "created": ("timestamp with time zone", "NO"),
    "expires_at": ("timestamp with time zone", "NO"),
    "completed_at": ("timestamp with time zone", "YES"),
}

EXPECTED_CHECKS = (
    "ck_domain_idempotency_keys_state",
    "ck_domain_idempotency_keys_pending_shape",
    "ck_domain_idempotency_keys_completed_shape",
    "ck_domain_idempotency_keys_resource_coherence",
    "ck_domain_idempotency_keys_request_method",
    "ck_domain_idempotency_keys_request_path",
    "ck_domain_idempotency_keys_key_shape",
    "ck_domain_idempotency_keys_fingerprint",
    "ck_domain_idempotency_keys_fingerprint_version",
    "ck_domain_idempotency_keys_response_status",
    "ck_domain_idempotency_keys_response_body",
    "ck_domain_idempotency_keys_expiry",
)
UNIQUE_CONSTRAINT = "uq_domain_idempotency_keys_tenant_actor_key"
FOREIGN_KEYS = (
    "fk_domain_idempotency_keys_tenant_club_id",
    "fk_domain_idempotency_keys_actor_user_id",
)
EXPIRY_INDEX = "ix_domain_idempotency_keys_expires_at"


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


def _table_exists(engine: Engine) -> bool:
    return bool(
        _scalar(
            engine,
            "SELECT count(*) FROM information_schema.tables WHERE table_name = :name",
            {"name": TABLE},
        )
    )


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


def _drop_new_table(engine: Engine) -> None:
    if _table_exists(engine):
        _execute(engine, f"DROP TABLE {TABLE}")


def _seed_tenant_and_actor(engine: Engine) -> tuple[int, int]:
    """Un club y un usuario reales (las claves ajenas nuevas apuntan a ellos)."""
    with engine.begin() as connection:
        club_id = connection.execute(
            text("INSERT INTO clubs (name) VALUES ('lab-club') RETURNING id")
        ).scalar_one()
        user_id = connection.execute(
            text(
                """
                INSERT INTO users (email, name, password_hash, account_type)
                VALUES ('lab-s33c3@example.org', 'Lab', 'synthetic', 'REGULAR') RETURNING id
                """
            )
        ).scalar_one()
    return int(club_id), int(user_id)


RAW_INSERT = """
    INSERT INTO domain_idempotency_keys
        (tenant_club_id, actor_user_id, idempotency_key, request_fingerprint,
         fingerprint_key_version, request_method, request_path, state, resource_type, resource_id,
         response_status, response_body, created, expires_at, completed_at)
    VALUES
        (:tenant_club_id, :actor_user_id, :key, repeat('a', 64), 'v1', 'POST',
         '/clubs/1/competitors', :state, :resource_type, :resource_id, :response_status,
         CAST(:response_body AS jsonb), now(), now() + interval '1 hour', :completed_at)
"""


def _insert(engine: Engine, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "tenant_club_id": 1,
        "actor_user_id": 1,
        "key": "migration-lab-key-0001",
        "state": "IN_PROGRESS",
        "resource_type": None,
        "resource_id": None,
        "response_status": None,
        "response_body": None,
        "completed_at": None,
    }
    values.update(overrides)
    _execute(engine, RAW_INSERT, values)


def _constraint_name(error: Exception) -> str | None:
    original = getattr(error, "orig", None)
    diagnostic = getattr(original, "diag", None)
    return getattr(diagnostic, "constraint_name", None)


@pytest.fixture
def migration_lab() -> Iterator[Engine]:
    """Base temporal limpia en el servidor de pruebas, en el estado posterior a S3.3c-2."""
    admin = create_engine(_dsn("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{_scratch_database()}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{_scratch_database()}"'))

    engine = create_engine(_dsn(_scratch_database()))
    metadata.create_all(engine)
    # Aspecto fisico antes de esta revision: la tabla nueva no existe. Si el metadata vivo todavia
    # no la declara, el esquema ya esta en el estado previo y no hay nada que retirar.
    _drop_new_table(engine)
    _alembic("stamp", S33C2_REVISION)

    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{_scratch_database()}" WITH (FORCE)')
            )
        admin.dispose()


def test_upgrade_creates_the_table_with_the_declared_columns(migration_lab: Engine) -> None:
    _alembic("upgrade", "head")

    with migration_lab.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns WHERE table_name = :table
                """
            ),
            {"table": TABLE},
        ).all()
    columns = {str(row[0]): (str(row[1]), str(row[2])) for row in rows}

    assert columns == EXPECTED_COLUMNS
    assert _scalar(migration_lab, "SELECT version_num FROM alembic_version") == S33C3_REVISION


def test_upgrade_declares_every_check_unique_and_foreign_key(migration_lab: Engine) -> None:
    _alembic("upgrade", "head")

    assert set(_constraints(migration_lab, "c")) >= set(EXPECTED_CHECKS)
    assert UNIQUE_CONSTRAINT in _constraints(migration_lab, "u")
    assert set(FOREIGN_KEYS) <= set(_constraints(migration_lab, "f"))


def test_the_expiry_index_exists_and_is_not_unique(migration_lab: Engine) -> None:
    _alembic("upgrade", "head")

    with migration_lab.connect() as connection:
        definitions = [
            str(row[0])
            for row in connection.execute(
                text("SELECT indexdef FROM pg_indexes WHERE tablename = :table"), {"table": TABLE}
            ).all()
        ]

    expiry = [item for item in definitions if EXPIRY_INDEX in item]
    assert len(expiry) == 1
    assert "UNIQUE" not in expiry[0]
    assert any("UNIQUE" in item for item in definitions), "el indice de la clave unica sigue ahi"


def test_the_migration_only_creates_the_table_and_writes_no_history() -> None:
    source = MIGRATION_FILE.read_text(encoding="utf-8")

    assert "op.create_table(" in source
    assert "op.drop_table(" in source
    for forbidden in ("op.execute", "UPDATE ", "DELETE ", "INSERT ", "bulk_insert"):
        assert forbidden not in source, f"la migracion no debe contener {forbidden!r}"


def test_upgrade_leaves_history_untouched_and_the_new_table_empty(migration_lab: Engine) -> None:
    club_id, user_id = _seed_tenant_and_actor(migration_lab)

    _alembic("upgrade", "head")

    assert _scalar(migration_lab, "SELECT count(*) FROM clubs WHERE id = :id", {"id": club_id}) == 1
    assert _scalar(migration_lab, "SELECT count(*) FROM users WHERE id = :id", {"id": user_id}) == 1
    assert _scalar(migration_lab, f"SELECT count(*) FROM {TABLE}") == 0


def test_the_state_check_closes_the_vocabulary(migration_lab: Engine) -> None:
    _alembic("upgrade", "head")
    club_id, user_id = _seed_tenant_and_actor(migration_lab)

    _insert(
        migration_lab,
        tenant_club_id=club_id,
        actor_user_id=user_id,
        state="IN_PROGRESS",
    )

    with pytest.raises(IntegrityError) as error:
        _insert(
            migration_lab,
            tenant_club_id=club_id,
            actor_user_id=user_id,
            key="migration-lab-key-0002",
            state="COMMITTED",
        )

    assert _constraint_name(error.value) == "ck_domain_idempotency_keys_state"


def test_a_completed_row_without_its_response_is_rejected(migration_lab: Engine) -> None:
    _alembic("upgrade", "head")
    club_id, user_id = _seed_tenant_and_actor(migration_lab)

    with pytest.raises(IntegrityError) as error:
        _insert(
            migration_lab,
            tenant_club_id=club_id,
            actor_user_id=user_id,
            state="COMPLETED",
            completed_at=None,
            response_status=None,
        )

    assert _constraint_name(error.value) == "ck_domain_idempotency_keys_completed_shape"


def test_a_pending_row_cannot_carry_response_data(migration_lab: Engine) -> None:
    _alembic("upgrade", "head")
    club_id, user_id = _seed_tenant_and_actor(migration_lab)

    with pytest.raises(IntegrityError) as error:
        _insert(
            migration_lab,
            tenant_club_id=club_id,
            actor_user_id=user_id,
            state="IN_PROGRESS",
            response_status=200,
        )

    assert _constraint_name(error.value) == "ck_domain_idempotency_keys_pending_shape"


@pytest.mark.parametrize(
    "body", ['{"token": "abc"}', '{"resource_id": {"nested": 1}}', "[1, 2, 3]"]
)
def test_the_response_body_check_rejects_disallowed_content(
    migration_lab: Engine, body: str
) -> None:
    _alembic("upgrade", "head")
    club_id, user_id = _seed_tenant_and_actor(migration_lab)

    with pytest.raises(IntegrityError) as error:
        _insert(
            migration_lab,
            tenant_club_id=club_id,
            actor_user_id=user_id,
            state="COMPLETED",
            resource_type="tournament_registration",
            resource_id=7,
            response_status=201,
            response_body=body,
            completed_at=datetime.now(UTC),
        )

    assert _constraint_name(error.value) == "ck_domain_idempotency_keys_response_body"


def test_the_unique_index_is_scoped_by_tenant_and_actor(migration_lab: Engine) -> None:
    _alembic("upgrade", "head")
    club_a, user_a = _seed_tenant_and_actor(migration_lab)
    with migration_lab.begin() as connection:
        club_b = connection.execute(
            text("INSERT INTO clubs (name) VALUES ('lab-club-b') RETURNING id")
        ).scalar_one()

    _insert(migration_lab, tenant_club_id=club_a, actor_user_id=user_a)
    _insert(migration_lab, tenant_club_id=club_b, actor_user_id=user_a)

    with pytest.raises(IntegrityError) as error:
        _insert(migration_lab, tenant_club_id=club_a, actor_user_id=user_a)

    assert _constraint_name(error.value) == UNIQUE_CONSTRAINT


def test_the_foreign_keys_reject_unknown_tenant_and_actor(migration_lab: Engine) -> None:
    _alembic("upgrade", "head")
    club_id, user_id = _seed_tenant_and_actor(migration_lab)

    with pytest.raises(IntegrityError) as error:
        _insert(migration_lab, tenant_club_id=999_999_999, actor_user_id=user_id)

    assert _constraint_name(error.value) == "fk_domain_idempotency_keys_tenant_club_id"

    with pytest.raises(IntegrityError) as error:
        _insert(migration_lab, tenant_club_id=club_id, actor_user_id=999_999_999)

    assert _constraint_name(error.value) == "fk_domain_idempotency_keys_actor_user_id"


def test_downgrade_removes_exactly_what_it_added(migration_lab: Engine) -> None:
    _alembic("upgrade", "head")
    club_id, user_id = _seed_tenant_and_actor(migration_lab)
    _insert(migration_lab, tenant_club_id=club_id, actor_user_id=user_id)

    _alembic("downgrade", S33C2_REVISION)

    assert not _table_exists(migration_lab)
    assert _scalar(migration_lab, "SELECT version_num FROM alembic_version") == S33C2_REVISION
    assert _scalar(migration_lab, "SELECT count(*) FROM clubs WHERE id = :id", {"id": club_id}) == 1
    assert _scalar(migration_lab, "SELECT count(*) FROM users WHERE id = :id", {"id": user_id}) == 1
    assert (
        _scalar(
            migration_lab,
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'domain_change_log'",
        )
        == 1
    ), "el resto del esquema sigue intacto"


def test_upgrade_can_be_reapplied_after_a_downgrade(migration_lab: Engine) -> None:
    _alembic("upgrade", "head")
    _alembic("downgrade", S33C2_REVISION)
    _alembic("upgrade", "head")

    assert _table_exists(migration_lab)
    assert _scalar(migration_lab, f"SELECT count(*) FROM {TABLE}") == 0
    assert _scalar(migration_lab, "SELECT version_num FROM alembic_version") == S33C3_REVISION
    assert set(_constraints(migration_lab, "c")) >= set(EXPECTED_CHECKS)
