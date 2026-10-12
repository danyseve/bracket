/**
 * Protocolo de idempotencia en el lado del cliente (S4.1 §10).
 *
 * Cada acción de escritura del organizador viaja con una `Idempotency-Key` que representa **esa**
 * acción lógica, no cada intento de red:
 *
 * - reintentar tras un fallo (mismo alcance) reutiliza la clave, así el backend no repite la
 *   operación aunque la primera petición sí llegara;
 * - cambiar de acción o de datos (alcance distinto) estrena clave, para no arrastrar la reserva de
 *   una operación anterior;
 * - dos pulsaciones seguidas del botón no producen dos escrituras: la segunda se ignora mientras la
 *   primera está en vuelo.
 *
 * La clave nunca se muestra al usuario y no se persiste: vive en memoria mientras dura la acción.
 * El formato lo exige el backend (`logic/idempotency.py`): 8-255 caracteres de `[A-Za-z0-9._:-]`.
 */

/** Longitud mínima aceptada por el backend. */
export const KEY_MIN_LENGTH = 8;

/** Formato aceptado por el backend: nada fuera de `[A-Za-z0-9._:-]`. */
export const KEY_PATTERN = /^[A-Za-z0-9._:-]+$/;

export type UuidSource = () => string;

/** Identificador aleatorio disponible en cualquier contexto (no solo `https`) y sin dependencias. */
export function defaultUuid(): string {
  const cryptoApi = (globalThis as { crypto?: Crypto }).crypto;
  if (cryptoApi != null && typeof cryptoApi.randomUUID === 'function') {
    return cryptoApi.randomUUID();
  }
  if (cryptoApi != null && typeof cryptoApi.getRandomValues === 'function') {
    const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  }
  // Último recurso: sigue siendo una clave válida y única por acción dentro de la sesión.
  return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 14)}`;
}

/** Alcance de una acción lógica: misma cadena ⇒ misma intención del usuario. */
export function actionScope(parts: (string | number | boolean | null | undefined)[]): string {
  return parts.map((part) => String(part ?? '')).join('|');
}

export function newKey(uuid: UuidSource = defaultUuid): string {
  const key = `f3-${uuid()}`;
  if (key.length < KEY_MIN_LENGTH || !KEY_PATTERN.test(key)) {
    // No debería ocurrir nunca: si el generador devolviera algo inválido, preferimos fallar aquí y no
    // enviar una clave que el backend rechazaría con un 500.
    throw new Error('Idempotency-Key generada con formato inválido');
  }
  return key;
}

export interface PendingAction {
  scope: string;
  key: string;
}

/**
 * Devuelve el estado pendiente para un alcance: si ya había una acción con el mismo alcance se
 * conserva su clave (reintento lógico), y si el alcance es nuevo se estrena clave.
 */
export function resolvePending(
  pending: PendingAction | null,
  scope: string,
  uuid: UuidSource = defaultUuid
): PendingAction {
  if (pending != null && pending.scope === scope) {
    return pending;
  }
  return { scope, key: newKey(uuid) };
}

export interface F3RunResult<T> {
  /** `ignored` cuando ya había una escritura en vuelo: no se ha llamado al servidor. */
  status: 'done' | 'ignored';
  value?: T;
  /** Clave usada, solo para trazas de test: la UI no la muestra. */
  key?: string;
}

/**
 * Puerta de escritura: serializa las acciones de escritura de una pantalla y les da su clave.
 * Es pura (sin React) para poder ejercerla en los tests.
 */
export class WriteGate {
  private inflight = false;

  private pending: PendingAction | null = null;

  private readonly uuid: UuidSource;

  /** Sin propiedades de parámetro: el runner de tests ejecuta TypeScript en modo strip-only. */
  constructor(uuid: UuidSource = defaultUuid) {
    this.uuid = uuid;
  }

  get isInflight(): boolean {
    return this.inflight;
  }

  /** Clave pendiente, o `null` si no hay ninguna acción a medias. */
  get pendingKey(): string | null {
    return this.pending != null ? this.pending.key : null;
  }

  /** Clave que se usaría para un alcance, sin reservar nada (para mostrar estado, no para enviar). */
  keyFor(scope: string): string | null {
    return this.pending != null && this.pending.scope === scope ? this.pending.key : null;
  }

  /**
   * Ejecuta la acción una sola vez. La clave se libera al completarse con éxito; si falla, se
   * conserva para que el reintento del usuario sea el mismo intento lógico.
   */
  async run<T>(scope: string, perform: (key: string) => Promise<T>): Promise<F3RunResult<T>> {
    if (this.inflight) {
      return { status: 'ignored' };
    }
    this.inflight = true;
    this.pending = resolvePending(this.pending, scope, this.uuid);
    const key = this.pending.key;
    try {
      const value = await perform(key);
      this.pending = null;
      return { status: 'done', value, key };
    } finally {
      this.inflight = false;
    }
  }

  /** Abandona la acción pendiente (el usuario cerró el formulario o cambió de intención). */
  reset(): void {
    this.pending = null;
    this.inflight = false;
  }
}
