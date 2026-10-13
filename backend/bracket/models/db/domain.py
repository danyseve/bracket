"""Modelos del dominio Competitor/SportsClub/Registration.

S1 (``bracket/sql/domain_reads.py``): proyeccion **solo lectura**, minima y sin PII.

S2 (``bracket/sql/domain_writes.py`` + ``bracket/logic/competitors.py``) anade el
contexto de escritura (:class:`ActorContext`) y el contrato de datos basicos
editables (:class:`CompetitorBasicDataUpdate`). Sigue sin haber variantes
``*Insertable``: las escrituras usan parametros explicitos y ``managed_by_club_id``
no es un dato de entrada, sale siempre del contexto autorizado.

S3.1 (``bracket/sql/registration_writes.py`` + ``bracket/logic/registrations.py``)
anade el contrato de datos de una inscripcion en borrador
(:class:`RegistrationDraftData`). Tampoco hay ``*Insertable`` de inscripcion: el
estado, la revision, los enlaces de correccion y los snapshots derivados no son
datos de entrada y el ``tournament_id`` se valida contra el tenant del contexto.

Acceso minimo a PII: se excluyen a proposito los campos que no hacen falta para
consultar el dominio:

* ``tournament_registrations.verified_by_user_id`` / ``verified_at``;
* ``*_name_history.changed_by_user_id``;
* ``competitors_x_sports_clubs.note`` (texto libre).

Si una UI futura necesita alguno de ellos, se anadira como campo explicito y
revisado, nunca como ``SELECT *``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from heliclockter import datetime_utc

from bracket.models.db.shared import BaseModelORM
from bracket.utils.id_types import (
    BeltId,
    ClubId,
    CompetitorId,
    CompetitorXSportsClubId,
    SportsClubId,
    TournamentId,
    TournamentRegistrationId,
    UserId,
)

type RegistrationIdentityStatus = Literal["UNVERIFIED", "AMBIGUOUS", "VERIFIED"]
type RegistrationRepresentation = Literal["INDEPENDENT", "CLUB"]
type RegistrationStatus = Literal["DRAFT", "CONFIRMED", "WITHDRAWN", "DISQUALIFIED", "CORRECTED"]
# Vocabularios de clasificacion (S4.8a-1). El sexo de la **persona** y la restriccion de la
# **categoria** son tipos distintos a proposito: ``MIXED`` describe una categoria, no a nadie.
type CompetitorSex = Literal["MALE", "FEMALE"]
type SexConstraint = Literal["MALE", "FEMALE", "MIXED"]
# Modalidad de una participacion deportiva concreta: GI y NO_GI son inscripciones distintas.
type Modality = Literal["GI", "NO_GI"]
# Escala del cinturon: el mismo color existe en la juvenil y en la adulta, y el orden solo es
# comparable dentro de su escala.
type BeltScope = Literal["JUVENILE", "ADULT"]
type DomainChangeLogAction = Literal[
    "CREATE",
    "UPDATE",
    "DEACTIVATE",
    "ACTIVATE",
    "CONFIRM",
    "WITHDRAW",
    "REINSTATE",
    "DISQUALIFY",
    "SET_REGISTRATION_WINDOW",
    "CLOSE_REGISTRATIONS",
    "GENERATE_BRACKET",
    "TATAMI_ASSIGNED",
    "TATAMI_REASSIGNED",
    "TATAMI_UNASSIGNED",
]
# Estado de identidad que el llamador puede declarar en una inscripcion **sin**
# competidor. ``VERIFIED`` no se acepta desde fuera: se deriva de ``competitor_id``.
type RegistrationUnknownIdentityStatus = Literal["UNVERIFIED", "AMBIGUOUS"]

# Estado **derivado** de la ventana de inscripcion del torneo (S3.4c): no hay columna que lo
# guarde, se calcula a partir del plazo y del cierre manual. ``DEADLINE_PASSED`` significa que
# el plazo ya vencio; ``CLOSED``, que hay un cierre manual activo (manda sobre el plazo).
type RegistrationWindowState = Literal["OPEN", "CLOSED", "DEADLINE_PASSED"]


class DomainWriteScopeError(RuntimeError):
    """Una escritura de dominio intento salir del tenant de su entidad (S3.3a).

    La garantia **no** es la clave ajena a ``clubs(id)``: una referencia valida no demuestra
    coherencia. La aplican las sentencias de ``bracket/sql/domain_writes.py``, que comprueban
    dentro de la misma transaccion que la entidad pertenece al tenant que se le atribuye. Si
    la comprobacion no encuentra la entidad en ese tenant no se escribe nada y se levanta este
    error, de modo que la operacion y su evento de auditoria revierten juntos.
    """


class ActorContext(BaseModelORM):
    """Contexto autorizado de quien escribe: tenant y actor **explicitos** (S2).

    Lo construye el llamador. Hoy lo fabrica el servicio interno que invoca la
    operacion; cuando exista la capa HTTP tendra que derivarse de la sesion
    autenticada, nunca del payload (ver la limitacion documentada en
    ``bracket/logic/competitors.py``).
    """

    tenant_club_id: ClubId
    actor_user_id: UserId
    actor_label: str | None = None


class CompetitorBasicDataUpdate(BaseModelORM):
    """Datos basicos editables de un competidor.

    ``managed_by_club_id`` (tenant) **no** es editable: el tenant sale del
    :class:`ActorContext`. El estado se cambia con la baja logica dedicada, no aqui.
    """

    display_name: str


class SportsClub(BaseModelORM):
    """Academia (``sports_clubs``). ``tenant_club_id`` NULL = academia de plataforma."""

    id: SportsClubId
    name: str
    active: bool
    tenant_club_id: ClubId | None = None
    created: datetime_utc
    updated_at: datetime_utc | None = None


class SportsClubNameHistory(BaseModelORM):
    id: int
    sports_club_id: SportsClubId
    name: str
    valid_from: datetime_utc
    valid_to: datetime_utc | None = None


class Belt(BaseModelORM):
    """Cinturon del catalogo cerrado (``belts``).

    ``code`` es la clave logica estable (``BLUE``); ``label`` es la etiqueta canonica de respaldo
    y la traduccion vive en el frontend. ``rank`` ordena **dentro** de ``scope``: el mismo color
    existe en la escala juvenil y en la adulta, asi que un rango solo se compara con otro de su
    misma escala.
    """

    id: BeltId
    code: str
    label: str
    rank: int
    scope: BeltScope
    active: bool
    created: datetime_utc


class Competitor(BaseModelORM):
    """Identidad de competidor administrada por un tenant (``managed_by_club_id``).

    Los datos de clasificacion (``date_of_birth``, ``sex``, ``belt_id``) son nuables y no
    sustituyen a la afiliacion: el club que representa el competidor es una relacion temporal
    (``competitors_x_sports_clubs``), no una columna de esta tabla.
    """

    id: CompetitorId
    display_name: str
    active: bool
    managed_by_club_id: ClubId | None = None
    created: datetime_utc
    updated_at: datetime_utc | None = None
    date_of_birth: date | None = None
    sex: CompetitorSex | None = None
    belt_id: BeltId | None = None


class CompetitorNameHistory(BaseModelORM):
    id: int
    competitor_id: CompetitorId
    display_name: str
    valid_from: datetime_utc
    valid_to: datetime_utc | None = None


class CompetitorXSportsClub(BaseModelORM):
    """Afiliacion (vigente o historica) de un competidor con una academia."""

    id: CompetitorXSportsClubId
    competitor_id: CompetitorId
    sports_club_id: SportsClubId
    valid_from: datetime_utc
    valid_to: datetime_utc | None = None
    is_primary: bool
    created: datetime_utc


class TournamentRegistration(BaseModelORM):
    """Inscripcion de un competidor en un torneo, con su revision y snapshots.

    ``competitor_id`` NULL es un estado valido y permanente (identidad desconocida);
    ``competitor_name_snapshot`` es el nombre tal como constaba al inscribirse.
    """

    id: TournamentRegistrationId
    tournament_id: TournamentId
    competitor_id: CompetitorId | None = None
    identity_status: RegistrationIdentityStatus
    representation: RegistrationRepresentation
    sports_club_id: SportsClubId | None = None
    affiliation_id: CompetitorXSportsClubId | None = None
    category_key: str | None = None
    category_label: str | None = None
    competitor_name_snapshot: str
    sports_club_name_snapshot: str | None = None
    status: RegistrationStatus
    revision: int
    corrects_registration_id: TournamentRegistrationId | None = None
    superseded_by_registration_id: TournamentRegistrationId | None = None
    created: datetime_utc
    updated_at: datetime_utc | None = None
    declared_weight_kg: Decimal | None = None
    modality: Modality | None = None


class RegistrationDraftData(BaseModelORM):
    """Estado deseado de una inscripcion en borrador (alta y edicion).

    Modelo de **reemplazo explicito**: los campos son obligatorios (salvo los
    opcionales declarados ``| None``) y describen el estado completo, de modo que una
    edicion no puede dejar campos a medias ni sobrescribir sin querer lo que no
    menciona.

    Lo que **no** esta aqui, a proposito:

    * ``tournament_id``: se pasa como parametro explicito y se valida contra el
      tenant del contexto;
    * ``status``, ``revision``, ``corrects_registration_id``,
      ``superseded_by_registration_id``: los cambian las operaciones de ciclo de vida,
      nunca el payload;
    * ``sports_club_name_snapshot``: se deriva de la academia elegida.

    ``competitor_name_snapshot`` solo aporta valor cuando no hay identidad verificada
    (``competitor_id`` NULL): el nombre de un competidor verificado se deriva de su
    identidad y no se acepta desde el payload.
    """

    competitor_id: CompetitorId | None
    identity_status: RegistrationUnknownIdentityStatus | None
    representation: RegistrationRepresentation
    sports_club_id: SportsClubId | None
    affiliation_id: CompetitorXSportsClubId | None
    category_key: str | None
    category_label: str | None
    competitor_name_snapshot: str | None
