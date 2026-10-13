/**
 * Peso declarado, modalidad y clasificación mostrada al organizador (S4.8a-4 §8-§14, §18).
 *
 * Ejercen los helpers puros que usa la UI real (sin DOM, sin red, sin navegador) y comprueban, por
 * fuente, que el panel se cablea a los **contratos reales** del backend:
 * `GET tournaments/{t}/registrations/{r}/classification` y la inscripción (`declared_weight_kg`,
 * `modality`). Reglas que estos tests protegen:
 *
 * - el motor de clasificación es la **autoridad**: el frontend solo traduce códigos, nunca reproduce
 *   reglas de categoría ni deriva compatibilidad;
 * - una única compatible se preselecciona; con **varias** no se elige la primera (§14);
 * - un aviso **no** es un error bloqueante (§13) y un bloqueo siempre se pinta como texto humano,
 *   nunca como código crudo salvo el fallback técnico (§12);
 * - la UI no impone campos que el backend no exige (§18: peso y modalidad son opcionales).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
  MODALITY_KEYS,
  WEIGHT_MAX_KG,
  classificationBlockerKeys,
  classificationState,
  classificationWarningKeys,
  modalityLabelKey,
  parseDeclaredWeightKg,
  preselectedCategoryKey,
  weightInputError,
} from '../src/components/utils/f3_classification.ts';
import type {
  ClassificationBlocker,
  ClassificationCategoryVerdict,
  ClassificationWarning,
  RegistrationClassification,
} from '../src/components/utils/f3_competitor_contract.ts';
import {
  CLASSIFICATION_BLOCKER_FALLBACK_KEY,
  CLASSIFICATION_WARNING_FALLBACK_KEY,
  MODALITIES,
  classificationBlockerKey,
  classificationWarningKey,
} from '../src/components/utils/f3_competitor_contract.ts';

const LOCALES_DIR = new URL('../public/locales/', import.meta.url);
const LOCALES = ['de', 'el', 'en', 'es', 'fa', 'fr', 'it', 'ja', 'nl', 'pt', 'sv', 'zh'];

function source(relative: string): string {
  return readFileSync(new URL(relative, import.meta.url), 'utf8');
}

function loadLocale(language: string): Record<string, string> {
  return JSON.parse(
    readFileSync(new URL(`${language}/common.json`, LOCALES_DIR), 'utf8')
  ) as Record<string, string>;
}

/** Veredicto del motor para una categoría, con los valores por defecto que el backend serializa. */
function verdict(
  category_key: string,
  category_label: string,
  extra: {
    compatible?: boolean;
    blockers?: ClassificationBlocker[];
    warnings?: ClassificationWarning[];
  } = {}
): ClassificationCategoryVerdict {
  return {
    category_key,
    category_label,
    compatible: extra.compatible ?? true,
    blockers: extra.blockers ?? [],
    warnings: extra.warnings ?? [],
  };
}

/* ------------------------------------------------------------------- REGISTRATION · peso (§8) */

test('el peso declarado acepta decimales dentro del rango real del backend (>0 y <=300 kg)', () => {
  assert.equal(WEIGHT_MAX_KG, 300);
  assert.equal(parseDeclaredWeightKg('62'), '62');
  assert.equal(parseDeclaredWeightKg('62.5'), '62.5');
  assert.equal(parseDeclaredWeightKg('62.50'), '62.50');
  // La coma decimal es natural en español; se normaliza a punto antes de enviarlo.
  assert.equal(parseDeclaredWeightKg('62,5'), '62.5');
  assert.equal(parseDeclaredWeightKg('  71.25  '), '71.25');
  // Límites inclusivos: 0 no vale (el backend exige >0) y 300 sí.
  assert.equal(parseDeclaredWeightKg('0.5'), '0.5');
  assert.equal(parseDeclaredWeightKg('300'), '300');
});

test('el peso declarado rechaza vacío, cero, negativo, no numérico y fuera de rango', () => {
  for (const invalid of ['', '   ', '0', '0.0', '-3', '-0.5', 'abc', '72 kg', '300.01', '999']) {
    assert.equal(parseDeclaredWeightKg(invalid), null, `debería rechazar "${invalid}"`);
  }
});

