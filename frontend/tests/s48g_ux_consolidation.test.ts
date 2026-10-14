/**
 * P2.8C/F3C-S4.8g — consolidación de UX del dominio BJJ (LAB FIRST, sin despliegue de producción).
 *
 * La fase no toca el motor legacy ni la base de datos: cambia qué superficies ve el organizador.
 * Estos tests fijan las decisiones para que no se deshagan solas:
 *
 *  - el menú del torneo enseña el flujo BJJ y esconde jugadores/equipos tras «avanzado» (§3);
 *  - `Club / Academia` es el término deportivo visible y las organizaciones son otra pantalla (§6/§9);
 *  - la ruta `/tournaments/:id/academies` está registrada y ya no cae en el 404 (§7);
 *  - el alta es un solo flujo con datos deportivos y Club / Academia, con edad derivada (§4/§5/§10);
 *  - el alta usa las APIs existentes (crear + afiliar) sin inventar endpoints (§15);
 *  - el éxito parcial se dice y se puede completar, no se oculta (§5).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { createInstance } from 'i18next';

import { i18nInitOptions } from '../i18n_options.ts';
import type { Belt } from '../src/components/utils/f3_competitor_contract.ts';
import { BELT_SCOPES, COMPETITOR_SEXES } from '../src/components/utils/f3_competitor_contract.ts';
import {
  COMPETITION_BELT_SCOPES,
  SPORTS_CLUB_INDEPENDENT,
  affiliationAction,
  affiliationSportsClubId,
  beltLabelForId,
  competitorCreateBody,
  derivedAge,
  sexOptions,
} from '../src/components/utils/f3_sports_profile.ts';

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8');
}

const NAV = source('../src/components/navbar/_main_links.tsx');
const MAIN = source('../src/main.tsx');
const COMPETITORS_PAGE = source('../src/pages/tournaments/[id]/competitors.tsx');
const WIZARD = source('../src/components/f3/competitor_create_wizard.tsx');
const ACADEMIES_PAGE = source('../src/pages/tournaments/[id]/academies.tsx');
const CLUBS_PAGE = source('../src/pages/clubs.tsx');

/** Bloque `TournamentLinks` completo (el menú del torneo, no el superior). */
const TOURNAMENT_LINKS = NAV.slice(NAV.indexOf('export function TournamentLinks'));

/** Array principal del menú: el que se pinta siempre, sin el bloque condicional de avanzado. */
const MAIN_MENU = TOURNAMENT_LINKS.slice(0, TOURNAMENT_LINKS.indexOf('if (advancedOpen)'));
const ADVANCED_MENU = TOURNAMENT_LINKS.slice(TOURNAMENT_LINKS.indexOf('if (advancedOpen)'));

async function translate(language: string) {
  const instance = createInstance();
  await instance.init({
    fallbackLng: i18nInitOptions.fallbackLng,
    defaultNS: i18nInitOptions.defaultNS,
    interpolation: i18nInitOptions.interpolation,
    lng: language,
    resources: {
      [language]: {
        common: JSON.parse(
          readFileSync(
            new URL(`../public/locales/${language}/common.json`, import.meta.url),
            'utf8'
          )
        ),
      },
    },
  });
  return instance;
}

/* ------------------------------------------------------------ §3 · navegación del torneo */

test('S4.8g: el menú normal no enseña jugadores ni equipos y los deja tras «avanzado»', () => {
  assert.ok(MAIN_MENU.length > 0, 'no se encontró el array principal del menú del torneo');

  // El flujo recomendado no incluye ninguna de las dos listas legacy…
  assert.doesNotMatch(MAIN_MENU, /\/players/, 'jugadores no debe estar en el menú normal');
  assert.doesNotMatch(MAIN_MENU, /\/teams/, 'equipos no debe estar en el menú normal');
  // …pero las rutas siguen existiendo (no se rompen los enlaces directos).
  assert.match(ADVANCED_MENU, /\/players/, 'el enlace legacy de jugadores se conserva');
  assert.match(ADVANCED_MENU, /\/teams/, 'el enlace legacy de equipos se conserva');
  assert.match(
    TOURNAMENT_LINKS,
    /advancedOpen/,
    'las secciones legacy van detrás de un conmutador'
  );
  assert.match(TOURNAMENT_LINKS, /f3_nav_legacy_section/, 'la sección legacy se rotula como tal');
  assert.match(TOURNAMENT_LINKS, /f3_nav_legacy_badge/, 'cada enlace legacy lleva su distintivo');
});

