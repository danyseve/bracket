"""Clasificacion **previa** al alta de una inscripcion (S4.8d §2, solo lectura).

Un ``POST`` que no escribe: el verbo es de transporte (el peso declarado y la modalidad viajan en
el cuerpo y no son identificadores de recurso) y el efecto es el de una lectura. No crea la
inscripcion, no escribe auditoria, no reserva ni consume ``Idempotency-Key`` y **no** pasa por la
puerta ``require_f3_write_enabled``, asi que no comparte el protocolo de las escrituras F3 y vive
aparte de ``domain_registrations`` (que ademas esta en el limite de tamano del proyecto).

El contrato es el de la clasificacion del alta: mismo motor puro, mismo cargador SQL y misma
proyeccion de respuesta (``_classification_response``, importada para que las dos no puedan
divergir), salvo el veredicto de la **categoria elegida**: la previa no lo publica porque todavia no
hay ninguna elegida y su ausencia no es un defecto de los datos declarados (S4.8d §2). El alcance es
el del router de inscripciones: el torneo lo impone la dependencia de
contexto (tenant/RBAC) y un competidor de otro tenant responde el mismo 404 que un recurso
inexistente. Un peso o una modalidad presentes pero invalidos responden el mismo 400 que un alta,
porque la validacion es la del dominio de inscripciones, no una copia.

Las reglas de clasificacion **no** se reimplementan aqui ni en el cliente: la respuesta es
literalmente la del motor ya validado.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from starlette import status

from bracket.logic.classification.engine import classify_input
from bracket.logic.registration_core import RegistrationDomainError
from bracket.logic.registrations import resolve_declared_modality, resolve_declared_weight
from bracket.models.db.domain import ActorContext
from bracket.models.db.registration_api import (
    RegistrationClassificationPreviewBody,
    RegistrationClassificationResponse,
)
from bracket.routes.domain_auth import actor_context_for_tournament
from bracket.routes.domain_registrations import (
    _NOT_FOUND_DETAIL,
    _classification_response,
    _http_exception,
)
from bracket.sql.classification import get_preview_classification_input
from bracket.utils.id_types import TournamentId

router = APIRouter()


@router.post(
    "/tournaments/{tournament_id}/registrations/classification-preview",
    response_model=RegistrationClassificationResponse,
)
async def preview_registration_classification_endpoint(
    tournament_id: TournamentId,
    body: RegistrationClassificationPreviewBody,
    context: ActorContext = Depends(actor_context_for_tournament),
) -> RegistrationClassificationResponse:
    """Clasificacion previa al alta: que categorias encajan con lo que se va a declarar (§2).

    No escribe nada: sin inscripcion, sin auditoria y sin idempotencia. Mismo 404 de recurso
    inexistente y mismo 400 de peso o modalidad invalidos que el alta.

    La proyeccion es la compartida con el alta, sin el veredicto de la categoria elegida: la previa
    corre **antes** de elegir categoria (§2), asi que un ``CATEGORY_NOT_RESOLVED`` por no haberla
    elegido todavia no es un defecto de los datos declarados. Los bloqueos y avisos que si salen de
    los hechos declarados y del motor se publican integros.
    """
    try:
        declared_weight_kg = resolve_declared_weight(body.declared_weight_kg)
        modality = resolve_declared_modality(body.modality)
    except RegistrationDomainError as error:
        # Mismo catalogo cerrado de errores del alta: el 400 es el contractual, no una copia.
        raise _http_exception(error) from error

    data = await get_preview_classification_input(
        tournament_id,
        context.tenant_club_id,
        competitor_id=body.competitor_id,
        declared_weight_kg=declared_weight_kg,
        modality=modality,
    )
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
    return _classification_response(classify_input(data), with_chosen_category=False)
