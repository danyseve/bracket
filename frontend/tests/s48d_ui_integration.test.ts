/**
 * Integración de UI de la fase S4.8d: competidores, inscripciones, categorías, clubes y cinturones.
 *
 * Estos tests protegen exactamente los fallos que se vieron en producción:
 *
 * 1. el desplegable de competidores pedía `limit: 200` y el backend responde 422 (`le=100`), así que
 *    la lista salía vacía;
 * 2. las categorías del formulario salían de `bracket-readiness`, que sólo publica las categorías
 *    que **ya tienen inscripciones**: un torneo recién creado mostraba cero categorías. La fuente
 *    correcta son los `stage_items` del torneo, con la clave contractual `si:<stage_item_id>`;
 * 3. el perfil deportivo no mostraba la edad (derivada) y no se ofrecía el catálogo del proyecto
 *    (Blanco · Azul · Violeta · Marrón · Negro);
 * 4. el logo no se podía tocar en ninguna pantalla de clubes.
 *
 * Igual que el resto de la suite: sin DOM y sin red. Los helpers puros se ejercen de verdad y los
 * cableados se comprueban por fuente.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
  categoryKeyFromStageItem,
  tournamentCategoryOptions,
} from '../src/components/utils/f3_category_catalog.ts';
import {
  classificationBlockerKeys,
  classificationState,
  classificationWarningKeys,
  modalityLabelKey,
  parseDeclaredWeightKg,
  preselectedCategoryKey,
} from '../src/components/utils/f3_classification.ts';
import type { RegistrationClassification } from '../src/components/utils/f3_competitor_contract.ts';
import { registrationClassificationPreviewUrl } from '../src/components/utils/f3_competitor_contract.ts';
import {
  affiliationLabel,
  competitorOptionLabel,
  competitorSummaryRows,
} from '../src/components/utils/f3_registration_options.ts';
import {
  beltOptions,
  COMPETITION_BELT_SCOPES,
  DERIVED_AGE_LABEL_KEY,
  derivedAge,
  profileBody,
} from '../src/components/utils/f3_sports_profile.ts';

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

function belt(id: number, code: string, rank: number, scope: 'ADULT' | 'JUVENILE') {
  return { id, code, label: code, rank, scope, active: true, created: '2026-01-01T00:00:00Z' };
}

/** Los 10 cinturones sembrados en producción: escala juvenil + escala adulto. */
const SEEDED_BELTS = [
  belt(1, 'WHITE', 1, 'JUVENILE'),
  belt(2, 'GREY', 2, 'JUVENILE'),
  belt(3, 'YELLOW', 3, 'JUVENILE'),
  belt(4, 'ORANGE', 4, 'JUVENILE'),
  belt(5, 'GREEN', 5, 'JUVENILE'),
  belt(6, 'WHITE', 1, 'ADULT'),
  belt(7, 'BLUE', 2, 'ADULT'),
  belt(8, 'PURPLE', 3, 'ADULT'),
  belt(9, 'BROWN', 4, 'ADULT'),
  belt(10, 'BLACK', 5, 'ADULT'),
];

const ES: Record<string, string> = {
  f3_belt_white: 'Blanco',
  f3_belt_blue: 'Azul',
  f3_belt_purple: 'Violeta',
  f3_belt_brown: 'Marrón',
  f3_belt_black: 'Negro',
};

function esLookup(key: string): string | null {
  return ES[key] ?? null;
}

function stage(id: number, items: { id: number; name: string | null }[]) {
  return {
    id,
    name: `Stage ${id}`,
    tournament_id: 1,
    is_active: true,
    created: '',
    stage_items: items,
  };
}

/* ------------------------------------------------------- 1. desplegable de competidores ------------- */

test('el desplegable de competidores usa limit=100 y ya no pide 200', () => {
  const page = source('../src/pages/tournaments/[id]/registrations.tsx');
  assert.ok(
    page.includes('limit: COMPETITOR_PAGE_SIZE'),
    'la consulta de competidores debe usar COMPETITOR_PAGE_SIZE'
  );
  assert.match(page, /const COMPETITOR_PAGE_SIZE = 100;/);
  assert.ok(
    !/limit:\s*200/.test(page),
    'ningún sitio debe pedir limit:200: el backend responde 422 (le=100)'
  );
});

