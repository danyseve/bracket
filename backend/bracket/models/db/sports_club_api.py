"""Contrato HTTP de salida de la academia deportiva (S4.8a-3 §4, LAB ONLY).

Proyeccion **explicita**: no se reutiliza el modelo interno como respuesta. Publica ``logo_path``
(la ruta relativa del logo ya persistido), que es justo lo que el cliente necesita para pintarlo;
nada mas.
"""

from __future__ import annotations

from pydantic import BaseModel

from bracket.models.db.domain import SportsClub
from bracket.utils.id_types import SportsClubId


class SportsClubResponse(BaseModel):
    """Proyeccion publica de una academia: identidad minima y logo opcional."""

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