test('S4.8g: el menú normal enseña el flujo BJJ en el orden acordado', () => {
  const labels = [...MAIN_MENU.matchAll(/t\('([a-z0-9_]+)'\)/g)].map((match) => match[1]);

  assert.deepEqual(
    labels,
    [
      'stage_title',
      'competitors_title',
      'f3_academies_title',
      'registrations_title',
      'brackets_title',
      'tatamis_title',
      'planning_title',
      'results_title',
      'rankings_title',
      'tournament_setting_title',
    ],
    'el menú del torneo cambió de orden o de contenido'
  );

  const hrefs = [...MAIN_MENU.matchAll(/link: `\$\{tm_prefix\}([^`]*)`/g)].map((match) => match[1]);
  assert.deepEqual(hrefs, [
    '/stages',
    '/competitors',
    '/academies',
    '/registrations',
    '/brackets',
    '/tatamis',
    '/schedule',
    '/results',
    '/rankings',
    '/settings',
  ]);
});

/* --------------------------------------------------------- §7 · ruta Club / Academia registrada */

test('S4.8g: la ruta de Club / Academia está registrada y no cae en el 404', () => {
  assert.match(MAIN, /import AcademiesPage from '\.\/pages\/tournaments\/\[id\]\/academies';/);
  assert.match(MAIN, /<Route path="academies" element=\{<AcademiesPage \/>\} \/>/);

  // La ruta vive dentro del torneo y antes del comodín: nada la eclipsa.
  const academiesIndex = MAIN.indexOf('path="academies"');
  const tournamentIndex = MAIN.indexOf('path=":id"');
  const notFoundIndex = MAIN.indexOf('path="*"');
  assert.ok(
    tournamentIndex >= 0 && academiesIndex > tournamentIndex,
    'academies va dentro del torneo'
  );
  assert.ok(notFoundIndex < 0 || academiesIndex < notFoundIndex, 'el comodín no debe taparla');

  // Y el enlace del menú apunta exactamente ahí.
  assert.match(TOURNAMENT_LINKS, /link: `\$\{tm_prefix\}\/academies`/);

  // No es un marcador de posición: la pantalla existente lista y gestiona el logo del tenant.
  assert.match(ACADEMIES_PAGE, /getSportsClubs\(club_id/);
  assert.match(ACADEMIES_PAGE, /SportsClubLogo/);
});

/* --------------------------------------------- §6/§9 · Club / Academia frente a organizaciones */

test('S4.8g: la UI deportiva dice Club / Academia y no vocabulario técnico', async () => {
  assert.match(TOURNAMENT_LINKS, /t\('f3_academies_title'\)/);

  // El vocabulario técnico del dominio (SportsClub, tenant, managed_by…) no se enseña como copy: los
  // símbolos del código sí pueden nombrarlo (`getSportsClubs`), la interfaz no.
  for (const [name, text] of [
    ['menú', NAV],
    ['competidores', COMPETITORS_PAGE],
    ['alta', WIZARD],
  ] as const) {
    assert.doesNotMatch(
      text,
      /t\('(sports_club|tenant_club|managed_by_club)[a-z_]*'\)/,
      `${name}: no debe haber claves de copy técnicas`
    );
  }

  const es = await translate('es');
  const en = await translate('en');
  for (const key of [
    'f3_academies_title',
    'f3_profile_club_label',
    'f3_profile_club_description',
    'f3_academies_description',
  ]) {
    for (const text of [String(es.t(key)), String(en.t(key))]) {
      assert.doesNotMatch(
        text,
        /SportsClub|tenant|managed_by/,
        `${key} no debe ser vocabulario técnico`
      );
    }
  }
});

test('S4.8g: la pantalla de organizaciones no se llama «clubes»', async () => {
  assert.match(CLUBS_PAGE, /t\('f3_organizations_title'\)/);
  assert.doesNotMatch(CLUBS_PAGE, /t\('clubs_title'\)/);

  const es = await translate('es');
  const en = await translate('en');
  assert.equal(es.t('f3_organizations_title'), 'Mis organizaciones');
  assert.equal(en.t('f3_organizations_title'), 'My organizations');
  assert.equal(es.t('f3_academies_title'), 'Club / Academia');
  assert.notEqual(es.t('f3_academies_title'), es.t('f3_organizations_title'));
});

/* --------------------------------------------------- §4/§5 · alta de competidor en un solo flujo */

test('S4.8g: el alta pide datos deportivos y Club / Academia en un flujo de dos pasos', () => {
  for (const key of [
    'f3_competitor_name_label',
    'f3_profile_dob_label',
    'f3_sex_label',
    'f3_competitor_create_step1_title',
    'f3_competitor_create_step2_title',
    'f3_competitor_create_step_indicator',
    'f3_competitor_create_next',
    'f3_competitor_create_back',
    'f3_competitor_create_club_note',
  ]) {
    assert.match(WIZARD, new RegExp(`t\\('${key}'`), `falta ${key} en el alta`);
  }

  assert.match(WIZARD, /BeltSelect/, 'el alta pide el cinturón');
  assert.match(WIZARD, /SportsClubSelect/, 'el alta pide el Club / Academia');
  assert.match(WIZARD, /COMPETITION_BELT_SCOPES/, 'el alta ofrece la escala adulto');
  assert.match(WIZARD, /data-create-step=\{step\}/, 'el paso es observable');

  // Un solo flujo: paso 1 → paso 2 y vuelta atrás, sin cerrar para completar el perfil a mano.
  assert.match(WIZARD, /setStep\(2\)/);
  assert.match(WIZARD, /setStep\(1\)/);
  assert.match(COMPETITORS_PAGE, /<CompetitorCreateWizard/);
  assert.doesNotMatch(
    COMPETITORS_PAGE,
    /setProfileTarget\(outcome\.value\.data\)/,
    'el alta ya no encadena el modal de perfil'
  );
  assert.match(
    COMPETITORS_PAGE,
    /setProfileTarget\(\{\s*id: created\.id/,
    'el perfil se abre solo cuando queda un paso pendiente'
  );
});

test('S4.8g: la edad se deriva y no se persiste', () => {
  assert.match(WIZARD, /derivedAge\(/, 'la edad se calcula a partir de la fecha de nacimiento');
  assert.match(WIZARD, /f3_profile_age_value/);
  assert.match(WIZARD, /data-derived-age/);

  const dto = competitorCreateBody(
    { display_name: ' Ana ', date_of_birth: '2010-05-04', sex: 'FEMALE', belt_id: 7 },
    'COMPETITOR_CREATE'
  );
  assert.deepEqual(dto, {
    display_name: 'Ana',
    date_of_birth: '2010-05-04',
    sex: 'FEMALE',
    belt_id: 7,
    reason_code: 'COMPETITOR_CREATE',
    reason_note: null,
  });
  assert.ok(!('age' in dto), 'la edad no viaja al backend');
  assert.ok(!('sports_club_id' in dto), 'la afiliación no viaja en el alta');
  assert.equal(derivedAge('2010-05-04', '2026-10-14'), 16);
  assert.equal(derivedAge(null, '2026-10-14'), null);
});

test('S4.8g: la escala del flujo es la adulto y el catálogo juvenil no se borra', () => {
  assert.deepEqual(COMPETITION_BELT_SCOPES, ['ADULT']);
  assert.ok(BELT_SCOPES.includes('JUVENILE'), 'el catálogo juvenil sigue existiendo');
  assert.deepEqual(COMPETITOR_SEXES, ['MALE', 'FEMALE']);
  assert.deepEqual(
    sexOptions(COMPETITOR_SEXES).map((option) => option.value),
    ['MALE', 'FEMALE']
  );
});

/* ------------------------------------------------------------ §15 · APIs existentes, sin inventar */

test('S4.8g: el alta se apoya en las APIs y el protocolo de escritura existentes', () => {
  assert.match(COMPETITORS_PAGE, /createCompetitor\(/, 'el alta crea el competidor');
  assert.match(COMPETITORS_PAGE, /assignAffiliation\(/, 'la afiliación usa el endpoint existente');
  assert.match(COMPETITORS_PAGE, /competitorCreateBody\(/);
  assert.match(COMPETITORS_PAGE, /actionScope\(\['competitor-create'/);
  assert.match(COMPETITORS_PAGE, /actionScope\(\['competitor-affiliation'/);
  assert.doesNotMatch(COMPETITORS_PAGE, /fetch\(/, 'la pantalla no escribe con fetch suelto');
  assert.doesNotMatch(WIZARD, /fetch\(/, 'el alta no escribe por su cuenta');
  assert.doesNotMatch(WIZARD, /@services/, 'el componente de alta no conoce la capa de servicios');
});

test('S4.8g: «Independiente» no escribe y elegir academia sí', () => {
  assert.equal(affiliationSportsClubId(affiliationAction(null, SPORTS_CLUB_INDEPENDENT)), null);
  assert.equal(affiliationSportsClubId(affiliationAction(null, '12')), 12);
  assert.equal(affiliationAction(null, SPORTS_CLUB_INDEPENDENT).kind, 'none');

  assert.match(
    COMPETITORS_PAGE,
    /if \(sportsClubId == null\) \{[\s\S]*?competitors_create_success/,
    'elegir Independiente cierra el alta sin escritura extra'
  );
  assert.match(COMPETITORS_PAGE, /affiliationSportsClubId\(/);
});

/* ------------------------------------------------------------- §5 · fallo parcial, sin ocultarlo */

test('S4.8g: el éxito parcial se dice con el nombre y se puede completar', () => {
  const failureIndex = COMPETITORS_PAGE.indexOf('setPartialFailure(');
  const profileIndex = COMPETITORS_PAGE.indexOf('setProfileTarget(');

  assert.ok(failureIndex > 0, 'el fallo parcial se registra');
  assert.ok(profileIndex > failureIndex, 'tras el fallo parcial se ofrece completar el perfil');
  assert.match(COMPETITORS_PAGE, /f3_competitor_create_partial_title/);
  assert.match(COMPETITORS_PAGE, /f3_competitor_create_partial_failure/);
  assert.match(COMPETITORS_PAGE, /data-create-partial-failure/);
  assert.match(
    COMPETITORS_PAGE,
    /t\('f3_competitor_create_partial_failure', \{ name: partialFailure\.name \}\)/,
    'el aviso nombra al competidor que sí se creó'
  );
});

/* ------------------------------------------------------------------- §10 · listado de competidores */

test('S4.8g: el listado muestra edad, sexo, cinturón, Club / Academia y estado', () => {
  for (const key of [
    'competitors_age_column',
    'competitors_sex_column',
    'competitors_belt_column',
    'competitors_club_column',
    'competitors_state_column',
    'competitors_name_column',
  ]) {
    assert.match(COMPETITORS_PAGE, new RegExp(`t\\('${key}'\\)`), `falta la columna ${key}`);
  }

  assert.match(COMPETITORS_PAGE, /ageLabel\(competitor\.date_of_birth\)/);
  assert.match(COMPETITORS_PAGE, /sexLabel\(competitor\.sex\)/);
  assert.match(COMPETITORS_PAGE, /beltColumnLabel\(competitor\.belt_id\)/);
  assert.match(COMPETITORS_PAGE, /competitor\.sports_club_name/);

  // Fallbacks de datos incompletos: no es un error fatal, es información que falta.
  for (const key of [
    'f3_profile_dob_none',
    'f3_profile_belt_none',
    'f3_sex_unspecified',
    'f3_club_independent_option',
  ]) {
    assert.match(COMPETITORS_PAGE, new RegExp(key), `falta el texto de reserva ${key}`);
  }

  // Identificadores fuera de la tabla (solo se usan como `key` de React).
  assert.doesNotMatch(COMPETITORS_PAGE, /<Table\.Td>\{competitor\.id\}/);
  assert.doesNotMatch(COMPETITORS_PAGE, /<Table\.Td>\{competitor\.(belt_id|sex)\}/);

  // El cinturón se traduce por catálogo; el `id` no se enseña nunca.
  const belts: Belt[] = [
    {
      id: 3,
      code: 'PURPLE',
      label: 'Purple',
      rank: 3,
      scope: 'ADULT',
      active: true,
      created: '2026-10-01T00:00:00Z',
    },
  ];
  const lookup = (key: string) => (key === 'f3_belt_purple' ? 'Violeta' : null);
  assert.equal(beltLabelForId(belts, 3, lookup), 'Violeta');
  assert.equal(beltLabelForId(belts, null, lookup), null);
  assert.equal(beltLabelForId(belts, 99, lookup), null);
});

/* ------------------------------------------------------------------------------ §16 · i18n */

test('S4.8g: el copy nuevo resuelve en es/en y mantiene «Violeta»', async () => {
  const es = await translate('es');
  const en = await translate('en');

  assert.equal(es.t('f3_competitor_create_step_indicator', { step: 1, total: 2 }), 'Paso 1 de 2');
  assert.equal(en.t('f3_competitor_create_step_indicator', { step: 2, total: 2 }), 'Step 2 of 2');
  assert.equal(es.t('f3_sex_unspecified'), 'Sin especificar');
  assert.equal(es.t('f3_profile_dob_none'), 'Sin fecha');
  assert.equal(es.t('f3_belt_purple'), 'Violeta');
  assert.equal(es.t('competitors_age_column'), 'Edad');
  assert.equal(es.t('competitors_club_column'), 'Club / Academia');
  assert.equal(es.t('f3_nav_legacy_badge'), 'legacy');

  const partial = String(es.t('f3_competitor_create_partial_failure', { name: 'Ana López' }));
  assert.match(partial, /Ana López/);
  assert.doesNotMatch(partial, /\{\{/, 'el aviso parcial no debe enseñar marcadores crudos');
  assert.match(partial, /Club \/ Academia/);

  const summary = String(en.t('f3_competitor_create_summary', { name: 'Ana Lopez' }));
  assert.match(summary, /Ana Lopez/);
  assert.doesNotMatch(summary, /\{\{/);
});
