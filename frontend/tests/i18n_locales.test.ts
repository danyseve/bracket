/**
 * Comprobaciones reales de i18n del frontend (P2.8B F2C + P2.8C/F3C S4.1).
 *
 * No basta con que las claves estén en el JSON: la UI se sirve a través de i18next +
 * i18next-http-backend, y un namespace mal puesto o un locale cacheado hacen que el operador vea
 * la clave cruda (`generate_bracket_modal_title`) en pantalla. Estos tests resuelven cada clave
 * `t('...')` de los componentes con la MISMA configuración que usa la aplicación.
 *
 * S4.1 añade dos familias que no aparecen como literal en el código —se componen en tiempo de
 * ejecución (`registration_status_<estado>`, `readiness_blocker_<código>`, `f3_reason_<motivo>`…)—
 * y una comprobación de paridad sobre los 12 locales.
 */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { test } from 'node:test';

import { createInstance } from 'i18next';

import { i18nInitOptions, LOCALES_REVISION } from '../i18n_options.ts';
import { boardStageItems, bracketStateKey } from '../src/components/utils/bracket_view.ts';
import {
  COMPETITOR_CREATE_REASON_CODE,
  COMPETITOR_LIFECYCLE_REASON_CODES,
  COMPETITOR_RENAME_REASON_CODE,
  readinessBlockerKey,
  REGISTRATION_CREATE_REASON_CODE,
  REGISTRATION_REASON_CODES,
  REGISTRATION_STATUSES,
  registrationActionKey,
  registrationStatusKey,
  registrationWindowKey,
} from '../src/components/utils/f3_contract.ts';
import { tatamiStateKey } from '../src/components/utils/tatami_board.ts';

const LOCALES_DIR = new URL('../public/locales/', import.meta.url);

const COMPONENT_SOURCES = [
  '../src/components/modals/generate_bracket_modal.tsx',
  '../src/components/utils/generate_bracket.ts',
  '../src/components/builder/builder.tsx',
  // S4.1 — F3 core
  '../src/components/utils/f3_contract.ts',
  '../src/components/utils/f3_errors.ts',
  '../src/components/utils/f3_write.ts',
  '../src/components/utils/f3_write_hook.ts',
  '../src/components/utils/tatami_board.ts',
  '../src/components/utils/bracket_view.ts',
  '../src/components/f3/f3_error_alert.tsx',
  '../src/components/f3/status_badge.tsx',
  '../src/components/f3/organizer_only.tsx',
  '../src/components/f3/reason_modal.tsx',
  '../src/components/f3/registration_window_card.tsx',
  '../src/components/f3/readiness_panel.tsx',
  '../src/components/f3/tatami_board_view.tsx',
  '../src/components/f3/tatami_control_board.tsx',
  // S4.8a-4 — perfil deportivo del competidor y club
  '../src/components/utils/f3_sports_profile.ts',
  '../src/components/f3/belt_select.tsx',
  '../src/components/f3/sports_club_select.tsx',
  '../src/components/f3/sports_club_logo.tsx',
  '../src/components/f3/competitor_sports_profile.tsx',
  // S4.8g — alta de competidor en un solo flujo (dos pasos, con Club)
  '../src/components/f3/competitor_create_wizard.tsx',
  '../src/components/modals/competitor_modal.tsx',
  '../src/components/modals/registration_modal.tsx',
  // S4.8a-4 — peso, modalidad y clasificación del organizador
  '../src/components/utils/f3_classification.ts',
  '../src/components/f3/registration_classification_panel.tsx',
  '../src/pages/tournaments/[id]/competitors.tsx',
  '../src/pages/tournaments/[id]/registrations.tsx',
  '../src/pages/tournaments/[id]/brackets.tsx',
  '../src/pages/tournaments/[id]/tatamis.tsx',
  // S4.8d — integración de UI: catálogo de categorías, opciones de inscripción y clubes
  '../src/components/utils/f3_category_catalog.ts',
  '../src/components/utils/f3_registration_options.ts',
  '../src/pages/tournaments/[id]/clubs.tsx',
];

/** Los 12 idiomas que publica el proyecto. */
const LOCALES = ['de', 'el', 'en', 'es', 'fa', 'fr', 'it', 'ja', 'nl', 'pt', 'sv', 'zh'];

/** Prefijos de las claves introducidas en S4.1 (ninguno existía antes: no hay colisión). */
const S4_1_PREFIXES = [
  'f3_',
  'competitors_',
  'registrations_',
  'registration_window_',
  'registration_status_',
  'registration_action_',
  'readiness_',
  'bracket_state_',
  'brackets_',
  'tatami_',
  'tatamis_',
  'organizer_actions_',
];