/* ------------------------------------------------------- 2. catálogo de categorías ---------------- */

test('categoryKeyFromStageItem usa la clave contractual del motor', () => {
  assert.equal(categoryKeyFromStageItem(52), 'si:52');
  assert.equal(categoryKeyFromStageItem(1), 'si:1');
});

test('las categorías salen de los stage_items del torneo, en orden y con etiqueta', () => {
  const options = tournamentCategoryOptions([
    stage(19, [
      { id: 23, name: '-62 kg' },
      { id: 24, name: '-66 kg' },
    ]),
  ]);
  assert.deepEqual(options, [
    { category_key: 'si:23', label: '-62 kg' },
    { category_key: 'si:24', label: '-66 kg' },
  ]);
});

test('el catálogo ignora items sin nombre y claves repetidas', () => {
  const options = tournamentCategoryOptions([
    stage(1, [
      { id: 7, name: '  ' },
      { id: 8, name: null },
      { id: 9, name: 'Sub-18' },
    ]),
    stage(2, [
      { id: 9, name: 'Sub-18 (duplicado)' },
      { id: 10, name: 'Absoluto' },
    ]),
  ]);
  assert.deepEqual(options, [
    { category_key: 'si:9', label: 'Sub-18' },
    { category_key: 'si:10', label: 'Absoluto' },
  ]);
});

test('sin stages (o sin stage_items) el catálogo es vacío, no inventado', () => {
  assert.deepEqual(tournamentCategoryOptions(null), []);
  assert.deepEqual(tournamentCategoryOptions(undefined), []);
  assert.deepEqual(tournamentCategoryOptions([]), []);
  assert.deepEqual(tournamentCategoryOptions([stage(1, [])]), []);
});

test('el formulario de inscripción lee los stages del torneo, no la readiness', () => {
  const page = source('../src/pages/tournaments/[id]/registrations.tsx');
  assert.ok(page.includes('getStages(tournamentData.id)'), 'debe consultar los stages del torneo');
  assert.ok(
    page.includes('tournamentCategoryOptions('),
    'las categorías deben construirse con el catálogo de stage_items'
  );
  assert.ok(
    !/categoryOptions[\s\S]{0,400}swrReadiness\.data[\s\S]{0,200}\.categories/.test(page),
    'las categorías del alta ya no pueden salir de bracket-readiness'
  );
  assert.ok(page.includes('swrStages.mutate()'), 'el refresco debe recargar también los stages');
});

test('la modal envía la clave del catálogo y mantiene la etiqueta libre como respaldo', () => {
  const modal = source('../src/components/modals/registration_modal.tsx');
  assert.ok(modal.includes('category_key: hasCategoryCatalog'), 'la clave elegida viaja tal cual');
  assert.ok(
    modal.includes('categoryKeyFromLabel(values.category_label)'),
    'sin catálogo se conserva el camino de etiqueta libre'
  );
  assert.ok(
    modal.includes('const hasCategoryCatalog = categoryOptions.length > 0;'),
    'el camino depende de que el torneo tenga categorías declaradas'
  );
});

/* ------------------------------------------------------- 3. edad derivada (§3) --------------------- */

test('derivedAge calcula años cumplidos y no inventa edad', () => {
  assert.equal(derivedAge('1990-05-10', '2026-05-09'), 35);
  assert.equal(derivedAge('1990-05-10', '2026-05-10'), 36);
  assert.equal(derivedAge('2010-02-28', '2026-02-28'), 16);
  assert.equal(derivedAge('2026-10-13', '2026-10-13'), 0);
  assert.equal(derivedAge(null, '2026-10-13'), null);
  assert.equal(derivedAge('', '2026-10-13'), null);
  assert.equal(derivedAge('no-es-fecha', '2026-10-13'), null);
  assert.equal(derivedAge('2027-01-01', '2026-10-13'), null);
});

