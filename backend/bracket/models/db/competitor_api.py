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

from bracket.models.db.domain import Competitor, CompetitorSex
from bracket.utils.id_types import BeltId, CompetitorId


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


class CompetitorListResponse(BaseModel):
    """Pagina de competidores con el total del **filtro**, no el de la pagina."""

    count: int
    competitors: list[CompetitorResponse]
