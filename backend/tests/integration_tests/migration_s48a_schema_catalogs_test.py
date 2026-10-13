# pylint: disable=redefined-outer-name  # `migration_lab` es el fixture de este modulo.
"""Migracion de S4.8a-1: esquema de clasificacion (catalogos, reglas de categoria, datos basicos).

Se prueba contra una base temporal del servidor de pruebas, con el CLI real de Alembic: el esquema
vivo (``metadata.create_all``) no ejecuta la revision, asi que no sirve como evidencia de la
migracion.

Lo que fija este modulo:

* el upgrade crea el **catalogo cerrado** ``belts`` con sus diez filas (cinco juveniles y cinco
  adultas), sin tocar ninguna tabla existente;
* anade **solo** columnas nulables (reglas de categoria, datos del competidor, peso y modalidad de
  la inscripcion, logo de club) y los cuatro tipos enumerados cerrados;
* las comprobaciones de rango y el ``RESTRICT`` del catalogo son reales (los impone PostgreSQL);
* un cuadro legacy sobrevive al upgrade sin reglas (todo NULL) y al downgrade sin perder su fila;
* el downgrade retira exactamente lo anadido (catalogo, columnas y tipos) y la cadena sigue con
  **una sola cabeza**.
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
SCRATCH_DATABASE = "bracket_test_migration_s48a"
PREVIOUS_REVISION = "b2f5a7c31d90"
REVISION = "c1a4f7e92b08"

BELT_SCOPE = "belt_scope"
COMPETITOR_SEX = "competitor_sex"
SEX_CONSTRAINT = "sex_constraint"
MODALITY = "modality"

#: Tipos enumerados cerrados que la revision crea, con sus etiquetas exactas.
EXPECTED_ENUMS: dict[str, tuple[str, ...]] = {
    BELT_SCOPE: ("JUVENILE", "ADULT"),
    COMPETITOR_SEX: ("MALE", "FEMALE"),
    SEX_CONSTRAINT: ("MALE", "FEMALE", "MIXED"),
    MODALITY: ("GI", "NO_GI"),
}

#: Catalogo sembrado por la migracion: ``scope``, ``code``, ``rank``.
EXPECTED_BELTS: tuple[tuple[str, str, int], ...] = (
    ("JUVENILE", "WHITE", 1),
    ("JUVENILE", "GREY", 2),
    ("JUVENILE", "YELLOW", 3),
    ("JUVENILE", "ORANGE", 4),
    ("JUVENILE", "GREEN", 5),
    ("ADULT", "WHITE", 1),
    ("ADULT", "BLUE", 2),
    ("ADULT", "PURPLE", 3),
    ("ADULT", "BROWN", 4),
    ("ADULT", "BLACK", 5),
)

#: Columnas anadidas: ``tabla`` -> ``columna`` -> (data_type, udt_name, is_nullable).
EXPECTED_COLUMNS: dict[str, dict[str, tuple[str, str, str]]] = {
    "belts": {
        "code": ("character varying", "varchar", "NO"),
        "label": ("character varying", "varchar", "NO"),
        "rank": ("integer", "int4", "NO"),
        "scope": ("USER-DEFINED", BELT_SCOPE, "NO"),
        "active": ("boolean", "bool", "NO"),
    },
    "clubs": {"logo_path": ("character varying", "varchar", "YES")},
    "stage_items": {
        "sex_constraint": ("USER-DEFINED", SEX_CONSTRAINT, "YES"),
        "belt_min_id": ("bigint", "int8", "YES"),
        "belt_max_id": ("bigint", "int8", "YES"),
        "age_min": ("integer", "int4", "YES"),
        "age_max": ("integer", "int4", "YES"),
        "weight_min_kg": ("numeric", "numeric", "YES"),
        "weight_max_kg": ("numeric", "numeric", "YES"),
        "modality": ("USER-DEFINED", MODALITY, "YES"),
    },
    "competitors": {
        "date_of_birth": ("date", "date", "YES"),
        "sex": ("USER-DEFINED", COMPETITOR_SEX, "YES"),
        "belt_id": ("bigint", "int8", "YES"),
    },
    "tournament_registrations": {
        "declared_weight_kg": ("numeric", "numeric", "YES"),
        "modality": ("USER-DEFINED", MODALITY, "YES"),
    },
}

#: Restricciones de comprobacion que la revision declara.
EXPECTED_CHECKS: tuple[tuple[str, str], ...] = (
    ("stage_items", "ck_stage_items_age_range"),
    ("stage_items", "ck_stage_items_weight_range"),
    ("tournament_registrations", "ck_tournament_registrations_declared_weight"),
)

#: Restricciones de unicidad del catalogo.
EXPECTED_UNIQUES: tuple[tuple[str, str], ...] = (
    ("belts", "uq_belts_scope_code"),
    ("belts", "uq_belts_scope_rank"),
)

#: Claves foraneas anadidas: ``tabla`` -> ``columna``.
EXPECTED_FOREIGN_KEYS: tuple[tuple[str, str], ...] = (
    ("stage_items", "belt_min_id"),
    ("stage_items", "belt_max_id"),
    ("competitors", "belt_id"),
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
    """Un unico valor; abre transaccion propia porque este auxiliar tambien siembra filas."""
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


def _table_exists(engine: Engine, table: str) -> bool:
    return bool(_scalar(engine, "SELECT to_regclass(:table) IS NOT NULL", {"table": table}))


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


def _enum_labels(engine: Engine) -> dict[str, tuple[str, ...]]:
    """Etiquetas de los tipos enumerados de laboratorio, en su orden declarado."""
    return {
        str(row[0]): tuple(str(row[1]).split(","))
        for row in _rows(
            engine,
            """
            SELECT t.typname, array_to_string(array_agg(e.enumlabel ORDER BY e.enumsortorder), ',')
              AS labels
            FROM pg_type t
                JOIN pg_enum e ON e.enumtypid = t.oid
            WHERE t.typname = ANY(:names)
            GROUP BY t.typname
            """,
            {"names": list(EXPECTED_ENUMS)},
        )
    }


def _constraint_exists(engine: Engine, table: str, constraint: str) -> bool:
    return bool(
        _scalar(
            engine,
            """
            SELECT count(*) FROM pg_constraint
            WHERE conrelid = to_regclass(:table) AND conname = :constraint
            """,
            {"table": table, "constraint": constraint},
        )
    )


def _foreign_key_delete_action(engine: Engine, table: str, column: str) -> str | None:
    """Accion al borrar del destino (``r`` = RESTRICT) de la clave foranea de esa columna."""
    row = _rows(
        engine,
        """
        SELECT confdeltype FROM pg_constraint
        WHERE conrelid = to_regclass(:table) AND contype = 'f' AND conname = :constraint
        """,
        {"table": table, "constraint": f"{table}_{column}_fkey"},
    )
    return None if not row else str(row[0][0])


def _belts(engine: Engine) -> tuple[tuple[str, str, int], ...]:
    return tuple(
        (str(row[0]), str(row[1]), int(row[2]))
        for row in _rows(engine, "SELECT scope, code, rank FROM belts ORDER BY scope, rank")
    )


def _seed_legacy_stage_item(engine: Engine) -> int:
    """Un cuadro real sin reglas de clasificacion: debe sobrevivir al upgrade y al downgrade."""
    with engine.begin() as connection:
        club_id = connection.execute(
            text("INSERT INTO clubs (name) VALUES ('lab-club-s48a') RETURNING id")
        ).scalar_one()
        tournament_id = connection.execute(
            text(
                """
                INSERT INTO tournaments (name, start_time, club_id, dashboard_public)
                VALUES ('lab-tournament-s48a', now(), :club_id, true) RETURNING id
                """
            ),
            {"club_id": club_id},
        ).scalar_one()
        stage_id = connection.execute(
            text(
                """
                INSERT INTO stages (name, tournament_id) VALUES ('lab-stage-s48a', :tournament_id)
                RETURNING id
                """
            ),
            {"tournament_id": tournament_id},
        ).scalar_one()
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
                    VALUES ('-71 kg', :stage_id, 2, :ranking_id, 'SINGLE_ELIMINATION')
                    RETURNING id
                    """
                ),
                {"stage_id": stage_id, "ranking_id": ranking_id},
            ).scalar_one()
        )


