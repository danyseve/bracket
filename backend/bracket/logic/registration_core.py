"""Primitivas compartidas del nucleo de inscripciones (S3.1/S3.4c, F3B).

La base de errores de dominio y la guarda de acceso al tenant viven aqui porque las comparten
el ciclo de vida de la inscripcion (``bracket.logic.registrations``) y la ventana del torneo
(``bracket.logic.registration_window``): ninguno de los dos modulos puede importarlas del otro
sin crear un ciclo de importacion.
"""

from __future__ import annotations

from bracket.logic.competitors import TenantNotAuthorizedError
from bracket.models.db.domain import ActorContext
from bracket.sql.users import get_user_relation_to_club


class RegistrationDomainError(Exception):
    """Base de los errores de la capa interna. Los mensajes no llevan datos personales."""


class TournamentNotFoundError(RegistrationDomainError):
    """El torneo no existe o no es del tenant del contexto (el mismo error a proposito)."""


async def authorize_tenant_actor(context: ActorContext) -> None:
    """El actor tiene que tener relacion con el tenant del contexto.

    No distingue "tenant inexistente" de "tenant no autorizado": no se filtra la
    existencia del tenant. OWNER y COLLABORATOR valen igual para este nucleo.
    """
    relation = await get_user_relation_to_club(context.tenant_club_id, context.actor_user_id)
    if relation is None:
        raise TenantNotAuthorizedError("el actor no tiene acceso a este tenant")
