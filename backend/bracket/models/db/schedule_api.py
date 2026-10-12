"""S4.5 — contrato HTTP del planificador de torneo (F3C, LAB ONLY).

Dos cuerpos y dos respuestas, sin sorpresas: la peticion de planificar (§10) y el resumen del
efecto. El resumen lleva **solo escalares** porque es lo que se guarda con la operacion confirmada:
repetir la misma clave devuelve exactamente ese mismo resumen, no uno recalculado sobre el estado
actual. El detalle del plan (que combate en que tatami y a que hora) se lee del ``GET``, que es una
lectura y no un efecto.
"""

from __future__ import annotations

from heliclockter import datetime_utc
from pydantic import BaseModel, Field

from bracket.utils.id_types import CourtId, TournamentId

#: Descanso minimo inicial de LAB (§6): parametro de la operacion, no una columna ni un valor fijo.
DEFAULT_MINIMUM_REST_MINUTES = 15


class ScheduleGenerateBody(BaseModel):
    """Lo que el organizador decide: cuando empieza, cuanto descansa y en que tatamis se juega."""

    start_time: datetime_utc | None = None
    minimum_rest_minutes: int = Field(DEFAULT_MINIMUM_REST_MINUTES, ge=0, le=240)
    #: Tatamis activos del evento. ``None`` = todos los del torneo (no siempre son seis, §1).
    court_ids: list[CourtId] | None = None
    #: Solo se mueve una asignacion explicita si el organizador lo pide expresamente (§8).
    preserve_assigned: bool = True


class ScheduleSummaryResponse(BaseModel):
    """Efecto de la operacion: cuantos combates se planificaron y cuantos se movieron."""

    tournament_id: TournamentId
    court_count: int
    scheduled_match_count: int
    moved_match_count: int
    manual_match_count: int
    unscheduled_match_count: int
    start_time: str


class TournamentScheduleResponse(BaseModel):
    """Vista de calendario del torneo: por tatami, AHORA, SIGUIENTE y los siguientes (§12)."""

    tournament: dict[str, object]
    generated_at: str
    courts: list[dict[str, object]]
