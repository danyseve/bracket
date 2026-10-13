# pylint: disable=redefined-outer-name  # `lab` es el fixture de este modulo.
"""Invariantes de esquema de S4.8a-1 (clasificacion) sobre el **esquema vivo** del modelo.

Complementa a ``migration_s48a_schema_catalogs_test`` (que prueba la revision con el CLI real): aqui
se comprueba que los modelos declaran las mismas restricciones reales de PostgreSQL — tipos
enumerados cerrados, columnas nulables, comprobaciones de rango y unicidades del catalogo — y que
las filas legacy (sin reglas de clasificacion) siguen siendo validas.

Los datos patrimoniales no se tocan: el fixture crea sus propias filas y las retira al terminar.

Requiere PostgreSQL (los tipos enumerados, los ``NUMERIC`` exactos y los indices unicos parciales no
existen en SQLite). Se ejecuta contra la base de tests, que la fixture de sesion recrea desde los
modelos.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError

from bracket.database import engine

#: Tipos enumerados cerrados que S4.8a introduce, con sus etiquetas exactas y en su orden.
EXPECTED_ENUMS: dict[str, tuple[str, ...]] = {
    "belt_scope": ("JUVENILE", "ADULT"),
    "competitor_sex": ("MALE", "FEMALE"),
    "sex_constraint": ("MALE", "FEMALE", "MIXED"),
    "modality": ("GI", "NO_GI"),
}

#: ``tabla`` -> ``columna`` -> (data_type, udt_name, is_nullable).
EXPECTED_COLUMNS: dict[str, dict[str, tuple[str, str, str]]] = {
    "clubs": {"logo_path": ("character varying", "varchar", "YES")},
    "stage_items": {
        "sex_constraint": ("USER-DEFINED", "sex_constraint", "YES"),
        "belt_min_id": ("bigint", "int8", "YES"),
        "belt_max_id": ("bigint", "int8", "YES"),
        "age_min": ("integer", "int4", "YES"),
        "age_max": ("integer", "int4", "YES"),
        "weight_min_kg": ("numeric", "numeric", "YES"),
        "weight_max_kg": ("numeric", "numeric", "YES"),
        "modality": ("USER-DEFINED", "modality", "YES"),
    },
    "competitors": {
        "date_of_birth": ("date", "date", "YES"),
        "sex": ("USER-DEFINED", "competitor_sex", "YES"),
        "belt_id": ("bigint", "int8", "YES"),
    },
    "tournament_registrations": {
        "declared_weight_kg": ("numeric", "numeric", "YES"),
        "modality": ("USER-DEFINED", "modality", "YES"),
    },
}


def _scalar(sql: str, params: dict[str, Any] | None = None) -> Any:
    with engine.begin() as connection:
        return connection.execute(text(sql), params or {}).scalar_one()


def _rows(sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    with engine.begin() as connection:
        return list(connection.execute(text(sql), params or {}).all())


def _execute(sql: str, params: dict[str, Any] | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params or {})


def _fails(sql: str, params: dict[str, Any] | None = None) -> None:
    with pytest.raises(IntegrityError):
        _execute(sql, params)


def _fails_with_type_error(sql: str, params: dict[str, Any] | None = None) -> None:
    with pytest.raises(DataError):
        _execute(sql, params)


def _columns(table: str) -> dict[str, tuple[str, str, str]]:
    return {
        str(row[0]): (str(row[1]), str(row[2]), str(row[3]))
        for row in _rows(
            """
            SELECT column_name, data_type, udt_name, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :table
            """,
            {"table": table},
        )
    }


def _enum_labels() -> dict[str, tuple[str, ...]]:
    return {
        str(row[0]): tuple(str(label) for label in str(row[1]).split(","))
        for row in _rows(
            """
            SELECT t.typname,
                   array_to_string(array_agg(e.enumlabel ORDER BY e.enumsortorder), ',') AS labels
            FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
            WHERE t.typname = ANY(:names)
            GROUP BY t.typname
            """,
            {"names": list(EXPECTED_ENUMS)},
        )
    }


def _constraint_exists(table: str, constraint: str) -> bool:
    return bool(
        _scalar(
            """
            SELECT count(*) FROM pg_constraint
            WHERE conrelid = to_regclass(:table) AND conname = :constraint
            """,
            {"table": table, "constraint": constraint},
        )
    )


@pytest.fixture
def lab() -> Iterator[dict[str, int]]:
    """Un club, un torneo, una etapa, un ranking, un cuadro legacy y dos cinturones de laboratorio."""
    club_id = int(_scalar("INSERT INTO clubs (name) VALUES ('s48a-lab-club') RETURNING id"))
    tournament_id = int(
        _scalar(
            "INSERT INTO tournaments (name, start_time, club_id, dashboard_public) "
            "VALUES ('s48a-lab-tournament', now(), :club_id, true) RETURNING id",
            {"club_id": club_id},
        )
    )
    stage_id = int(
        _scalar(
            "INSERT INTO stages (name, tournament_id) VALUES ('s48a-lab-stage', :t) RETURNING id",
            {"t": tournament_id},
        )
    )
    ranking_id = int(
        _scalar(
            """
            INSERT INTO rankings (tournament_id, position, win_points, draw_points, loss_points,
                                  add_score_points)
            VALUES (:t, 1, 1, 0, 0, false) RETURNING id
            """,
            {"t": tournament_id},
        )
    )
    stage_item_id = int(
        _scalar(
            """
            INSERT INTO stage_items (name, stage_id, team_count, ranking_id, type)
            VALUES ('s48a-lab-item', :stage_id, 2, :ranking_id, 'SINGLE_ELIMINATION') RETURNING id
            """,
            {"stage_id": stage_id, "ranking_id": ranking_id},
        )
    )
    low_belt_id = int(
        _scalar(
            "INSERT INTO belts (code, label, rank, scope) "
            "VALUES ('LAB_LOW', 'Lab low', 90, 'ADULT') RETURNING id"
        )
    )
    high_belt_id = int(
        _scalar(
            "INSERT INTO belts (code, label, rank, scope) "
            "VALUES ('LAB_HIGH', 'Lab high', 91, 'ADULT') RETURNING id"
        )
    )
    try:
        yield {
            "club_id": club_id,
            "tournament_id": tournament_id,
            "stage_item_id": stage_item_id,
            "low_belt_id": low_belt_id,
            "high_belt_id": high_belt_id,
        }
    finally:
        _execute("UPDATE competitors SET belt_id = NULL WHERE belt_id IN (:a, :b)",
                 {"a": low_belt_id, "b": high_belt_id})
        _execute("UPDATE stage_items SET belt_min_id = NULL, belt_max_id = NULL "
                 "WHERE belt_min_id IN (:a, :b) OR belt_max_id IN (:a, :b)",
                 {"a": low_belt_id, "b": high_belt_id})
        _execute("UPDATE tournament_registrations SET declared_weight_kg = NULL, modality = NULL "
                 "WHERE tournament_id = :t", {"t": tournament_id})
        _execute("DELETE FROM tournament_registrations WHERE tournament_id = :t", {"t": tournament_id})
        _execute("DELETE FROM competitors WHERE display_name LIKE 's48a-lab-%'")
        _execute("DELETE FROM belts WHERE id IN (:a, :b)", {"a": low_belt_id, "b": high_belt_id})
        _execute("DELETE FROM stage_items WHERE stage_id IN "
                 "(SELECT id FROM stages WHERE tournament_id = :t)", {"t": tournament_id})
        _execute("DELETE FROM rankings WHERE tournament_id = :t", {"t": tournament_id})
        _execute("DELETE FROM stages WHERE tournament_id = :t", {"t": tournament_id})
        _execute("DELETE FROM tournaments WHERE id = :t", {"t": tournament_id})
        _execute("DELETE FROM clubs WHERE id = :c", {"c": club_id})


def test_the_four_closed_enums_are_declared_with_exactly_these_labels() -> None:
    """Los vocabularios son cerrados: ni ``MIXED`` como sexo de persona ni ``GI_AND_NO_GI``."""
    assert _enum_labels() == EXPECTED_ENUMS


def test_the_new_columns_are_nullable_with_their_exact_type() -> None:
    """Todo lo anadido es nulable (compatibilidad legacy) y del tipo declarado."""
    for table, columns in EXPECTED_COLUMNS.items():
        declared = _columns(table)
        for column, shape in columns.items():
            assert declared.get(column) == shape, f"{table}.{column}: {declared.get(column)} != {shape}"


def test_a_legacy_stage_item_is_valid_without_any_rule(lab: dict[str, int]) -> None:
    """Un cuadro sin reglas de clasificacion sigue siendo valido: NULL = sin restriccion."""
    row = _rows(
        """
        SELECT sex_constraint, belt_min_id, belt_max_id, age_min, age_max, weight_min_kg,
               weight_max_kg, modality
        FROM stage_items WHERE id = :id
        """,
        {"id": lab["stage_item_id"]},
    )
    assert len(row) == 1
    assert all(value is None for value in row[0])


def test_the_category_rules_are_constrained_by_the_database(lab: dict[str, int]) -> None:
    """Rangos invertidos, edades negativas y pesos fuera de (0, 300] no entran; los validos si."""
    stage_item_id = lab["stage_item_id"]

    _fails("UPDATE stage_items SET age_min = 30, age_max = 18 WHERE id = :id",
           {"id": stage_item_id})
    _fails("UPDATE stage_items SET age_min = -1 WHERE id = :id", {"id": stage_item_id})
    _fails("UPDATE stage_items SET weight_min_kg = 80, weight_max_kg = 70 WHERE id = :id",
           {"id": stage_item_id})
    _fails("UPDATE stage_items SET weight_max_kg = 300.01 WHERE id = :id", {"id": stage_item_id})
    _fails_with_type_error("UPDATE stage_items SET age_max = 30.5 WHERE id = :id",
                           {"id": stage_item_id})

    _execute(
        """
        UPDATE stage_items
        SET sex_constraint = 'MIXED', belt_min_id = :low, belt_max_id = :high,
            age_min = 18, age_max = 29, weight_min_kg = 0.01, weight_max_kg = 300.00,
            modality = 'GI'
        WHERE id = :id
        """,
        {"id": stage_item_id, "low": lab["low_belt_id"], "high": lab["high_belt_id"]},
    )
    assert _scalar(
        """
        SELECT count(*) FROM stage_items
        WHERE id = :id AND sex_constraint = 'MIXED' AND modality = 'GI'
          AND weight_min_kg = 0.01 AND weight_max_kg = 300.00
        """,
        {"id": stage_item_id},
    ) == 1


def test_the_competitor_sex_is_closed_and_the_belt_is_a_catalog_reference(
    lab: dict[str, int],
) -> None:
    """``competitors.sex`` solo admite MALE/FEMALE y el cinturon apunta al catalogo."""
    competitor_id = int(
        _scalar(
            "INSERT INTO competitors (display_name, date_of_birth, sex, belt_id) "
            "VALUES ('s48a-lab-athlete', '1990-05-04', 'FEMALE', :belt_id) RETURNING id",
            {"belt_id": lab["low_belt_id"]},
        )
    )

    _fails_with_type_error(
        "UPDATE competitors SET sex = 'MIXED' WHERE id = :id", {"id": competitor_id}
    )
    _fails_with_type_error(
        "UPDATE competitors SET date_of_birth = 'no-es-fecha' WHERE id = :id", {"id": competitor_id}
    )
    _fails("UPDATE competitors SET belt_id = 999999999 WHERE id = :id", {"id": competitor_id})
    _fails("DELETE FROM belts WHERE id = :b", {"b": lab["low_belt_id"]})

    _execute("UPDATE competitors SET belt_id = NULL WHERE id = :id", {"id": competitor_id})
    _execute("DELETE FROM competitors WHERE id = :id", {"id": competitor_id})


def test_the_registration_weight_and_modality_are_constrained(lab: dict[str, int]) -> None:
    """El peso declarado esta acotado a (0, 300] kg y la modalidad es GI o NO_GI."""
    registration_id = int(
        _scalar(
            """
            INSERT INTO tournament_registrations
                (tournament_id, identity_status, representation, competitor_name_snapshot,
                 declared_weight_kg, modality)
            VALUES (:t, 'UNVERIFIED', 'INDEPENDENT', 's48a-lab-athlete', 71.35, 'GI') RETURNING id
            """,
            {"t": lab["tournament_id"]},
        )
    )

    for weight in ("0", "300.01", "-0.01"):
        _fails(
            "UPDATE tournament_registrations SET declared_weight_kg = :w WHERE id = :id",
            {"w": weight, "id": registration_id},
        )
    _fails_with_type_error(
        "UPDATE tournament_registrations SET modality = 'GI_AND_NO_GI' WHERE id = :id",
        {"id": registration_id},
    )
    _execute(
        "UPDATE tournament_registrations SET declared_weight_kg = 300.00, modality = NULL "
        "WHERE id = :id",
        {"id": registration_id},
    )
    assert _scalar(
        "SELECT count(*) FROM tournament_registrations WHERE id = :id "
        "AND declared_weight_kg = 300.00 AND modality IS NULL",
        {"id": registration_id},
    ) == 1


def test_the_belt_catalog_uniqueness_is_per_scope() -> None:
    """``(scope, code)`` y ``(scope, rank)`` son unicos; el mismo color convive entre escalas."""
    _fails("INSERT INTO belts (code, label, rank, scope) VALUES ('LAB_LOW', 'Otro', 92, 'ADULT')")
    _fails("INSERT INTO belts (code, label, rank, scope) VALUES ('LAB_OTHER', 'Otro', 90, 'ADULT')")

    with pytest.raises(DataError):
        _execute("INSERT INTO belts (code, label, rank, scope) VALUES ('LAB_X', 'X', 1, 'OPEN')")

    juvenile_id = int(
        _scalar(
            "INSERT INTO belts (code, label, rank, scope) "
            "VALUES ('LAB_LOW', 'Lab low', 1, 'JUVENILE') RETURNING id"
        )
    )
    assert _scalar("SELECT scope FROM belts WHERE id = :id", {"id": juvenile_id}) == "JUVENILE"
    _execute("DELETE FROM belts WHERE id = :id", {"id": juvenile_id})


def test_the_club_logo_is_optional_and_does_not_block_creating_a_club() -> None:
    """Un club sin logo es valido: ``logo_path`` es nulable."""
    assert _columns("clubs")["logo_path"] == ("character varying", "varchar", "YES")
    club_id = int(_scalar("INSERT INTO clubs (name) VALUES ('s48a-lab-nologo') RETURNING id"))
    assert _scalar("SELECT logo_path FROM clubs WHERE id = :id", {"id": club_id}) is None
    _execute("DELETE FROM clubs WHERE id = :id", {"id": club_id})
