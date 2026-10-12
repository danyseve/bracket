/**
 * Pantalla pública del planificador (S4.5): contrato, sondeo, derivación pura y lo que no puede
 * salir por la pantalla.
 *
 * Los componentes React no se montan (no hay DOM): se comprueba lo que decide el comportamiento y
 * que es verificable sin navegador — la ruta declarada, el endpoint que sondea la pantalla, el
 * intervalo de sondeo, la derivación pura ejerciéndola con **dos cuerpos públicos reales** copiados
 * del laboratorio (uno planificado y otro con el tatami 30 minutos tarde) y la paridad de las claves
 * nuevas en los doce idiomas.
 */
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { test } from 'node:test';

import {
  PUBLIC_SCHEDULE_POLL_MS,
  PUBLIC_SCHEDULE_STATES,
  isPublicScheduleState,
  publicScheduleUrl,
  publicStateKey,
} from '../src/components/utils/f3_contract.ts';
import {
  courtsToShow,
  fightersLabel,
  isEmptySchedule,
  isLateCourt,
  pendingCount,
  resultsFor,
  rowsFor,
  waitingNames,
} from '../src/components/utils/public_schedule_display.ts';

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8');
}

/**
 * Cuerpos públicos reales, copiados de respuestas del laboratorio
 * (`GET /public/tournaments/1/schedule`, plan generado a las 09:00).
 */
const PLANNED_PAYLOAD = {
  tournament: { name: 'Some Cool Tournament', start_time: '2022-01-11T04:32:11+00:00' },
  generated_at: '2026-10-12T17:53:39.220549+00:00',
  courts: [
    {
      court_id: 1,
      court_name: 'Tatami 1',
      lag_minutes: 0,
      current: {
        match_id: 1,
        category: 'Cuadro S4.5',
        round_name: 'Round 01',
        fighter_a: 'Equipo 1',
        fighter_b: 'Equipo 2',
        scheduled_start: '2026-06-01T09:00:00+00:00',
        estimated_start: '2026-06-01T09:00:00+00:00',
        state: 'NOW',
        position_in_schedule: 1,
        waiting_for: [],
      },
      next: {
        match_id: 2,
        category: 'Cuadro S4.5',
        round_name: 'Round 01',
        fighter_a: 'Equipo 3',
        fighter_b: 'Equipo 4',
        scheduled_start: '2026-06-01T09:15:00+00:00',
        estimated_start: '2026-06-01T09:15:00+00:00',
        state: 'NEXT',
        position_in_schedule: 2,
        waiting_for: [],
      },
      upcoming: [
        {
          match_id: 3,
          category: 'Cuadro S4.5',
          round_name: 'Round 02',
          fighter_a: null,
          fighter_b: null,
          scheduled_start: '2026-06-01T09:45:00+00:00',
          estimated_start: '2026-06-01T09:45:00+00:00',
          state: 'WAITING',
          position_in_schedule: 3,
          waiting_for: ['Ganador del combate 1', 'Ganador del combate 2'],
        },
      ],
      completed: [],
    },
  ],
};

/** El mismo torneo después de terminar el primer combate 30 minutos tarde. */
const LATE_PAYLOAD = {
  tournament: { name: 'Some Cool Tournament', start_time: '2022-01-11T04:32:11+00:00' },
  generated_at: '2026-10-12T17:53:39.235901+00:00',
  courts: [
    {
      court_id: 1,
      court_name: 'Tatami 1',
      lag_minutes: 30,
      current: {
        match_id: 2,
        category: 'Cuadro S4.5',
        round_name: 'Round 01',
        fighter_a: 'Equipo 3',
        fighter_b: 'Equipo 4',
        scheduled_start: '2026-06-01T09:15:00+00:00',
        estimated_start: '2026-06-01T09:45:00+00:00',
        state: 'DELAYED',
        position_in_schedule: 2,
        waiting_for: [],
      },
      next: {
        match_id: 3,
        category: 'Cuadro S4.5',
        round_name: 'Round 02',
        fighter_a: null,
        fighter_b: null,
        scheduled_start: '2026-06-01T09:45:00+00:00',
        estimated_start: '2026-06-01T10:15:00+00:00',
        state: 'WAITING',
        position_in_schedule: 3,
        waiting_for: ['Ganador del combate 1', 'Ganador del combate 2'],
      },
      upcoming: [],
      completed: [
        {
          match_id: 1,
          category: 'Cuadro S4.5',
          round_name: 'Round 01',
          fighter_a: 'Equipo 1',
          fighter_b: 'Equipo 2',
          scheduled_start: '2026-06-01T09:00:00+00:00',
          estimated_start: '2026-06-01T09:30:00+00:00',
          state: 'COMPLETED',
          position_in_schedule: 1,
          waiting_for: [],
        },
      ],
    },
  ],
};

