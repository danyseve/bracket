"""Ventana de inscripcion del torneo (S3.4c, F3B): plazo, cierre manual y estado derivado.

Vive en su propio modulo porque crece sobre el ciclo de vida de la inscripcion
(``bracket.logic.registrations``), que ya es el modulo mas largo del nucleo: aqui queda el
plazo (``registration_deadline``), el cierre manual (``registrations_closed_at``) y el estado
**derivado** (``OPEN``/``CLOSED``/``DEADLINE_PASSED``, sin columna ni enum), mas la lectura con
bloqueo de fila compartida con el alta.

Reglas (``docs/26`` §39):

* el estado se **deriva** de las dos columnas y del instante de la operacion; no se almacena;
* el cierre manual manda sobre el plazo;
* ``now >= registration_deadline`` ya no admite altas nuevas (frontera incluida);
* la ventana gobierna **solo** el alta: confirmar, retirar, readmitir y descalificar siguen
  su propio juez (elegibilidad y matriz de transiciones);
* no hay reapertura: limpiar el plazo no retira un cierre manual;
* el plazo se guarda sin fraccion (resolucion temporal real del proyecto).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from heliclockter import datetime_utc

from bracket.database import database
from bracket.logic.registration_core import (
    RegistrationDomainError,
    TournamentNotFoundError,
    authorize_tenant_actor,
)
from bracket.models.db.domain import ActorContext, RegistrationWindowState
from bracket.sql import registration_writes
from bracket.sql.domain_writes import sql_insert_domain_change_log
from bracket.utils.id_types import TournamentId

# La ventana de inscripcion se audita como una propiedad del **torneo**, no como una
# transicion de una inscripcion: entidad propia y sin motivo del catalogo de S3.3c-2
# (estas operaciones no estan en ese catalogo y no se amplia en esta fase).
_TOURNAMENT_ENTITY = "tournament"
_WINDOW_CHANGED_FIELDS: tuple[str, ...] = ("registration_deadline",)
_CLOSE_CHANGED_FIELDS: tuple[str, ...] = ("registrations_closed_at",)
_SET_WINDOW_REASON = "configuracion del plazo de inscripciones del torneo"
_CLOSE_REASON = "cierre manual de la ventana de inscripciones del torneo"


# Estado derivado de la ventana. No hay columna ni enum: se calcula con el instante que
# decide la operacion, para que una misma transaccion vea siempre el mismo estado.
# El vocabulario vive en el modelo (``bracket.models.db.domain``), como el resto de estados.
#: Vocabulario cerrado del estado derivado, anotado con el alias del modelo: un valor de fuera de
#: esa lista es un error de tipos aqui, no un estado nuevo que se cuela en la API.
REGISTRATION_WINDOW_STATES: Final[tuple[RegistrationWindowState, ...]] = (
    "OPEN",
    "CLOSED",
    "DEADLINE_PASSED",
)


class RegistrationWindowClosedError(RegistrationDomainError):
    """La ventana del torneo no admite altas: cierre manual activo o plazo vencido."""


class RegistrationWindowAlreadyClosedError(RegistrationDomainError):
    """El torneo ya tenia el cierre manual activo: repetirlo no es un no-op silencioso."""


def registration_window_state(
    *,
    registration_deadline: datetime_utc | None,
    registrations_closed_at: datetime_utc | None,
    now: datetime_utc,
) -> RegistrationWindowState:
    """Estado derivado de la ventana del torneo (funcion pura: se prueba con reloj inyectado).

    * cierre manual -> ``CLOSED`` (manda sobre el plazo, tambien si ya vencio);
    * sin cierre manual y plazo vencido -> ``DEADLINE_PASSED`` (``now >= deadline``: en el
      instante exacto del plazo ya no se admite una inscripcion nueva);
    * en cualquier otro caso -> ``OPEN``.

    La comparacion es entre instantes: las dos columnas son ``timestamptz`` y ``now`` es
    UTC, asi que el resultado no depende de la zona horaria del proceso.
    """
    if registrations_closed_at is not None:
        return "CLOSED"
    if registration_deadline is not None and now >= registration_deadline:
        return "DEADLINE_PASSED"
    return "OPEN"


async def load_window_row(
    context: ActorContext, tournament_id: TournamentId, *, for_update: bool
) -> registration_writes.TournamentRegistrationWindowRow:
    """Fila de la ventana del torneo, **con el bloqueo de fila ya tomado**.

    Un torneo de otro tenant y un torneo inexistente dan el mismo error (no se filtra su
    existencia) y en los dos casos se sale antes de escribir nada.
    """
    row = await registration_writes.sql_tournament_registration_window(
        tournament_id=tournament_id, tenant_club_id=context.tenant_club_id, for_update=for_update
    )
    if row is None:
        raise TournamentNotFoundError("torneo no encontrado en este tenant")
    return row


def assert_window_open(
    row: registration_writes.TournamentRegistrationWindowRow, *, now: datetime_utc
) -> None:
    """El alta solo pasa si la ventana esta abierta en el instante de la operacion."""
    if (
        registration_window_state(
            registration_deadline=row.registration_deadline,
            registrations_closed_at=row.registrations_closed_at,
            now=now,
        )
        != "OPEN"
    ):
        raise RegistrationWindowClosedError(
            "la ventana de inscripcion del torneo no admite altas nuevas"
        )


@dataclass(frozen=True)
class RegistrationWindow:
    """Ventana de inscripcion del torneo tal y como la ve el dominio."""

    tournament_id: TournamentId
    registration_deadline: datetime_utc | None
    registrations_closed_at: datetime_utc | None
    state: RegistrationWindowState

    @property
    def registrations_open(self) -> bool:
        """``True`` solo si la ventana admite altas nuevas en el instante derivado."""
        return self.state == "OPEN"


def _window_from_row(
    tournament_id: TournamentId,
    row: registration_writes.TournamentRegistrationWindowRow,
    *,
    now: datetime_utc,
) -> RegistrationWindow:
    """Proyecta la fila leida al modelo de la ventana, con el estado derivado del momento."""
    return RegistrationWindow(
        tournament_id=tournament_id,
        registration_deadline=row.registration_deadline,
        registrations_closed_at=row.registrations_closed_at,
        state=registration_window_state(
            registration_deadline=row.registration_deadline,
            registrations_closed_at=row.registrations_closed_at,
            now=now,
        ),
    )


async def registration_window(
    context: ActorContext, tournament_id: TournamentId, *, now: datetime_utc | None = None
) -> RegistrationWindow:
    """Lectura de la ventana del torneo del tenant: no escribe ni audita nada.

    La lectura comparte el camino de la fila bloqueada en modo compartido, que en una
    sentencia suelta se libera al terminar: una lectura concurrente con un cierre en vuelo
    espera y ve el estado ya cerrado, nunca un estado intermedio.
    """
    await authorize_tenant_actor(context)
    moment = datetime_utc.now() if now is None else now
    row = await load_window_row(context, tournament_id, for_update=False)
    return _window_from_row(tournament_id, row, now=moment)


async def set_registration_deadline(
    context: ActorContext,
    tournament_id: TournamentId,
    *,
    registration_deadline: datetime_utc | None,
    now: datetime_utc | None = None,
) -> RegistrationWindow:
    """Fija (o limpia, con ``None``) el plazo de inscripcion del torneo del tenant.

    Idempotente por valor: si el plazo no cambia no se escribe un segundo evento de
    auditoria (mismo criterio que una edicion de borrador que no cambia nada). **No reabre
    nada**: limpiar el plazo no retira un cierre manual y poner un plazo nuevo tampoco. El
    plazo se compara por instante, asi que el mismo momento con otro desplazamiento horario
    no genera un evento de mas.

    El plazo se guarda sin fraccion: el codec de timestamps del proyecto
    (``bracket.database``) lee los instantes descartando la fraccion, asi que un plazo con
    microsegundos nunca volveria a coincidir con lo leido y cada peticion auditaria de nuevo.
    """
    moment = datetime_utc.now() if now is None else now
    deadline = (
        None if registration_deadline is None else registration_deadline.replace(microsecond=0)
    )
    async with database.transaction():
        await authorize_tenant_actor(context)
        row = await load_window_row(context, tournament_id, for_update=True)
        if row.registration_deadline == deadline:
            return _window_from_row(tournament_id, row, now=moment)

        stored = await registration_writes.sql_set_registration_deadline(
            tournament_id=tournament_id,
            tenant_club_id=context.tenant_club_id,
            registration_deadline=deadline,
        )
        if not stored:  # inalcanzable con la fila ya bloqueada; no se confirma a medias
            raise TournamentNotFoundError("torneo no encontrado en este tenant")
        await sql_insert_domain_change_log(
            entity=_TOURNAMENT_ENTITY,
            entity_id=tournament_id,
            action="SET_REGISTRATION_WINDOW",
            changed_fields=list(_WINDOW_CHANGED_FIELDS),
            actor_user_id=context.actor_user_id,
            actor_label=context.actor_label,
            reason=_SET_WINDOW_REASON,
            reason_code=None,
            reason_note=None,
            tenant_club_id=context.tenant_club_id,
        )
        return RegistrationWindow(
            tournament_id=tournament_id,
            registration_deadline=deadline,
            registrations_closed_at=row.registrations_closed_at,
            state=registration_window_state(
                registration_deadline=deadline,
                registrations_closed_at=row.registrations_closed_at,
                now=moment,
            ),
        )


async def close_registrations(
    context: ActorContext, tournament_id: TournamentId, *, now: datetime_utc | None = None
) -> RegistrationWindow:
    """Cierre manual de la ventana del torneo, con su evento de auditoria en la misma transaccion.

    Un torneo ya cerrado da :class:`RegistrationWindowAlreadyClosedError`: repetir el cierre
    **no** es un no-op silencioso (con la misma clave de idempotencia, el replay del protocolo
    si es estable y ni siquiera llega aqui). El cierre no toca ninguna inscripcion: la ventana
    solo gobierna el alta de inscripciones nuevas.
    """
    moment = datetime_utc.now() if now is None else now
    async with database.transaction():
        await authorize_tenant_actor(context)
        row = await load_window_row(context, tournament_id, for_update=True)
        if row.registrations_closed_at is not None:
            raise RegistrationWindowAlreadyClosedError(
                "el torneo ya tenia el cierre manual de inscripciones activo"
            )

        closed = await registration_writes.sql_close_registrations(
            tournament_id=tournament_id, tenant_club_id=context.tenant_club_id, closed_at=moment
        )
        if not closed:  # inalcanzable con la fila ya bloqueada; no se confirma a medias
            raise TournamentNotFoundError("torneo no encontrado en este tenant")
        await sql_insert_domain_change_log(
            entity=_TOURNAMENT_ENTITY,
            entity_id=tournament_id,
            action="CLOSE_REGISTRATIONS",
            changed_fields=list(_CLOSE_CHANGED_FIELDS),
            actor_user_id=context.actor_user_id,
            actor_label=context.actor_label,
            reason=_CLOSE_REASON,
            reason_code=None,
            reason_note=None,
            tenant_club_id=context.tenant_club_id,
        )
    return RegistrationWindow(
        tournament_id=tournament_id,
        registration_deadline=row.registration_deadline,
        registrations_closed_at=moment,
        state="CLOSED",
    )
