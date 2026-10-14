"""Contratos HTTP de entrada/salida de las rutas F3 de competidores (S3.7a, LAB ONLY).

Modelos **explicitos** de la capa HTTP. No se reutiliza ningun modelo interno como
cuerpo ni como respuesta: el contrato externo no depende de la forma de la fila, de
modo que una columna nueva en la tabla no aparece sola en la respuesta.

Lo que **no** esta aqui, a proposito:

* identidad y tenant (``actor_user_id``, ``actor_label``, ``tenant_club_id``, rol): los
  resuelve el servidor desde el JWT y el club de la ruta. No son datos de entrada y un
  cuerpo que los traiga se **rechaza** (``extra="forbid"`` -> 422), no se ignora;
* el estado (``active``) y el historial de nombres: los cambian las operaciones de ciclo
  de vida (baja y reactivacion), nunca el payload;
* ``managed_by_club_id``: el tenant sale del contexto autorizado. La respuesta tampoco lo
  publica: el ambito ya esta en la propia ruta y repetirlo no aporta nada al cliente. S4.8a-3
  pedia extender la lectura con ese campo: se mantiene fuera a proposito (ajuste declarado) y
  lo que se publica es el perfil deportivo.
* el **perfil deportivo** (``date_of_birth``, ``sex``, ``belt_id``) si viaja en la respuesta: lo
  necesita quien clasifica y no revela ambito ninguno.

El motivo de auditoria entra solo por el vocabulario cerrado ``reason_code``/
``reason_note`` (catalogo de S3.3c-2, alcance ``competitor``): no hay campo de texto libre.
"""

from __future__ import annotations

from datetime import date

from heliclockter import datetime_utc
from pydantic import BaseModel, ConfigDict

from bracket.models.db.domain import (
    Competitor,
    CompetitorSex,
    CompetitorXSportsClub,
    OpenPrimaryAffiliation,
    RegistrationRepresentation,
)
from bracket.utils.id_types import BeltId, CompetitorId, SportsClubId


class CompetitorRequestModel(BaseModel):
    """Base de los cuerpos F3 de competidores.

    ``extra="forbid"``: un intento de aportar identidad o tenant (``actor_user_id``,
    ``actor_label``, ``tenant_club_id``, rol) es un dato invalido, no un campo a ignorar.
    Asi el cliente no puede creer que su valor se ha tenido en cuenta.
    """

    model_config = ConfigDict(extra="forbid")


class CompetitorCreateBody(CompetitorRequestModel):
    """Alta de identidad: nombre, perfil deportivo opcional y motivo del alta.

    No hay campo de academia: en el dominio certificado (S2) un competidor puede existir
    sin academia y la afiliacion no es un dato de entrada del alta.

    El perfil deportivo (S4.8a-3 §2) es **opcional**: un cliente anterior a S4.8a sigue dando de
    alta identidades sin enviarlo y los tres campos quedan en ``NULL``. No hay valor por defecto
    deportivo: no se inventa cinturon, sexo ni fecha.
    """

    display_name: str
    date_of_birth: date | None = None
    sex: CompetitorSex | None = None
    belt_id: BeltId | None = None
    reason_code: str | None = None
    reason_note: str | None = None


class CompetitorProfileBody(CompetitorRequestModel):
    """Perfil deportivo de un competidor: reemplazo explicito de los tres campos.

    Los tres viajan **siempre**, aunque sean ``NULL``: es la misma politica que la inscripcion en
    borrador, donde una edicion describe el estado completo y no puede dejar campos a medias.
    ``NULL`` es un valor legitimo (dato no declarado) y no un campo sin mencionar.
    """

    date_of_birth: date | None
    sex: CompetitorSex | None
    belt_id: BeltId | None
    reason_code: str | None = None
    reason_note: str | None = None


class CompetitorRenameBody(CompetitorRequestModel):
    """Renombrado controlado: el nombre nuevo y el motivo del cambio de datos basicos."""

    display_name: str
    reason_code: str | None = None
    reason_note: str | None = None


class CompetitorLifecycleBody(CompetitorRequestModel):
    """Motivo de una transicion de estado (baja logica o reactivacion).

    Cual de las dos formas es obligatoria lo decide el catalogo de motivos dentro de la
    operacion de dominio, no este modelo: aqui solo se declara el vocabulario cerrado.
    """

    reason_code: str | None = None
    reason_note: str | None = None


