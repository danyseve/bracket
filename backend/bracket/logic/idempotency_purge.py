"""S3.8c — retencion y purga fisica de ``domain_idempotency_keys`` (LAB ONLY).

Reglas y orquestacion de la purga. No publica rutas HTTP y **no** toca el protocolo de S3.3c-4: la
purga es un job operativo que se ejecuta aparte, nunca dentro de una peticion.

Que borra:

* solo filas ``COMPLETED``. Una reserva ``IN_PROGRESS`` puede pertenecer a una operacion que sigue
  viva, asi que su fila no se toca nunca: es la forma de garantizar que ninguna operacion activa se
  quede sin su fila debajo;
* solo filas cuyo ``expires_at`` quedo por detras del margen (``grace``): la ventana protegida real
  es ``expires_at + grace``, no ``expires_at``;
* por lotes de tamano acotado, cada lote en su propia transaccion: sin bloqueos largos y sin WAL
  generado de golpe.

Consecuencia de contrato, documentada y medida (docs/26 §56):

* con la fila presente, un reintento con la misma clave despues de ``expires_at`` recibe
  ``IDEMPOTENCY_RESULT_EXPIRED`` (409) y **no** se reejecuta;
* con la fila ya purgada no queda nada que verificar: esa clave se trata como **nueva** y el
  protocolo la reserva y la ejecuta.

La proteccion frente a reejecucion la da por tanto la **ventana de retencion**, no la permanencia de
la fila. Es el limite que S3.3c-4 dejo explicitamente pendiente de decidir.

La salida de esta fase **nunca** imprime claves, huellas, payloads, actores ni PII: solo agregados
(elegibles, borradas, lotes, duracion).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta

from heliclockter import datetime_utc

from bracket.database import database
from bracket.logic.idempotency import (
    IDEMPOTENCY_RETENTION_HOURS,
    IDEMPOTENCY_STATE_COMPLETED,
)
from bracket.sql.idempotency_purge import (
    sql_count_purgeable_idempotency_reservations,
    sql_purge_idempotency_reservations_batch,
)

#: Estados que la purga puede borrar. Vocabulario cerrado a proposito: ``IN_PROGRESS`` queda fuera
#: porque su fila puede estar sosteniendo una operacion todavia en curso.
PURGEABLE_STATES: tuple[str, ...] = (IDEMPOTENCY_STATE_COMPLETED,)

#: Filas por sentencia de borrado. Acota bloqueos y WAL por lote.
IDEMPOTENCY_PURGE_BATCH_SIZE_DEFAULT = 500

#: Lotes por ejecucion (tope de filas por barrido: ``batch_size * max_batches``).
IDEMPOTENCY_PURGE_MAX_BATCHES_DEFAULT = 100

#: Margen despues de ``expires_at`` antes de borrar. Por defecto igual a la retencion: la fila se
#: conserva el doble de la ventana contractual, de modo que un reintento tardio sigue encontrandola.
IDEMPOTENCY_PURGE_GRACE_HOURS_DEFAULT = IDEMPOTENCY_RETENTION_HOURS


@dataclass(frozen=True)
class PurgePolicy:
    """Criterio de elegibilidad y forma de la ejecucion, con valores explicitos."""

    batch_size: int = IDEMPOTENCY_PURGE_BATCH_SIZE_DEFAULT
    max_batches: int = IDEMPOTENCY_PURGE_MAX_BATCHES_DEFAULT
    grace_hours: int = IDEMPOTENCY_PURGE_GRACE_HOURS_DEFAULT
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise ValueError("batch_size tiene que ser al menos 1")
        if self.max_batches < 1:
            raise ValueError("max_batches tiene que ser al menos 1")
        if self.grace_hours < 0:
            raise ValueError("grace_hours no puede ser negativo")

    def grace(self) -> timedelta:
        return timedelta(hours=self.grace_hours)

    def cutoff(self, now: datetime_utc) -> datetime_utc:
        """Solo es elegible lo que caduco antes de este instante."""
        return now - self.grace()

    def is_purgeable(self, *, state: str, expires_at: datetime_utc, now: datetime_utc) -> bool:
        """Espejo en Python del ``WHERE`` del lote: el mismo criterio, sin tocar la base."""
        if state not in PURGEABLE_STATES:
            return False
        return expires_at < self.cutoff(now)

    def row_ceiling(self) -> int:
        """Tope de filas que un barrido puede borrar."""
        return self.batch_size * self.max_batches


@dataclass(frozen=True)
class PurgeReport:
    """Resultado agregado de un barrido. No lleva claves ni huellas: solo numeros."""

    dry_run: bool
    #: Elegibles medidos por el dry-run. En una ejecucion real es igual a ``deleted`` (lo que era
    #: elegible dentro del tope de lotes): no se hace un conteo extra que alargue el barrido.
    candidates: int
    deleted: int
    batches: int
    duration_seconds: float
    stopped_at_batch_limit: bool

    def render_lines(self) -> list[str]:
        mode = "dry-run" if self.dry_run else "real"
        lines = [
            f"idempotency-purge modo={mode}",
            f"filas elegibles={self.candidates}",
            f"filas borradas={self.deleted}",
            f"lotes={self.batches}",
            f"duracion_s={self.duration_seconds:.3f}",
        ]
        if self.stopped_at_batch_limit:
            lines.append("quedan filas elegibles (limite de lotes alcanzado): repetir la ejecucion")
        return lines


async def purge_expired_idempotency_reservations(
    policy: PurgePolicy, *, now: datetime_utc | None = None
) -> PurgeReport:
    """Barre reservas caducadas por lotes y devuelve el agregado.

    ``dry_run`` usa **el mismo** criterio que el borrado (mismo ``cutoff``, mismo estado) y no
    escribe nada: el conteo es una foto del instante de la consulta.
    """
    moment = now if now is not None else datetime_utc.now()
    cutoff = policy.cutoff(moment)
    started = time.monotonic()

    if policy.dry_run:
        candidates = await sql_count_purgeable_idempotency_reservations(cutoff=cutoff)
        return PurgeReport(
            dry_run=True,
            candidates=candidates,
            deleted=0,
            batches=0,
            duration_seconds=time.monotonic() - started,
            stopped_at_batch_limit=False,
        )

    deleted = 0
    batches = 0
    last_batch_size = 0
    while batches < policy.max_batches:
        async with database.transaction():
            last_batch_size = await sql_purge_idempotency_reservations_batch(
                cutoff=cutoff, batch_size=policy.batch_size
            )
        batches += 1
        deleted += last_batch_size
        if last_batch_size < policy.batch_size:
            break

    return PurgeReport(
        dry_run=False,
        candidates=deleted,
        deleted=deleted,
        batches=batches,
        duration_seconds=time.monotonic() - started,
        stopped_at_batch_limit=last_batch_size == policy.batch_size
        and batches == policy.max_batches,
    )
