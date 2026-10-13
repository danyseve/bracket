/**
 * §23 - el cuerpo de edicion de combate (`match_modal`) incluye el contrato REAL de
 * `MatchBody` publicado por el backend: `result_method` y `winner_stage_item_input_id`
 * (S4.4b). El desajuste del modal con el esquema publicado solo aflora al regenerar el
 * cliente OpenAPI; estos tests lo fijan.
 *
 * Se comprueba ademas que el flujo de ganador/metodo de resultado NO cambia: el modal
 * reenvia tal cual el ganador y el metodo ya declarados en el combate, de modo que
 * guardar una edicion de marcador/pista no borra un resultado durable, y sigue enviando
 * los campos que ya enviaba (marcador, pista, duraciones).
 *
 * Sin DOM: se lee el fuente del componente y los tipos generados, como el resto de tests
 * del repo (no se montan componentes React).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8');
}

const TYPES = '../src/openapi/types.gen.ts';
const MODAL = '../src/components/modals/match_modal.tsx';

/** Enum del metodo de resultado tal y como lo publica el backend. */
const RESULT_METHODS = [
  'points',
  'submission',
  'decision',
  'disqualification',
  'walkover',
  'referee_stoppage',
  'other',
];

/** Bloque `export type <name> = { ... };` de los tipos generados. */
function typeBlock(contents: string, name: string): string {
  const match = contents.match(new RegExp(`export type ${name} = \\{[\\s\\S]*?\\n\\};`));
  assert.ok(match, `no se encontro el tipo generado ${name}`);
  return match![0];
}

/** Objeto `const updatedMatch = { ... };` que el modal envia como cuerpo del PUT. */
function editBody(modal: string): string {
  const match = modal.match(/const updatedMatch = \{[\s\S]*?\n\s*\};/);
  assert.ok(match, 'no se encontro el cuerpo de edicion (updatedMatch) en match_modal.tsx');
  return match![0];
}

/* ------------------------------------------------------- contrato real publicado */

test('el cliente generado expone MatchBody con result_method y winner_stage_item_input_id', () => {
  const body = typeBlock(source(TYPES), 'MatchBody');

  assert.match(body, /result_method:/, 'MatchBody debe declarar result_method');
  assert.match(
    body,
    /winner_stage_item_input_id:\s*number \| null;/,
    'MatchBody debe declarar winner_stage_item_input_id como number|null'
  );

  // El enum del metodo de resultado es exactamente el que publica el backend.
  for (const method of RESULT_METHODS) {
    assert.match(body, new RegExp(`'${method}'`), `falta el metodo ${method} en el enum`);
  }
});

/* ------------------------------------------------------------------ cuerpo del modal */

test('el cuerpo del modal incluye winner y result_method tomados del combate', () => {
  const body = editBody(source(MODAL));

  assert.match(
    body,
    /winner_stage_item_input_id:\s*match\.winner_stage_item_input_id/,
    'el cuerpo debe reenviar winner_stage_item_input_id del combate'
  );
  assert.match(
    body,
    /result_method:\s*match\.result_method/,
    'el cuerpo debe reenviar result_method del combate'
  );
});

test('el modal no usa cast inseguro para satisfacer el contrato', () => {
  const modal = source(MODAL);

  assert.doesNotMatch(
    modal,
    /\bas any\b|as unknown as|@ts-ignore|@ts-expect-error/,
    'no se debe esconder el desajuste de tipos con un cast'
  );
});

test('el modal sigue enviando marcador, pista y duraciones como antes', () => {
  const body = editBody(source(MODAL));

  for (const field of [
    'round_id',
    'stage_item_input1_score',
    'stage_item_input2_score',
    'court_id',
    'custom_duration_minutes',
    'custom_margin_minutes',
  ]) {
    assert.match(body, new RegExp(`${field}:`), `el cuerpo debe seguir enviando ${field}`);
  }
});