class CompetitorResponse(BaseModel):
    """Proyeccion publica y estable de un competidor (lista blanca explicita).

    Se excluye a proposito el tenant (``managed_by_club_id``): el ambito ya esta en la ruta
    y el cliente no necesita repetirlo. Tampoco viaja ningun dato del actor ni de la
    auditoria, ni el historial de nombres (que es la traza interna del renombrado).
    """

    id: CompetitorId
    display_name: str
    active: bool
    created: datetime_utc
    updated_at: datetime_utc | None
    date_of_birth: date | None
    sex: CompetitorSex | None
    belt_id: BeltId | None

    @classmethod
    def from_competitor(cls, competitor: Competitor) -> CompetitorResponse:
        """Proyecta la fila del dominio a la respuesta publica."""
        return cls(
            id=competitor.id,
            display_name=competitor.display_name,
            active=competitor.active,
            created=competitor.created,
            updated_at=competitor.updated_at,
            date_of_birth=competitor.date_of_birth,
            sex=competitor.sex,
            belt_id=competitor.belt_id,
        )


class CompetitorListItem(CompetitorResponse):
    """Competidor de la lista **mas su afiliacion actual** (S4.8d §3, solo lectura).

    Es la unica forma de que el formulario de inscripcion pueda mostrar "academia / independiente"
    sin una consulta por competidor. Se publica dentro de la **misma** respuesta del listado, con el
    mismo orden y la misma paginacion: no hay endpoint nuevo ni N+1.

    ``affiliation_mode`` es el **estado actual** del competidor (su afiliacion primaria abierta)
    para que el formulario pueda proponerlo. No es una decision de la inscripcion: en el dominio
    certificado la academia representada se **elige** en el alta, y esto solo evita que el usuario
    tenga que recordarla. ``INDEPENDENT`` con los dos campos en ``NULL`` significa "sin afiliacion
    abierta": no se deduce ni se inventa ninguna academia.
    """

    affiliation_mode: RegistrationRepresentation
    sports_club_id: SportsClubId | None = None
    sports_club_name: str | None = None

    @classmethod
    def from_competitor(
        cls,
        competitor: Competitor,
        affiliation: OpenPrimaryAffiliation | None = None,
    ) -> CompetitorListItem:
        """Proyecta la fila y su afiliacion abierta (si la tiene) a la respuesta del listado."""
        base = CompetitorResponse.from_competitor(competitor)
        if affiliation is None:
            return cls(**base.model_dump(), affiliation_mode="INDEPENDENT")
        return cls(
            **base.model_dump(),
            affiliation_mode="CLUB",
            sports_club_id=affiliation.sports_club_id,
            sports_club_name=affiliation.sports_club_name,
        )


class CompetitorListResponse(BaseModel):
    """Pagina de competidores con el total del **filtro**, no el de la pagina."""

    count: int
    competitors: list[CompetitorListItem]


class CompetitorAffiliationBody(CompetitorRequestModel):
    """Cuerpo de la asignacion de afiliacion: **solo** la academia destino.

    No hay campos de estado: si nace primaria lo decide el servidor (es la primera afiliacion
    vigente del competidor) y cambiar la primaria es una ruta de accion propia.
    """

    sports_club_id: SportsClubId


class CompetitorAffiliationResponse(BaseModel):
    """Proyeccion publica y estable de una afiliacion del competidor (lista blanca explicita)."""

    id: int
    competitor_id: CompetitorId
    sports_club_id: SportsClubId
    is_primary: bool
    valid_from: datetime_utc
    valid_to: datetime_utc | None

    @classmethod
    def from_affiliation(cls, affiliation: CompetitorXSportsClub) -> CompetitorAffiliationResponse:
        """Proyecta la fila del dominio a la respuesta publica."""
        return cls(
            id=affiliation.id,
            competitor_id=affiliation.competitor_id,
            sports_club_id=affiliation.sports_club_id,
            is_primary=affiliation.is_primary,
            valid_from=affiliation.valid_from,
            valid_to=affiliation.valid_to,
        )


class CompetitorAffiliationsResponse(BaseModel):
    """Historial de afiliaciones de un competidor tras pasar a independiente."""

    competitor_id: CompetitorId
    affiliations: list[CompetitorAffiliationResponse]