const LOCALES = ['de', 'el', 'en', 'es', 'fa', 'fr', 'it', 'ja', 'nl', 'pt', 'sv', 'zh'];

/** Claves de traducción que estrena esta pantalla (S4.5). */
const PUBLIC_KEYS = [
  'public_state_now',
  'public_state_next',
  'public_state_upcoming',
  'public_state_waiting',
  'public_state_completed',
  'public_state_delayed',
  'public_schedule_waiting_for',
  'public_schedule_lag',
  'public_schedule_empty',
  'public_schedule_completed_title',
  'public_schedule_updated',
  'public_schedule_nothing',
  'public_schedule_unavailable',
];

/** Claves con hueco: si a un idioma le falta el hueco, la pantalla enseña texto crudo. */
const KEYS_WITH_PLACEHOLDER: Record<string, string> = {
  public_schedule_waiting_for: '{{names}}',
  public_schedule_lag: '{{minutes}}',
  public_schedule_updated: '{{time}}',
};

function locale(language: string): Record<string, string> {
  return JSON.parse(source(`../public/locales/${language}/common.json`));
}

// --- el contrato con el backend -----------------------------------------------------------------

test('la ruta pública es la única puerta de la pantalla', () => {
  assert.equal(publicScheduleUrl(7), 'public/tournaments/7/schedule');
});

test('los estados son un vocabulario cerrado y el display no inventa ninguno', () => {
  assert.deepEqual(PUBLIC_SCHEDULE_STATES, [
    'NOW',
    'NEXT',
    'UPCOMING',
    'WAITING',
    'COMPLETED',
    'DELAYED',
  ]);
  for (const state of PUBLIC_SCHEDULE_STATES) {
    assert.equal(isPublicScheduleState(state), true);
  }
  assert.equal(isPublicScheduleState('ALMOST'), false);
  assert.equal(isPublicScheduleState('now'), false);
});

test('cada estado tiene su clave de traducción', () => {
  assert.equal(publicStateKey('NOW'), 'public_state_now');
  assert.equal(publicStateKey('DELAYED'), 'public_state_delayed');
});

// --- la pantalla sondea, no adivina -------------------------------------------------------------

test('el sondeo cae dentro de la ventana de 5 a 10 segundos', () => {
  assert.ok(
    PUBLIC_SCHEDULE_POLL_MS >= 5_000 && PUBLIC_SCHEDULE_POLL_MS <= 10_000,
    `sondeo fuera de ventana: ${PUBLIC_SCHEDULE_POLL_MS} ms`
  );
});

test('la pantalla se declara como ruta de presentación, junto a las demás', () => {
  const main = source('../src/main.tsx');
  assert.match(
    main,
    /import SchedulePresentPage from '\.\/pages\/tournaments\/\[id\]\/dashboard\/present\/schedule';/
  );
  assert.match(main, /<Route path="schedule" element=\{<SchedulePresentPage \/>\} \/>/);
});