test('la edad es presentación: no viaja en el cuerpo del perfil', () => {
  const body = profileBody({
    date_of_birth: '1990-05-10',
    sex: 'MALE',
    belt_id: 7,
  });
  assert.deepEqual(Object.keys(body).sort(), [
    'belt_id',
    'date_of_birth',
    'reason_code',
    'reason_note',
    'sex',
  ]);
  assert.ok(!('age' in body), 'la edad nunca se persiste ni se envía');
});

test('el perfil muestra la edad derivada y etiqueta la fecha de nacimiento', () => {
  const profile = source('../src/components/f3/competitor_sports_profile.tsx');
  assert.ok(profile.includes('derivedAge(dateOfBirth, today)'), 'la edad se deriva al presentar');
  assert.ok(profile.includes('t(DERIVED_AGE_LABEL_KEY)'), 'la etiqueta sale de la clave i18n');
  assert.ok(profile.includes('f3_profile_age_value'), 'el valor usa la clave con {{age}}');
  assert.ok(profile.includes('f3_profile_age_note'), 'se explica que es calculada');
  assert.equal(DERIVED_AGE_LABEL_KEY, 'f3_profile_age_label');
});

/* ------------------------------------------------------- 4. cinturones (§4) ------------------------ */

test('el catálogo de competición ofrece las cinco escalas adulto, en orden de rank', () => {
  const options = beltOptions(SEEDED_BELTS, COMPETITION_BELT_SCOPES, esLookup);
  assert.deepEqual(
    options.map((option) => option.label),
    ['Blanco', 'Azul', 'Violeta', 'Marrón', 'Negro']
  );
  assert.deepEqual(
    options.map((option) => option.value),
    ['6', '7', '8', '9', '10']
  );
  assert.ok(
    options.every((option) => option.scope === 'ADULT'),
    'la escala juvenil no se ofrece en el perfil de competición'
  );
});

test('el perfil del competidor limita el selector a la escala adulto', () => {
  const profile = source('../src/components/f3/competitor_sports_profile.tsx');
  assert.ok(profile.includes('scopes={COMPETITION_BELT_SCOPES}'), 'el selector recibe la escala');
  const selector = source('../src/components/f3/belt_select.tsx');
  assert.ok(selector.includes('scopes = BELT_SCOPES'), 'por defecto el componente no cambia');
  assert.ok(
    selector.includes('beltOptions(belts, scopes, lookup)'),
    'el filtro se aplica de verdad'
  );
});

test('la etiqueta del cinturón violeta es «Violeta» en español', () => {
  assert.equal(loadLocale('es').f3_belt_purple, 'Violeta');
});

/* ------------------------------------------------------- 5. etiqueta del competidor (§6) ----------- */

test('la opción del competidor compone nombre · cinturón · edad sin inventar datos', () => {
  const context = {
    referenceDate: '2026-10-13',
    beltLabel: (beltId: number) => (beltId === 7 ? 'Azul' : null),
    ageLabel: (age: number) => `${age} años`,
  };
  assert.equal(
    competitorOptionLabel(
      { display_name: 'Ana', belt_id: 7, date_of_birth: '1990-05-10' },
      context
    ),
    'Ana · Azul · 36 años'
  );
  assert.equal(
    competitorOptionLabel({ display_name: 'Ana', belt_id: null, date_of_birth: null }, context),
    'Ana'
  );
  assert.equal(
    competitorOptionLabel({ display_name: 'Ana', belt_id: 99, date_of_birth: null }, context),
    'Ana'
  );
});

/* ------------------------------------------------------- 6. clubes y logo (§2/§5) -------------- */

test('existe una pantalla de clubes cableada a los endpoints reales', () => {
  const page = source('../src/pages/tournaments/[id]/clubs.tsx');
  assert.ok(
    page.includes('getSportsClubs(club_id, true)'),
    'la gestión lista el endpoint de clubes incluyendo los dados de baja'
  );
  assert.ok(page.includes('<SportsClubLogo'), 'reutiliza el control de logo existente');
  assert.ok(page.includes('canManage={organizerActions}'), 'la escritura va tras el gate de UI');
  assert.ok(page.includes('onChanged'), 'el listado se refresca tras cambiar el logo');
});

