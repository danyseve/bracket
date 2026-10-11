"""Puerta de escritura de F3 (S3.6b, F3B, LAB ONLY).

Una sola puerta para toda la superficie F3 que **escribe**: mientras ``F3_WRITE_ENABLED`` este
apagada (valor por defecto del campo), las rutas de escritura responden ``503 F3_WRITE_DISABLED``
antes de reservar la ``Idempotency-Key``, de bloquear filas, de escribir dominio y de auditar. Las
rutas de lectura no dependen de esta puerta: el estado de la ventana, el listado, el detalle y el
readiness siguen publicandose con la bandera apagada.

No es un permiso del actor y por eso **no** es 403: es el interruptor de la fase, con la misma
respuesta para cualquier llamante y sin revelar nada de la operacion (ni internals, ni el estado del
torneo, ni si la clave de idempotencia serveria).

El valor se lee en cada peticion de la configuracion viva, no se cachea al arrancar: encender la
bandera en el despliegue no exige reiniciar nada mas que el propio proceso que la declara.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from bracket.config import config

#: Codigo estable del rechazo mientras la escritura F3 esta apagada.
F3_WRITE_DISABLED = "F3_WRITE_DISABLED"


def require_f3_write_enabled() -> None:
    """Dependencia de las rutas F3 de escritura: falla cerrado si la bandera esta apagada.

    Se resuelve antes del cuerpo del endpoint, de modo que el rechazo ocurre antes de cualquier
    efecto (reserva de idempotencia, bloqueos, escritura de dominio y auditoria). Es la unica
    comprobacion de la bandera: las rutas no repiten el ``if``.
    """
    if not config.f3_write_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=F3_WRITE_DISABLED
        )
