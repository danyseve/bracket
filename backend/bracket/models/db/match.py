from decimal import Decimal
from typing import Literal

from heliclockter import datetime_utc, timedelta
from pydantic import BaseModel, model_validator

from bracket.models.db.court import Court
from bracket.models.db.shared import BaseModelORM
from bracket.models.db.stage_item_inputs import StageItemInput
from bracket.utils.id_types import CourtId, MatchId, RoundId, StageItemInputId
from bracket.utils.types import assert_some


class MatchBaseInsertable(BaseModelORM):
    created: datetime_utc
    start_time: datetime_utc | None = None
    duration_minutes: int
    margin_minutes: int
    custom_duration_minutes: int | None = None
    custom_margin_minutes: int | None = None
    position_in_schedule: int | None = None
    round_id: RoundId
    stage_item_input1_score: int
    stage_item_input2_score: int
    court_id: CourtId | None = None
    stage_item_input1_conflict: bool
    stage_item_input2_conflict: bool

    @property
    def end_time(self) -> datetime_utc:
        assert self.start_time
        return self.start_time + timedelta(minutes=self.duration_minutes + self.margin_minutes)


#: Catalogo cerrado de metodos de victoria (S4.4b, F3C, LAB ONLY).
#:
#: Es el MISMO vocabulario que ya usa la maquina de estados del marcador
#: (``scoreboard-adapter/state.js``: ``METHODS``), que hasta ahora era local y "no mapeado a
#: nada". No se inventa una enumeracion nueva: se adopta la existente como contrato del backend,
#: que pasa a ser quien la valida y la persiste.
ResultMethod = Literal[
    "points",
    "submission",
    "decision",
    "disqualification",
    "walkover",
    "referee_stoppage",
    "other",
]


class MatchInsertable(MatchBaseInsertable):
    stage_item_input1_id: StageItemInputId | None = None
    stage_item_input2_id: StageItemInputId | None = None
    stage_item_input1_winner_from_match_id: MatchId | None = None
    stage_item_input2_winner_from_match_id: MatchId | None = None
    #: Hecho durable del resultado (S4.4b): ganador declarado por la mesa/marcador y por que
    #: metodo. Nulos en un combate sin jugar; sin ellos solo queda la deduccion por puntuaciones.
    winner_stage_item_input_id: StageItemInputId | None = None
    result_method: ResultMethod | None = None
    #: Momento en que el combate quedo resuelto. Un combate jugado no vuelve a estar pendiente.
    finished_at: datetime_utc | None = None


class Match(MatchInsertable):
    id: MatchId
    stage_item_input1: StageItemInput | None = None
    stage_item_input2: StageItemInput | None = None

    def get_winner(self) -> StageItemInput | None:
        """Ganador del combate: el declarado, si lo hay; si no, el que dicen las puntuaciones.

        Un combate puede terminar por puntos, sumision, decision, descalificacion, walkover o
        parada del arbitro: solo la puntuacion no lo representa. El ganador declarado
        (``winner_stage_item_input_id`` + ``result_method``) es el hecho durable que escribe la
        mesa y lo que avanza al ganador; sin el se mantiene la deduccion por puntuaciones de
        siempre, que es lo que necesitan los datos y los combates ya jugados.
        """
        if self.winner_stage_item_input_id is not None:
            for input_ in (self.stage_item_input1, self.stage_item_input2):
                if input_ is not None and input_.id == self.winner_stage_item_input_id:
                    return input_

            return None

        if self.stage_item_input1_score > self.stage_item_input2_score:
            return self.stage_item_input1
        if self.stage_item_input1_score < self.stage_item_input2_score:
            return self.stage_item_input2

        return None


class MatchWithDetails(Match):
    """
    MatchWithDetails has zero or one defined stage item inputs, but not both.
    """

    court: Court | None = None


def get_match_hash(
    stage_item_input1_id: StageItemInputId | None, stage_item_input2_id: StageItemInputId | None
) -> str:
    return f"{stage_item_input1_id}-{stage_item_input2_id}"


class MatchWithDetailsDefinitive(Match):
    stage_item_input1: StageItemInput  # pyrefly: ignore [bad-override]
    stage_item_input2: StageItemInput  # pyrefly: ignore [bad-override]
    court: Court | None = None

    @property
    def stage_item_inputs(self) -> list[StageItemInput]:
        return [self.stage_item_input1, self.stage_item_input2]

    @property
    def stage_item_input_ids(self) -> list[StageItemInputId]:
        return [assert_some(self.stage_item_input1_id), assert_some(self.stage_item_input2_id)]

    def get_input_ids_hashes(self) -> list[str]:
        return [
            get_match_hash(self.stage_item_input1_id, self.stage_item_input2_id),
            get_match_hash(self.stage_item_input2_id, self.stage_item_input1_id),
        ]


class MatchBody(BaseModelORM):
    round_id: RoundId
    stage_item_input1_score: int = 0
    stage_item_input2_score: int = 0
    court_id: CourtId | None = None
    custom_duration_minutes: int | None = None
    custom_margin_minutes: int | None = None
    #: Resultado declarado (S4.4b). Los dos campos van juntos o ninguno de los dos: un ganador sin
    #: metodo no es un resultado, y un metodo sin ganador tampoco. Que el ganador sea uno de los
    #: dos pases del combate se comprueba en la ruta, contra el combate real, no en el cuerpo.
    winner_stage_item_input_id: StageItemInputId | None = None
    result_method: ResultMethod | None = None

    @model_validator(mode="after")
    def _winner_and_method_go_together(self) -> "MatchBody":
        has_winner = self.winner_stage_item_input_id is not None
        has_method = self.result_method is not None
        if has_winner != has_method:
            raise ValueError("RESULT_WINNER_AND_METHOD_REQUIRED")
        return self


class MatchCreateBodyFrontend(BaseModelORM):
    round_id: RoundId
    court_id: CourtId | None = None
    stage_item_input1_id: StageItemInputId | None = None
    stage_item_input2_id: StageItemInputId | None = None
    stage_item_input1_winner_from_match_id: MatchId | None = None
    stage_item_input2_winner_from_match_id: MatchId | None = None


class MatchCreateBody(MatchCreateBodyFrontend):
    duration_minutes: int
    margin_minutes: int
    custom_duration_minutes: int | None = None
    custom_margin_minutes: int | None = None


class MatchRescheduleBody(BaseModelORM):
    old_court_id: CourtId
    old_position: int
    new_court_id: CourtId
    new_position: int


class MatchFilter(BaseModel):
    elo_diff_threshold: int
    only_recommended: bool
    limit: int
    iterations: int


class SuggestedMatch(BaseModel):
    stage_item_input1: StageItemInput
    stage_item_input2: StageItemInput
    elo_diff: Decimal
    swiss_diff: Decimal
    is_recommended: bool
    times_played_sum: int
    player_behind_schedule_count: int

    @property
    def stage_item_input_ids(self) -> list[int]:
        return [self.stage_item_input1.id, self.stage_item_input2.id]
