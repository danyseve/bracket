// Importes relativos a propósito: este módulo lo cargan los tests con `node --test` (sin alias).
import type { F3WriteOutcome } from '../../services/f3.ts';
import type { StageItemCourtResult } from './f3_contract.ts';
import type { F3Failure } from './f3_errors.ts';
import type { F3RunResult, WriteGate } from './f3_write.ts';
import { actionScope } from './f3_write.ts';
import type { AssignmentAction, AssignmentFeedback } from './tatami_board.ts';
import { assignmentAction } from './tatami_board.ts';

/** Lo que el controlador necesita de la puerta: ejecutar con clave. El hook de React la envuelve. */
type Gate = Pick<WriteGate, 'run' | 'restorePending'>;

/**
 * Acción de asignación de tatami, en lógica pura y con el protocolo de idempotencia por delante.
 *
 * Es la pieza que ejercen los tests sin navegador: la tarjeta solo recoge la intención y pinta el
 * resultado. Reglas que se cumplen aquí, no en el componente:
 *
 * - el doble clic no produce dos escrituras: la segunda pasa por la puerta y se ignora;
 * - un reintento del **mismo intento lógico** reutiliza la clave; una acción nueva estrena clave;
 * - el no-op (mandar el cuadro al tatami en el que ya está) no se envía: la UI no ofrece ese destino,
 *   y si llegara por otra vía no se escribe nada;
 * - tras el éxito se relee el estado publicado (stages/courts); la pantalla **no** conserva una copia
 *   optimista como fuente de verdad;
 * - un fallo devuelve el fallo tipado de F3 para que la pantalla elija el mensaje: nunca se enseña el
 *   `detail` crudo del servidor.
 */

/** Intención del organizador: este cuadro, a este tatami (o a ninguno). */
export interface AssignmentRequest {
  tournament_id: number;
  stage_item_id: number;
  /** Tatami actual del cuadro según el estado publicado (no lo decide la tarjeta). */
  current_court_id: number | null;
  /** Tatami destino; `null` = quitar del tatami. */
  target_court_id: number | null;
}

export type AssignmentOutcome =
  | { status: 'applied'; action: AssignmentAction; feedback: AssignmentFeedback }
  | { status: 'ignored' }
  | { status: 'rejected'; action: AssignmentAction }
  | { status: 'failed'; failure: F3Failure };

export type AssignmentWriter = (
  tournament_id: number,
  stage_item_id: number,
  court_id: number | null,
  key: string
) => Promise<F3WriteOutcome<StageItemCourtResult>>;

export interface AssignmentDeps {
  /** Escritura real del servicio F3 (la inyecta la página; en los tests, un doble). */
  write: AssignmentWriter;
  /** Relectura del estado publicado. Se llama solo tras un éxito confirmado. */
  refresh?: () => Promise<unknown>;
}

/**
 * Alcance de la acción lógica: torneo, cuadro y **tatami destino**.
 *
 * No incluye el tatami actual a propósito: la intención del organizador («este cuadro va al tatami 3»)
 * no cambia porque el cuadro se haya movido por otro lado entre el primer intento y el reintento.
 */
export function assignmentScope(request: AssignmentRequest): string {
  return actionScope([
    'stage-item-court',
    request.tournament_id,
    request.stage_item_id,
    request.target_court_id ?? 'none',
  ]);
}

function emptyResultFailure(): F3Failure {
  return {
    kind: 'server',
    messageKey: 'f3_error_server',
    technicalCode: 'F3_WRITE_EMPTY_RESULT',
    retryAfterSeconds: null,
    blockingIssues: [],
  };
}

export async function runAssignment(
  gate: Gate,
  request: AssignmentRequest,
  deps: AssignmentDeps
): Promise<AssignmentOutcome> {
  const action = assignmentAction(request.current_court_id, request.target_court_id);
  if (action === 'noop') {
    return { status: 'rejected', action };
  }

  const { write } = deps;
  const result: F3RunResult<F3WriteOutcome<StageItemCourtResult>> = await gate.run(
    assignmentScope(request),
    (key) => write(request.tournament_id, request.stage_item_id, request.target_court_id, key)
  );

  if (result.status === 'ignored') {
    return { status: 'ignored' };
  }

  const outcome = result.value;
  if (outcome == null) {
    return { status: 'failed', failure: emptyResultFailure() };
  }
  if (!outcome.ok) {
    // Reintento del mismo intento lógico (S4.3 §8): la clave confirmada por el servidor no se
    // cambia; una acción distinta (otro tatami) estrena clave sola.
    gate.restorePending(assignmentScope(request), result.key ?? '');
    return { status: 'failed', failure: outcome.failure };
  }

  if (deps.refresh != null) {
    await deps.refresh();
  }

  return {
    status: 'applied',
    action,
    feedback: {
      stage_item_id: outcome.data.stage_item_id,
      court_id: outcome.data.court_id,
      court_name: outcome.data.court_name,
      affected_match_count: outcome.data.affected_match_count,
    },
  };
}
