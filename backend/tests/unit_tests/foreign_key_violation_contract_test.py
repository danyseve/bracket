"""Contrato del guardia de `ForeignKeyViolationError` (hotfix DELETE TOURNAMENT 500).

`check_foreign_key_violation` convertia una constraint no contemplada en `AssertionError` y, con
ello, en un HTTP 500 (y con una constraint contemplada pero no esperada por la ruta, otra 500). Una
dependencia no contemplada no puede reventar el proceso: el error original debe propagarse para que
el llamante lo traduzca a un 409 estable.
"""

import asyncpg  # type: ignore[import-untyped]
import pytest

from bracket.utils.errors import ForeignKey, check_foreign_key_violation


def make_violation(constraint_name: str) -> asyncpg.exceptions.ForeignKeyViolationError:
    """
    Un `ForeignKeyViolationError` con el nombre de constraint que la base de datos reporta en
    `as_dict()` (los construidos a mano solo traen `sqlstate`, como los de verdad).
    """

    class _Violation(asyncpg.exceptions.ForeignKeyViolationError):
        def as_dict(self) -> dict[str, str]:
            return {"constraint_name": constraint_name, "sqlstate": "23503"}

    return _Violation(
        f'update or delete on table "x" violates foreign key constraint "{constraint_name}"'
    )


def test_an_unknown_constraint_is_not_converted_into_an_assertion_error() -> None:
    error = make_violation("some_future_fkey")

    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        with check_foreign_key_violation({ForeignKey.stages_tournament_id_fkey}):
            raise error


def test_a_known_but_unexpected_constraint_is_not_converted_into_an_assertion_error() -> None:
    error = make_violation(ForeignKey.stages_tournament_id_fkey.value)

    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        with check_foreign_key_violation(set()):
            raise error


def test_a_violation_without_constraint_name_is_not_converted_into_an_assertion_error() -> None:
    class _ViolationWithoutName(asyncpg.exceptions.ForeignKeyViolationError):
        def as_dict(self) -> dict[str, str]:
            return {"sqlstate": "23503"}

    error = _ViolationWithoutName("foreign key violation")

    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        with check_foreign_key_violation({ForeignKey.stages_tournament_id_fkey}):
            raise error


def test_the_delete_path_constraints_are_in_the_app_contract() -> None:
    """
    Las dos constraints del defecto original deben estar en el contrato de aplicacion: sin ellas el
    borrado de un torneo con stage_items que referencian su ranking no se reconoce.
    """
    for name in ("stage_items_ranking_id_fkey", "stage_item_inputs_tournament_id_fkey"):
        assert name in ForeignKey.values()


def test_the_delete_conflict_is_a_stable_safe_409() -> None:
    """
    El error de conflicto del borrado es un HTTP 409 con codigo de aplicacion estable y sin
    detalles internos (se importa aqui a proposito: forma parte del contrato que se anade en el
    fix, y el test debe fallar como asercion, no como error de importacion, antes del fix).
    """
    from bracket.utils.errors import TournamentDeleteConflictError

    error = TournamentDeleteConflictError()

    assert error.status_code == 409
    assert error.code == "TOURNAMENT_DELETE_CONFLICT"
    assert isinstance(error.detail, str) and error.detail

    serialized = f"{error.detail} {error.code}".lower()
    for forbidden in ("fkey", "constraint", "select", "traceback", "asyncpg", "psycopg"):
        assert forbidden not in serialized, serialized