test('el error de UX del peso no impone la obligatoriedad que el backend no exige (§18)', () => {
  // Sin declarar es un estado legítimo: el motor decide con su blocker MISSING_WEIGHT.
  assert.equal(weightInputError(''), null);
  assert.equal(weightInputError('   '), null);
  // Solo cuando el flujo lo pida explícitamente (required) la falta es un error de UX.
  assert.equal(weightInputError('', true), 'f3_registration_weight_required');
  assert.equal(weightInputError('abc'), 'f3_registration_weight_invalid');
  assert.equal(weightInputError('300.01'), 'f3_registration_weight_invalid');
  assert.equal(weightInputError('62.5'), null);
});

test('el alta de inscripción envía declared_weight_kg (campo "Peso (kg)")', () => {
  const modal = source('../src/components/modals/registration_modal.tsx');
  assert.match(modal, /declared_weight_kg/);
  assert.match(modal, /f3_registration_weight_label/);
});

/* -------------------------------------------------------------- REGISTRATION · modalidad (§9) */

test('la modalidad es GI|NO_GI: no existe GI_AND_NO_GI', () => {
  assert.deepEqual(MODALITIES, ['GI', 'NO_GI']);
  assert.ok(!MODALITIES.includes('GI_AND_NO_GI' as never));
  assert.equal(MODALITY_KEYS.GI, 'f3_registration_modality_gi');
  assert.equal(MODALITY_KEYS.NO_GI, 'f3_registration_modality_no_gi');
});

test('una modalidad sin declarar no se fuerza a un valor inventado (§9/§18)', () => {
  assert.equal(modalityLabelKey(null), null);
  assert.equal(modalityLabelKey('GI'), 'f3_registration_modality_gi');
  assert.equal(modalityLabelKey('NO_GI'), 'f3_registration_modality_no_gi');
});

test('el alta de inscripción envía modality', () => {
  const modal = source('../src/components/modals/registration_modal.tsx');
  assert.match(modal, /modality/);
  assert.match(modal, /f3_registration_modality_label/);
});

/* --------------------------------------------------------- CLASSIFICATION · única (§10/§14) */

test('con una sola categoría compatible se preselecciona esa categoría (§14)', () => {
  const only = verdict('si:5', 'Gi -76 kg');
  const state = classificationState({
    compatible_categories: [only],
    recommended_category: only,
    blockers: [],
    warnings: [],
  });
  assert.equal(state.kind, 'single');
  if (state.kind === 'single') {
    assert.equal(state.category.category_key, 'si:5');
  }
  assert.equal(preselectedCategoryKey({ compatible_categories: [only] }), 'si:5');
});

/* ----------------------------------------------------- CLASSIFICATION · múltiples (§13/§14) */

test('con varias compatibles NO se elige la primera y se avisa (§14)', () => {
  const a = verdict('si:5', 'Gi -76 kg');
  const b = verdict('si:6', 'Gi -82 kg');
  const classification: RegistrationClassification = {
    compatible_categories: [a, b],
    recommended_category: null,
    blockers: [],
    warnings: ['MULTIPLE_COMPATIBLE_CATEGORIES'],
  };
  const state = classificationState(classification);
  assert.equal(state.kind, 'multiple');
  if (state.kind === 'multiple') {
    assert.equal(state.categories.length, 2);
  }
  // El backend no recomienda ninguna: la UI no inventa una selección.
  assert.equal(preselectedCategoryKey(classification), null);
  assert.deepEqual(classificationWarningKeys(classification.warnings), [
    'classification_warning_multiple_compatible_categories',
  ]);
  // Un aviso no es un bloqueo.
  assert.deepEqual(classificationBlockerKeys(classification.blockers), []);
});

/* -------------------------------------------------- CLASSIFICATION · ninguna (§11/§12) */

test('sin ninguna categoría compatible el estado es "no se puede clasificar"', () => {
  const state = classificationState({
    compatible_categories: [],
    recommended_category: null,
    blockers: ['NO_COMPATIBLE_CATEGORY'],
    warnings: [],
  });
  assert.equal(state.kind, 'unclassifiable');
  if (state.kind === 'unclassifiable') {
    assert.deepEqual(state.blockerKeys, ['classification_blocker_no_compatible_category']);
  }
});

/* ------------------------------------------------ CLASSIFICATION · sin datos (§11/§12) */

