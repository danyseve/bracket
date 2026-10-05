# pylint: disable=redefined-outer-name  # `migration_lab` es el fixture de este modulo.
"""S3.3c-2 — columnas de motivo cerrado en la auditoria: upgrade, CHECKs y downgrade.

TDD: el esqueleto de estas pruebas se escribio **antes** de la migracion
(``d5c1a7e83f21_s33c2_audit_reason_code.py``) y fallo en rojo (la revision no existia: las
columnas no se creaban y los CHECK no existian); la migracion posterior las lleva a verde.

Se ejecuta contra una base **propia y temporal** (``bracket_s33c2_migration_test_<pid>``) creada y
eliminada por el propio modulo sobre el servidor PostgreSQL de pruebas: no se toca ``bracket_test``
ni ``bracket_dev``, no se crean usuarios ni contenedores y no se escribe en ``bracket_ci``.

Que se comprueba:

* la migracion es aditiva: dos columnas NULLABLE (``reason_code`` ``String(32)`` y ``reason_note``
  ``Text``) y deja la cadena en la revision esperada;
* el historico **no se reescribe**: las filas anteriores quedan con ``reason_code IS NULL`` y
  siguen siendo validas;
* los CHECK cierran el vocabulario: codigo fuera del catalogo, codigo que no corresponde a la
  accion, nota con un codigo que no la admite y nota fuera de forma o longitud se rechazan;
* entidades sin catalogo (``sports_club``) siguen pudiendo auditarse sin codigo, como antes;
* ``downgrade`` retira exactamente lo que anadio y no pierde el historico;
* upgrade sobre tabla vacia y reaplicacion tras downgrade.

Nota de metodo: la cadena Alembic **no es reproducible desde una base vacia** (la revision raiz
``274385f2a757`` asume un esquema legado previo). Por eso el laboratorio reconstruye el estado
posterior a S3.3a con el ``metadata`` vivo, retira las columnas nuevas —que es exactamente el
aspecto fisico de ``domain_change_log`` antes de esta revision—, marca la revision ``b3f7a1c92e04``
con ``alembic stamp`` y aplica ``upgrade`` hasta ``d5c1a7e83f21``: lo unico que se ejecuta es la
migracion de S3.3c-2 (fijar la revision propia, y no ``head``, mantiene la prueba valida cuando
una fase posterior anade revisiones, igual que hace el laboratorio de S3.3a).
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
from sqlalchemy.exc import IntegrityError

from bracket.config import config
from bracket.schema import metadata

BACKEND_DIR = Path(__file__).resolve().parents[2]

S33A_REVISION = "b3f7a1c92e04"
S33C2_REVISION = "d5c1a7e83f21"
SCRATCH_DATABASE_PREFIX = "bracket_s33c2_migration_test"

ADDED_COLUMNS = ("reason_code", "reason_note")
NEW_CHECKS = (
    "ck_domain_change_log_reason_code_catalog",
    "ck_domain_change_log_reason_code_action",
    "ck_domain_change_log_reason_note",
)

# Historial previo a la migracion: se inserta SIN las columnas nuevas (que todavia no existen).
SEED = (
    """
    INSERT INTO domain_change_log (entity, entity_id, action, changed_fields, reason)
    VALUES (:entity, :entity_id, :action, '{}'::text[], :reason)
    """,
)

INSERT_EVENT = """
    INSERT INTO domain_change_log
        (entity, entity_id, action, changed_fields, reason, reason_code, reason_note)
    VALUES (:entity, 1, :action, '{}'::text[], 'texto canonico', :code, :note)
"""


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


def _scalar(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> Any:
    with engine.connect() as connection:
        return connection.execute(text(sql), params or {}).scalar_one()


def _execute(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params or {})


def _column_definition(engine: Engine, column: str) -> tuple[str, Any, str]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT data_type, character_maximum_length, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'domain_change_log' AND column_name = :column
                """
            ),
            {"column": column},
        ).one_or_none()
    assert row is not None, f"falta la columna {column}"
    return str(row[0]), row[1], str(row[2])


def _check_count(engine: Engine, name: str) -> int:
    return int(
        _scalar(
            engine,
            "SELECT count(*) FROM pg_constraint WHERE conname = :name AND contype = 'c'",
            {"name": name},
        )
    )


