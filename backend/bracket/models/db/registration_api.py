"""Contratos HTTP de entrada y salida de las rutas F3 de inscripciones (S3.4a/S3.4b, LAB ONLY).

Modelos **explicitos** de la capa HTTP. No se reutiliza ningun modelo interno como cuerpo ni
como respuesta: el contrato externo no depende de la forma de la fila, de modo que una columna
nueva en la tabla no aparece sola en la respuesta.

Lo que **no** esta aqui, a proposito:

* identidad y tenant (``actor_user_id``, ``actor_label``, ``tenant_club_id``, rol): los resuelve el
  servidor a partir del JWT. No son datos de entrada y un cuerpo que los traiga se **rechaza**
  (``extra="forbid"`` -> 422), no se ignora en silencio;
* ``status``, ``revision`` y los enlaces de correccion: los cambia el ciclo de vida, nunca el
  payload (ya lo documenta ``RegistrationDraftData``);
* huellas, clave de idempotencia, ``verified_by_user_id``/``verified_at`` y cualquier metadato
  interno: no viajan al cliente.

El motivo de auditoria entra solo por el vocabulario cerrado ``reason_code``/``reason_note``: no
hay campo de texto libre.
"""

from __future__ import annotations

from typing import Literal

from heliclockter import datetime_utc
from pydantic import BaseModel, ConfigDict

from bracket.models.db.domain import (
    RegistrationDraftData,
    RegistrationIdentityStatus,
    RegistrationRepresentation,
    RegistrationStatus,
    RegistrationUnknownIdentityStatus,
    TournamentRegistration,
)
from bracket.utils.id_types import (
    CompetitorId,
    CompetitorXSportsClubId,
    SportsClubId,
    TournamentId,
    TournamentRegistrationId,
)


class RegistrationRequestModel(BaseModel):
    """Base de los cuerpos F3.

    ``extra="forbid"``: un intento de aportar identidad o tenant (``actor_user_id``,
    ``actor_label``, ``tenant_club_id``, rol) es un dato invalido, no un campo a ignorar. Asi el
    cliente no puede creer que su valor se ha tenido en cuenta.
    """

    model_config = ConfigDict(extra="forbid")


class RegistrationCreateBody(RegistrationRequestModel):
    """Estado deseado de una inscripcion en borrador, mas el motivo opcional del alta.

    Los campos de borrador son los de :class:`RegistrationDraftData` en su forma de transporte:
    ``identity_status`` no admite ``VERIFIED`` (se deriva de ``competitor_id``) y ni ``status`` ni
    ``revision`` ni los enlaces de correccion son datos de entrada.
    """

    competitor_id: CompetitorId | None = None
    identity_status: RegistrationUnknownIdentityStatus | None = None
    representation: RegistrationRepresentation
    sports_club_id: SportsClubId | None = None
    affiliation_id: CompetitorXSportsClubId | None = None
    category_key: str | None = None
    category_label: str | None = None
    competitor_name_snapshot: str | None = None
    reason_code: str | None = None
    reason_note: str | None = None

    def to_draft(self) -> RegistrationDraftData:
        """Traduce el cuerpo al contrato de dominio del borrador.

        El motivo de auditoria **no** forma parte del borrador: se pasa aparte a la operacion.
        """
        return RegistrationDraftData(
            competitor_id=self.competitor_id,
            identity_status=self.identity_status,
            representation=self.representation,
            sports_club_id=self.sports_club_id,
            affiliation_id=self.affiliation_id,
            category_key=self.category_key,
            category_label=self.category_label,
            competitor_name_snapshot=self.competitor_name_snapshot,
        )


class RegistrationLifecycleBody(RegistrationRequestModel):
    """Motivo de una transicion del ciclo de vida.

    Cual de las dos formas es obligatoria lo decide el catalogo de motivos (S3.3c-2) dentro de la
    operacion de dominio, no este modelo: aqui solo se declara el vocabulario cerrado.
    """

    reason_code: str | None = None
    reason_note: str | None = None


class RegistrationResponse(BaseModel):
    """Proyeccion publica y estable de una inscripcion (lista blanca explicita).

    Se excluyen a proposito ``corrects_registration_id``/``superseded_by_registration_id``
    (enlaces internos de correccion), ``verified_by_user_id``/``verified_at`` y todo metadato de
    idempotencia. Los nulos se serializan explicitos: el contrato no cambia de forma entre
    respuestas.
    """

    id: TournamentRegistrationId
    tournament_id: TournamentId
    competitor_id: CompetitorId | None
    identity_status: RegistrationIdentityStatus
    representation: RegistrationRepresentation
    sports_club_id: SportsClubId | None
    category_key: str | None
    category_label: str | None
    competitor_name_snapshot: str
    sports_club_name_snapshot: str | None
    status: RegistrationStatus
    revision: int
    created: datetime_utc
    updated_at: datetime_utc | None

    @classmethod
    def from_registration(cls, registration: TournamentRegistration) -> RegistrationResponse:
        """Construye la respuesta campo a campo: nada entra por defecto."""
        return cls(
            id=registration.id,
            tournament_id=registration.tournament_id,
            competitor_id=registration.competitor_id,
            identity_status=registration.identity_status,
            representation=registration.representation,
            sports_club_id=registration.sports_club_id,
            category_key=registration.category_key,
            category_label=registration.category_label,
            competitor_name_snapshot=registration.competitor_name_snapshot,
            sports_club_name_snapshot=registration.sports_club_name_snapshot,
            status=registration.status,
            revision=registration.revision,
            created=registration.created,
            updated_at=registration.updated_at,
        )


#: Estados por los que se puede filtrar el listado. ``CORRECTED`` **no** esta: una inscripcion
#: sustituida por una correccion no forma parte del listado vigente (S1 ya excluye las sustituidas),
#: asi que seria un valor que nunca puede devolver filas. Se rechaza con 422 en lugar de responder
#: una lista vacia que el cliente no sabria distinguir de "no hay ninguna".
RegistrationFilterStatus = Literal["DRAFT", "CONFIRMED", "WITHDRAWN", "DISQUALIFIED"]


class RegistrationListResponse(BaseModel):
    """Pagina de inscripciones vigentes del torneo, con el total del filtro.

    ``count`` es el total del filtro **sin** ``limit``/``offset``: es lo que permite saber si
    quedan mas paginas sin pedir una de mas. La pagina no repite ni se salta filas porque el orden
    es total (competidor, categoria e identificador).
    """

    count: int
    registrations: list[RegistrationResponse]