test('los clubes están enlazadas en la navegación del torneo', () => {
  const links = source('../src/components/navbar/_main_links.tsx');
  assert.ok(links.includes('/clubs'), 'sin enlace en el menú la pantalla no se descubre');
  assert.ok(links.includes("t('f3_clubs_title')"));
});

/* ------------------------------------------------------- 7. i18n ---------------------------------- */

test('las claves nuevas existen, no están vacías y mantienen sus marcadores', () => {
  const keys = [
    'f3_clubs_title',
    'f3_clubs_description',
    'f3_clubs_empty_title',
    'f3_clubs_empty_description',
    'f3_registration_preview_title',
    'f3_registration_preview_hint',
    'f3_profile_age_label',
    'f3_profile_age_value',
    'f3_profile_age_note',
    'f3_registration_category_catalog_note',
  ];
  const placeholders = (value: string) =>
    [...value.matchAll(/\{\{(\w+)\}\}/g)].map((match) => match[1]).sort();

  for (const language of LOCALES) {
    const locale = loadLocale(language);
    for (const key of keys) {
      const value = locale[key];
      assert.equal(typeof value, 'string', `${language}: falta ${key}`);
      assert.ok(value.trim() !== '', `${language}: ${key} está vacío`);
    }
    assert.deepEqual(
      placeholders(locale.f3_profile_age_value),
      ['age'],
      `${language}: f3_profile_age_value debe interpolar {{age}}`
    );
  }
});

test('los locales versionados siguen coincidiendo con su hash', async () => {
  const { LOCALES_REVISION } = (await import('../i18n_options.ts')) as {
    LOCALES_REVISION: string;
  };
  const { createHash } = await import('node:crypto');
  const { readdirSync } = await import('node:fs');
  const hash = createHash('sha256');
  for (const language of readdirSync(LOCALES_DIR).sort()) {
    const url = new URL(`${language}/common.json`, LOCALES_DIR);
    hash.update(`${language}/common.json\n`);
    hash.update(readFileSync(url, 'utf8'));
  }
  assert.equal(LOCALES_REVISION, hash.digest('hex').slice(0, 12));
});

/* ------------------------------------------------------- 8. afiliación del competidor (S4.8d §3) -- */

/** Traductores del resumen, como los inyecta la página real. */
const SUMMARY_CONTEXT = {
  referenceDate: '2026-10-13',
  beltLabel: (beltId: number) => (beltId === 7 ? 'Azul' : null),
  ageLabel: (age: number) => `${age} años`,
  sexLabel: (sex: 'MALE' | 'FEMALE') => (sex === 'FEMALE' ? 'Femenino' : 'Masculino'),
  independentLabel: 'Independiente',
};

test('la etiqueta del competidor añadel club real o «Independiente»', () => {
  const context = {
    referenceDate: '2026-10-13',
    beltLabel: () => 'Azul',
    ageLabel: (age: number) => `${age} años`,
    independentLabel: 'Independiente',
  };
  assert.equal(
    competitorOptionLabel(
      {
        display_name: 'Ana',
        belt_id: 7,
        date_of_birth: '1990-05-10',
        affiliation_mode: 'CLUB',
        sports_club_name: 'Club Norte',
      },
      context
    ),
    'Ana · Azul · 36 años · Club Norte'
  );
  assert.equal(
    competitorOptionLabel(
      { display_name: 'Ana', belt_id: null, date_of_birth: null, affiliation_mode: 'INDEPENDENT' },
      context
    ),
    'Ana · Independiente'
  );
});

