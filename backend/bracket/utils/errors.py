from collections.abc import Iterator
from contextlib import contextmanager
from enum import auto

import asyncpg  # type: ignore[import-untyped]
from fastapi import HTTPException
from starlette import status

from bracket.utils.types import EnumAutoStr


class UniqueIndex(EnumAutoStr):
    ix_tournaments_dashboard_endpoint = auto()
    ix_users_email = auto()
    stage_item_inputs_stage_item_id_team_id_key = auto()
    stage_item_inputs_stage_item_id_winner_from_stage_item_id_w_key = auto()


class ForeignKey(EnumAutoStr):
    courts_tournament_id_fkey = auto()
    matches_court_id_fkey = auto()
    matches_round_id_fkey = auto()
    matches_stage_item_input1_id_fkey = auto()
    matches_stage_item_input1_winner_from_match_id_fkey = auto()
    matches_stage_item_input2_id_fkey = auto()
    matches_stage_item_input2_winner_from_match_id_fkey = auto()
    players_tournament_id_fkey = auto()
    players_x_teams_player_id_fkey = auto()
    players_x_teams_team_id_fkey = auto()
    rankings_tournament_id_fkey = auto()
    rounds_stage_item_id_fkey = auto()
    stage_item_inputs_stage_item_id_fkey = auto()
    stage_item_inputs_team_id_fkey = auto()
    stage_item_inputs_tournament_id_fkey = auto()
    stage_item_inputs_winner_from_stage_item_id_fkey = auto()
    stage_items_ranking_id_fkey = auto()
    stage_items_stage_id_fkey = auto()
    stages_tournament_id_fkey = auto()
    teams_tournament_id_fkey = auto()
    tournaments_club_id_fkey = auto()


unique_index_violation_error_lookup = {
    UniqueIndex.ix_tournaments_dashboard_endpoint: "This dashboard link is already taken",
    UniqueIndex.ix_users_email: "This email is already taken",
    UniqueIndex.stage_item_inputs_stage_item_id_team_id_key: (
        "This team is already assigned to another stage item"
    ),
    UniqueIndex.stage_item_inputs_stage_item_id_winner_from_stage_item_id_w_key: (
        "This stage item winner is already assigned to another stage item"
    ),
}


foreign_key_violation_error_lookup = {
    ForeignKey.courts_tournament_id_fkey: "This tournament still has courts, delete those first",
    ForeignKey.matches_stage_item_input1_id_fkey: "This team is still part of matches",
    ForeignKey.matches_stage_item_input2_id_fkey: "This team is still part of matches",
    ForeignKey.players_tournament_id_fkey: "This tournament still has players, delete those first",
    ForeignKey.stage_item_inputs_team_id_fkey: "Invalid team as input to this stage item",
    ForeignKey.stages_tournament_id_fkey: "This tournament still has stages, delete those first",
    ForeignKey.teams_tournament_id_fkey: "This tournament still has teams, delete those first",
    ForeignKey.tournaments_club_id_fkey: "This club still has tournaments, delete those first",
    ForeignKey.rankings_tournament_id_fkey: (
        "This tournament still has rankings, delete those first"
    ),
}


@contextmanager
def check_unique_constraint_violation(expected_violations: set[UniqueIndex]) -> Iterator[None]:
    try:
        yield
    except asyncpg.exceptions.UniqueViolationError as exc:
        constraint_name = exc.as_dict()["constraint_name"]
        assert constraint_name, "UniqueViolationError occurred but no constraint_name defined"
        assert constraint_name in UniqueIndex.values(), "Unknown UniqueViolationError occurred"
        constraint = UniqueIndex(constraint_name)

        if (
            constraint not in unique_index_violation_error_lookup
            or constraint not in expected_violations
        ):
            raise exc

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=unique_index_violation_error_lookup[constraint],
        ) from exc


@contextmanager
def check_foreign_key_violation(expected_violations: set[ForeignKey]) -> Iterator[None]:
    try:
        yield
    except asyncpg.exceptions.ForeignKeyViolationError as exc:
        # Un nombre de constraint ausente o desconocido NO puede convertirse en AssertionError
        # (seria un HTTP 500): se propaga el error original para que el llamante lo traduzca.
        constraint_name = exc.as_dict().get("constraint_name")
        if not constraint_name or constraint_name not in ForeignKey.values():
            raise exc

        constraint = ForeignKey(constraint_name)

        if (
            constraint not in foreign_key_violation_error_lookup
            or constraint not in expected_violations
        ):
            raise exc

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=foreign_key_violation_error_lookup[constraint],
        ) from exc


class BracketConflictError(HTTPException):
    """
    HTTP 409 con codigo de aplicacion estable. El `detail` es apto para el cliente: nunca incluye
    nombre de constraint, SQL, stack trace ni detalles de PostgreSQL.
    """

    code: str

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=detail)
        self.code = code


class TournamentDeleteConflictError(BracketConflictError):
    """
    El torneo tiene datos dependientes que el borrado controlado no puede eliminar (FK protegida o
    desconocida). Los detalles tecnicos quedan en el log del servidor.
    """

    def __init__(self) -> None:
        super().__init__(
            code="TOURNAMENT_DELETE_CONFLICT",
            detail="This tournament still has dependent data that prevents deleting it",
        )
