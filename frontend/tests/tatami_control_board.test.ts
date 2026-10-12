/**
 * Tablero de control de tatamis (P2.8C/F3C S4.3): modelo del tablero, acciones y contrato de
 * idempotencia de la asignación por cuadro.
 *
 * Ejercita los módulos puros que usan la página y el componente reales, sin red ni DOM. Los fallos se
 * construyen con la forma de axios que devuelve `describeF3Failure`, que es la función de verdad que
 * clasifica la respuesta del backend.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { describeF3Failure } from '../src/components/utils/f3_errors.ts';
import { WriteGate } from '../src/components/utils/f3_write.ts';
import { runAssignment } from '../src/components/utils/tatami_actions.ts';
import {
  assignmentAction,
  assignmentFeedbackKey,
  assignmentTargets,
  buildTatamiControlBoard,
  courtLabel,
  hasStageItemCourtContract,
  needsReassignmentConfirmation,
  TATAMI_ASSIGNMENT_CONTRACT,
} from '../src/components/utils/tatami_board.ts';

/** Respuesta de error con la forma de axios (lo único que ve `describeF3Failure`). */
function httpError(status: number, detail?: unknown) {
  return { isAxiosError: true, response: { status, data: detail != null ? { detail } : {} } };
}

function match(id: number, court_id: number | null, scores: [number, number] | null) {
  return {
    id,
    court_id,
    start_time: null,
    position_in_schedule: id,
    stage_item_input1_score: scores != null ? scores[0] : 0,
    stage_item_input2_score: scores != null ? scores[1] : 0,
    input1_team_id: id,
    input2_team_id: id + 100,
  };
}

function stageItem(
  id: number,
  court_id: number | null,
  matches: ReturnType<typeof match>[],
  name = `Categoria ${id}`
) {
  return {
    id,
    name,
    type: 'single_elimination' as const,
    stage_id: 100,
    stage_name: 'Eliminatoria',
    court_id,
    matches,
  };
}

const COURTS = [
  { id: 61, name: 'Tatami 1' },
  { id: 62, name: 'Tatami A' },
  { id: 63, name: 'Tatami 3' },
];

/* ------------------------------------------------------------------ contrato y tablero (S4.3) */

test('la capacidad de asignar por cuadro sale del contrato publicado, no de una versión', () => {
  assert.equal(TATAMI_ASSIGNMENT_CONTRACT, 'stage_item.court_id');
  assert.equal(hasStageItemCourtContract([{ court_id: null }]), true);
  assert.equal(hasStageItemCourtContract([{ court_id: 62 }]), true);
  assert.equal(hasStageItemCourtContract([{ id: 1 }]), false, 'un backend anterior no lo publica');
  assert.equal(hasStageItemCourtContract([]), false);
});

test('el tablero pinta una columna por tatami devuelto, más la bandeja de sin asignar', () => {
  const board = buildTatamiControlBoard(
    [
      stageItem(1, null, [match(1, null, null), match(2, null, [3, 1])]),
      stageItem(2, 63, [match(3, 63, null)]),
      stageItem(3, 63, []),
    ],
    COURTS
  );

  assert.deepEqual(
    board.columns.map((column) => column.kind),
    ['unassigned', 'court', 'court', 'court']
  );
  assert.deepEqual(
    board.columns.map((column) => column.title),
    ['', 'Tatami 1', 'Tatami 2', 'Tatami 3'],
    'el título es la posición real del tatami, no su identificador'
  );
  assert.deepEqual(
    board.columns.map((column) => column.cards.length),
    [1, 0, 0, 2]
  );
});

test('un tatami sin cuadros sigue apareciendo, y un cuadro con tatami desconocido no se pierde', () => {
  const board = buildTatamiControlBoard([stageItem(4, 99, [match(5, 99, null)])], COURTS);

  assert.equal(board.columns.length, 5, 'las tres columnas + sin asignar + el tatami desconocido');
  assert.deepEqual(
    board.columns.map((column) => column.cards.length),
    [0, 0, 0, 0, 1]
  );
  assert.equal(board.columns[4].court_id, 99);
});

