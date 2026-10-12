"""S4.5 — rutas del planificador de torneo (F3C, LAB ONLY).

Tres rutas: dos escrituras y una lectura.

* ``POST /tournaments/{tournament_id}/schedule/generate`` rellena lo que no tiene tatami ni hora y
  **no toca** nada ya asignado.
* ``POST /tournaments/{tournament_id}/schedule/recalculate`` vuelve a planificar lo pendiente (tras
  un retraso, un cambio de tatamis o una mesa nueva) y solo mueve una asignacion explicita si el
  organizador lo pide con ``preserve_assigned=false``: la estimacion se mueve sola, el plan solo a
  mano (§9).
* ``GET /tournaments/{tournament_id}/schedule`` es la vista de calendario del organizador: por
  tatami, AHORA, SIGUIENTE y los siguientes, con la hora prevista y la estimada.

Las dos escrituras son OWNER-only, pasan por la puerta de F3 (``503 F3_WRITE_DISABLED`` antes de
reservar, bloquear, escribir o auditar) y por el protocolo de idempotencia con ``Idempotency-Key``
obligatoria. El resumen que se guarda con la operacion confirmada lleva **solo escalares**: repetir
la misma clave devuelve ese mismo resumen, no uno recalculado sobre el estado actual. Como el plan
es determinista, repetir la operacion con una clave nueva no mueve ni un combate mas.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette import status

from bracket.config import config
from bracket.logic.idempotency import IdempotencyDecision
from bracket.logic.idempotency_protocol import OperationOutcome
from bracket.logic.planning.schedule_service import (
    ScheduleGenerationError,
    TournamentPlan,
    apply_plan,
    lag_minutes_by_court,
    load_tournament_plan,
    match_rows,
    public_matches,
)
from bracket.logic.scheduling.public_schedule import (
    assert_public_payload_is_safe,
    build_court_display,
    to_public_payload,
)
from bracket.models.db.domain import ActorContext
from bracket.models.db.schedule_api import (
    ScheduleGenerateBody,
    ScheduleSummaryResponse,
    TournamentScheduleResponse,
)
from bracket.routes.domain_auth import actor_context_for_tournament
from bracket.routes.domain_idempotency import execute_idempotent_operation, idempotency_request
from bracket.routes.f3_write_gate import require_f3_write_enabled
from bracket.sql.courts import get_all_courts_in_tournament
from bracket.sql.stages import get_full_tournament_details
from bracket.sql.tournaments import sql_get_tournament
from bracket.utils.id_types import TournamentId

router = APIRouter(prefix=config.api_prefix)

_RESOURCE_TYPE: Final = "schedule"
_ERROR_NOT_PLANNABLE: Final = "SCHEDULE_NOT_PLANNABLE"


def _summary(
    *, tournament_id: TournamentId, plan: TournamentPlan, moved_match_count: int
) -> dict[str, object]:
    """Resumen del efecto, en escalares: es lo que se guarda y lo que se repite."""
    start_time = min(
        (slot.start_time for slot in plan.plan.slots), default=plan.tournament.start_time
    )
    return {
        "tournament_id": int(tournament_id),
        "court_count": len(plan.court_ids),
        "scheduled_match_count": plan.scheduled_match_count,
        "moved_match_count": moved_match_count,
        "manual_match_count": len(plan.plan.manual_match_ids),
        "unscheduled_match_count": len(plan.plan.unscheduled_match_ids),
        "start_time": start_time.astimezone(UTC).isoformat() if start_time is not None else "",
    }


async def _load_schedule_view(
    *, tournament_id: TournamentId, upcoming_limit: int = 3
) -> TournamentScheduleResponse:
    """Vista de calendario del torneo: solo datos deportivos, con la estimacion ya derivada.

    Es una lectura: no escribe nada ni mueve el plan. El guardian de privacidad se aplica aqui
    tambien, porque esta vista y la publica comparten cuerpo: si alguien anade un dato privado al
    contrato, esta ruta falla en vez de publicarlo.
    """
    tournament = await sql_get_tournament(tournament_id)
    courts = await get_all_courts_in_tournament(tournament_id)
    stages = await get_full_tournament_details(tournament_id)
    lag_by_court = lag_minutes_by_court(
        matches=[match for _, _, _, match in match_rows(stages)], tournament=tournament
    )
    public = public_matches(stages=stages, tournament=tournament, lag_by_court=lag_by_court)
    courts_display = [
        build_court_display(
            court_id=int(court.id),
            court_name=court.name,
            matches=public,
            lag_minutes=max(0, int(lag_by_court.get(int(court.id), 0))),
            upcoming_limit=upcoming_limit,
        )
        for court in sorted(courts, key=lambda item: (item.name, int(item.id)))
    ]
    payload = to_public_payload(
        tournament_name=tournament.name,
        tournament_start_time=tournament.start_time,
        courts=courts_display,
        generated_at=datetime.now(UTC),
    )
    assert_public_payload_is_safe(payload)
    return TournamentScheduleResponse.model_validate(payload)


async def _run_schedule_operation(
    *,
    tournament_id: TournamentId,
    body: ScheduleGenerateBody,
    request: Request,
    context: ActorContext,
    mode: str,
) -> ScheduleSummaryResponse:
    """Ejecuta la operacion bajo el protocolo y devuelve el resumen confirmado."""
    executed: list[dict[str, object]] = []

    async def operation() -> OperationOutcome:
        plan = await load_tournament_plan(
            tournament_id=tournament_id,
            start_time=body.start_time,
            minimum_rest_minutes=body.minimum_rest_minutes,
            court_ids=None if body.court_ids is None else [int(court) for court in body.court_ids],
            preserve_assigned=body.preserve_assigned,
            mode="generate" if mode == "generate" else "recalculate",
        )
        moved = await apply_plan(plan=plan)
        summary = _summary(tournament_id=tournament_id, plan=plan, moved_match_count=moved)
        executed.append(summary)
        return OperationOutcome(
            response_status=status.HTTP_200_OK,
            metadata=summary,
            resource_type=_RESOURCE_TYPE,
            resource_id=int(tournament_id),
        )

    protocol_request = await idempotency_request(request)
    try:
        result = await execute_idempotent_operation(
            context=context,
            request=protocol_request,
            operation=operation,
            require_owner=True,
        )
    except ScheduleGenerationError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": _ERROR_NOT_PLANNABLE, "message": str(error)},
        ) from error

    if result.decision is IdempotencyDecision.RESERVED and executed:
        return ScheduleSummaryResponse(**executed[-1])
    return ScheduleSummaryResponse(**(result.response_body or {}))


@router.post(
    "/tournaments/{tournament_id}/schedule/generate",
    dependencies=[Depends(require_f3_write_enabled)],
    response_model=ScheduleSummaryResponse,
)
async def generate_schedule_endpoint(
    tournament_id: TournamentId,
    body: ScheduleGenerateBody,
    request: Request,
    context: ActorContext = Depends(actor_context_for_tournament),
) -> ScheduleSummaryResponse:
    """Planifica lo que todavia no tiene tatami ni hora, sin tocar lo ya asignado (§7)."""
    return await _run_schedule_operation(
        tournament_id=tournament_id, body=body, request=request, context=context, mode="generate"
    )


@router.post(
    "/tournaments/{tournament_id}/schedule/recalculate",
    dependencies=[Depends(require_f3_write_enabled)],
    response_model=ScheduleSummaryResponse,
)
async def recalculate_schedule_endpoint(
    tournament_id: TournamentId,
    body: ScheduleGenerateBody,
    request: Request,
    context: ActorContext = Depends(actor_context_for_tournament),
) -> ScheduleSummaryResponse:
    """Vuelve a planificar el torneo; una asignacion explicita solo se mueve si se pide (§9)."""
    return await _run_schedule_operation(
        tournament_id=tournament_id, body=body, request=request, context=context, mode="recalculate"
    )


@router.get(
    "/tournaments/{tournament_id}/schedule",
    response_model=TournamentScheduleResponse,
)
async def get_tournament_schedule_endpoint(
    tournament_id: TournamentId,
    context: ActorContext = Depends(actor_context_for_tournament),
) -> TournamentScheduleResponse:
    """Vista de calendario del organizador: por tatami, AHORA, SIGUIENTE y los siguientes (§12)."""
    return await _load_schedule_view(tournament_id=tournament_id)
