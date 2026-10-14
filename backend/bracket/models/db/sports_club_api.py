"""Contrato HTTP del club deportiva (S4.8a-3 §4 y S4.8g-2 §14, LAB ONLY).

Proyeccion **explicita**: no se reutiliza el modelo interno como respuesta. Publica ``logo_path``
(la ruta relativa del logo ya persistido), que es justo lo que el cliente necesita para pintarlo;
nada mas.

S4.8g-2 §14 anade los cuerpos de escritura de la entidad que el producto llama **Club**: alta y
edicion (renombrado y activo). Igual que en competidores, ``extra="forbid"``: la identidad y el
tenant salen **solo** del contexto autorizado (JWT + club de la ruta), asi que un cuerpo que
intente aportarlos se rechaza con 422 en vez de ignorarse. El motivo de auditoria entra por el
vocabulario cerrado ``reason_code``/``reason_note`` (catalogo de S3.3c-2, alcance ``sports_club``).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from bracket.models.db.domain import SportsClub
from bracket.utils.id_types import SportsClubId


class SportsClubResponse(BaseModel):
    """Proyeccion publica de un club: identidad minima y logo opcional."""

    id: SportsClubId
    name: str
    active: bool
    logo_path: str | None

    @classmethod
    def from_sports_club(cls, sports_club: SportsClub) -> SportsClubResponse:
        """Proyecta la fila interna; ``logo_path`` viaja tal cual (ya es la ruta relativa)."""
        return cls(
            id=sports_club.id,
            name=sports_club.name,
            active=sports_club.active,
            logo_path=sports_club.logo_path,
        )


class SportsClubRequestModel(BaseModel):
    """Base de los cuerpos de escritura de la entidad Club.

    ``extra="forbid"``: intentar aportar identidad o tenant (``actor_user_id``, ``actor_label``,
    ``tenant_club_id``, rol) es un dato invalido, no un campo a ignorar.
    """

    model_config = ConfigDict(extra="forbid")


class SportsClubCreateBody(SportsClubRequestModel):
    """Alta de un club del tenant: nombre, estado inicial y motivo.

    ``active`` es opcional y por defecto ``True``: es el estado natural de un club recien dado de
    alta. No hay campo de logo: el logo se sube despues por su propia ruta (S4.8a-3 §4), de forma
    que el alta nunca depende de un fichero binario.
    """

    name: str
    active: bool = True
    reason_code: str | None = None
    reason_note: str | None = None


class SportsClubUpdateBody(SportsClubRequestModel):
    """Edicion de un club del tenant: renombrado y/o cambio de estado.

    Los dos campos son **opcionales** y solo se aplica lo que viaja: renombrar no obliga a reenviar
    el estado y viceversa. Un cuerpo sin ``name`` ni ``active`` no es un no-op silencioso: la capa
    de dominio lo rechaza como peticion invalida.
    """

    name: str | None = None
    active: bool | None = None
    reason_code: str | None = None
    reason_note: str | None = None