test('cada tarjeta lleva categoría, stage, participantes, combates y estado derivado', () => {
  const board = buildTatamiControlBoard(
    [stageItem(7, 61, [match(1, 61, [3, 1]), match(2, 61, null)])],
    COURTS
  );
  const card = board.columns[1].cards[0];

  assert.equal(card.category_name, 'Categoria 7');
  assert.equal(card.stage_name, 'Eliminatoria');
  assert.equal(card.participants, 4, 'cuatro competidores distintos en los dos combates');
  assert.equal(card.match_count, 2);
  assert.equal(card.played_count, 1);
  assert.equal(card.state, 'running');
  assert.equal(card.court_id, 61);
  assert.equal(card.next_match_id, 2, 'el siguiente es el primero sin resultado');
});

test('el resumen de cada columna prepara el marcador: cuadros, combates y siguiente combate', () => {
  const board = buildTatamiControlBoard(
    [stageItem(8, 61, [match(1, 61, [3, 1])]), stageItem(9, 61, [match(2, 61, null)])],
    COURTS
  );
  const column = board.columns[1];

  assert.equal(column.bracket_count, 2);
  assert.equal(column.match_count, 2);
  assert.equal(column.played_count, 1);
  assert.equal(column.next_match_id, 2);
});

test('el tablero declara de dónde saca la asignación', () => {
  assert.equal(buildTatamiControlBoard([], COURTS).assignment_source, 'stage_item');
  assert.equal(buildTatamiControlBoard([], COURTS, 'match').assignment_source, 'match');
});

/* ------------------------------------------------------------------ acciones (assign / reassign) */

const ACTIONS_COURTS = [
  { id: 61, name: 'Tatami 1' },
  { id: 62, name: 'Tatami 2' },
];

test('la acción se decide por lo que el cuadro tiene y lo que se pide', () => {
  assert.equal(assignmentAction(null, 61), 'assign');
  assert.equal(assignmentAction(61, 62), 'reassign');
  assert.equal(assignmentAction(61, null), 'unassign');
  assert.equal(assignmentAction(61, 61), 'noop');
  assert.equal(assignmentAction(null, null), 'noop');
});

test('solo el cambio de tatami pide confirmación: asignar y quitar no la piden', () => {
  assert.equal(needsReassignmentConfirmation(61, 62), true);
  assert.equal(needsReassignmentConfirmation(null, 62), false);
  assert.equal(needsReassignmentConfirmation(61, null), false);
});

test('los destinos ofrecidos no incluyen el tatami en el que ya está', () => {
  assert.deepEqual(
    assignmentTargets(ACTIONS_COURTS, 61).map((court) => court.id),
    [62]
  );
  assert.deepEqual(
    assignmentTargets(ACTIONS_COURTS, null).map((court) => court.id),
    [61, 62]
  );
});

test('el tatami se nombra por su posición real; el identificador no se enseña', () => {
  assert.equal(courtLabel(ACTIONS_COURTS, 62), 'Tatami 2');
  assert.equal(courtLabel(ACTIONS_COURTS, 99, 'Tatami Azul'), 'Tatami Azul');
  assert.equal(courtLabel(ACTIONS_COURTS, null), '');
});

test('el mensaje confirma el efecto real, incluido el no-op', () => {
  assert.equal(
    assignmentFeedbackKey({
      stage_item_id: 7,
      court_id: 61,
      court_name: 'Tatami 1',
      affected_match_count: 4,
    }),
    'tatami_feedback_assigned'
  );
  assert.equal(
    assignmentFeedbackKey({
      stage_item_id: 7,
      court_id: 61,
      court_name: 'Tatami 1',
      affected_match_count: 0,
    }),
    'tatami_feedback_unchanged'
  );
  assert.equal(
    assignmentFeedbackKey({
      stage_item_id: 7,
      court_id: null,
      court_name: null,
      affected_match_count: 3,
    }),
    'tatami_feedback_unassigned'
  );
});

/* ------------------------------------------------------------------ ejecución de la escritura */

const FAKE_UUID = 'aaaaaaaaaaaa';
const FAKE_KEY = `f3-${FAKE_UUID}`;

