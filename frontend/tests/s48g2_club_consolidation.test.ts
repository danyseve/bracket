/**
 * P2.8C/F3C-S4.8g-2 — CLUB DOMAIN UX CONSOLIDATION (LAB ONLY, sin despliegue de producción).
 *
 * Decisión de producto cerrada: **una única entidad deportiva visible = Club**. Este fichero fija
 * el vocabulario y la superficie para que no se deshaga solo:
 *
 *  - «Academia», «Club / Academia», «SportsClub» y «tenant club» desaparecen del copy visible;
 *  - la organización (tenant) se sigue llamando «Mis organizaciones» y no se fusiona con el club;
 *  - el club se lista, se crea, se renombra, se activa/desactiva y lleva logo (§7/§15);
 *  - el naming técnico NO cambia: tabla `sports_clubs`, `sports_club_id`, `SportsClub` (§6);
 *  - la inscripción sigue congelando el club representado con su snapshot (§13).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { createInstance } from 'i18next';

import { LOCALES_REVISION, i18nInitOptions } from '../i18n_options.ts';

const LOCALES = ['de', 'el', 'en', 'es', 'fa', 'fr', 'it', 'ja', 'nl', 'pt', 'sv', 'zh'] as const;

/** Palabras retiradas del producto, en los idiomas que sirve la app. */
const FORBIDDEN_TERMS = [
  'academia',
  'akademie',
  'académie',
  'accademia',
  'akademi',
  'アカデミー',
  '道場',
  '学院',
  'ακαδημ',
  'آکادمی',
  'academy',
];

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8');
}

function locale(language: string): Record<string, string> {
  return JSON.parse(
    readFileSync(new URL(`../public/locales/${language}/common.json`, import.meta.url), 'utf8')
  );
}

async function translate(language: string) {
  const instance = createInstance();
  await instance.init({
    fallbackLng: i18nInitOptions.fallbackLng,
    defaultNS: i18nInitOptions.defaultNS,
    interpolation: i18nInitOptions.interpolation,
    lng: language,
    resources: { [language]: { common: locale(language) } },
  });
  return instance;
}

const NAV = source('../src/components/navbar/_main_links.tsx');
const MAIN = source('../src/main.tsx');
const COMPETITORS_PAGE = source('../src/pages/tournaments/[id]/competitors.tsx');
const WIZARD = source('../src/components/f3/competitor_create_wizard.tsx');
const CLUB_SELECT = source('../src/components/f3/sports_club_select.tsx');
const CLUB_LOGO = source('../src/components/f3/sports_club_logo.tsx');
const CLUBS_PAGE = source('../src/pages/tournaments/[id]/clubs.tsx');
const ORGANIZATIONS_PAGE = source('../src/pages/clubs.tsx');
const CONTRACT = source('../src/components/utils/f3_competitor_contract.ts');
const SERVICES = source('../src/services/f3.ts');

/** Claves visibles del flujo deportivo: son las que un organizador lee en pantalla. */
const VISIBLE_KEYS = [
  'f3_clubs_title',
  'f3_clubs_description',
  'f3_clubs_empty_title',
  'f3_clubs_empty_description',
  'f3_profile_club_label',
  'f3_profile_club_description',
  'f3_profile_club_loading',
  'f3_club_logo_manage_title',
  'f3_club_independent_option',
  'competitors_club_column',
  'competitors_create_description',
  'f3_competitor_create_club_note',
  'f3_registration_representation_notice',
  'f3_competitor_create_partial_failure',
] as const;

/* ------------------------------------------- §1/§2/§3 · un solo nombre visible, y es Club */

test('S4.8g-2: ningún idioma enseña ya «Academia» ni «Club / Academia»', () => {
  for (const language of LOCALES) {
    const entries = Object.entries(locale(language));
    for (const [key, value] of entries) {
      assert.doesNotMatch(
        key,
        /academ/i,
        `${language}: la clave ${key} sigue nombrando la academia`
      );
      const lower = value.toLowerCase();
      for (const term of FORBIDDEN_TERMS) {
        assert.ok(
          !lower.includes(term.toLowerCase()),
          `${language}: ${key} dice "${term}": "${value}"`
        );
      }
    }
  }
});