test('el sondeo de la pantalla usa el contrato y su intervalo, y no otra ruta', () => {
  const adapter = source('../src/services/adapter.tsx');
  assert.match(adapter, /publicScheduleUrl\(tournament_id\)/);
  assert.match(adapter, /refreshInterval: PUBLIC_SCHEDULE_POLL_MS/);
  assert.match(adapter, /export function getPublicScheduleLive\(/);
});

test('la página vive de una sola fuente y no deriva estados', () => {
  const page = source('../src/pages/tournaments/[id]/dashboard/present/schedule.tsx');
  assert.match(page, /getPublicScheduleLive\(/);
  // El único servicio que consulta es el sondeo público: nada de cuadros, tatamis ni plan del organizador.
  assert.equal(/getStagesLive|getCourtsLive|getMatchesLive/.test(page), false, page);
  assert.equal(/fetch\(|axios/.test(page), false, page);
  // Ni calcula horas ni estados de combate por su cuenta.
  assert.equal(/isMatchHappening|isMatchInTheFuture|dayjs\(/.test(page), false, page);
});

// --- la derivación, con cuerpos reales ----------------------------------------------------------

test('con el plan recién generado, la pantalla pone AHORA, SIGUIENTE y el que espera', () => {
  const court = PLANNED_PAYLOAD.courts[0];
  const rows = rowsFor(court);

  assert.deepEqual(
    rows.map((row) => row.state),
    ['NOW', 'NEXT', 'WAITING']
  );
  assert.deepEqual(
    rows.map((row) => row.matchId),
    [1, 2, 3]
  );
  assert.equal(rows[0].stateKey, 'public_state_now');
  assert.equal(rows[0].title, 'Cuadro S4.5 · Round 01');
  assert.equal(rows[0].fighters, 'Equipo 1 vs Equipo 2');
  assert.equal(rows[2].fighters, '');
  assert.deepEqual(rows[2].waitingFor, ['Ganador del combate 1', 'Ganador del combate 2']);
  assert.equal(pendingCount(court), 3);
  assert.equal(resultsFor(court).length, 0);
  assert.equal(isLateCourt(court), false);
  assert.equal(isEmptySchedule(PLANNED_PAYLOAD), false);
  assert.deepEqual(
    courtsToShow(PLANNED_PAYLOAD).map((entry) => entry.court_name),
    ['Tatami 1']
  );
});

test('con el tatami 30 minutos tarde, la pantalla enseña el retraso y lo terminado', () => {
  const court = LATE_PAYLOAD.courts[0];
  const rows = rowsFor(court);
  const results = resultsFor(court);

  assert.equal(isLateCourt(court), true);
  assert.equal(court.lag_minutes, 30);

  assert.deepEqual(
    rows.map((row) => row.state),
    ['DELAYED', 'WAITING']
  );
  // La hora la manda el backend: la pantalla no vuelve a sumar el retraso.
  assert.equal(rows[0].scheduledStart, '2026-06-01T09:15:00+00:00');
  assert.equal(rows[0].estimatedStart, '2026-06-01T09:45:00+00:00');
  assert.equal(rows[1].estimatedStart, '2026-06-01T10:15:00+00:00');

  assert.equal(results.length, 1);
  assert.equal(results[0].matchId, 1);
  assert.equal(results[0].state, 'COMPLETED');
  assert.equal(results[0].fighters, 'Equipo 1 vs Equipo 2');
  assert.equal(pendingCount(court), 2);
  assert.equal(isEmptySchedule(LATE_PAYLOAD), false);
});

test('un combate sin rivales no se pinta como si los tuviera', () => {
  const waiting = PLANNED_PAYLOAD.courts[0].upcoming[0];
  assert.equal(fightersLabel(waiting), '');
  assert.deepEqual(waitingNames(waiting), ['Ganador del combate 1', 'Ganador del combate 2']);
  assert.equal(
    fightersLabel({ ...waiting, fighter_a: 'Equipo 9', fighter_b: null }),
    '',
    'con un solo nombre no hay combate que anunciar'
  );
  assert.equal(fightersLabel({ ...waiting, fighter_a: '', fighter_b: 'Equipo 9' }), '');
});

test('un torneo sin plan deja la pantalla vacía pero en pie', () => {
  const empty = {
    tournament: { name: 'Some Cool Tournament', start_time: null },
    generated_at: '2026-10-12T17:52:54.657434+00:00',
    courts: [
      {
        court_id: 1,
        court_name: 'Tatami 1',
        lag_minutes: 0,
        current: null,
        next: null,
        upcoming: [],
        completed: [],
      },
    ],
  };

  assert.equal(isEmptySchedule(empty), true);
  assert.equal(pendingCount(empty.courts[0]), 0);
  assert.deepEqual(rowsFor(empty.courts[0]), []);
  assert.equal(courtsToShow(empty).length, 1, 'el tatami sigue en pantalla, con su cartel');
});

// --- lo que la pantalla no puede soltar ---------------------------------------------------------

test('el cuerpo público no trae nada de cuentas ni de auditoría', () => {
  const forbidden = [
    'email',
    'date_of_birth',
    'jwt',
    'token',
    'password',
    'audit',
    'user_id',
    'idempotency',
    'competitor_id',
    'player_id',
    'registration',
    'club_id',
  ];
  for (const payload of [PLANNED_PAYLOAD, LATE_PAYLOAD]) {
    const raw = JSON.stringify(payload).toLowerCase();
    for (const needle of forbidden) {
      assert.equal(raw.includes(needle), false, `la pantalla no puede llevar ${needle}`);
    }
  }
});

test('la pantalla solo pinta datos deportivos declarados', () => {
  const matchFields = [
    'match_id',
    'category',
    'round_name',
    'fighter_a',
    'fighter_b',
    'scheduled_start',
    'estimated_start',
    'state',
    'position_in_schedule',
    'waiting_for',
  ];
  const courtFields = [
    'court_id',
    'court_name',
    'lag_minutes',
    'current',
    'next',
    'upcoming',
    'completed',
  ];
  const payload: Record<string, unknown> = PLANNED_PAYLOAD;

  assert.deepEqual(Object.keys(payload).sort(), ['courts', 'generated_at', 'tournament']);
  assert.deepEqual(Object.keys(PLANNED_PAYLOAD.tournament).sort(), ['name', 'start_time']);
  assert.deepEqual(Object.keys(PLANNED_PAYLOAD.courts[0]).sort(), [...courtFields].sort());
  assert.deepEqual(Object.keys(PLANNED_PAYLOAD.courts[0].current).sort(), [...matchFields].sort());
});

// --- los doce idiomas ---------------------------------------------------------------------------

test('las claves nuevas están en los doce idiomas', () => {
  for (const language of LOCALES) {
    const messages = locale(language);
    for (const key of PUBLIC_KEYS) {
      const value = messages[key];
      assert.equal(typeof value, 'string', `${language} no tiene ${key}`);
      assert.ok(value.trim() !== '', `${language} deja ${key} vacío`);
    }
  }
});

test('a ninguna traducción se le pierde el hueco', () => {
  for (const language of LOCALES) {
    const messages = locale(language);
    for (const [key, placeholder] of Object.entries(KEYS_WITH_PLACEHOLDER)) {
      assert.ok(
        messages[key].includes(placeholder),
        `${language}: a ${key} le falta ${placeholder} (${messages[key]})`
      );
    }
  }
});

test('las claves que usa la pantalla existen en los idiomas', () => {
  const view = source('../src/components/f3/public_schedule_view.tsx');
  const page = source('../src/pages/tournaments/[id]/dashboard/present/schedule.tsx');
  const used = new Set<string>();
  for (const text of [view, page]) {
    for (const match of text.matchAll(/t\('([a-z0-9_]+)'/g)) {
      used.add(match[1]);
    }
  }

  assert.ok(used.size > 0, 'la pantalla no usa ninguna clave');
  for (const language of ['en', 'es']) {
    const messages = locale(language);
    for (const key of used) {
      assert.equal(typeof messages[key], 'string', `${language} no tiene ${key}`);
    }
  }
  // Y las claves de estado que puede pedir el display también están.
  for (const state of PUBLIC_SCHEDULE_STATES) {
    for (const language of ['en', 'es']) {
      assert.equal(typeof locale(language)[publicStateKey(state)], 'string', state);
    }
  }
});

test('los ficheros de idioma del proyecto siguen siendo los doce', () => {
  const languages = readdirSync(new URL('../public/locales', import.meta.url)).sort();
  assert.deepEqual(languages, LOCALES);
});
