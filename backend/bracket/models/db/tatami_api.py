"""Modelos HTTP de la asignacion explicita de tatami (S4.2, F3C, LAB ONLY).

El contrato es **cerrado**: entrar entra solo el tatami destino (o ``null``), y sale solo lo que el
tablero necesita para pintarse. Ningun interno del modelo cruza la frontera: ni SQL, ni
restricciones, ni el actor que hizo el cambio (eso vive en la auditoria).
"""

from __future__ import annotations

from bracket.models.db.shared import BaseModelORM
from bracket.utils.id_types import CourtId, StageItemId


class StageItemCourtBody(BaseModelORM):
    """Cuerpo de la operacion: el tatami destino, o ``null`` para desasignar.

    ``court_id`` es obligatorio **como campo**: ``null`` significa "quitar el tatami", y un cuerpo
    sin el campo no se interpreta como una desasignacion implicita (seria adivinar la intencion).
    El torneo y el cuadro viajan en la ruta, no en el cuerpo: el cuerpo no puede contradecir a la
    ruta.
    """

    court_id: CourtId | None


class StageItemCourtResponse(BaseModelORM):
    """Tatami vigente del cuadro tras la operacion, con el efecto exacto sobre sus combates.

    ``affected_match_count`` es el efecto de **esta** operacion: cuantos combates ha movido;
    repetir la misma operacion con la misma clave no la re-ejecuta, asi que vale 0 y el resto de la
    respuesta describe el estado vigente. ``court_name`` viaja junto al identificador para que el
    cliente no tenga que cruzar el listado de tatamis por su cuenta.
    """

    stage_item_id: StageItemId
    court_id: CourtId | None
    court_name: str | None
    affected_match_count: int
