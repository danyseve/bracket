/**
 * Comprobaciones reales de i18n del frontend (P2.8B F2C).
 *
 * No basta con que las claves estén en el JSON: la UI se sirve a través de i18next +
 * i18next-http-backend, y un namespace mal puesto o un locale cacheado hacen que el operador vea
 * la clave cruda (`generate_bracket_modal_title`) en pantalla. Estos tests resuelven cada clave
 * `t('...')` de los componentes con la MISMA configuración que usa la aplicación.
 */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { test } from 'node:test';

import { createInstance } from 'i18next';

import { i18nInitOptions, LOCALES_REVISION } from '../i18n_options.ts';

const LOCALES_DIR = new URL('../public/locales/', import.meta.url);

const COMPONENT_SOURCES = [
  '../src/components/modals/generate_bracket_modal.tsx',
  '../src/components/utils/generate_bracket.ts',
  '../src/components/builder/builder.tsx',
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
