import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { getRoundLabelKey } from '../src/components/utils/round.ts';

/**
 * Copy/UX checks for the BJJ terminology of the frontend.
 *
 * These tests read the translation files directly: they are the source of truth of what the
 * operator sees, and they must not mention "cancha" anywhere in Spanish.
 */
function loadLocale(language: string): Record<string, string> {
  const url = new URL(`../public/locales/${language}/common.json`, import.meta.url);
  return JSON.parse(readFileSync(url, 'utf8'));
}

test('es: el tatami sustituye a la cancha en todo el copy visible', () => {
  const es = loadLocale('es');

  assert.equal(es.courts_title, 'tatamis');
  assert.equal(es.add_court_title, 'Añadir Tatami');
  assert.equal(es.create_court_button, 'Crear Tatami');
  assert.equal(es.delete_court_button, 'Eliminar Tatami');
  assert.equal(es.go_to_courts_page, 'Ir a la página de tatamis');
  assert.equal(es.no_courts_title, 'Aún no hay tatamis');
  assert.equal(es.court_name_input_placeholder, 'Tatami 1');
  assert.equal(es.courts_filled_badge, 'tatamis completados');
  assert.equal(es.auto_assign_courts_label, 'Asignar automáticamente tatamis a partidos');
  assert.ok(!/cancha/i.test(es.active_next_round_modal_title));
  assert.ok(!/cancha/i.test(es.all_matches_scheduled_description));
  assert.ok(!/cancha/i.test(es.no_courts_description));
  assert.ok(!/cancha/i.test(es.no_courts_description_swiss));
});

test('es: no queda ningún valor visible que hable de canchas', () => {
  const es = loadLocale('es');

  const conCancha = Object.entries(es).filter(
    ([, value]) => typeof value === 'string' && /cancha/i.test(value)
  );

  assert.deepEqual(conCancha, []);
});

test('los nombres de ronda de BJJ están traducidos en es y en', () => {
  const es = loadLocale('es');
  const en = loadLocale('en');

  assert.equal(es.round_label_final, 'Final');
  assert.equal(es.round_label_semifinal, 'Semifinal');
  assert.equal(es.round_label_quarterfinal, 'Cuartos');
  assert.equal(es.round_label_round_of_16, 'Octavos');
  assert.equal(es.round_label_round_of_32, 'Dieciseisavos');

  assert.equal(en.round_label_final, 'Final');
  assert.equal(en.round_label_semifinal, 'Semifinal');
  assert.equal(en.round_label_quarterfinal, 'Quarterfinal');
  assert.equal(en.round_label_round_of_16, 'Round of 16');
  assert.equal(en.round_label_round_of_32, 'Round of 32');
});

test('toda etiqueta de ronda que el frontend puede pedir existe en es y en', () => {
  const es = loadLocale('es');
  const en = loadLocale('en');

  for (let totalRounds = 1; totalRounds <= 5; totalRounds++) {
    for (let index = 0; index < totalRounds; index++) {
      const key = getRoundLabelKey(totalRounds, index);
      assert.notEqual(key, null, `sin clave para ${totalRounds} rondas, índice ${index}`);
      assert.ok(
        typeof es[key as string] === 'string' && es[key as string] !== '',
        `falta ${key} en es`
      );
      assert.ok(
        typeof en[key as string] === 'string' && en[key as string] !== '',
        `falta ${key} en en`
      );
    }
  }
});
