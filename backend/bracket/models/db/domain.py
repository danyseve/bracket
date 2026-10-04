"""Modelos del dominio Competitor/SportsClub/Registration.

S1 (``bracket/sql/domain_reads.py``): proyeccion **solo lectura**, minima y sin PII.

S2 (``bracket/sql/domain_writes.py`` + ``bracket/logic/competitors.py``) anade el
contexto de escritura (:class:`ActorContext`) y el contrato de datos basicos
editables (:class:`CompetitorBasicDataUpdate`). Sigue sin haber variantes
``*Insertable``: las escrituras usan parametros explicitos y ``managed_by_club_id``
no es un dato de entrada, sale siempre del contexto autorizado.

Acceso minimo a PII: se excluyen a proposito los campos que no hacen falta para
consultar el dominio:

* ``tournament_registrations.verified_by_user_id`` / ``verified_at``;
* ``*_name_history.changed_by_user_id``;
* ``competitors_x_sports_clubs.note`` (texto libre).

Si una UI futura necesita alguno de ellos, se anadira como campo explicito y
revisado, nunca como ``SELECT *``.
"""

from __future__ import annotations

from typing import Literal

from heliclockter import datetime_utc

from bracket.models.db.shared import BaseModelORM
from bracket.utils.id_types import (
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
type DomainChangeLogAction = Literal["CREATE", "UPDATE", "DEACTIVATE"]


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


class Competitor(BaseModelORM):
    """Identidad de competidor administrada por un tenant (``managed_by_club_id``)."""

    id: CompetitorId
    display_name: str
    active: bool
    managed_by_club_id: ClubId | None = None
    created: datetime_utc
    updated_at: datetime_utc | None = None


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