type Locale = Record<string, string>;

function loadLocale(language: string): Locale {
  const url = new URL(`${language}/common.json`, LOCALES_DIR);
  return JSON.parse(readFileSync(url, 'utf8')) as Locale;
}

/** Mismo cálculo que documenta `i18n_options.ts`: sha256 de los locales, en orden de idioma. */
function localesHash(): string {
  const hash = createHash('sha256');
  for (const language of readdirSync(LOCALES_DIR).sort()) {
    const url = new URL(`${language}/common.json`, LOCALES_DIR);
    if (!existsSync(url)) {
      continue;
    }
    hash.update(`${language}/common.json\n`);
    hash.update(readFileSync(url, 'utf8'));
  }
  return hash.digest('hex').slice(0, 12);
}

/** Claves literales que los componentes piden a `t('...')`. */
function translationKeys(sourceRelativePath: string): string[] {
  const source = readFileSync(new URL(sourceRelativePath, import.meta.url), 'utf8');
  const keys = [...source.matchAll(/\bt\(\s*['"]([^'"]+)['"]/g)].map((match) => match[1]);
  return [...new Set(keys)];
}

/** Instancia i18next con la configuración de la app, pero con los locales ya cargados. */
async function translate(language: string) {
  const instance = createInstance();
  await instance.init({
    fallbackLng: i18nInitOptions.fallbackLng,
    defaultNS: i18nInitOptions.defaultNS,
    interpolation: i18nInitOptions.interpolation,
    lng: language,
    resources: { [language]: { common: loadLocale(language) } },
  });
  return instance;
}

test('los locales van versionados y el hash se recalcula al tocarlos', () => {
  assert.equal(
    LOCALES_REVISION,
    localesHash(),
    'LOCALES_REVISION no coincide con el hash de public/locales: actualiza la constante al cambiar un locale'
  );
  assert.ok(
    i18nInitOptions.backend.loadPath.includes(`?v=${LOCALES_REVISION}`),
    'loadPath debe versionar la URL de los locales para no servir un JSON cacheado'
  );
  assert.equal(i18nInitOptions.backend.requestOptions.cache, 'no-cache');
});

test('el namespace de la app es common y los recursos viven en common.json', () => {
  assert.equal(i18nInitOptions.defaultNS, 'common');
  assert.equal(i18nInitOptions.fallbackLng, 'en');
  assert.ok(existsSync(new URL('es/common.json', LOCALES_DIR)));
  assert.ok(existsSync(new URL('en/common.json', LOCALES_DIR)));
});

test('generate_bracket_modal.tsx usa el namespace por defecto (sin namespace explícito)', () => {
  const source = readFileSync(
    new URL('../src/components/modals/generate_bracket_modal.tsx', import.meta.url),
    'utf8'
  );

  assert.match(source, /useTranslation\(\s*\)/);
  assert.doesNotMatch(source, /useTranslation\(\s*['"]/);
});

for (const language of ['es', 'en']) {
  test(`${language}: ninguna clave t() de los componentes queda sin resolver`, async () => {
    const i18n = await translate(language);
    const keys = COMPONENT_SOURCES.flatMap((source) => translationKeys(source));

    assert.ok(keys.length >= 15, 'se esperaban al menos 15 claves en los componentes');

    for (const key of keys) {
      const value = String(i18n.t(key, { count: 6 }));

      assert.notEqual(value, key, `la clave ${key} no resuelve en ${language}`);
      assert.ok(
        !/^[a-z][a-z0-9_]*$/.test(value),
        `la clave ${key} muestra un identificador en ${language}: ${value}`
      );
    }
  });
}

test('es: el copy visible del modal y del resumen es texto humano', async () => {
  const i18n = await translate('es');

  assert.equal(i18n.t('generate_bracket_button'), 'Generar cuadro');
  assert.equal(i18n.t('generate_bracket_modal_title'), 'Generar cuadro');
  assert.equal(
    i18n.t('generate_bracket_modal_description'),
    'Se distribuirán los participantes en el cuadro y se aplicarán los pases directos necesarios.'
  );
  assert.equal(
    i18n.t('generate_bracket_current_participants', { count: 6 }),
    'Participantes actuales: 6'
  );
  assert.equal(i18n.t('generate_bracket_size', { count: 8 }), 'Tamaño del cuadro: 8');
  assert.equal(
    i18n.t('generate_bracket_expected_byes', { count: 2 }),
    'Pases directos previstos: 2'
  );
  assert.equal(i18n.t('regenerate_bracket_button'), 'Regenerar cuadro');
  assert.equal(
    i18n.t('regenerate_bracket_modal_warning'),
    'Esto cambiará la distribución actual del cuadro.'
  );
  assert.equal(i18n.t('generate_bracket_success_title'), 'Cuadro generado');
  assert.equal(i18n.t('generate_bracket_success_participants', { count: 6 }), '6 participantes');
  assert.equal(i18n.t('generate_bracket_success_size', { count: 8 }), 'Cuadro de 8');
  assert.equal(i18n.t('generate_bracket_success_byes', { count: 2 }), '2 pases directos');
  assert.equal(i18n.t('generate_bracket_success_empty_matchups', { count: 0 }), '0 cruces vacíos');
  assert.equal(
    i18n.t('generate_bracket_no_change'),
    'El cuadro ya estaba correcto: no ha cambiado nada.'
  );
  assert.equal(i18n.t('close_button'), 'Cerrar');
});

test('en: el copy visible también es texto humano', async () => {
  const i18n = await translate('en');

  assert.equal(i18n.t('generate_bracket_modal_title'), 'Generate bracket');
  assert.equal(i18n.t('generate_bracket_success_participants', { count: 6 }), '6 participants');
  assert.equal(
    i18n.t('generate_bracket_no_change'),
    'The bracket was already correct: nothing changed.'
  );
  assert.equal(i18n.t('close_button'), 'Close');
});

/* --------------------------------------------------------------------------------- S4.1 (F3) */

test('S4.1: las claves dinámicas de estado, acción, bloqueo y motivo resuelven en es', async () => {
  const i18n = await translate('es');

  const blockers = [
    'REGISTRATION_WINDOW_OPEN',
    'REGISTRATIONS_PENDING',
    'CATEGORY_NOT_ENOUGH_COMPETITORS',
    'COMPETITOR_INACTIVE',
    'REGISTRATION_INCONSISTENT',
    'INVALID_CATEGORY',
    'BRACKET_ALREADY_EXISTS',
  ];
  const reasonCodes = [
    ...Object.values(REGISTRATION_REASON_CODES).flat(),
    ...COMPETITOR_LIFECYCLE_REASON_CODES,
    COMPETITOR_CREATE_REASON_CODE,
    COMPETITOR_RENAME_REASON_CODE,
    REGISTRATION_CREATE_REASON_CODE,
  ];

  const dynamicKeys = [
    ...REGISTRATION_STATUSES.map(registrationStatusKey),
    registrationStatusKey('CORRECTED'),
    ...['OPEN', 'CLOSED', 'DEADLINE_PASSED'].map(registrationWindowKey),
    ...(['confirm', 'withdraw', 'reinstate', 'disqualify'] as const).map(registrationActionKey),
    ...blockers.map(readinessBlockerKey),
    ...reasonCodes.map((code) => `f3_reason_${code.toLowerCase()}`),
    ...(['not_generated', 'generated', 'running', 'finished'] as const).map(bracketStateKey),
    ...(['free', 'planned', 'partially_planned', 'running'] as const).map(tatamiStateKey),
  ];

  assert.ok(dynamicKeys.length >= 40, 'se esperaban las familias dinámicas completas');

  for (const key of dynamicKeys) {
    assert.notEqual(String(i18n.t(key)), key, `la clave dinámica ${key} no resuelve en es`);
  }
});

test('S4.1: los 12 locales tienen las claves nuevas, sin vacíos ni marcadores perdidos', () => {
  const reference = loadLocale('en');
  const keys = Object.keys(reference).filter((key) =>
    S4_1_PREFIXES.some((prefix) => key.startsWith(prefix))
  );

  assert.ok(keys.length >= 190, `se esperaban >=190 claves de S4.1, hay ${keys.length}`);

  const placeholders = (value: string) =>
    [...value.matchAll(/\{\{(\w+)\}\}/g)].map((match) => match[1]).sort();

  for (const language of LOCALES) {
    const locale = loadLocale(language);
    for (const key of keys) {
      const value = locale[key];
      assert.equal(typeof value, 'string', `${language}: falta la clave ${key} de S4.1`);
      assert.ok(value.trim().length > 0, `${language}: la clave ${key} está vacía`);
      assert.deepEqual(
        placeholders(value),
        placeholders(reference[key]),
        `${language}: los marcadores de ${key} no coinciden con el inglés`
      );
    }
  }
});

test('S4.1: el adaptador del cuadro se apoya en los tipos generados', () => {
  const source = readFileSync(
    new URL('../src/components/utils/bracket_view.ts', import.meta.url),
    'utf8'
  );

  // Solo tipos: el módulo no debe arrastrar dependencias de ejecución.
  assert.match(source, /import type \{ MatchWithDetails, StageWithStageItems \} from '@openapi'/);
  assert.equal(typeof boardStageItems, 'function');
});