@pytest.fixture
def migration_lab() -> Iterator[Engine]:
    """Base temporal limpia en el servidor de pruebas, en el estado posterior a S3.3a."""
    admin = create_engine(_dsn("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{_scratch_database()}" WITH (FORCE)'))
        connection.execute(text(f'CREATE DATABASE "{_scratch_database()}"'))

    engine = create_engine(_dsn(_scratch_database()))
    metadata.create_all(engine)
    # Aspecto fisico de domain_change_log antes de esta revision: sin las dos columnas nuevas (con
    # ellas caen tambien sus CHECK). Si el metadata vivo todavia no las declara, el esquema ya esta
    # en el estado previo y no hay nada que retirar.
    _drop_added_columns(engine)
    _alembic("stamp", S33A_REVISION)

    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{_scratch_database()}" WITH (FORCE)')
            )
        admin.dispose()


def _drop_added_columns(engine: Engine) -> None:
    for column in ADDED_COLUMNS:
        exists = int(
            _scalar(
                engine,
                """
                SELECT count(*) FROM information_schema.columns
                WHERE table_name = 'domain_change_log' AND column_name = :column
                """,
                {"column": column},
            )
        )
        if exists:
            _execute(engine, f"ALTER TABLE domain_change_log DROP COLUMN {column}")


def _seed_history(engine: Engine) -> None:
    """Historial tipico: eventos de competidor, de inscripcion y de entidad de plataforma."""
    events = {
        ("competitor", 101, "CREATE"): "alta de competidor",
        ("competitor", 101, "DEACTIVATE"): "baja logica de competidor",
        ("tournament_registration", 202, "WITHDRAW"): "retirada solicitada",
        ("sports_club", 303, "CREATE"): "alta de academia",
    }
    with engine.begin() as connection:
        for (entity, entity_id, action), reason in events.items():
            connection.execute(
                text(SEED[0]),
                {"entity": entity, "entity_id": entity_id, "action": action, "reason": reason},
            )


def _historical_codes(engine: Engine) -> list[Any]:
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                text("SELECT reason_code FROM domain_change_log ORDER BY id")
            ).all()
        ]


def test_upgrade_adds_the_two_nullable_columns_and_the_checks(migration_lab: Engine) -> None:
    _seed_history(migration_lab)

    _alembic("upgrade", S33C2_REVISION)

    data_type, length, nullable = _column_definition(migration_lab, "reason_code")
    assert data_type == "character varying"
    assert length == 32
    assert nullable == "YES", "NULL = evento anterior a la migracion"

    note_type, note_length, note_nullable = _column_definition(migration_lab, "reason_note")
    assert note_type == "text"
    assert note_length is None
    assert note_nullable == "YES"

    for name in NEW_CHECKS:
        assert _check_count(migration_lab, name) == 1, f"falta el CHECK {name}"

    assert _historical_codes(migration_lab) == [None, None, None, None], (
        "el historico no se reescribe: se queda sin codigo"
    )


def test_catalog_and_coherence_checks_accept_the_catalog_and_reject_the_rest(
    migration_lab: Engine,
) -> None:
    _alembic("upgrade", S33C2_REVISION)

    accepted = (
        {"entity": "competitor", "action": "CREATE", "code": "PLANNED_ENTRY", "note": None},
        {
            "entity": "tournament_registration",
            "action": "WITHDRAW",
            "code": "WITHDRAWAL_REQUEST",
            "note": None,
        },
        {
            "entity": "tournament_registration",
            "action": "DISQUALIFY",
            "code": "RULE_VIOLATION",
            "note": "incumplio el reglamento de la organizacion",
        },
        {"entity": "competitor", "action": "DEACTIVATE", "code": "DATA_ERROR", "note": "duplicado"},
        # Entidad sin catalogo: se sigue pudiendo auditar sin codigo, como hasta ahora.
        {"entity": "sports_club", "action": "CREATE", "code": None, "note": None},
    )
    for values in accepted:
        _execute(migration_lab, INSERT_EVENT, values)

    rejected = (
        # Codigo inexistente en el catalogo.
        {"entity": "competitor", "action": "CREATE", "code": "INJURY", "note": None},
        # Codigo real, pero de otra accion.
        {"entity": "competitor", "action": "CREATE", "code": "READY", "note": None},
        # Nota con un codigo que no la admite.
        {"entity": "tournament_registration", "action": "CONFIRM", "code": "READY", "note": "nota"},
        # Nota por encima del limite.
        {
            "entity": "competitor",
            "action": "DEACTIVATE",
            "code": "ADMINISTRATIVE",
            "note": "a" * 201,
        },
        # Nota vacia o solo con espacios.
        {"entity": "competitor", "action": "DEACTIVATE", "code": "ADMINISTRATIVE", "note": "   "},
        # Nota con caracter de control.
        {
            "entity": "competitor",
            "action": "DEACTIVATE",
            "code": "ADMINISTRATIVE",
            "note": "baja\tadministrativa",
        },
    )
    for values in rejected:
        with pytest.raises(IntegrityError):
            _execute(migration_lab, INSERT_EVENT, values)

    assert _scalar(migration_lab, "SELECT count(*) FROM domain_change_log") == len(accepted)


def test_downgrade_removes_only_what_it_added(migration_lab: Engine) -> None:
    _seed_history(migration_lab)
    _alembic("upgrade", S33C2_REVISION)

    _alembic("downgrade", "-1")

    for column in ADDED_COLUMNS:
        with pytest.raises(AssertionError):
            _column_definition(migration_lab, column)
    for name in NEW_CHECKS:
        assert _check_count(migration_lab, name) == 0
    assert _scalar(migration_lab, "SELECT count(*) FROM domain_change_log") == 4, (
        "el historico sobrevive al downgrade"
    )


def test_upgrade_is_safe_on_an_empty_table_and_repeatable(migration_lab: Engine) -> None:
    _alembic("upgrade", S33C2_REVISION)

    assert _scalar(migration_lab, "SELECT count(*) FROM domain_change_log") == 0
    assert _column_definition(migration_lab, "reason_code")[0] == "character varying"

    _alembic("downgrade", "-1")
    _alembic("upgrade", S33C2_REVISION)

    assert _column_definition(migration_lab, "reason_note")[0] == "text"
    assert _scalar(migration_lab, "SELECT version_num FROM alembic_version") == S33C2_REVISION