test('S4.8g-2: el copy visible dice Club/Clubes y no vocabulario técnico', async () => {
  const es = await translate('es');
  const en = await translate('en');

  assert.equal(es.t('f3_clubs_title'), 'Clubes');
  assert.equal(en.t('f3_clubs_title'), 'Clubs');
  assert.equal(es.t('f3_profile_club_label'), 'Club');
  assert.equal(es.t('competitors_club_column'), 'Club');
  assert.equal(es.t('f3_club_independent_option'), 'Independiente');
  assert.equal(es.t('f3_club_new_button'), 'Nuevo club');
  assert.equal(es.t('f3_club_edit_button'), 'Editar club');
  assert.equal(es.t('f3_club_name_label'), 'Nombre');
  assert.equal(es.t('f3_club_active_label'), 'Activo');
  assert.equal(es.t('f3_club_name_required'), 'El nombre es obligatorio.');
  assert.match(String(es.t('f3_club_duplicate')), /club/i);

  for (const language of LOCALES) {
    const instance = await translate(language);
    for (const key of VISIBLE_KEYS) {
      const text = String(instance.t(key));
      assert.ok(text.length > 0, `${language}: falta ${key}`);
      assert.doesNotMatch(
        text,
        /SportsClub|sports_club|tenant|managed_by/,
        `${language}: ${key} filtra vocabulario técnico`
      );
    }
  }
});

test('S4.8g-2: el naming técnico histórico no se toca (tabla, FK, tipos)', () => {
  // §6: la DB sigue siendo `sports_clubs` con `sports_club_id`; el producto dice Club.
  assert.match(CONTRACT, /export interface SportsClub\b/);
  assert.match(CONTRACT, /sports_club_id/);
  assert.match(CONTRACT, /const base = `clubs\/\$\{club_id\}\/sports-clubs`;/);
  assert.match(CONTRACT, /return includeInactive \? `\$\{base\}\?include_inactive=true` : base;/);
  assert.match(CONTRACT, /return `clubs\/\$\{club_id\}\/sports-clubs\/\$\{sports_club_id\}`;/);
  assert.match(CONTRACT, /export interface SportsClubCreateBody/);
  assert.match(CONTRACT, /export interface SportsClubUpdateBody/);
  // La inscripción conserva el snapshot del club representado (§13): se comprueba en el
  // contrato de la inscripción, que es donde vive.
  assert.match(source('../src/components/utils/f3_contract.ts'), /sports_club_name_snapshot/);
});

test('S4.8g-2: el módulo técnico explica que `sports_clubs` es el nombre histórico de Club', () => {
  const backendModule = source('../../backend/bracket/logic/sports_clubs.py');
  assert.match(backendModule, /sports_clubs/);
  assert.match(backendModule, /Club/);
  assert.doesNotMatch(backendModule, /academia/i, 'la prosa del módulo ya no dice academia');
});

/* ------------------------------------------------------------ §4 · navegación del torneo */

test('S4.8g-2: el menú del torneo enseña Clubes y el resto del flujo BJJ', () => {
  const tournamentLinks = NAV.slice(NAV.indexOf('export function TournamentLinks'));
  const mainMenu = tournamentLinks.slice(0, tournamentLinks.indexOf('if (advancedOpen)'));

  assert.match(mainMenu, /t\('f3_clubs_title'\)/);
  assert.match(mainMenu, /link: `\$\{tm_prefix\}\/clubs`/);
  assert.doesNotMatch(mainMenu, /\/academies/, 'el menú ya no enlaza academies');
  assert.doesNotMatch(mainMenu, /\/players/);
  assert.doesNotMatch(mainMenu, /\/teams/);
});

/* -------------------------------------------------------------- §5 · ruta y compatibilidad */

test('S4.8g-2: `/tournaments/:id/clubs` es la ruta limpia y `academies` redirige', () => {
  assert.match(MAIN, /<Route path="clubs" element=\{<TournamentClubsPage \/>\} \/>/);
  assert.match(
    MAIN,
    /<Route path="academies" element=\{<Navigate to="\.\.\/clubs" replace \/>\} \/>/
  );
  assert.doesNotMatch(MAIN, /AcademiesPage/, 'la página antigua ya no se importa');
});

/* ---------------------------------------------------------- §10/§11 · alta y listado con Club */

test('S4.8g-2: el wizard y el listado usan Club, nunca «Club / Academia»', () => {
  assert.match(WIZARD, /SportsClubSelect/);
  assert.match(WIZARD, /f3_competitor_create_club_note/);
  assert.doesNotMatch(WIZARD, /Academia/);
  assert.doesNotMatch(COMPETITORS_PAGE, /Academia/);
  assert.doesNotMatch(CLUB_SELECT, /Academia/);
  assert.match(CLUB_SELECT, /f3_club_independent_option/);
  assert.match(COMPETITORS_PAGE, /t\('competitors_club_column'\)/);
});

/* ------------------------------------------------------------ §7/§14/§15 · CRUD mínimo de club */

