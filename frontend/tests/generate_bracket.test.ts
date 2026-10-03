/**
 * Copy/UX checks for the explicit "Generar cuadro" action (P2.8B F2C).
 *
 * Generating a bracket is the step that spreads the direct byes over the first round, and the
 * operator has to be able to decide it from the UI without knowing anything about the internals:
 * the copy is asserted verbatim here because it is what the operator reads.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
  buildGenerateBracketUrl,
  countParticipants,
  ERROR_WITHOUT_DETAIL,
  getBlockerCode,
  getBracketSize,
  getExpectedByes,
  getGenerationMode,
  getResponseErrorDetail,
  isBracketSpread,
} from '../src/components/utils/generate_bracket.ts';

const BLOCKER_CODES = [
  'scores',
  'winner',
  'planning',
  'tentative_inputs',
  'not_single_elimination',
  'too_few_entrants',
  'bracket_too_large',
  'inconsistent_slots',
  'unknown',
];

function loadLocale(language: string): Record<string, string> {
  const url = new URL(`../public/locales/${language}/common.json`, import.meta.url);
  return JSON.parse(readFileSync(url, 'utf8'));
}

/** A stage item as the UI sees it: `slots` holds the team id of every slot, or null. */
function stageItem(slots: (number | null)[], teamCount = slots.length) {
  return {
    team_count: teamCount,
    inputs: slots.map((team_id, index) => ({ slot: index + 1, team_id })),
  };
}

test('es: el copy de "Generar cuadro" es el acordado', () => {
  const es = loadLocale('es');

  assert.equal(es.generate_bracket_button, 'Generar cuadro');
  assert.equal(es.regenerate_bracket_button, 'Regenerar cuadro');
  assert.equal(es.generate_bracket_modal_title, 'Generar cuadro');
  assert.equal(
    es.generate_bracket_modal_description,
    'Se distribuirán los participantes en el cuadro y se aplicarán los pases directos necesarios.'
  );
  assert.equal(
    es.regenerate_bracket_modal_warning,
    'Esto cambiará la distribución actual del cuadro.'
  );
  assert.equal(es.generate_bracket_success_title, 'Cuadro generado');
  assert.equal(es.generate_bracket_success_byes, '{{count}} pases directos');
  assert.equal(es.generate_bracket_success_empty_matchups, '{{count}} cruces vacíos');
});

test('es/en: el resumen y los bloqueos no hablan de fantasmas', () => {
  for (const language of ['es', 'en']) {
    const locale = loadLocale(language);
    const generationCopy = Object.entries(locale).filter(([key]) =>
      key.startsWith('generate_bracket')
    );

    assert.ok(generationCopy.length > 0);
    for (const [key, value] of generationCopy) {
      assert.ok(!/ghost/i.test(value), `${language}.${key} no debe decir "ghost"`);
      assert.ok(!/fantasma/i.test(value), `${language}.${key} no debe decir "fantasma"`);
    }
  }
});

test('es/en: cada motivo de bloqueo del API tiene su mensaje', () => {
  for (const language of ['es', 'en']) {
    const locale = loadLocale(language);

    for (const code of BLOCKER_CODES) {
      const message = locale[`generate_bracket_blocked_${code}`];
      assert.equal(typeof message, 'string', `${language} no traduce el bloqueo ${code}`);
      assert.ok(message.length > 0);
    }
    assert.equal(locale.generate_bracket_button, locale.generate_bracket_confirm_button);
  }
});

test('resumen de la generación: participantes, cuadro y pases directos', () => {
  const gap = stageItem([1, 2, 3, 4, 5, 6, null, null], 8);

  assert.equal(countParticipants(gap), 6);
  assert.equal(getBracketSize(gap), 8);
  assert.equal(getExpectedByes(gap), 2);

  const complete = stageItem([1, 2, 3, 4], 4);
  assert.equal(getExpectedByes(complete), 0);

  const oversized = stageItem([1, 2, null, null, null, null, null, null], 8);
  assert.equal(getExpectedByes(oversized), 6);
});

test('un cuadro recién rellenado se genera; uno ya repartido se regenera', () => {
  const gap = stageItem([1, 2, 3, 4, 5, 6, null, null], 8);
  const spread = stageItem([1, 2, 3, 4, 6, null, 5, 7], 8);
  const complete = stageItem([1, 2, 3, 4, 5, 6, 7, 8], 8);
  const empty = stageItem([null, null, null, null], 4);

  assert.equal(isBracketSpread(gap), false);
  assert.equal(getGenerationMode(gap), 'generate');

  assert.equal(isBracketSpread(spread), true);
  assert.equal(getGenerationMode(spread), 'regenerate');

  assert.equal(getGenerationMode(complete), 'regenerate');
  assert.equal(getGenerationMode(empty), 'generate');
});

test('la acción llama al endpoint explícito de generación', () => {
  assert.equal(buildGenerateBracketUrl(9, 12), 'tournaments/9/stage_items/12/generate_bracket');
});

test('el motivo del bloqueo se lee del detalle del API', () => {
  assert.equal(getBlockerCode('generate_bracket_blocked: scores'), 'scores');
  assert.equal(getBlockerCode('generate_bracket_blocked: bracket_too_large'), 'bracket_too_large');
  assert.equal(getBlockerCode("Stage item doesn't exist"), null);
  assert.equal(getBlockerCode('generate_bracket_blocked:'), null);
});

test('un fallo de la petición se convierte en el motivo a mostrar', () => {
  assert.equal(
    getResponseErrorDetail({
      isAxiosError: true,
      response: { data: { detail: 'generate_bracket_blocked: planning' } },
    }),
    'generate_bracket_blocked: planning'
  );
  assert.equal(
    getResponseErrorDetail({ isAxiosError: true, response: { data: {} } }),
    ERROR_WITHOUT_DETAIL
  );
  assert.equal(
    getResponseErrorDetail({ data: { detail: 'generate_bracket_blocked: scores' } }),
    null
  );
  assert.equal(getResponseErrorDetail(null), null);
});