function okResult(court_id: number | null, court_name: string | null, count: number) {
  return {
    ok: true as const,
    data: { stage_item_id: 7, court_id, court_name, affected_match_count: count },
  };
}

function request(
  overrides: Partial<Record<'current_court_id' | 'target_court_id', number | null>> = {}
) {
  return {
    tournament_id: 3,
    stage_item_id: 7,
    current_court_id: null,
    target_court_id: 62,
    ...overrides,
  };
}

test('asignar escribe con la puerta, relee el estado publicado y enseña el efecto confirmado', async () => {
  const gate = new WriteGate(() => FAKE_UUID);
  const keys: string[] = [];
  let refreshes = 0;

  const outcome = await runAssignment(gate, request(), {
    write: async (_tournamentId, _stageItemId, _courtId, key) => {
      keys.push(key);
      return okResult(62, 'Tatami 2', 4);
    },
    refresh: async () => {
      refreshes += 1;
    },
  });

  assert.equal(outcome.status, 'applied');
  assert.deepEqual(keys, [FAKE_KEY]);
  assert.equal(refreshes, 1, 'el estado se relee del backend: no hay copia optimista');
  if (outcome.status === 'applied') {
    assert.equal(outcome.action, 'assign');
    assert.equal(outcome.feedback.affected_match_count, 4);
    assert.equal(courtLabel(ACTIONS_COURTS, outcome.feedback.court_id), 'Tatami 2');
  }
});

test('quitar del tatami manda el nulo y confirma los combates afectados', async () => {
  const gate = new WriteGate(() => FAKE_UUID);
  const sent: (number | null)[] = [];

  const outcome = await runAssignment(
    gate,
    request({ current_court_id: 62, target_court_id: null }),
    {
      write: async (_tournamentId, _stageItemId, courtId) => {
        sent.push(courtId);
        return okResult(null, null, 3);
      },
      refresh: async () => {},
    }
  );

  assert.deepEqual(sent, [null]);
  assert.equal(outcome.status, 'applied');
  if (outcome.status === 'applied') {
    assert.equal(outcome.action, 'unassign');
    assert.equal(assignmentFeedbackKey(outcome.feedback), 'tatami_feedback_unassigned');
  }
});

test('el doble clic no llega a escribir dos veces', async () => {
  const gate = new WriteGate(() => FAKE_UUID);
  let release: () => void = () => {};
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  let writes = 0;
  const deps = {
    write: async () => {
      writes += 1;
      await pending;
      return okResult(62, 'Tatami 2', 4);
    },
    refresh: async () => {},
  };

  const first = runAssignment(gate, request(), deps);
  const second = await runAssignment(gate, request(), deps);

  assert.equal(second.status, 'ignored', 'la segunda pulsacion no produce una segunda escritura');
  release();
  assert.equal((await first).status, 'applied');
  assert.equal(writes, 1);
});

test('el reintento del mismo intento conserva la clave y una acción nueva la estrena', async () => {
  // Fuente de uuid creciente: cada acción nueva pide una clave y el reintento reutiliza la pendiente.
  const uuids = ['aaaaaaaaaaa1', 'aaaaaaaaaaa2', 'aaaaaaaaaaa3'];
  let sequence = 0;
  const gate = new WriteGate(() => uuids[sequence++] ?? 'aaaaaaaaaaa9');
  const keys: string[] = [];
  const failing = {
    write: async (
      _tournamentId: number,
      _stageItemId: number,
      _courtId: number | null,
      key: string
    ) => {
      keys.push(key);
      return {
        ok: false as const,
        failure: describeF3Failure(httpError(503, 'F3_WRITE_DISABLED')),
      };
    },
    refresh: async () => {},
  };

  await runAssignment(gate, request(), failing);
  await runAssignment(gate, request(), failing);
  assert.deepEqual(keys, ['f3-aaaaaaaaaaa1', 'f3-aaaaaaaaaaa1'], 'mismo intento: misma clave');

  await runAssignment(gate, request({ target_court_id: 61 }), failing);
  assert.equal(keys.length, 3);
  assert.equal(keys[2], 'f3-aaaaaaaaaaa2', 'otro destino es otra acción: clave nueva');
});