def _remove_everything_this_revision_adds(engine: Engine) -> None:
    """Deja el esquema vivo en el aspecto fisico previo a esta revision."""
    for table, constraint in EXPECTED_CHECKS:
        _execute(engine, f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}")
    for table, columns in EXPECTED_COLUMNS.items():
        if table == "belts":
            continue
        for column in columns:
            _execute(engine, f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
    _execute(engine, "DROP TABLE IF EXISTS belts")
    for enum_name in EXPECTED_ENUMS:
        _execute(engine, f"DROP TYPE IF EXISTS {enum_name}")


@pytest.fixture
def migration_lab() -> Iterator[Engine]:
    """Base temporal limpia en el servidor de pruebas, en el estado posterior a S4.5."""
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


def test_the_previous_state_has_none_of_the_new_objects(migration_lab: Engine) -> None:
    """Punto de partida: sin catalogo, sin columnas nuevas y sin los tipos enumerados."""
    assert _scalar(migration_lab, "SELECT version_num FROM alembic_version") == PREVIOUS_REVISION
    assert not _table_exists(migration_lab, "belts")
    assert _enum_labels(migration_lab) == {}
    for table, columns in EXPECTED_COLUMNS.items():
        if table == "belts":
            continue
        declared = _columns(migration_lab, table)
        for column in columns:
            assert column not in declared, f"{table}.{column} no deberia existir todavia"


def test_upgrade_creates_the_closed_belt_catalog_with_its_ten_rows(
    migration_lab: Engine,
) -> None:
    """El catalogo entra con cinco cinturones juveniles y cinco adultos, todos activos."""
    _alembic("upgrade", REVISION)

    assert _table_exists(migration_lab, "belts")
    assert _belts(migration_lab) == EXPECTED_BELTS
    assert _scalar(migration_lab, "SELECT count(*) FROM belts WHERE active") == 10
    assert _scalar(migration_lab, "SELECT count(*) FROM belts WHERE NOT active") == 0


def test_upgrade_adds_only_nullable_columns_and_the_closed_enums(migration_lab: Engine) -> None:
    """Todas las columnas anadidas son nulables, con su tipo exacto y su enumerado cerrado."""
    _alembic("upgrade", REVISION)

    for table, columns in EXPECTED_COLUMNS.items():
        body = _columns(migration_lab, table)
        for column, shape in columns.items():
            assert body.get(column) == shape, f"{table}.{column}: {body.get(column)} != {shape}"

    assert _enum_labels(migration_lab) == EXPECTED_ENUMS


def test_upgrade_declares_the_range_checks_and_the_catalog_uniqueness(
    migration_lab: Engine,
) -> None:
    """Las comprobaciones de rango, las unicidades del catalogo y los RESTRICT son reales."""
    _alembic("upgrade", REVISION)

    for table, constraint in EXPECTED_CHECKS + EXPECTED_UNIQUES:
        assert _constraint_exists(migration_lab, table, constraint), f"falta {constraint}"

    for table, column in EXPECTED_FOREIGN_KEYS:
        assert _foreign_key_delete_action(migration_lab, table, column) == "r"


def test_a_legacy_stage_item_survives_the_upgrade_without_rules(migration_lab: Engine) -> None:
    """Sin backfill: el cuadro legacy sigue ahi y sus reglas nuevas quedan NULL."""
    stage_item_id = _seed_legacy_stage_item(migration_lab)

    _alembic("upgrade", REVISION)

    row = _rows(
        migration_lab,
        """
        SELECT name, sex_constraint, belt_min_id, belt_max_id, age_min, age_max,
               weight_min_kg, weight_max_kg, modality
        FROM stage_items WHERE id = :stage_item_id
        """,
        {"stage_item_id": stage_item_id},
    )
    assert len(row) == 1
    assert row[0][0] == "-71 kg"
    assert all(value is None for value in row[0][1:])


def test_the_age_and_weight_ranges_are_enforced_by_the_database(migration_lab: Engine) -> None:
    """Un rango invertido no entra; uno coherente si, y los limites son inclusivos."""
    stage_item_id = _seed_legacy_stage_item(migration_lab)
    _alembic("upgrade", REVISION)

    _fails(
        migration_lab,
        "UPDATE stage_items SET age_min = 30, age_max = 18 WHERE id = :stage_item_id",
        {"stage_item_id": stage_item_id},
    )
    _fails(
        migration_lab,
        "UPDATE stage_items SET weight_min_kg = 80, weight_max_kg = 70 WHERE id = :stage_item_id",
        {"stage_item_id": stage_item_id},
    )
    _execute(
        migration_lab,
        """
        UPDATE stage_items SET sex_constraint = 'MIXED', age_min = 18, age_max = 29,
               weight_min_kg = 0.01, weight_max_kg = 300.00, modality = 'GI'
        WHERE id = :stage_item_id
        """,
        {"stage_item_id": stage_item_id},
    )
    assert (
        _scalar(
            migration_lab,
            "SELECT count(*) FROM stage_items WHERE id = :stage_item_id "
            "AND weight_min_kg = 0.01 AND weight_max_kg = 300.00",
            {"stage_item_id": stage_item_id},
        )
        == 1
    )


def test_the_declared_weight_is_bounded_by_the_database(migration_lab: Engine) -> None:
    """El peso declarado acepta 0.01 y 300.00 y rechaza 0, 300.01 y los negativos."""
    club_id = _scalar(
        migration_lab, "INSERT INTO clubs (name) VALUES ('lab-club-weight') RETURNING id"
    )
    tournament_id = _scalar(
        migration_lab,
        "INSERT INTO tournaments (name, start_time, club_id, dashboard_public) "
        "VALUES ('lab-tournament-weight', now(), :club_id, true) RETURNING id",
        {"club_id": club_id},
    )
    _alembic("upgrade", REVISION)

    registration_id = _scalar(
        migration_lab,
        """
        INSERT INTO tournament_registrations
            (tournament_id, competitor_id, identity_status, representation, modality,
             competitor_name_snapshot, declared_weight_kg)
        VALUES (:tournament_id, NULL, 'UNVERIFIED', 'INDEPENDENT', 'NO_GI', 'lab-athlete', 0.01)
        RETURNING id
        """,
        {"tournament_id": tournament_id},
    )
    _execute(
        migration_lab,
        "UPDATE tournament_registrations SET declared_weight_kg = 300.00 WHERE id = :id",
        {"id": registration_id},
    )
    for weight in ("0", "300.01", "-1"):
        _fails(
            migration_lab,
            "UPDATE tournament_registrations SET declared_weight_kg = :weight WHERE id = :id",
            {"weight": weight, "id": registration_id},
        )


def test_the_belt_catalog_rejects_duplicates_and_protects_the_referenced_rows(
    migration_lab: Engine,
) -> None:
    """``(scope, code)`` y ``(scope, rank)`` son unicos; un cinturon en uso no se borra."""
    _alembic("upgrade", REVISION)

    _fails(
        migration_lab,
        "INSERT INTO belts (code, label, rank, scope) VALUES ('BLUE', 'Blue', 9, 'ADULT')",
    )
    _fails(
        migration_lab,
        "INSERT INTO belts (code, label, rank, scope) VALUES ('LAB', 'Lab', 2, 'ADULT')",
    )
    # El mismo codigo en otro ambito si convive: las escalas juvenil y adulta son distintas.
    _execute(
        migration_lab,
        "INSERT INTO belts (code, label, rank, scope) VALUES ('BLUE', 'Blue', 9, 'JUVENILE')",
    )

    belt_id = _scalar(migration_lab, "SELECT id FROM belts WHERE scope = 'ADULT' AND code = 'BLUE'")
    competitor_id = _scalar(
        migration_lab,
        "INSERT INTO competitors (display_name, belt_id) VALUES ('lab-athlete', :belt_id) "
        "RETURNING id",
        {"belt_id": belt_id},
    )
    _fails(migration_lab, "DELETE FROM belts WHERE id = :belt_id", {"belt_id": belt_id})
    _execute(
        migration_lab, "UPDATE competitors SET belt_id = NULL WHERE id = :id", {"id": competitor_id}
    )
    _execute(migration_lab, "DELETE FROM belts WHERE id = :belt_id", {"belt_id": belt_id})


def test_downgrade_removes_exactly_what_the_upgrade_added_and_keeps_the_data(
    migration_lab: Engine,
) -> None:
    """El downgrade deja la base como estaba y el upgrade la vuelve a levantar igual."""
    stage_item_id = _seed_legacy_stage_item(migration_lab)
    _alembic("upgrade", REVISION)
    _alembic("downgrade", PREVIOUS_REVISION)

    assert _scalar(migration_lab, "SELECT version_num FROM alembic_version") == PREVIOUS_REVISION
    assert not _table_exists(migration_lab, "belts")
    assert _enum_labels(migration_lab) == {}
    for table, columns in EXPECTED_COLUMNS.items():
        if table == "belts":
            continue
        declared = _columns(migration_lab, table)
        for column in columns:
            assert column not in declared, f"{table}.{column} deberia haberse retirado"
    with migration_lab.connect() as connection:
        name = connection.execute(
            text("SELECT name FROM stage_items WHERE id = :id"), {"id": stage_item_id}
        ).scalar_one()
    assert name == "-71 kg"

    _alembic("upgrade", "head")

    assert _belts(migration_lab) == EXPECTED_BELTS
    assert _enum_labels(migration_lab) == EXPECTED_ENUMS


def test_the_revision_is_a_single_head_on_top_of_s45(migration_lab: Engine) -> None:
    """La revision cuelga de S4.5 y la cadena sigue siendo lineal: una sola cabeza."""
    assert _scalar(migration_lab, "SELECT version_num FROM alembic_version") == PREVIOUS_REVISION
    assert REVISION in _alembic("history")
    output = _alembic("heads")
    assert output.count("(head)") == 1
