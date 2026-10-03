import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { TFunction } from 'i18next';
import type { SWRResponse } from 'swr';

import type { RoundWithMatches, StagesWithStageItemsResponse } from '@openapi';

import { getRoundDisplayName, getRoundLabelKey } from '../src/components/utils/round.ts';

/* -------------------------------------------------------------------------- */
/* getRoundLabelKey: nombres por tamaño de cuadro                              */
/* -------------------------------------------------------------------------- */

test('1 ronda: Final', () => {
  assert.equal(getRoundLabelKey(1, 0), 'round_label_final');
});

test('2 rondas: Semifinal, Final', () => {
  assert.equal(getRoundLabelKey(2, 0), 'round_label_semifinal');
  assert.equal(getRoundLabelKey(2, 1), 'round_label_final');
});

test('3 rondas: Cuartos, Semifinal, Final', () => {
  assert.equal(getRoundLabelKey(3, 0), 'round_label_quarterfinal');
  assert.equal(getRoundLabelKey(3, 1), 'round_label_semifinal');
  assert.equal(getRoundLabelKey(3, 2), 'round_label_final');
});

test('4 rondas: Octavos, Cuartos, Semifinal, Final', () => {
  assert.equal(getRoundLabelKey(4, 0), 'round_label_round_of_16');
  assert.equal(getRoundLabelKey(4, 1), 'round_label_quarterfinal');
  assert.equal(getRoundLabelKey(4, 2), 'round_label_semifinal');
  assert.equal(getRoundLabelKey(4, 3), 'round_label_final');
});

test('5 rondas: Dieciseisavos, Octavos, Cuartos, Semifinal, Final', () => {
  assert.equal(getRoundLabelKey(5, 0), 'round_label_round_of_32');
  assert.equal(getRoundLabelKey(5, 1), 'round_label_round_of_16');
  assert.equal(getRoundLabelKey(5, 2), 'round_label_quarterfinal');
  assert.equal(getRoundLabelKey(5, 3), 'round_label_semifinal');
  assert.equal(getRoundLabelKey(5, 4), 'round_label_final');
});

test('tamaño de cuadro desconocido o índice inválido: sin etiqueta (fallback)', () => {
  assert.equal(getRoundLabelKey(0, 0), null);
  assert.equal(getRoundLabelKey(6, 0), null);
  assert.equal(getRoundLabelKey(7, 6), null);
  assert.equal(getRoundLabelKey(2, -1), null);
  assert.equal(getRoundLabelKey(2, 2), null);
  assert.equal(getRoundLabelKey(2.5, 0), null);
  assert.equal(getRoundLabelKey(3, 1.5), null);
  assert.equal(getRoundLabelKey(Number.NaN, 0), null);
});

/* -------------------------------------------------------------------------- */
/* getRoundDisplayName: etiqueta traducida con fallback al nombre original     */
/* -------------------------------------------------------------------------- */

function fakeTranslate(labels: Record<string, string>): TFunction {
  return ((key: string, options?: { defaultValue?: string }) =>
    labels[key] ?? options?.defaultValue ?? key) as unknown as TFunction;
}

function fakeRound(id: number, stageItemId: number, name: string): RoundWithMatches {
  return { id, stage_item_id: stageItemId, name } as unknown as RoundWithMatches;
}

function fakeStagesResponse(
  stageItems: {
    id: number;
    type: string;
    rounds: RoundWithMatches[];
  }[]
): SWRResponse<StagesWithStageItemsResponse> {
  return {
    data: {
      data: [{ id: 1, stage_items: stageItems }],
    },
  } as unknown as SWRResponse<StagesWithStageItemsResponse>;
}

test('cuadro de eliminación simple: usa la etiqueta traducida', () => {
  const round = fakeRound(30, 10, 'Round 02');
  const response = fakeStagesResponse([
    {
      id: 10,
      type: 'SINGLE_ELIMINATION',
      rounds: [fakeRound(20, 10, 'Round 01'), round, fakeRound(40, 10, 'Round 03')],
    },
  ]);
  const t = fakeTranslate({ round_label_semifinal: 'Semifinal' });

  assert.equal(getRoundDisplayName(round, response, t), 'Semifinal');
});

test('la posición se calcula ordenando las rondas por id, no por el orden recibido', () => {
  const final = fakeRound(40, 10, 'Round 03');
  const response = fakeStagesResponse([
    {
      id: 10,
      type: 'SINGLE_ELIMINATION',
      rounds: [final, fakeRound(20, 10, 'Round 01'), fakeRound(30, 10, 'Round 02')],
    },
  ]);
  const t = fakeTranslate({ round_label_final: 'Final', round_label_semifinal: 'Semifinal' });

  assert.equal(getRoundDisplayName(final, response, t), 'Final');
});

test('stage item que no es eliminación simple (round robin): nombre original', () => {
  const round = fakeRound(20, 10, 'Round 01');
  const response = fakeStagesResponse([{ id: 10, type: 'ROUND_ROBIN', rounds: [round] }]);
  const t = fakeTranslate({ round_label_final: 'Final' });

  assert.equal(getRoundDisplayName(round, response, t), 'Round 01');
});

test('stage item no encontrado: nombre original', () => {
  const round = fakeRound(20, 99, 'Round 01');
  const response = fakeStagesResponse([{ id: 10, type: 'SINGLE_ELIMINATION', rounds: [round] }]);

  assert.equal(getRoundDisplayName(round, response, fakeTranslate({})), 'Round 01');
});

test('ronda que no pertenece a su stage item: nombre original', () => {
  const round = fakeRound(20, 10, 'Round 01');
  const response = fakeStagesResponse([
    { id: 10, type: 'SINGLE_ELIMINATION', rounds: [fakeRound(21, 10, 'Round 02')] },
  ]);

  assert.equal(
    getRoundDisplayName(round, response, fakeTranslate({ round_label_final: 'Final' })),
    'Round 01'
  );
});

test('cuadro grande sin nombre conocido: nombre original', () => {
  const rounds = Array.from({ length: 6 }, (_, i) => fakeRound(20 + i, 10, `Round 0${i + 1}`));
  const response = fakeStagesResponse([{ id: 10, type: 'SINGLE_ELIMINATION', rounds }]);

  assert.equal(
    getRoundDisplayName(rounds[5], response, fakeTranslate({ round_label_final: 'Final' })),
    'Round 06'
  );
});

test('etiqueta no traducida en el idioma activo: nombre original', () => {
  const round = fakeRound(30, 10, 'Round 02');
  const response = fakeStagesResponse([
    { id: 10, type: 'SINGLE_ELIMINATION', rounds: [fakeRound(20, 10, 'Round 01'), round] },
  ]);

  assert.equal(getRoundDisplayName(round, response, fakeTranslate({})), 'Round 02');
});
