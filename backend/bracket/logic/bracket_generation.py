"""Generacion explicita del cuadro del torneo (S3.5a, F3B, LAB ONLY).

El cuadro **no** se genera al cerrar inscripciones: se genera cuando el organizador lo pide, y esa
peticion no se fia de ninguna revision anterior del cliente. Dentro de la propia transaccion de la
operacion, y con la fila del torneo ya bloqueada, la revision de S3.4d se **recalcula** con las
mismas funciones puras; solo si sale lista se ejecuta la generacion certificada de F2C.

No se duplica ninguna regla: que cuadro se puede generar lo decide el bloqueo de F2C
(``get_bracket_generation_blocker``), el reparto lo decide su planificador
(``plan_bracket_generation``), la ventana es la de S3.4c, el minimo de participantes es el unico de
``generation.py`` y la revision es la de S3.4d entera (misma funcion, mismos datos,
mismo veredicto).

Orden de bloqueos de la operacion completa: clave de idempotencia (protocolo de S3.3c-4) -> fila del
torneo, ``FOR NO KEY UPDATE`` (el mismo bloqueo de las escrituras de la ventana) -> escrituras del
reparto. Ninguna escritura de F3 toma esos bloqueos en otro orden, asi que no hay ciclo posible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from heliclockter import datetime_utc

from bracket.logic.bracket_readiness import BracketReadiness, evaluate_bracket_readiness
from bracket.logic.registration_core import RegistrationDomainError
from bracket.logic.registration_window import (
    load_window_row,
    registration_window,
    registration_window_state,
)
from bracket.logic.scheduling.generation import (
    MINIMUM_ENTRANTS,
    BracketGenerationPlan,
    generate_bracket_for_stage_item,
    get_bracket_generation_blocker,
    plan_bracket_generation,
)
from bracket.models.db.domain import ActorContext, DomainChangeLogAction
from bracket.models.db.util import StageItemWithRounds
from bracket.sql.bracket_generation import get_single_elimination_stage_items
from bracket.sql.bracket_readiness import (
    get_existing_single_elimination_brackets,
    get_registration_readiness_rows,
)
from bracket.sql.domain_writes import sql_insert_domain_change_log
from bracket.utils.id_types import StageItemId, TournamentId

#: Entidad de auditoria: generar el cuadro es una escritura **del torneo**, no de una inscripcion.
#: Es la misma entidad que usan las dos operaciones de ventana de S3.4c.
_TOURNAMENT_ENTITY = "tournament"

#: Accion del catalogo cerrado ``DomainChangeLogAction``. No es una transicion de una inscripcion,
#: asi que el evento no lleva ``reason_code`` del catalogo de motivos de S3.3c-2 (la base lo admite:
#: su restriccion solo acota los codigos no nulos).
_GENERATE_ACTION: DomainChangeLogAction = "GENERATE_BRACKET"

#: Nombres de los campos que la generacion escribe: identidad de columnas, nunca valores ni
#: identificadores de equipos (la auditoria no guarda el reparto, solo que se reparto).
_GENERATED_CHANGED_FIELDS: tuple[str, ...] = ("stage_item_inputs.team_id",)

_GENERATE_REASON = "generacion explicita del cuadro de eliminacion directa"


class BracketNotReadyError(RegistrationDomainError):
    """La revision del torneo no esta lista: no se genera nada.

    Lleva la revision **recalculada dentro de la transaccion**, que es lo unico que explica el
    rechazo con datos derivados. No es un error de peticion: es el estado del dominio, y por eso el
    contrato lo responde como conflicto de estado y no como peticion invalida.
    """

    def __init__(self, readiness: BracketReadiness) -> None:
        super().__init__("el torneo no esta listo para generar el cuadro")
        self.readiness = readiness


@dataclass(frozen=True)
class GeneratedBracket:
    """Un cuadro generatable y el plan que le corresponde (el de F2C, sin recalcular nada)."""

    stage_item_id: StageItemId
    plan: BracketGenerationPlan


@dataclass(frozen=True)
class TournamentBracketGeneration:
    """Lo que la operacion deja: los cuadros con reparto y los que quedaron fuera."""

    tournament_id: TournamentId
    brackets: tuple[GeneratedBracket, ...]
    skipped_stage_items: int

    @property
    def changed(self) -> bool:
        """Algun cuadro cambio de reparto **con esta llamada** (es lo que decide la auditoria)."""
        return any(bracket.plan.changed for bracket in self.brackets)

    @property
    def generated(self) -> bool:
        """Post-condicion del torneo: hay al menos un cuadro con el minimo de participantes.

        No dice "esta llamada escribio algo": es el estado resultante, asi que una repeticion con
        otra clave devuelve lo mismo. Si el cuadro no llega al minimo, F2C no lo genera y aqui no
        viaja como generado.
        """
        return any(bracket.plan.entrant_count >= MINIMUM_ENTRANTS for bracket in self.brackets)


def generatable_stage_items(
    stage_items: Sequence[StageItemWithRounds],
) -> tuple[list[StageItemWithRounds], int]:
    """Los cuadros que F2C admite generar ahora mismo, en el orden de la lectura, y los que no.

    El veredicto es literalmente el bloqueo certificado de F2C: aqui no hay una segunda lista de
    motivos. Un cuadro bloqueado no se toca y no es un error (sin participantes, alimentado por otro
    cuadro, o con actividad encima): simplemente no tiene nada que generar.
    """
    generatable = [
        stage_item
        for stage_item in stage_items
        if get_bracket_generation_blocker(stage_item) is None
    ]
    return generatable, len(stage_items) - len(generatable)


def plan_generatable_brackets(
    stage_items: Sequence[StageItemWithRounds],
) -> tuple[list[GeneratedBracket], int]:
    """Plan **puro** de cada cuadro generatable, para poder leerlo sin escribir nada.

    Reutiliza el filtro de ``generatable_stage_items`` y el planificador de F2C: la lectura y la
    escritura no pueden discrepar sobre que cuadro se genera ni sobre como se reparte.
    """
    generatable, skipped = generatable_stage_items(stage_items)
    return [
        GeneratedBracket(stage_item_id=stage_item.id, plan=plan_bracket_generation(stage_item))
        for stage_item in generatable
    ], skipped


async def generate_bracket_for_tournament(
    context: ActorContext, tournament_id: TournamentId
) -> TournamentBracketGeneration:
    """Genera los cuadros del torneo revalidando la revision **dentro** de la transaccion.

    Se llama siempre dentro de la transaccion de la operacion (protocolo de S3.3c-4), que es la
    unica que hace ``commit``: aqui no hay commits intermedios, asi que un fallo en cualquier cuadro
    deshace tambien los anteriores y la reserva de idempotencia no queda consumida.
    """
    moment = datetime_utc.now()
    # La fila del torneo se bloquea **antes** de leer nada que dependa de ella: es lo que serializa
    # esta operacion contra el alta, el cierre, el ciclo de vida y otra generacion en vuelo,
    # y hace que la revision de abajo vea el estado real, no el que el cliente leyo antes.
    row = await load_window_row(context, tournament_id, for_update=True)
    window_state = registration_window_state(
        registration_deadline=row.registration_deadline,
        registrations_closed_at=row.registrations_closed_at,
        now=moment,
    )

    readiness = evaluate_bracket_readiness(
        tournament_id=tournament_id,
        tenant_club_id=context.tenant_club_id,
        window_state=window_state,
        rows=await get_registration_readiness_rows(
            tournament_id, tenant_club_id=context.tenant_club_id
        ),
        existing_brackets=await get_existing_single_elimination_brackets(tournament_id),
    )
    if not readiness.ready:
        raise BracketNotReadyError(readiness)

    stage_items = await get_single_elimination_stage_items(tournament_id)
    generatable, skipped = generatable_stage_items(stage_items)
    brackets = [
        GeneratedBracket(
            stage_item_id=stage_item.id,
            plan=await generate_bracket_for_stage_item(tournament_id, stage_item),
        )
        for stage_item in generatable
    ]

    generation = TournamentBracketGeneration(
        tournament_id=tournament_id,
        brackets=tuple(brackets),
        skipped_stage_items=skipped,
    )
    if generation.changed:
        # Solo si el reparto cambio. Reintentar con otra clave y encontrar el cuadro ya repartido es
        # un no-op: no escribe un segundo evento ni "regenera" lo mismo otra vez.
        await sql_insert_domain_change_log(
            entity=_TOURNAMENT_ENTITY,
            entity_id=tournament_id,
            action=_GENERATE_ACTION,
            changed_fields=list(_GENERATED_CHANGED_FIELDS),
            actor_user_id=context.actor_user_id,
            actor_label=context.actor_label,
            reason=_GENERATE_REASON,
            reason_code=None,
            reason_note=None,
            tenant_club_id=context.tenant_club_id,
        )
    return generation


async def read_tournament_bracket_generation(
    context: ActorContext, tournament_id: TournamentId
) -> TournamentBracketGeneration:
    """Relee el reparto de los cuadros **sin escribir nada**, para componer la respuesta.

    Es el mismo camino para una operacion ejecutada y para un replay con la misma clave (que el
    protocolo no vuelve a ejecutar): el cuerpo se reconstruye del estado del torneo, igual que las
    demas escrituras de F3, de modo que una repeticion devuelve el mismo reparto. La ventana se lee
    primero porque es la que autoriza el torneo y acota al tenant; no se toma el bloqueo exclusivo:
    componer una respuesta no puede esperar a una generacion en vuelo.
    """
    await registration_window(context, tournament_id)
    brackets, skipped = plan_generatable_brackets(
        await get_single_elimination_stage_items(tournament_id)
    )
    return TournamentBracketGeneration(
        tournament_id=tournament_id,
        brackets=tuple(brackets),
        skipped_stage_items=skipped,
    )