test('un club sin nombre no se pinta: nada de clubes inventadas', () => {
  assert.equal(
    affiliationLabel({ affiliation_mode: 'CLUB', sports_club_name: '   ' }),
    null,
    'sin nombre no hay club que mostrar'
  );
  assert.equal(affiliationLabel({ affiliation_mode: 'CLUB', sports_club_name: null }), null);
  assert.equal(
    affiliationLabel({ affiliation_mode: 'INDEPENDENT' }),
    null,
    'sin etiqueta traducida no se inventa un texto'
  );
  assert.equal(
    affiliationLabel({ affiliation_mode: 'INDEPENDENT' }, { independentLabel: 'Independiente' }),
    'Independiente'
  );
});

test('el resumen del competidor lista fecha, edad, sexo, cinturón y afiliación', () => {
  const rows = competitorSummaryRows(
    {
      id: 7,
      display_name: 'Ana',
      belt_id: 7,
      date_of_birth: '1990-05-10',
      sex: 'FEMALE',
      affiliation_mode: 'CLUB',
      sports_club_name: 'Club Norte',
    },
    SUMMARY_CONTEXT
  );
  assert.deepEqual(rows, [
    { labelKey: 'f3_profile_dob_label', value: '1990-05-10' },
    { labelKey: 'f3_profile_age_label', value: '36 años' },
    { labelKey: 'f3_sex_label', value: 'Femenino' },
    { labelKey: 'f3_profile_belt_label', value: 'Azul' },
    { labelKey: 'f3_profile_club_label', value: 'Club Norte' },
  ]);
});

test('el resumen no inventa filas para los datos que no existen', () => {
  assert.deepEqual(
    competitorSummaryRows(
      { id: 1, display_name: 'Ana', belt_id: null, date_of_birth: null },
      SUMMARY_CONTEXT
    ),
    []
  );
  assert.deepEqual(
    competitorSummaryRows(
      {
        id: 1,
        display_name: 'Ana',
        belt_id: null,
        date_of_birth: null,
        affiliation_mode: 'INDEPENDENT',
      },
      SUMMARY_CONTEXT
    ),
    [{ labelKey: 'f3_profile_club_label', value: 'Independiente' }]
  );
});

test('la modal muestra el resumen y la página le inyecta los traductores', () => {
  const modal = source('../src/components/modals/registration_modal.tsx');
  assert.ok(modal.includes('competitorSummaryRows(selectedCompetitor, summaryContext)'));
  assert.ok(modal.includes("t('classification_competitor_title')"));

  const page = source('../src/pages/tournaments/[id]/registrations.tsx');
  assert.ok(
    page.includes('sexLabel: (sex) => t(sexLabelKey(sex))'),
    'el sexo se traduce con el vocabulario existente'
  );
  assert.ok(
    page.includes("independentLabel: t('f3_club_independent_option')"),
    '«Independiente» es una cadena traducida, no un literal'
  );
});

/* ------------------------------------------------------- 9. clasificación previa (S4.8d §4) ------ */

test('la previa usa la URL contractual y el mismo cuerpo que el alta', () => {
  assert.equal(
    registrationClassificationPreviewUrl(9),
    'tournaments/9/registrations/classification-preview'
  );
});

test('una única compatible se preselecciona; el estado lo dice el motor', () => {
  const single = {
    compatible_categories: [
      {
        category_key: 'si:23',
        category_label: '-62 kg',
        compatible: true,
        blockers: [],
        warnings: [],
      },
    ],
    recommended_category: {
      category_key: 'si:23',
      category_label: '-62 kg',
      compatible: true,
      blockers: [],
      warnings: [],
    },
    blockers: [],
    warnings: [],
  };
  assert.deepEqual(classificationState(single), {
    kind: 'single',
    category: single.compatible_categories[0],
  });
  assert.equal(preselectedCategoryKey(single), 'si:23');
});