test('los datos que faltan se listan como texto humano, nunca como código crudo', () => {
  const keys = classificationBlockerKeys(['MISSING_WEIGHT', 'MISSING_BELT', 'MISSING_MODALITY']);
  assert.deepEqual(keys, [
    'classification_blocker_missing_weight',
    'classification_blocker_missing_belt',
    'classification_blocker_missing_modality',
  ]);
  const state = classificationState({
    compatible_categories: [],
    recommended_category: null,
    blockers: ['MISSING_WEIGHT'],
    warnings: [],
  });
  assert.equal(state.kind, 'unclassifiable');
  if (state.kind === 'unclassifiable') {
    assert.deepEqual(state.blockerKeys, ['classification_blocker_missing_weight']);
  }
});

test('un código desconocido (motor futuro) cae en el fallback técnico, no en una clave cruda', () => {
  assert.equal(classificationBlockerKey('CODIGO_FUTURO'), CLASSIFICATION_BLOCKER_FALLBACK_KEY);
  assert.deepEqual(classificationBlockerKeys(['CODIGO_FUTURO']), [
    CLASSIFICATION_BLOCKER_FALLBACK_KEY,
  ]);
  assert.equal(classificationWarningKey('AVISO_FUTURO'), CLASSIFICATION_WARNING_FALLBACK_KEY);
  assert.deepEqual(classificationWarningKeys(['AVISO_FUTURO']), [
    CLASSIFICATION_WARNING_FALLBACK_KEY,
  ]);
});

/* --------------------------------------------------------------------------------- i18n */

test('es: los textos exigidos por la especificación son texto humano', () => {
  const es = loadLocale('es');
  assert.equal(
    es['classification_unclassifiable_title'],
    'No podemos clasificar todavía esta inscripción.'
  );
  assert.equal(
    es['classification_warning_multiple_compatible_categories'],
    'Hay varias categorías compatibles. Selecciona una.'
  );
  for (const code of ['MISSING_WEIGHT', 'MISSING_BELT', 'MISSING_MODALITY']) {
    const key = classificationBlockerKey(code);
    assert.ok(typeof es[key] === 'string' && es[key].trim() !== '', `falta ${key} en es`);
    assert.notEqual(es[key], key);
  }
});

test('los 12 locales tienen todas las claves classification_* con la misma forma', () => {
  const reference = loadLocale('en');
  const keys = Object.keys(reference).filter((key) => key.startsWith('classification_'));
  assert.ok(keys.length >= 20, `se esperaban >=20 claves classification_*, hay ${keys.length}`);

  const placeholders = (value: string) =>
    [...value.matchAll(/\{\{(\w+)\}\}/g)].map((match) => match[1]).sort();

  for (const language of LOCALES) {
    const locale = loadLocale(language);
    for (const key of keys) {
      const value = locale[key];
      assert.equal(typeof value, 'string', `${language}: falta la clave ${key}`);
      assert.ok(value.trim().length > 0, `${language}: la clave ${key} está vacía`);
      assert.deepEqual(
        placeholders(value),
        placeholders(reference[key]),
        `${language}: los marcadores de ${key} no coinciden con el inglés`
      );
    }
  }
});

/* --------------------------------------------------------------- contratos y anti-reglas */

test('el servicio lee la clasificación y la inscripción por sus rutas reales', () => {
  const service = source('../src/services/f3.ts');
  assert.match(service, /getRegistrationClassification/);
  assert.match(service, /getRegistration\b/);
  assert.match(service, /registrationClassificationUrl/);
});

test('el frontend NO reproduce las reglas de clasificación: solo traduce códigos', () => {
  const module = source('../src/components/utils/f3_classification.ts');
  // Nada de cotas de reglas de categoría (eso es el motor del backend).
  assert.doesNotMatch(module, /belt_min|belt_max|age_min|age_max|weight_min|weight_max/);
  // La compatibilidad de una categoría y la recomendación las decide el backend; el módulo no las
  // calcula a partir de los datos del competidor.
  assert.doesNotMatch(module, /category_key\s*===/);
  const panel = source('../src/components/f3/registration_classification_panel.tsx');
  // El código crudo de un bloqueo no se pinta: se traduce siempre con la clave humana.
  assert.match(panel, /classificationBlockerKeys|classificationBlockerKey/);
});

test('el panel muestra la ficha del competidor y los datos declarados de la inscripción (§11)', () => {
  const panel = source('../src/components/f3/registration_classification_panel.tsx');
  assert.match(panel, /competitor_name_snapshot/);
  assert.match(panel, /declared_weight_kg/);
  assert.match(panel, /modality/);
  assert.match(panel, /classification_unclassifiable_title/);
});