test('S4.8g-2: la pantalla de clubes crea, renombra, activa/desactiva y gestiona el logo', () => {
  assert.match(CLUBS_PAGE, /getSportsClubs\(club_id/);
  assert.match(CLUBS_PAGE, /createSportsClub\(/);
  assert.match(CLUBS_PAGE, /updateSportsClub\(/);
  assert.match(CLUBS_PAGE, /SportsClubLogo/);
  for (const attribute of [
    'data-clubs-page',
    'data-club-row',
    'data-club-create',
    'data-club-edit',
    'data-club-name-input',
    'data-club-active-input',
    'data-club-toggle',
  ]) {
    assert.match(CLUBS_PAGE, new RegExp(attribute), `falta ${attribute} en la pantalla de clubes`);
  }
  assert.match(CLUBS_PAGE, /t\('f3_club_new_button'\)/);
  assert.match(CLUBS_PAGE, /t\('f3_club_edit_button'\)/);
  assert.match(CLUBS_PAGE, /t\('f3_clubs_title'\)/);
  assert.match(CLUBS_PAGE, /actionScope\(\['sports-club-create'/);
  assert.match(CLUBS_PAGE, /actionScope\(\['sports-club-update'/);
});

test('S4.8g-2: las escrituras de club usan los endpoints y el protocolo F3 existentes', () => {
  assert.match(SERVICES, /export async function createSportsClub\(/);
  assert.match(SERVICES, /export async function updateSportsClub\(/);
  assert.match(
    SERVICES,
    /createAxios\(\)\.post\(sportsClubsUrl\(club_id\), body, idempotentHeaders\(key\)\)/
  );
  assert.match(
    SERVICES,
    /createAxios\(\)\.patch\(sportsClubUrl\(club_id, sports_club_id\), body, idempotentHeaders\(key\)\)/
  );
  assert.doesNotMatch(SERVICES, /delete\(sportsClub/, 'no hay borrado físico en el flujo normal');
});

test('S4.8g-2: el logo del club sigue en su endpoint y se puede reemplazar o quitar', () => {
  // El constructor de la ruta vive en los servicios; el componente solo la usa a traves de ellos.
  assert.match(SERVICES, /sportsClubLogoUrl\(club_id, sports_club_id\)/);
  assert.match(SERVICES, /export async function setSportsClubLogo\(/);
  assert.match(CLUB_LOGO, /sportsClubLogoDisplay/);
  assert.match(CLUB_LOGO, /logoFileError/);
});

/* --------------------------------------------------------------- §9 · organización ≠ club */

test('S4.8g-2: la organización sigue aparte y se llama «Mis organizaciones»', async () => {
  const es = await translate('es');
  assert.equal(es.t('f3_organizations_title'), 'Mis organizaciones');
  assert.match(ORGANIZATIONS_PAGE, /f3_organizations_title/);
  assert.doesNotMatch(ORGANIZATIONS_PAGE, /f3_clubs_title/);
  assert.notEqual(es.t('f3_organizations_title'), es.t('f3_clubs_title'));
});

/* ---------------------------------------------------------------------------- §16 · i18n */

test('S4.8g-2: los 12 locales tienen las claves nuevas y la revisión está al día', async () => {
  // La paridad total entre locales es deuda previa (los ficheros ya divergian): lo que esta fase
  // garantiza es que el vocabulario del club exista en los 12 idiomas.
  const clubKeys = (language: string): string[] =>
    Object.keys(locale(language))
      .filter((key) => key.startsWith('f3_club'))
      .sort();
  for (const language of LOCALES) {
    assert.deepEqual(
      clubKeys(language),
      clubKeys('es'),
      `${language}: el vocabulario del club no coincide con es`
    );
  }

  for (const key of [
    'f3_clubs_title',
    'f3_clubs_description',
    'f3_clubs_empty_title',
    'f3_clubs_empty_description',
    'f3_club_new_button',
    'f3_club_edit_button',
    'f3_club_create_title',
    'f3_club_edit_title',
    'f3_club_create_description',
    'f3_club_name_label',
    'f3_club_active_label',
    'f3_club_actions_label',
    'f3_club_activate_button',
    'f3_club_deactivate_button',
    'f3_club_create_success',
    'f3_club_update_success',
    'f3_club_name_required',
    'f3_club_duplicate',
  ]) {
    for (const language of LOCALES) {
      assert.ok(String((await translate(language)).t(key)).length > 0, `${language}: falta ${key}`);
    }
  }

  // §16: PURPLE sigue siendo «Violeta».
  const es = await translate('es');
  assert.equal(es.t('f3_belt_purple'), 'Violeta');
  assert.ok(
    LOCALES_REVISION.length >= 8,
    'la revisión de locales debe existir para invalidar cachés'
  );
});