test('con varias compatibles no se elige por el organizador', () => {
  const multiple: RegistrationClassification = {
    compatible_categories: [
      {
        category_key: 'si:23',
        category_label: '-62 kg',
        compatible: true,
        blockers: [],
        warnings: [],
      },
      {
        category_key: 'si:24',
        category_label: 'Absoluto',
        compatible: true,
        blockers: [],
        warnings: [],
      },
    ],
    recommended_category: null,
    blockers: [],
    warnings: ['MULTIPLE_COMPATIBLE_CATEGORIES'],
  };
  assert.equal(classificationState(multiple).kind, 'multiple');
  assert.equal(preselectedCategoryKey(multiple), null, 'con varias no hay preselección');
  assert.deepEqual(classificationWarningKeys(multiple.warnings), [
    'classification_warning_multiple_compatible_categories',
  ]);
});

test('sin compatibles el estado nombra los bloqueos del motor', () => {
  const none: RegistrationClassification = {
    compatible_categories: [],
    recommended_category: null,
    blockers: ['MISSING_WEIGHT', 'NO_COMPATIBLE_CATEGORY'],
    warnings: [],
  };
  assert.deepEqual(classificationState(none), {
    kind: 'unclassifiable',
    blockerKeys: [
      'classification_blocker_missing_weight',
      'classification_blocker_no_compatible_category',
    ],
  });
  assert.equal(preselectedCategoryKey(none), null);
});

test('los avisos del motor tienen clave propia y no bloquean', () => {
  assert.deepEqual(classificationBlockerKeys(['MISSING_SEX']), [
    'classification_blocker_missing_sex',
  ]);
  assert.deepEqual(classificationWarningKeys(['CATEGORY_RULES_PARTIAL']), [
    'classification_warning_category_rules_partial',
  ]);
  assert.notDeepEqual(
    classificationWarningKeys(['CATEGORY_RULES_PARTIAL']),
    classificationBlockerKeys(['CATEGORY_RULES_PARTIAL'])
  );
});

test('la modal consulta la previa, pinta el veredicto y no decide compatibilidad', () => {
  const modal = source('../src/components/modals/registration_modal.tsx');
  assert.ok(
    modal.includes('const loader = loadPreviewRef.current;') && modal.includes('void loader({'),
    'la previa se pide con el lector vigente del ref al cambiar competidor/peso/modalidad'
  );
  assert.ok(modal.includes('competitor_id: Number(competitorId)'));
  assert.ok(modal.includes('declared_weight_kg: parseDeclaredWeightKg(weightInput)'));
  assert.ok(modal.includes("modality: modalityInput === '' ? null : (modalityInput as Modality)"));
  assert.ok(modal.includes('classificationState(classification)'), 'el estado sale del veredicto');
  assert.ok(
    modal.includes('preselectedCategoryKey(outcome.data)'),
    'la única compatible se preselecciona'
  );
  assert.ok(modal.includes('classificationWarningKeys(classification.warnings)'));
  assert.ok(
    modal.includes("t('classification_blocker_no_compatible_category')"),
    'sin compatibles se dice NO_COMPATIBLE_CATEGORY'
  );
  assert.ok(
    modal.includes('disabled={inflight || previewBlocks}'),
    'sin compatibles no se confirma'
  );
  assert.ok(modal.includes("t('classification_recommended_badge')"), 'la recomendada se marca');

  const page = source('../src/pages/tournaments/[id]/registrations.tsx');
  assert.ok(
    page.includes('previewRegistrationClassification(tournamentData.id, body)'),
    'la página usa el servicio, no una regla propia'
  );
});

test('peso y modalidad viajan tal cual los valida el formulario', () => {
  assert.equal(parseDeclaredWeightKg('74,5'), '74.5');
  assert.equal(parseDeclaredWeightKg(' 74.5 '), '74.5');
  assert.equal(parseDeclaredWeightKg(''), null);
  assert.equal(parseDeclaredWeightKg('0'), null);
  assert.equal(parseDeclaredWeightKg('301'), null);
  assert.equal(parseDeclaredWeightKg('setenta'), null);
  assert.equal(modalityLabelKey('GI'), 'f3_registration_modality_gi');
  assert.equal(modalityLabelKey('NO_GI'), 'f3_registration_modality_no_gi');
  assert.equal(modalityLabelKey(null), null, 'sin modalidad no se inventa una');
});
