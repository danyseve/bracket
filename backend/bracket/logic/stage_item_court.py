"""Asignacion explicita del tatami de un cuadro (S4.2, F3C, LAB ONLY).

Que anade, y por que hacia falta
--------------------------------
Hasta S4.1 el unico sitio donde vivia un tatami era ``matches.court_id``, asi que el organizador no
tenia forma de decir "este cuadro entero se disputa en este tatami": habia que ir combate a combate
y la decision no quedaba registrada en ninguna parte. Esta operacion anade esa decision al modelo:

* se **persiste** en ``stage_items.court_id`` (1 cuadro -> 1 tatami, nullable). Un tatami aloja
  varios cuadros, asi que no hay indice unico ni exclusividad: lo que es unico es el tatami de cada
  cuadro, no el cuadro de cada tatami;
* se **propaga** a los combates del cuadro (``matches.court_id``), para que el modelo legado y la
  lectura de combates sigan diciendo la verdad de cada combate;
* se **audita** con un evento por efecto real (``TATAMI_ASSIGNED``, ``TATAMI_REASSIGNED``,
  ``TATAMI_UNASSIGNED``): una operacion que no cambia nada no deja evento, como en la ventana de
  inscripcion.

Todo ocurre en **una transaccion y con los bloqueos tomados antes de escribir**: o cambian el cuadro
y sus combates, o no cambia absolutamente nada.

Reglas (y el limite que se declara en vez de inventarlo)
-------------------------------------------------------
El backend **no** tiene estado oficial de combate: ``matches`` no guarda "en curso" ni "finalizado"
(el tanteo arranca en cero y no hay marca de cierre), y por eso no se publica un estado que no
existe. El bloqueo por actividad se apoya solo en evidencia fiable de actividad real: un combate con
tanteo distinto de cero o con hora de inicio. Un ``court_id`` previo, una ``position_in_schedule`` o
el simple hecho de tener cuadro **no** son actividad: son planificacion, y la operacion existe
precisamente para cambiarlos. Asignar, reasignar y desasignar comparten la misma guarda, para que
desasignar no sea un atajo que esquive el bloqueo.

Cuando el cuadro ya tiene actividad, el rechazo es un 409 con recuento (``STAGE_ITEM_IN_USE``): el
llamante sabe cuantos combates lo bloquean sin que se le invente un estado ni se le filtre nada mas.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from bracket.database import database
from bracket.logic.registration_core import authorize_tenant_actor
from bracket.models.db.domain import ActorContext, DomainChangeLogAction
from bracket.sql import stage_item_courts
from bracket.sql.domain_writes import sql_insert_domain_change_log
from bracket.utils.id_types import CourtId, StageItemId, TournamentId

#: Entidad auditada y campo que cambia. El evento guarda el **nombre** del campo, nunca su valor.
ENTITY: Final = "stage_item"
CHANGED_FIELDS: Final = ("court_id",)

#: Acciones del catalogo cerrado de auditoria. Cada efecto real tiene la suya.
ACTION_ASSIGNED: Final = "TATAMI_ASSIGNED"
ACTION_REASSIGNED: Final = "TATAMI_REASSIGNED"
ACTION_UNASSIGNED: Final = "TATAMI_UNASSIGNED"

#: Texto canonico legible de cada evento. No lleva identificadores ni nombre del tatami: el motivo
#: del cambio es la transicion, y los valores concretos viven en el estado, no en la auditoria.
_REASON_ASSIGNED: Final = "tatami asignado al cuadro"
_REASON_REASSIGNED: Final = "tatami reasignado al cuadro"
_REASON_UNASSIGNED: Final = "tatami desasignado del cuadro"


class StageItemCourtError(Exception):
    """Base de los rechazos del dominio de tatami: la ruta los traduce por codigo, no por texto."""


class StageItemNotFoundError(StageItemCourtError):
    """No hay cuadro en ese torneo del tenant: unifica "no existe" y "es de otro torneo"."""


class CourtNotFoundError(StageItemCourtError):
    """No hay tatami en ese torneo: unifica "no existe" y "es de otro torneo"."""


class StageItemInUseError(StageItemCourtError):
    """El cuadro ya tiene actividad real: su tatami queda congelado mientras siga asi."""

    def __init__(self, activity_match_count: int) -> None:
        """Guarda el recuento de combates con actividad que bloquea la operacion."""
        super().__init__("el cuadro ya tiene actividad")
        self.activity_match_count = activity_match_count


@dataclass(frozen=True)
class StageItemCourtAssignment:
    """Resultado publicado: el cuadro, su tatami (o ``None``) y cuantos combates se movieron."""

    stage_item_id: StageItemId
    court_id: CourtId | None
    court_name: str | None
    affected_match_count: int


def _action_for(previous: CourtId | None, target: CourtId | None) -> DomainChangeLogAction:
    """Accion del catalogo que corresponde a la transicion del cuadro."""
    if previous is None:
        return ACTION_ASSIGNED
    if target is None:
        return ACTION_UNASSIGNED
    return ACTION_REASSIGNED


def _reason_for(previous: CourtId | None, target: CourtId | None) -> str:
    """Texto canonico del evento, coherente con la accion elegida."""
    if previous is None:
        return _REASON_ASSIGNED
    if target is None:
        return _REASON_UNASSIGNED
    return _REASON_REASSIGNED


async def assign_stage_item_court(
    *,
    context: ActorContext,
    tournament_id: TournamentId,
    stage_item_id: StageItemId,
    court_id: CourtId | None,
) -> StageItemCourtAssignment:
    """Fija (o quita) el tatami de un cuadro **entero** y lo propaga a sus combates.

    Una sola transaccion con el cuadro y el tatami destino bloqueados antes de escribir:

    1. autoriza al actor contra el tenant del contexto;
    2. bloquea el cuadro del torneo del tenant (o rechaza: no existe, es de otro torneo o es de otro
       tenant, siempre con el mismo error);
    3. bloquea el tatami destino si se esta asignando (mismo criterio: es de este torneo o no vale);
    4. rechaza si el cuadro tiene actividad real;
    5. propaga el tatami a los combates del cuadro y persiste la decision en el cuadro;
    6. audita el efecto **real**: ni una operacion sin evento, ni un evento por una operacion que no
       cambio nada.

    El tatami destino se bloquea aunque el cuadro no lo estuviera: dos operaciones sobre el mismo
    tatami desde cuadros distintos se serializan, y no queda un cuadro apuntando a un tatami borrado
    a mitad.
    """
    async with database.transaction():
        await authorize_tenant_actor(context)

        stage_item = await stage_item_courts.sql_lock_stage_item_for_court(
            stage_item_id=stage_item_id,
            tournament_id=tournament_id,
            tenant_club_id=context.tenant_club_id,
        )
        if stage_item is None:
            raise StageItemNotFoundError("cuadro no encontrado en el torneo del tenant")

        court_name: str | None = None
        if court_id is not None:
            court = await stage_item_courts.sql_lock_court(
                court_id=court_id, tournament_id=tournament_id
            )
            if court is None:
                raise CourtNotFoundError("tatami no encontrado en el torneo")
            court_name = court.name

        activity = await stage_item_courts.sql_count_stage_item_activity(stage_item_id)
        if activity > 0:
            raise StageItemInUseError(activity)

        affected_match_count = await stage_item_courts.sql_propagate_court_to_stage_item_matches(
            stage_item_id=stage_item_id, court_id=court_id
        )

        if stage_item.court_id != court_id:
            await stage_item_courts.sql_set_stage_item_court(
                stage_item_id=stage_item_id, court_id=court_id
            )
            await sql_insert_domain_change_log(
                entity=ENTITY,
                entity_id=int(stage_item_id),
                action=_action_for(stage_item.court_id, court_id),
                changed_fields=list(CHANGED_FIELDS),
                actor_user_id=context.actor_user_id,
                actor_label=context.actor_label,
                reason=_reason_for(stage_item.court_id, court_id),
                reason_code=None,
                reason_note=None,
                tenant_club_id=context.tenant_club_id,
            )

    return StageItemCourtAssignment(
        stage_item_id=stage_item_id,
        court_id=court_id,
        court_name=court_name,
        affected_match_count=affected_match_count,
    )


async def read_stage_item_court_assignment(
    *,
    context: ActorContext,
    tournament_id: TournamentId,
    stage_item_id: StageItemId,
) -> StageItemCourtAssignment:
    """Tatami **vigente** del cuadro para publicar la respuesta de una operacion ya resuelta.

    Se lee el estado actual y no el cuerpo guardado al reservar la clave: al repetir una operacion
    el cliente ve lo que hay, igual que hace la ventana de inscripcion. Un cuadro que no es del
    torneo del tenant da el mismo error de no encontrado, para que repetir tampoco filtre recursos.
    """
    assignment = await stage_item_courts.sql_stage_item_court_assignment(
        stage_item_id=stage_item_id,
        tournament_id=tournament_id,
        tenant_club_id=context.tenant_club_id,
    )
    if assignment is None:
        raise StageItemNotFoundError("cuadro no encontrado en el torneo del tenant")
    court_id, court_name = assignment
    return StageItemCourtAssignment(
        stage_item_id=stage_item_id,
        court_id=court_id,
        court_name=court_name,
        affected_match_count=0,
    )
