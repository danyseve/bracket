"""Modelos HTTP de la cola de trabajo de un tatami (S4.4, F3C, LAB ONLY).

El contrato es **cerrado**: sale lo justo para que un marcador pinte el torneo, el tatami, la
categoria, la ronda, los dos luchadores, el marcador y el siguiente combate, y nada mas. Ningun
interno del modelo cruza la frontera: ni SQL, ni restricciones, ni conflictos entre pases.

Dos ausencias son **decisiones**, no olvidos:

* no hay "combate actual": el estado vivo lo tiene el marcador, no el backend;
* no hay "terminado": el modelo no tiene estado oficial de combate y no se infiere del marcador
  (en datos reales los combates sin jugar traen puntuaciones distintas de cero, asi que
  ``score=0`` no significa nada y "terminado" tampoco se puede deducir de lo contrario).
"""

from __future__ import annotations

from typing import Literal

from heliclockter import datetime_utc

from bracket.models.db.shared import BaseModelORM
from bracket.utils.id_types import (
    CourtId,
    MatchId,
    RoundId,
    StageItemId,
    StageItemInputId,
    TeamId,
    TournamentId,
)

#: Estado de un combate en la cola: jugable, o esperando pases que aun no existen.
QueueState = Literal["playable", "waiting_on_winners"]


class QueueFighter(BaseModelORM):
    """Luchador de un combate, en la misma forma que el contrato normalizado del Bridge."""

    stage_item_input_id: StageItemInputId
    team_id: TeamId
    name: str


class QueueWaiting(BaseModelORM):
    """Pase que todavia no se puede jugar, con su procedencia.

    ``from_stage_item_id``/``winner_position`` apunta al ganador de otro cuadro; ``from_match_id``
    enlaza con el combate del mismo cuadro que lo alimenta. Los tres pueden venir a ``null``: un
    pase puede estar simplemente vacio, y eso tambien es un hecho del dato, no un error.
    """

    side: Literal["a", "b"]
    from_stage_item_id: StageItemId | None = None
    winner_position: int | None = None
    from_match_id: MatchId | None = None


class QueueMatch(BaseModelORM):
    """Combate de la cola del tatami, con lo necesario para pintarlo y nada mas."""

    match_id: MatchId
    round_id: RoundId
    round_name: str
    round_is_draft: bool
    position_in_schedule: int | None = None
    start_time: datetime_utc | None = None
    duration_seconds: int | None = None
    court_id: CourtId | None = None
    fighter_a: QueueFighter | None = None
    fighter_b: QueueFighter | None = None
    score_a: int
    score_b: int
    state: QueueState
    waiting_for: list[QueueWaiting]


class QueueStageItem(BaseModelORM):
    """Cuadro asignado al tatami, con sus combates en el orden del contrato de lectura."""

    stage_item_id: StageItemId
    name: str
    type_name: str
    matches: list[QueueMatch]


class CourtQueueResponse(BaseModelORM):
    """Cola de un tatami: la lectura unica que el marcador necesita para arrancar.

    ``next_match_id`` es el primer combate **jugable** en el orden publicado, o ``null`` si no hay
    ninguno. No es el combate vivo: nadie lo ha empezado todavia.
    """

    tournament_id: TournamentId
    tournament_name: str
    court_id: CourtId
    court_name: str
    stage_items: list[QueueStageItem]
    next_match_id: MatchId | None = None
