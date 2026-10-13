/**
 * Asignación de categoría, override y readiness con blockers visibles (S4.8a-4 §15-§17, §22, §23).
 *
 * Ejercen los helpers puros que usa la UI real (sin DOM, sin red, sin navegador) y comprueban, por
 * fuente, que el panel se cablea a los **contratos reales** del backend. Reglas que estos tests
 * protegen:
 *
 * - la asignación **normal** solo envía una categoría **compatible** que devolvió el motor (§15);
 * - con **varias** compatibles no se elige la primera: decide el organizador (§14/§23);
 * - el **override** (§16) exige un motivo del catálogo y nunca se envía en silencio; el flujo vive
 *   detrás del desplegable del organizador, así que un COLLABORATOR no lo ve como acción disponible;
 * - la revisión de cuadros muestra los **blockers de clasificación** agrupados por categoría y
 *   traducidos a lenguaje humano (§17);
 * - el endpoint de asignación **no** se llama sin una acción explícita del usuario (§22/§23).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
  CATEGORY_ASSIGNMENT_REASON_CODES,
  assignmentNeedsOverride,
  buildCategoryAssignment,
  compatibleCategoryKeys,
  isCompatibleChoice,
  overrideReasonError,
} from '../src/components/utils/f3_category_assignment.ts';
import { preselectedCategoryKey } from '../src/components/utils/f3_classification.ts';
import type { RegistrationClassification } from '../src/components/utils/f3_competitor_contract.ts';
import {
  BracketReadiness,
  classificationBlockedCategories,
  classificationBlockedCount,
  isClassificationReadinessBlocker,
} from '../src/components/utils/f3_contract.ts';

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8');
}

const PANEL = '../src/components/f3/registration_classification_panel.tsx';
const READINESS_PANEL = '../src/components/f3/readiness_panel.tsx';
const SERVICE = '../src/services/f3.ts';

/* --------------------------------------------------------------- clasificación de ejemplo (§15) */

const classification: RegistrationClassification = {
  compatible_categories: [
    {
      category_key: 'gi-76',
      category_label: 'Gi -76 kg',
      compatible: true,
      blockers: [],
      warnings: [],
    },
    {
      category_key: 'gi-82',
      category_label: 'Gi -82 kg',
      compatible: true,
      blockers: [],
      warnings: [],
    },
  ],
  recommended_category: null,
  blockers: [],
  warnings: [],
};

/* ------------------------------------------------------------------------------- §15 normal ----- */

test('la asignación normal solo envía una categoría compatible y sin override', () => {
  assert.deepEqual(compatibleCategoryKeys(classification), ['gi-76', 'gi-82']);
  assert.equal(isCompatibleChoice(classification, 'gi-76'), true);
  assert.equal(isCompatibleChoice(classification, 'gi-99'), false);
  assert.equal(isCompatibleChoice(classification, null), false);

  const built = buildCategoryAssignment({
    classification,
    category_key: 'gi-76',
    override: false,
  });
  assert.equal(built.ok, true);
  assert.ok(built.ok);
  assert.deepEqual(built.body, {
    category_key: 'gi-76',
    override: false,
    reason_code: null,
    reason_note: null,
  });
});

test('con varias compatibles no se elige la primera: nada preseleccionado (§14)', () => {
  assert.equal(preselectedCategoryKey(classification), null);
  const single: RegistrationClassification = {
    ...classification,
    compatible_categories: [classification.compatible_categories[0]],
  };
  assert.equal(preselectedCategoryKey(single), 'gi-76');
});

/* --------------------------------------------------------------------------------- §16 override -- */

test('nunca se envía una categoría no compatible sin override explícito', () => {
  assert.equal(assignmentNeedsOverride(classification, 'gi-99'), true);
  assert.equal(assignmentNeedsOverride(classification, 'gi-76'), false);
  assert.equal(assignmentNeedsOverride(classification, null), false);

  const built = buildCategoryAssignment({
    classification,
    category_key: 'gi-99',
    override: false,
  });
  assert.equal(built.ok, false);
  assert.ok(!built.ok);
  assert.equal(built.errorKey, 'classification_override_required');
});

test('el override exige un motivo válido del catálogo cerrado', () => {
  assert.ok(CATEGORY_ASSIGNMENT_REASON_CODES.includes('CATEGORY_CHANGE'));
  assert.equal(overrideReasonError(null), 'classification_override_reason_required');
  assert.equal(overrideReasonError(''), 'classification_override_reason_required');
  assert.equal(overrideReasonError('NO_EXISTE'), 'classification_override_reason_required');
  assert.equal(overrideReasonError('CATEGORY_CHANGE'), null);

  const withoutReason = buildCategoryAssignment({
    classification,
    category_key: 'gi-99',
    override: true,
  });
  assert.equal(withoutReason.ok, false);
  assert.ok(!withoutReason.ok);
  assert.equal(withoutReason.errorKey, 'classification_override_reason_required');

  const withReason = buildCategoryAssignment({
    classification,
    category_key: 'gi-99',
    override: true,
    reason_code: 'CATEGORY_CHANGE',
    reason_note: 'acuerdo del organizador',
  });
  assert.equal(withReason.ok, true);
  assert.ok(withReason.ok);
  assert.deepEqual(withReason.body, {
    category_key: 'gi-99',
    override: true,
    reason_code: 'CATEGORY_CHANGE',
    reason_note: 'acuerdo del organizador',
  });
});

test('sin categoría la asignación no se construye', () => {
  const built = buildCategoryAssignment({
    classification,
    category_key: null,
    override: false,
  });
  assert.equal(built.ok, false);
  assert.ok(!built.ok);
  assert.equal(built.errorKey, 'classification_assign_category_required');
});

