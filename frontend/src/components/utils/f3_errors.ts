/**
 * Traducción de los errores de F3 a algo que una persona pueda leer (S4.1 §17).
 *
 * El backend publica **códigos estables** (no frases): 401, 403 `TENANT_NOT_AUTHORIZED`,
 * 403 `ROLE_NOT_ALLOWED`, 404 `Not Found`, 409 (`Registration already exists`, `REGISTRATION_CLOSED`,
 * `REGISTRATION_ALREADY_CLOSED`, `BRACKET_NOT_READY`, `IDEMPOTENCY_KEY_REUSED`,
 * `OPERATION_IN_PROGRESS`), 422, 500 `STATE_ERROR` y 503 (`F3_WRITE_DISABLED`, `LOCK_TIMEOUT`,
 * `IDEMPOTENCY_NOT_CONFIGURED`).
 *
 * Regla dura: **nunca** se enseña al usuario el `detail` en crudo ni nada del servidor (SQL, nombres
 * de restricción, trazas). El mensaje sale de una clave de traducción y, como mucho, se muestra el
 * código estable en secundario para que soporte pueda buscarlo.
 */

export type F3FailureKind =
  | 'network'
  | 'unauthorized'
  | 'forbidden_tenant'
  | 'forbidden_role'
  | 'not_found'
  | 'invalid'
  | 'duplicate'
  | 'state_conflict'
  | 'registration_closed'
  | 'registration_already_closed'
  | 'bracket_not_ready'
  | 'idempotency_reused'
  | 'operation_in_progress'
  | 'write_disabled'
  | 'lock_timeout'
  | 'unavailable'
  | 'server';

export interface F3Failure {
  kind: F3FailureKind;
  /** Clave de traducción del mensaje humano. */
  messageKey: string;
  /** Código estable del backend (o `HTTP_<status>`), para soporte. Puede no haberlo. */
  technicalCode: string | null;
  /** Segundos que el protocolo pide esperar antes de reintentar, si los publica. */
  retryAfterSeconds: number | null;
  /** Bloqueos derivados de un `409 BRACKET_NOT_READY`; la UI los pinta en vez de repetir la revisión. */
  blockingIssues: unknown[];
}

const MESSAGE_KEYS: Record<F3FailureKind, string> = {
  network: 'f3_error_network',
  unauthorized: 'f3_error_unauthorized',
  forbidden_tenant: 'f3_error_forbidden_tenant',
  forbidden_role: 'f3_error_forbidden_role',
  not_found: 'f3_error_not_found',
  invalid: 'f3_error_invalid',
  duplicate: 'f3_error_duplicate',
  state_conflict: 'f3_error_state_conflict',
  registration_closed: 'f3_error_registration_closed',
  registration_already_closed: 'f3_error_registration_already_closed',
  bracket_not_ready: 'f3_error_bracket_not_ready',
  idempotency_reused: 'f3_error_idempotency_reused',
  operation_in_progress: 'f3_error_operation_in_progress',
  write_disabled: 'f3_error_write_disabled',
  lock_timeout: 'f3_error_lock_timeout',
  unavailable: 'f3_error_unavailable',
  server: 'f3_error_server',
};

/** Códigos estables del backend que la UI reconoce. Cualquier otro se trata por su estado HTTP. */
const DETAIL_TO_KIND: Record<string, F3FailureKind> = {
  TENANT_NOT_AUTHORIZED: 'forbidden_tenant',
  ROLE_NOT_ALLOWED: 'forbidden_role',
  'Not Found': 'not_found',
  'Registration request is invalid': 'invalid',
  'Competitor request is invalid': 'invalid',
  'Registration already exists': 'duplicate',
  'Registration state does not allow this operation': 'state_conflict',
  REGISTRATION_CLOSED: 'registration_closed',
  REGISTRATION_ALREADY_CLOSED: 'registration_already_closed',
  BRACKET_NOT_READY: 'bracket_not_ready',
  IDEMPOTENCY_KEY_REUSED: 'idempotency_reused',
  OPERATION_IN_PROGRESS: 'operation_in_progress',
  F3_WRITE_DISABLED: 'write_disabled',
  LOCK_TIMEOUT: 'lock_timeout',
  IDEMPOTENCY_NOT_CONFIGURED: 'unavailable',
  STATE_ERROR: 'server',
  IDEMPOTENCY_STATE_ERROR: 'server',
};

