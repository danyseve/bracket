"""S3.3c-3 — claves de idempotencia: reglas puras del almacen (sin base de datos).

Este modulo define **que** es una reserva valida y **como se clasifica** una reserva que ya existe.
No escribe: las sentencias viven en ``bracket/sql/idempotency.py`` y el esquema en
``bracket/schema.py``. La capa HTTP (cabecera ``Idempotency-Key``, codigos de respuesta) es
S3.3c-4 y **no** esta aqui.

Decisiones de esta fase:

* la clave la compone el cliente y el servidor solo la acota (forma y longitud): no se confia en
  ella como identificador de recurso;
* la comparacion de peticiones se hace con una **huella HMAC-SHA256** con una clave del servidor,
  nunca guardando el cuerpo: el cuerpo no se persiste en ningun caso;
* la huella lleva **version de clave**: rotar la clave cambia la huella y una fila antigua deja de
  ser comparable (se trata como conflicto, nunca como replay);
* sin secreto configurado, calcular una huella **falla cerrado** (no hay valor por defecto);
* los metadatos de respuesta son una **lista corta y cerrada**: no entra JSON arbitrario aunque
  quepa en el limite de tamano;
* una reserva **caducada no se reutiliza en silencio** ni se borra (la purga es S3.3c-4).

La retencion (``IDEMPOTENCY_RETENTION_HOURS``) es **provisional**: es el soporte de ``expires_at``,
no una garantia de proteccion frente a reejecucion. Se documenta como decision pendiente.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from enum import auto
from typing import Any, Protocol

from heliclockter import datetime_utc

from bracket.config import config
from bracket.utils.types import EnumAutoStr

# Estados: cerrados. "COMMITTED" no existe como estado propio porque una operacion confirmada **es**
# una reserva finalizada con su respuesta; admitir un tercer estado dejaria una operacion
# confirmada sin registro de idempotencia.
IDEMPOTENCY_STATE_IN_PROGRESS = "IN_PROGRESS"
IDEMPOTENCY_STATE_COMPLETED = "COMPLETED"
IDEMPOTENCY_STATES = (IDEMPOTENCY_STATE_IN_PROGRESS, IDEMPOTENCY_STATE_COMPLETED)

IDEMPOTENCY_METHODS = ("POST", "PUT", "PATCH", "DELETE")

IDEMPOTENCY_KEY_MIN_LENGTH = 8
IDEMPOTENCY_KEY_MAX_LENGTH = 255
IDEMPOTENCY_KEY_PATTERN = re.compile(r"\A[A-Za-z0-9._:-]+\Z")

IDEMPOTENCY_REQUEST_PATH_MAX_LENGTH = 255
IDEMPOTENCY_RESOURCE_TYPE_MAX_LENGTH = 32
IDEMPOTENCY_STATUS_VALUE_MAX_LENGTH = 32
IDEMPOTENCY_RESPONSE_BODY_MAX_BYTES = 2048
IDEMPOTENCY_FINGERPRINT_LENGTH = 64
IDEMPOTENCY_KEY_VERSION_MAX_LENGTH = 16
IDEMPOTENCY_KEY_VERSION_PATTERN = re.compile(r"\Av[0-9]{1,3}\Z")

# Lista permitida de metadatos de respuesta: identificadores tecnicos y estados. Nada mas. Si una
# operacion necesita devolver datos al cliente, se reconstruyen a partir del recurso y se vuelven a
# autorizar; lo guardado aqui **no** es una respuesta lista para reenviar.
IDEMPOTENCY_ALLOWED_RESPONSE_KEYS: tuple[str, ...] = (
    "audit_event_id",
    "competitor_id",
    "registration_status",
    "resource_id",
    "resource_type",
)
IDEMPOTENCY_INTEGER_RESPONSE_KEYS = ("audit_event_id", "competitor_id", "resource_id")
IDEMPOTENCY_TEXT_RESPONSE_KEYS = ("registration_status", "resource_type")

# Provisional y revisable: 24 h es retencion de almacenamiento, no una promesa de que no haya
# reejecucion despues (S3.3c-4 decide purga, rotacion y ventana real).
IDEMPOTENCY_RETENTION_HOURS = 24

IDEMPOTENCY_HMAC_KEY_ENV_VAR = "IDEMPOTENCY_HMAC_KEY"


class IdempotencyError(ValueError):
    """Dato de idempotencia invalido (clave, metodo, ruta, huella o metadatos)."""


class IdempotencyConfigurationError(RuntimeError):
    """Falta la clave HMAC del servidor: sin ella no se calcula ninguna huella (fallo cerrado)."""


class IdempotencyDecision(EnumAutoStr):
    """Que hacer con una clave: reservarla o resolver contra la reserva que ya existe."""

    RESERVED = auto()
    IN_PROGRESS = auto()
    REPLAY = auto()
    FINGERPRINT_CONFLICT = auto()
    EXPIRED = auto()


@dataclass(frozen=True)
class RequestFingerprint:
    """Huella de una peticion y la version de clave con la que se calculo."""

    fingerprint: str
    key_version: str


class ReservationView(Protocol):
    """Lo minimo que mira el clasificador de una reserva leida de la base."""

    state: str
    request_fingerprint: str
    fingerprint_key_version: str
    expires_at: datetime_utc


def validate_idempotency_key(key: Any) -> str:
    """Clave del cliente: forma y longitud. El valor nunca aparece en los mensajes de error."""
    if not isinstance(key, str) or not key:
        raise IdempotencyError("la clave de idempotencia es obligatoria y no puede estar vacia")
    if len(key) < IDEMPOTENCY_KEY_MIN_LENGTH:
        raise IdempotencyError(
            f"la tamano de la clave de idempotencia debe tener un minimo de "
            f"{IDEMPOTENCY_KEY_MIN_LENGTH} caracteres"
        )
    if len(key) > IDEMPOTENCY_KEY_MAX_LENGTH:
        raise IdempotencyError(
            f"la clave de idempotencia supera el maximo de {IDEMPOTENCY_KEY_MAX_LENGTH} caracteres"
        )
    if not IDEMPOTENCY_KEY_PATTERN.match(key):
        raise IdempotencyError(
            "la clave de idempotencia solo admite estos caracteres: letras ASCII, digitos y . _ : -"
        )
    return key


def validate_request_method(method: Any) -> str:
    """Solo metodos de escritura: una clave de idempotencia no tiene sentido en una lectura."""
    if not isinstance(method, str) or method not in IDEMPOTENCY_METHODS:
        raise IdempotencyError(
            f"el metodo de la peticion debe ser uno de {', '.join(IDEMPOTENCY_METHODS)}"
        )
    return method


def validate_request_path(path: Any) -> str:
    """Ruta absoluta del propio servicio: sin cadena de consulta, fragmento ni URL absoluta."""
    if not isinstance(path, str) or not path:
        raise IdempotencyError("la ruta de la peticion es obligatoria")
    if len(path) > IDEMPOTENCY_REQUEST_PATH_MAX_LENGTH:
        raise IdempotencyError(
            f"la ruta de la peticion supera el maximo de {IDEMPOTENCY_REQUEST_PATH_MAX_LENGTH} "
            "caracteres"
        )
    if not path.startswith("/"):
        raise IdempotencyError("la ruta de la peticion debe ser absoluta (empezar por /)")
    if "?" in path or "#" in path:
        raise IdempotencyError("la ruta de la peticion no admite cadena de consulta ni fragmento")
    if any(character in path for character in "\r\n\t") or any(
        ord(character) < 32 or ord(character) == 127 for character in path
    ):
        raise IdempotencyError("la ruta de la peticion no admite caracteres de control")
    return path


def validate_key_version(version: Any) -> str:
    """Version de la clave HMAC: se guarda junto a la huella para poder rotar sin reescribir."""
    if not isinstance(version, str) or not IDEMPOTENCY_KEY_VERSION_PATTERN.match(version):
        raise IdempotencyError("la version de la clave HMAC debe tener la forma v1, v2, ...")
    return version


def validate_idempotency_state(state: Any) -> str:
    """Estado persistido: cerrado. Cualquier otro valor es un dato corrupto, no un caso nuevo."""
    if not isinstance(state, str) or state not in IDEMPOTENCY_STATES:
        raise IdempotencyError(
            "estado de idempotencia desconocido: se esperaba uno de "
            f"{', '.join(IDEMPOTENCY_STATES)}"
        )
    return state


def _validated_metadata_value(key: str, value: Any) -> Any:
    if key in IDEMPOTENCY_INTEGER_RESPONSE_KEYS:
        # ``bool`` es subclase de ``int``: se descarta a proposito.
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise IdempotencyError(f"el metadato {key!r} debe ser un identificador entero positivo")
        return value

    limit = (
        IDEMPOTENCY_RESOURCE_TYPE_MAX_LENGTH
        if key == "resource_type"
        else IDEMPOTENCY_STATUS_VALUE_MAX_LENGTH
    )
    if not isinstance(value, str) or not value or len(value) > limit:
        raise IdempotencyError(f"el metadato {key!r} debe ser texto de 1 a {limit} caracteres")
    return value


def validate_response_metadata(metadata: Any) -> dict[str, Any]:
    """Metadatos de respuesta permitidos: lista cerrada, valores escalares y tamano acotado.

    No se acepta JSON arbitrario: un objeto con una clave de fuera se rechaza aunque quepa en el
    limite de tamano (el limite es una red de seguridad, no el criterio de admision).
    """
    if not isinstance(metadata, dict):
        raise IdempotencyError("los metadatos de respuesta deben ser un objeto JSON")

    unknown = sorted(str(key) for key in metadata if key not in IDEMPOTENCY_ALLOWED_RESPONSE_KEYS)
    if unknown:
        raise IdempotencyError(
            "hay metadatos de respuesta no permitidos: solo se admiten "
            f"{', '.join(IDEMPOTENCY_ALLOWED_RESPONSE_KEYS)}"
        )

    for key, value in metadata.items():
        if isinstance(value, (dict, list, tuple, set)):
            raise IdempotencyError(f"el metadato {key!r} no admite estructuras anidadas")
        _validated_metadata_value(str(key), value)

    if canonical_metadata_size(metadata) > IDEMPOTENCY_RESPONSE_BODY_MAX_BYTES:
        raise IdempotencyError(
            f"el tamano de los metadatos de respuesta supera el maximo de "
            f"{IDEMPOTENCY_RESPONSE_BODY_MAX_BYTES} bytes"
        )
    return dict(metadata)


def canonical_metadata_size(metadata: dict[str, Any]) -> int:
    """Bytes de la forma canonica (claves ordenadas, sin espacios) de los metadatos."""
    serialized = json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return len(serialized.encode("utf-8"))


def _hmac_secret(secret: str | None = None) -> bytes:
    """Clave HMAC del servidor. Sin ella no hay huella: se falla cerrado, no se inventa una."""
    if secret is None:
        secret = config.idempotency_hmac_key
    if not isinstance(secret, str) or not secret:
        raise IdempotencyConfigurationError(
            f"falta la clave HMAC del servidor ({IDEMPOTENCY_HMAC_KEY_ENV_VAR}): sin ella no se "
            "calcula la huella de la peticion"
        )
    return secret.encode("utf-8")


def canonical_request(*, method: str, path: str, payload: bytes, key_version: str) -> bytes:
    """Forma canonica de la peticion: campos con su longitud delante (sin ambiguedad de corte).

    El campo de version entra en el calculo: rotar la clave cambia todas las huellas nuevas y las
    filas antiguas dejan de ser comparables. El cuerpo **no** se guarda en ningun sitio.
    """
    pieces = (key_version.encode("utf-8"), method.encode("utf-8"), path.encode("utf-8"), payload)
    return b"".join(len(piece).to_bytes(8, "big") + piece for piece in pieces)


def compute_request_fingerprint(
    *,
    method: str,
    path: str,
    payload: bytes,
    key_version: str | None = None,
    secret: str | None = None,
) -> RequestFingerprint:
    """Huella HMAC-SHA256 (hex, 64 caracteres) de la peticion, con su version de clave."""
    validated_method = validate_request_method(method)
    validated_path = validate_request_path(path)
    validated_version = validate_key_version(key_version or config.idempotency_hmac_key_version)
    if not isinstance(payload, bytes):
        raise IdempotencyError("el cuerpo de la peticion debe llegar ya serializado (bytes)")

    canonical = canonical_request(
        method=validated_method,
        path=validated_path,
        payload=payload,
        key_version=validated_version,
    )
    digest = hmac.new(_hmac_secret(secret), canonical, hashlib.sha256).hexdigest()
    return RequestFingerprint(fingerprint=digest, key_version=validated_version)


def fingerprints_match(left: str, right: str) -> bool:
    """Comparacion en tiempo constante.

    Es total: longitudes distintas no lanzan excepcion, devuelven falso.
    """
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def default_expires_at(created: datetime_utc) -> datetime_utc:
    """Caducidad provisional: retencion de almacenamiento, no garantia contra reejecucion."""
    return created + timedelta(hours=IDEMPOTENCY_RETENTION_HOURS)


def classify_reservation(
    existing: ReservationView | None,
    *,
    fingerprint: str,
    fingerprint_key_version: str,
    now: datetime_utc,
) -> IdempotencyDecision:
    """Que hacer con la clave segun la reserva que ya existe (o su ausencia).

    Orden de comprobaciones (importa):

    1. sin fila, la clave se reserva;
    2. una fila **caducada** no se reutiliza aunque la huella coincida: se declara caducada y la
       decision de purgarla o rotarla es de S3.3c-4 (aqui no se borra nada);
    3. una huella calculada con **otra version de clave** no es comparable: se trata como
       conflicto, nunca como replay;
    4. con la misma huella, el estado decide: confirmada es replay, pendiente esta en curso;
    5. un estado desconocido es un dato corrupto y se levanta como error.
    """
    if existing is None:
        return IdempotencyDecision.RESERVED

    if existing.expires_at <= now:
        return IdempotencyDecision.EXPIRED

    if validate_key_version(existing.fingerprint_key_version) != validate_key_version(
        fingerprint_key_version
    ):
        return IdempotencyDecision.FINGERPRINT_CONFLICT

    state = validate_idempotency_state(existing.state)

    if not fingerprints_match(existing.request_fingerprint, fingerprint):
        return IdempotencyDecision.FINGERPRINT_CONFLICT

    return (
        IdempotencyDecision.REPLAY
        if state == IDEMPOTENCY_STATE_COMPLETED
        else IdempotencyDecision.IN_PROGRESS
    )