/* --------------------------------------------------------------------------------- §17 readiness -- */

const readiness: BracketReadiness = {
  tournament_id: 7,
  ready: false,
  registration_window: 'CLOSED',
  summary: { total: 3, confirmed: 3, draft: 0, withdrawn: 0, disqualified: 0 },
  categories: [
    {
      category_key: null,
      category_label: null,
      confirmed_count: 1,
      required_minimum: 2,
      ready: false,
      blocking_issues: [
        { code: 'MISSING_WEIGHT', count: null, category_key: null, required_minimum: null },
      ],
    },
    {
      category_key: 'gi-76',
      category_label: 'Gi -76 kg',
      confirmed_count: 2,
      required_minimum: 2,
      ready: false,
      blocking_issues: [
        { code: 'CATEGORY_INCOMPATIBLE', count: 1, category_key: 'gi-76', required_minimum: null },
        {
          code: 'CATEGORY_NOT_ENOUGH_COMPETITORS',
          count: null,
          category_key: 'gi-76',
          required_minimum: 2,
        },
      ],
    },
  ],
  blocking_issues: [
    { code: 'REGISTRATION_WINDOW_OPEN', count: null, category_key: null, required_minimum: null },
    { code: 'MISSING_MODALITY', count: 2, category_key: null, required_minimum: null },
  ],
};

test('solo los códigos de clasificación se reconocen como tales (§17)', () => {
  for (const code of [
    'MISSING_DATE_OF_BIRTH',
    'MISSING_SEX',
    'MISSING_BELT',
    'MISSING_WEIGHT',
    'MISSING_MODALITY',
    'CATEGORY_NOT_RESOLVED',
    'CATEGORY_INCOMPATIBLE',
    'NO_COMPATIBLE_CATEGORY',
    'INVALID_CATEGORY_BELT_RANGE',
  ]) {
    assert.equal(isClassificationReadinessBlocker(code), true, `${code} debería ser de clasificación`);
  }
  assert.equal(isClassificationReadinessBlocker('CATEGORY_NOT_ENOUGH_COMPETITORS'), false);
  assert.equal(isClassificationReadinessBlocker('REGISTRATION_WINDOW_OPEN'), false);
});

test('los blockers de clasificación se agrupan por categoría y con su motivo (§17)', () => {
  const rows = classificationBlockedCategories(readiness);

  // Una fila a nivel torneo (sin categoría) y otra por la categoría afectada.
  assert.equal(rows.length, 2);

  const tournamentLevel = rows[0];
  assert.equal(tournamentLevel.category_key, null);
  assert.deepEqual(
    tournamentLevel.issues.map((issue) => issue.code),
    ['MISSING_MODALITY']
  );

  const categoryRow = rows[1];
  assert.equal(categoryRow.category_key, 'gi-76');
  assert.equal(categoryRow.category_label, 'Gi -76 kg');
  // El bloqueo no-clasificatorio (faltan competidores) NO entra aquí.
  assert.deepEqual(
    categoryRow.issues.map((issue) => issue.code),
    ['CATEGORY_INCOMPATIBLE']
  );

  // 2 (falta modalidad) + 1 (categoría incompatible).
  assert.equal(classificationBlockedCount(rows), 3);
});

test('sin bloqueos de clasificación la sección queda vacía', () => {
  const clean: BracketReadiness = {
    ...readiness,
    categories: readiness.categories.map((category) => ({ ...category, blocking_issues: [] })),
    blocking_issues: [
      { code: 'REGISTRATION_WINDOW_OPEN', count: null, category_key: null, required_minimum: null },
    ],
  };
  assert.deepEqual(classificationBlockedCategories(clean), []);
  assert.equal(classificationBlockedCount([]), 0);
});

/* --------------------------------------------------------------- contratos y anti-reglas (§23) -- */

test('el servicio asigna la categoría por la ruta real del backend (§15)', () => {
  const service = source(SERVICE);
  assert.match(service, /assignRegistrationCategory/);
  assert.match(service, /registrationCategoryUrl/);
  assert.match(service, /RegistrationCategoryBody/);
});

test('el panel no llama al endpoint sin una acción explícita del usuario (§22/§23)', () => {
  const panel = source(PANEL);
  // El panel no dispara nada al montarse: no hay efectos ni llamadas fuera de los manejadores.
  assert.doesNotMatch(panel, /useEffect/);
  assert.match(panel, /async function assignCategory/);
  // El único que construye el cuerpo es el helper puro: el panel no arma un override a mano.
  assert.match(panel, /buildCategoryAssignment\(/);
  // Las acciones se enganchan a eventos del usuario, no al render.
  assert.match(panel, /onClick=\{\(\) => \{/);
});

test('el override vive tras el desplegable del organizador, no como acción normal (§16)', () => {
  const panel = source(PANEL);
  assert.match(panel, /OrganizerOnly/);
  assert.match(panel, /OrganizerOnly[\s\S]*classification_override_button/);
  // El motivo es obligatorio: el modal de motivo recibe el catálogo cerrado y no lo inventa.
  assert.match(panel, /CATEGORY_ASSIGNMENT_REASON_CODES/);
  assert.match(panel, /classification_override_notice/);
});

test('el panel de readiness muestra los bloqueos de clasificación por categoría (§17)', () => {
  const panel = source(READINESS_PANEL);
  assert.match(panel, /classificationBlockedCategories/);
  assert.match(panel, /classificationBlockedCount/);
  assert.match(panel, /readiness_classification_blockers_title/);
  assert.match(panel, /readiness_classification_blockers_empty/);
});