interface ErrorShape {
  code?: unknown;
  message?: unknown;
  response?: {
    status?: unknown;
    headers?: Record<string, unknown>;
    data?: { detail?: unknown } | unknown;
  };
}

function asErrorShape(error: unknown): ErrorShape {
  if (error != null && typeof error === 'object') {
    return error as ErrorShape;
  }
  return {};
}

/** Código estable del cuerpo de error: cadena simple o `{"code": "..."}` (rechazo por revisión). */
export function detailCode(detail: unknown): string | null {
  if (typeof detail === 'string') {
    return detail;
  }
  if (detail != null && typeof detail === 'object' && 'code' in detail) {
    const { code } = detail as { code?: unknown };
    return typeof code === 'string' ? code : null;
  }
  return null;
}

function blockersOf(detail: unknown): unknown[] {
  if (detail != null && typeof detail === 'object' && 'blocking_issues' in detail) {
    const { blocking_issues: blockers } = detail as { blocking_issues?: unknown };
    return Array.isArray(blockers) ? blockers : [];
  }
  return [];
}

function retryAfter(headers: Record<string, unknown> | undefined): number | null {
  if (headers == null) {
    return null;
  }
  const raw = headers['retry-after'] ?? headers['Retry-After'];
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function kindFromStatus(status: number, detail: unknown): F3FailureKind {
  const code = detailCode(detail);
  if (code != null && DETAIL_TO_KIND[code] != null) {
    return DETAIL_TO_KIND[code];
  }
  if (status === 401) {
    return 'unauthorized';
  }
  if (status === 403) {
    return 'forbidden_role';
  }
  if (status === 404) {
    return 'not_found';
  }
  if (status === 409) {
    return 'state_conflict';
  }
  if (status === 422) {
    return 'invalid';
  }
  if (status === 503) {
    return 'unavailable';
  }
  return 'server';
}

/** Clasifica cualquier fallo (de red, del protocolo o del dominio) en un mensaje comprensible. */
export function describeF3Failure(error: unknown): F3Failure {
  const shape = asErrorShape(error);

  if (shape.response == null || typeof shape.response.status !== 'number') {
    // Sin respuesta: red caída, DNS, CORS o timeout del navegador.
    return {
      kind: 'network',
      messageKey: MESSAGE_KEYS.network,
      technicalCode: typeof shape.code === 'string' ? shape.code : null,
      retryAfterSeconds: null,
      blockingIssues: [],
    };
  }

  const status = shape.response.status;
  const detail = (shape.response.data as { detail?: unknown } | undefined)?.detail;
  const kind = kindFromStatus(status, detail);
  const code = detailCode(detail);

  return {
    kind,
    messageKey: MESSAGE_KEYS[kind],
    technicalCode: code ?? `HTTP_${status}`,
    retryAfterSeconds: retryAfter(shape.response.headers),
    blockingIssues: blockersOf(detail),
  };
}

/** ¿El backend ha rechazado la escritura porque la función está deshabilitada en este entorno? */
export function isWriteDisabled(failure: F3Failure): boolean {
  return failure.kind === 'write_disabled';
}

/** ¿El POST de generación ha sido rechazado por la revisión recalculada dentro de la transacción? */
export function isBracketNotReady(failure: F3Failure): boolean {
  return failure.kind === 'bracket_not_ready';
}

/** ¿La escritura chocó con otra operación en vuelo sobre el mismo recurso? */
export function isOperationInProgress(failure: F3Failure): boolean {
  return failure.kind === 'operation_in_progress';
}
