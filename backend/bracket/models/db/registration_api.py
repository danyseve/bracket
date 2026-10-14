"""Contratos HTTP de entrada/salida de las rutas F3 de inscripciones (S3.4a/S3.4b/S3.4c, LAB ONLY).

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

from decimal import Decimal
from typing import Literal

from heliclockter import datetime_utc
from pydantic import BaseModel, ConfigDict

from bracket.logic.bracket_readiness import BracketReadinessBlocker
from bracket.logic.classification.vocabulary import ClassificationBlocker, ClassificationWarning
from bracket.models.db.domain import (
    Modality,
    RegistrationDraftData,
    RegistrationIdentityStatus,
    RegistrationRepresentation,
    RegistrationStatus,
    RegistrationUnknownIdentityStatus,
    RegistrationWindowState,
    TournamentRegistration,
)
from bracket.utils.id_types import (
    CompetitorId,
    CompetitorXSportsClubId,
    SportsClubId,
    StageItemId,
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
    declared_weight_kg: Decimal | None = None
    modality: Modality | None = None
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
            declared_weight_kg=self.declared_weight_kg,
            modality=self.modality,
        )


class RegistrationClassificationPreviewBody(RegistrationRequestModel):
    """Lo que hace falta para clasificar **antes** de crear la inscripcion (S4.8d §2).

    Solo tres datos: el competidor del tenant y lo que se declara en el formulario. No hay
    ``representation``, ni academia, ni motivos, ni ``status``: nada de eso interviene en la
    compatibilidad deportiva (el club viaja como contexto en el motor, no como regla) y ninguno de
    los dos campos declarados es un atributo permanente del competidor: el peso es de **esta**
    inscripcion.

    ``declared_weight_kg`` y ``modality`` son opcionales a proposito: el formulario puede preguntar
    la previa mientras todavia no los ha rellenado, y el motor responde entonces ``MISSING_WEIGHT``
    o ``MISSING_MODALITY``. Eso es informacion para el UI, no un error de la peticion. Un valor
    presente, en cambio, se valida con las mismas reglas que un alta (peso ``> 0`` y ``<= 300``,
    modalidad ``GI`` o ``NO_GI``) y un valor invalido se rechaza igual que alli.
    """

    competitor_id: CompetitorId
    declared_weight_kg: Decimal | None = None
    modality: Modality | None = None


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
    declared_weight_kg: Decimal | None = None
    modality: Modality | None = None
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
            declared_weight_kg=registration.declared_weight_kg,
            modality=registration.modality,
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


class RegistrationWindowResponse(BaseModel):
    """Ventana de inscripcion del torneo (S3.4c): estado derivado, sin auditoria ni internos.

    ``state`` y ``registrations_open`` dicen lo mismo de dos formas (vocabulario y booleano)
    porque el cliente que solo necesita saber si puede inscribir no deberia tener que
    interpretar el vocabulario. ``registrations_closed_at`` es el instante del cierre manual,
    **no** quien lo hizo: el actor vive en la auditoria y no viaja al cliente.
    """

    tournament_id: TournamentId
    registration_deadline: datetime_utc | None
    registrations_closed_at: datetime_utc | None
    state: RegistrationWindowState
    registrations_open: bool


class RegistrationWindowBody(BaseModel):
    """Estado deseado del plazo de inscripcion del torneo. El cierre manual no se configura aqui.

    ``registration_deadline`` ausente o nula significa "sin plazo": el cuerpo describe el estado
    deseado, no un parche, asi que enviar dos veces el mismo instante es un no-op que no vuelve a
    auditarse. Cualquier otro campo (``registrations_closed_at``, ``actor_user_id``,
    ``tenant_club_id``, ...) se rechaza con 422, no se ignora.
    """

    model_config = ConfigDict(extra="forbid")

    registration_deadline: datetime_utc | None = None


class RegistrationWindowCloseBody(BaseModel):
    """El cierre no lleva datos: el cuerpo existe para que un campo de mas sea un 422.

    No hay campo libre de motivo: el cierre no es una transicion de una inscripcion y no entra en
    el catalogo cerrado de motivos de S3.3c-2.
    """

    model_config = ConfigDict(extra="forbid")


class GenerateBracketTournamentBody(BaseModel):
    """La generacion no lleva datos: el cuerpo existe para que un campo de mas sea un 422.

    No hay campo libre de motivo: generar el cuadro no es una transicion de una inscripcion y no
    entra en el catalogo cerrado de motivos de S3.3c-2. El cuerpo tampoco puede aportar identidad,
    tenant ni el estado del cuadro.
    """

    model_config = ConfigDict(extra="forbid")


class GenerateBracketStageItemResponse(BaseModel):
    """Reparto de un cuadro tras la generacion: los mismos campos de reparto que publica F2C.

    Ni ``changed`` ni ningun otro dato de la llamada: la respuesta describe el **estado** del
    reparto, y por eso una repeticion con la misma clave devuelve exactamente el mismo cuerpo. Que
    la operacion escribiera algo queda en su evento de auditoria, no en el contrato de lectura.
    """

    stage_item_id: StageItemId
    entrant_count: int
    bracket_size: int
    bye_count: int
    ghost_count: int


class GenerateBracketTournamentResponse(BaseModel):
    """Resultado de generar los cuadros del torneo entero por accion explicita (S3.5a).

    No publica ``stages_created``/``matches_created``: la generacion de F2C no crea ni fases ni
    eliminatorias, escribe el reparto de las ranuras de los cuadros que ya existen.

    ``generated`` es la **post-condicion** del torneo (hay al menos un cuadro con el minimo de
    participantes de F2C), no "esta llamada concreta escribio algo": asi una repeticion con la misma
    clave devuelve el mismo cuerpo. ``skipped_stage_items`` cuenta los cuadros que F2C no admite
    generar ahora mismo y que por tanto no se han tocado.
    """

    tournament_id: TournamentId
    generated: bool
    stage_items: list[GenerateBracketStageItemResponse]
    skipped_stage_items: int


class BracketReadinessIssue(BaseModel):
    """Un bloqueo concreto de la revision de readiness (S3.4d).

    ``code`` pertenece a un vocabulario **cerrado** y es el contrato: no viaja ningun mensaje libre.
    ``count`` aparece solo cuando el bloqueo cuenta algo (inscripciones pendientes, competidores,
    cuadros) y ``required_minimum`` solo cuando compara un recuento con un minimo.
    """

    code: BracketReadinessBlocker
    count: int | None = None
    category_key: str | None = None
    required_minimum: int | None = None


class BracketReadinessCategory(BaseModel):
    """Una categoria del torneo: sus participantes confirmados y lo que le falta para tener cuadro.

    ``category_key`` y ``category_label`` nulas son la categoria sin clasificar, que el modelo
    admite: son las personas inscritas que todavia no tienen categoria asignada.
    """

    category_key: str | None
    category_label: str | None
    confirmed_count: int
    required_minimum: int
    ready: bool
    blocking_issues: list[BracketReadinessIssue]


class BracketReadinessSummary(BaseModel):
    """Recuento de inscripciones **vigentes** por estado; ``total`` es la suma de los demas.

    Las corregidas y las sustituidas por una correccion no entran: contarlas seria contar dos veces
    a la misma persona.
    """

    total: int
    confirmed: int
    draft: int
    withdrawn: int
    disqualified: int


class BracketReadinessResponse(BaseModel):
    """Respuesta de la revision: si el torneo esta listo y, si no, que lo impide.

    Es una revision derivada: no hay estado ``READY`` guardado en ninguna columna, asi que no puede
    desincronizarse del torneo. No viajan modelos SQL crudos, ni el actor, ni el tenant, ni las
    filas de inscripcion: solo recuentos y bloqueos con codigo estable.
    """

    tournament_id: TournamentId
    ready: bool
    registration_window: RegistrationWindowState
    summary: BracketReadinessSummary
    categories: list[BracketReadinessCategory]
    blocking_issues: list[BracketReadinessIssue]


class ClassificationCategoryVerdict(BaseModel):
    """Una categoria del torneo juzgada para **una** inscripcion (§6).

    Es la proyeccion publica del veredicto del motor: la clave y la etiqueta que el cliente ya ve en
    la inscripcion, ``compatible`` y los motivos. Ni reglas internas ni filas de ``stage_items``.
    """

    category_key: str
    category_label: str | None
    compatible: bool
    blockers: list[ClassificationBlocker]
    warnings: list[ClassificationWarning]


class RegistrationClassificationResponse(BaseModel):
    """Clasificacion de una inscripcion contra las categorias de su torneo (§6, solo lectura).

        El contrato son cuatro campos: las categorias compatibles, la recomendada (solo si es
    **unica**),
        los bloqueos y los avisos. Ninguna categoria se elige sola con varias compatibles.
    """

    compatible_categories: list[ClassificationCategoryVerdict]
    recommended_category: ClassificationCategoryVerdict | None
    blockers: list[ClassificationBlocker]
    warnings: list[ClassificationWarning]


class RegistrationCategoryBody(RegistrationRequestModel):
    """Cuerpo de la asignacion de categoria (§7/§8).

    ``category_key`` es opcional: sin el, la operacion usa la **recomendada** (solo cuando hay
    exactamente una compatible). ``override`` es una operacion **explicita** y OWNER-only: permite
    fijar una categoria incompatible, pero entonces ``reason_code`` es obligatorio.
    """

    category_key: str | None = None
    override: bool = False
    reason_code: str | None = None
    reason_note: str | None = None
