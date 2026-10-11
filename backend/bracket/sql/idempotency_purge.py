"""S3.8c — sentencias de purga de ``domain_idempotency_keys`` (LAB ONLY).

Vive aparte del almacen (`bracket/sql/idempotency.py`) a proposito. El almacen no borra sus propias
filas y esa garantia la fija un test de S3.3c-4
(`test_the_store_neither_deletes_nor_purges_its_own_rows_in_this_phase`), que sigue intacto: la
purga es un job operativo, con su propio modulo, su propio criterio y su propia sentencia.

Reutiliza de `bracket.sql.idempotency` solo el literal del estado
(`COMPLETED_STATE_SQL`), fuente unica del valor, y no importa nada mas del almacen.
"""

from heliclockter import datetime_utc

from bracket.database import database
from bracket.sql.idempotency import COMPLETED_STATE_SQL

#: Estados que la purga puede borrar. La decision de politica vive en
#: `bracket/logic/idempotency_purge.py`; aqui solo el literal que va a la sentencia.
PURGEABLE_STATE_SQL = COMPLETED_STATE_SQL

#: Un lote = una sentencia. La CTE elige por `expires_at` (usa
#: `ix_domain_idempotency_keys_expires_at`) y `SKIP LOCKED` evita esperar a una fila tomada por otra
#: transaccion: ni la purga bloquea trafico ni el trafico bloquea la purga. Nunca hay un `DELETE`
#: sin `LIMIT`.
PURGE_BATCH = f"""
    WITH purgeable AS (
        SELECT id
        FROM domain_idempotency_keys
        WHERE state = '{PURGEABLE_STATE_SQL}' AND expires_at < :cutoff
        ORDER BY expires_at
        LIMIT :batch_size
        FOR UPDATE SKIP LOCKED
    )
    DELETE FROM domain_idempotency_keys AS doomed
    USING purgeable
    WHERE doomed.id = purgeable.id
    RETURNING doomed.id
"""

#: Mismo criterio que el lote y sin escribir nada: el conteo seguro del dry-run.
PURGE_COUNT = f"""
    SELECT count(*) FROM domain_idempotency_keys
    WHERE state = '{PURGEABLE_STATE_SQL}' AND expires_at < :cutoff
"""


async def sql_purge_idempotency_reservations_batch(
    *,
    cutoff: datetime_utc,
    batch_size: int,
) -> int:
    """Borra un lote de reservas caducadas y devuelve cuantas filas borro.

    No abre transaccion propia (misma regla que el resto de la capa): la sentencia es atomica y el
    llamador decide si la envuelve para acotar el bloqueo.
    """
    rows = await database.fetch_all(
        query=PURGE_BATCH,
        values={"cutoff": cutoff, "batch_size": batch_size},
    )
    return len(rows)


async def sql_count_purgeable_idempotency_reservations(*, cutoff: datetime_utc) -> int:
    """Filas que cumplen exactamente el criterio de purga (dry-run). Solo devuelve el total."""
    total = await database.fetch_val(query=PURGE_COUNT, values={"cutoff": cutoff})
    return int(total)
