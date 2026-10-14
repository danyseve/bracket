"""Contratos HTTP de salida de los catalogos de lectura del club (S4.8a-4a, LAB ONLY).

Proyecciones **explicitas**: no se reutiliza el modelo interno como respuesta. El club
(``SportsClubResponse``) ya vive en ``bracket/models/db/sports_club_api.py`` y se reutiliza tal
cual; el cinturon se publica aqui porque hasta ahora solo existia la fila interna (``Belt``,
``bracket/models/db/domain.py``) y el lector ``get_belt`` (``bracket/sql/domain_reads.py``).

La lista blanca de ``BeltResponse`` publica la forma de la tabla ``belts`` que el frontend necesita
para pintar el selector: ``code`` es la clave logica estable, ``label`` la etiqueta canonica de
respaldo (la traduccion vive en el frontend) y ``rank`` solo es comparable **dentro** de ``scope``.
No hay campo ``modality``: el backend no asocia modalidad al cinturon.
"""

from __future__ import annotations

from heliclockter import datetime_utc
from pydantic import BaseModel

from bracket.models.db.domain import Belt, BeltScope
from bracket.utils.id_types import BeltId


class BeltResponse(BaseModel):
    """Proyeccion publica de un cinturon del catalogo de plataforma (``belts``)."""

    id: BeltId
    code: str
    label: str
    rank: int
    scope: BeltScope
    active: bool
    created: datetime_utc

    @classmethod
    def from_belt(cls, belt: Belt) -> BeltResponse:
        """Proyecta la fila interna del catalogo a la respuesta publica."""
        return cls(
            id=belt.id,
            code=belt.code,
            label=belt.label,
            rank=belt.rank,
            scope=belt.scope,
            active=belt.active,
            created=belt.created,
        )