test('mandar el cuadro al tatami en el que ya está no escribe nada', async () => {
  const gate = new WriteGate(() => FAKE_UUID);
  let writes = 0;

  const outcome = await runAssignment(
    gate,
    request({ current_court_id: 62, target_court_id: 62 }),
    {
      write: async () => {
        writes += 1;
        return okResult(62, 'Tatami 2', 0);
      },
      refresh: async () => {},
    }
  );

  assert.equal(outcome.status, 'rejected');
  assert.equal(writes, 0);
});

for (const [status, detail, kind] of [
  [401, undefined, 'unauthorized'],
  [403, 'ROLE_NOT_ALLOWED', 'forbidden_role'],
  [404, undefined, 'not_found'],
  [409, 'LOCK_TIMEOUT', 'lock_timeout'],
  [409, 'STAGE_ITEM_IN_USE', 'category_in_use'],
  [409, 'IDEMPOTENCY_KEY_REUSED', 'idempotency_reused'],
  [503, 'F3_WRITE_DISABLED', 'write_disabled'],
  [503, 'OPERATION_IN_PROGRESS', 'operation_in_progress'],
] as const) {
  test(`la respuesta ${status} ${detail ?? ''} llega tipada y sin releer el estado`, async () => {
    const gate = new WriteGate(() => FAKE_UUID);
    let refreshes = 0;

    const outcome = await runAssignment(gate, request(), {
      write: async () => ({
        ok: false as const,
        failure: describeF3Failure(httpError(status, detail)),
      }),
      refresh: async () => {
        refreshes += 1;
      },
    });

    assert.equal(outcome.status, 'failed');
    if (outcome.status === 'failed') {
      assert.equal(outcome.failure.kind, kind);
    }
    assert.equal(refreshes, 0, 'un fallo no relee nada');
  });
}

test('la pantalla no mueve la tarjeta por su cuenta: el estado lo publica el backend', async () => {
  const board = buildTatamiControlBoard([stageItem(7, 61, [match(1, 61, null)])], ACTIONS_COURTS);
  const snapshot = structuredClone(board);
  const gate = new WriteGate(() => FAKE_UUID);

  await runAssignment(gate, request({ current_court_id: 61, target_court_id: 62 }), {
    write: async () => okResult(62, 'Tatami 2', 1),
    refresh: async () => {},
  });

  assert.deepEqual(board, snapshot, 'el modelo del tablero no se toca: se relee del backend');
});

/* ------------------------------------------------------------------ cableado real (fuentes) */

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8');
}

test('el contrato publica la ruta de asignación por cuadro sobre el endpoint real', () => {
  const contract = source('../src/components/utils/f3_contract.ts');

  assert.match(contract, /stage-items\/\$\{stage_item_id\}\/court/);
  assert.match(contract, /court_id/);
});

test('la escritura pasa por el protocolo de idempotencia y por el servicio, no por un fetch suelto', () => {
  const write = source('../src/components/utils/f3_write.ts');
  const service = source('../src/services/f3.ts');

  assert.match(write, /Idempotency-Key/);
  assert.match(service, /stageItemCourtUrl\(tournament_id, stage_item_id\)/);
  assert.match(service, /idempotentHeaders\(key\)/);
});

test('la página ejecuta la acción con el controlador y relee el estado publicado', () => {
  const page = source('../src/pages/tournaments/[id]/tatamis.tsx');

  assert.match(page, /runAssignment/);
  assert.match(page, /swrStagesResponse\.mutate\(\)/);
  assert.match(page, /TatamiControlBoardView/);
  assert.match(page, /hasStageItemCourtContract/);
});

test('la tarjeta ofrece asignar, mover y quitar, y confirma el movimiento', () => {
  const component = source('../src/components/f3/tatami_control_board.tsx');

  assert.match(component, /tatami_remove_action/);
  assert.match(component, /needsReassignmentConfirmation/);
  assert.match(component, /tatami_reassign_confirm/);
  assert.match(component, /inflight/);
});
