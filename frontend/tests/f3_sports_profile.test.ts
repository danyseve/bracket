/**
 * Perfil deportivo del competidor y club (S4.8a-4 §3-§7).
 *
 * Ejerce los helpers puros que usa la UI de verdad (sin DOM, sin red) y comprueba, por fuente, que
 * los componentes se cablean a los **contratos reales** del backend: catálogo de cinturones
 * (`GET /clubs/:club_id/belts`), academias (`GET /clubs/:club_id/sports-clubs`), perfil, afiliación,
 * independencia y logo.
 *
 * Reglas que estos tests protegen: `MIXED` no es sexo de competidor, el orden de cinturones sale del
 * catálogo (no se hardcodea), la fecha de nacimiento se valida como UX pero no impone reglas de edad
 * y el club nunca se elige con texto libre.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
  BELT_SCOPES,
  CLUB_LOGO_EXTENSIONS,
  CLUB_LOGO_MAX_BYTES,
  CLUB_LOGO_MIME_TYPES,
  COMPETITOR_SEXES,
  sportsClubsUrl,
  beltsUrl,
} from '../src/components/utils/f3_competitor_contract.ts';
import {
  affiliationAction,
  beltLabel,
  beltOptions,
  currentSportsClubId,
  dateOfBirthError,
  isRealDate,
  logoFileError,
  logoInitials,
  profileBody,
  profileChanged,
  sexOptions,
  SPORTS_CLUB_INDEPENDENT,
  sportsClubLogoDisplay,
  sportsClubOptions,
  toIsoDate,
} from '../src/components/utils/f3_sports_profile.ts';

const LOCALES_DIR = new URL('../public/locales/', import.meta.url);
const LOCALES = ['de', 'el', 'en', 'es', 'fa', 'fr', 'it', 'ja', 'nl', 'pt', 'sv', 'zh'];

function source(relative: string): string {
  return readFileSync(new URL(relative, import.meta.url), 'utf8');
}

function loadLocale(language: string): Record<string, string> {
  return JSON.parse(readFileSync(new URL(`${language}/common.json`, LOCALES_DIR), 'utf8')) as Record<
    string,
    string
  >;
}

function belt(id: number, code: string, rank: number, scope: 'ADULT' | 'JUVENILE') {
  return { id, code, label: code, rank, scope, active: true, created: '2026-01-01T00:00:00Z' };
}

/* ------------------------------------------------------------------------------------ sexo (§4) */

test('el sexo del competidor solo ofrece MALE y FEMALE: MIXED nunca es una persona', () => {
  const options = sexOptions(['MALE', 'FEMALE', 'MIXED', 'OTRO_INVENTADO']);
  assert.deepEqual(
    options.map((option) => option.value),
    ['MALE', 'FEMALE']
  );
  assert.deepEqual(
    options.map((option) => option.labelKey),
    ['f3_sex_male', 'f3_sex_female']
  );

  // El catálogo de sexos del contrato no incluye MIXED (domain.py:52-53).
  assert.deepEqual(COMPETITOR_SEXES, ['MALE', 'FEMALE']);
  assert.equal(COMPETITOR_SEXES.length, 2);
});

/* ------------------------------------------------------------------------------ cinturones (§3) */

test('el orden de cinturones sale de los datos (scope/rank/code), no de una lista cableada', () => {
  const belts = [
    belt(9, 'BLACK', 5, 'ADULT'),
    belt(4, 'WHITE', 1, 'ADULT'),
    belt(7, 'GREEN', 5, 'JUVENILE'),
    belt(2, 'GREY', 2, 'JUVENILE'),
    belt(6, 'BLUE', 2, 'ADULT'),
  ];
  const ordered = beltOptions(belts, BELT_SCOPES, () => null).map((choice) => choice.value);

  // Primero la escala juvenil (según BELT_SCOPES) ordenada por rank y, dentro, la adulta.
  assert.deepEqual(ordered, ['2', '7', '4', '6', '9']);
});

test('la etiqueta del cinturón prefiere la traducción y cae al label del backend', () => {
  const white = belt(4, 'WHITE', 1, 'ADULT');
  assert.equal(beltLabel(white, () => 'Blanco'), 'Blanco');
  assert.equal(beltLabel(white, () => null), 'WHITE');
});

test('los campos del catálogo viajan en la opción (id, scale, code, rank)', () => {
  const choice = beltOptions([belt(4, 'WHITE', 1, 'ADULT')], BELT_SCOPES, () => null)[0];
  assert.equal(choice.value, '4');
  assert.equal(choice.scope, 'ADULT');
  assert.equal(choice.code, 'WHITE');
  assert.equal(choice.rank, 1);
});

/* ------------------------------------------------------------------------------------ club (§6) */

test('el selector de club ofrece las academias reales más la opción independiente', () => {
  const clubs = [
    { id: 3, name: 'Lotus', active: true, logo_path: 'static/sports-club-logos/a.png' },
    { id: 8, name: 'Kodokan', active: true, logo_path: null },
  ];
  const options = sportsClubOptions(clubs);

  assert.equal(options.length, 3);
  assert.deepEqual(
    options.map((option) => option.value),
    ['3', '8', SPORTS_CLUB_INDEPENDENT]
  );
  assert.equal(options[2].name, null);
});

test('la academia vigente es la afiliación sin valid_to', () => {
  assert.equal(
    currentSportsClubId([
      { sports_club_id: 3, valid_to: '2025-01-01T00:00:00Z' },
      { sports_club_id: 8, valid_to: null },
    ]),
    8
  );
  assert.equal(currentSportsClubId([]), null);
});

test('cambiar de club o a independiente usa la acción de dominio correcta', () => {
  assert.deepEqual(affiliationAction(3, '3'), { kind: 'none' });
  assert.deepEqual(affiliationAction(3, '8'), { kind: 'assign', sports_club_id: 8 });
  assert.deepEqual(affiliationAction(3, SPORTS_CLUB_INDEPENDENT), { kind: 'independent' });
  assert.deepEqual(affiliationAction(null, SPORTS_CLUB_INDEPENDENT), { kind: 'none' });
  assert.deepEqual(affiliationAction(null, '8'), { kind: 'assign', sports_club_id: 8 });
  assert.deepEqual(affiliationAction(3, ''), { kind: 'none' });
});

/* -------------------------------------------------------------------- perfil deportivo (§3-§5) */

test('el perfil se reemplaza con los tres campos siempre y un motivo del vocabulario cerrado', () => {
  const body = profileBody({ date_of_birth: '2010-05-01', sex: 'MALE', belt_id: 4 });
  assert.deepEqual(body, {
    date_of_birth: '2010-05-01',
    sex: 'MALE',
    belt_id: 4,
    reason_code: 'DATA_CORRECTION',
    reason_note: null,
  });
});

test('sin cambios reales de perfil no se escribe', () => {
  const initial = { date_of_birth: '2010-05-01', sex: 'MALE' as const, belt_id: 4 };
  assert.equal(profileChanged(initial, { ...initial }), false);
  assert.equal(profileChanged(initial, { ...initial, belt_id: 6 }), true);
  assert.equal(profileChanged(initial, { ...initial, sex: null }), true);
});

/* ---------------------------------------------------- fecha de nacimiento: validación de UX (§5) */

test('la fecha de nacimiento se valida como UX: no futura y con formato local', () => {
  const today = '2026-10-13';
  assert.equal(dateOfBirthError('2010-05-01', today), null);
  assert.equal(dateOfBirthError('2026-10-14', today), 'f3_profile_dob_future');
  assert.equal(dateOfBirthError('01/05/2010', today), 'f3_profile_dob_invalid');
  assert.equal(dateOfBirthError('2026-02-31', today), 'f3_profile_dob_invalid');
  assert.equal(dateOfBirthError('', today), null, 'no se impone obligatoriedad por defecto');
  assert.equal(dateOfBirthError('', today, true), 'f3_profile_dob_required');
});

test('el calendario real rechaza días que no existen y acepta el 29 de febrero bisiesto', () => {
  assert.equal(isRealDate('2024-02-29'), true);
  assert.equal(isRealDate('2026-02-29'), false);
  assert.equal(isRealDate('2026-13-01'), false);
  assert.equal(isRealDate('2010-5-1'), false);
});

test('la fecha «hoy» se compone en local, sin desplazar por zona horaria', () => {
  assert.equal(toIsoDate(new Date(2026, 9, 13)), '2026-10-13');
  assert.equal(toIsoDate(new Date(2026, 0, 3)), '2026-01-03');
});

/* ------------------------------------------------------------------- logo de academia (§7) ------ */

test('el logo cae a iniciales o icono neutro cuando no hay fichero', () => {
  assert.equal(logoInitials('Lotus Jiu Jitsu'), 'LJ');
  assert.equal(logoInitials('Kodokan'), 'K');
  assert.equal(logoInitials('   '), '');
  assert.equal(logoInitials(null), '');

  const display = sportsClubLogoDisplay(
    { id: 3, name: 'Lotus', active: true, logo_path: null },
    'http://api.test'
  );
  assert.equal(display.hasLogo, false);
  assert.equal(display.src, null);
  assert.equal(display.initials, 'L');
});

test('con logo, la URL se compone sobre la ruta relativa que publica el backend', () => {
  const display = sportsClubLogoDisplay(
    { id: 3, name: 'Lotus', active: true, logo_path: 'static/sports-club-logos/a.png' },
    'http://api.test/'
  );
  assert.equal(display.hasLogo, true);
  assert.equal(display.src, 'http://api.test/static/sports-club-logos/a.png');
});

test('el logo se valida antes de subirlo: tamaño y tipo admitidos', () => {
  const limits = {
    maxBytes: CLUB_LOGO_MAX_BYTES,
    mimeTypes: CLUB_LOGO_MIME_TYPES,
    extensions: CLUB_LOGO_EXTENSIONS,
  };
  assert.equal(CLUB_LOGO_MAX_BYTES, 2 * 1024 * 1024);

  assert.equal(logoFileError({ name: 'a.png', size: 1000, type: 'image/png' }, limits), null);
  assert.equal(
    logoFileError({ name: 'a.png', size: CLUB_LOGO_MAX_BYTES + 1, type: 'image/png' }, limits),
    'f3_club_logo_too_large'
  );
  assert.equal(
    logoFileError({ name: 'a.svg', size: 1000, type: 'image/svg+xml' }, limits),
    'f3_club_logo_wrong_type'
  );
  assert.equal(logoFileError({ name: 'a.png', size: 0, type: 'image/png' }, limits), 'f3_club_logo_too_large');
});

/* ----------------------------------------------------------------- rutas del contrato (backend) */

test('las rutas de lectura del catálogo son las del backend', () => {
  assert.equal(beltsUrl(4), 'clubs/4/belts');
  assert.equal(sportsClubsUrl(4), 'clubs/4/sports-clubs');
});

/* ------------------------------------------------------------------- cableado de componentes §21 */

const BELT_SELECT = '../src/components/f3/belt_select.tsx';
const SPORTS_CLUB_SELECT = '../src/components/f3/sports_club_select.tsx';
const SPORTS_CLUB_LOGO = '../src/components/f3/sports_club_logo.tsx';
const COMPETITOR_PROFILE = '../src/components/f3/competitor_sports_profile.tsx';
const COMPETITORS_PAGE = '../src/pages/tournaments/[id]/competitors.tsx';

test('el selector de cinturón se puebla del catálogo del backend y no hardcodea el orden', () => {
  const beltSelect = source(BELT_SELECT);
  assert.match(beltSelect, /beltOptions\(/);
  assert.match(beltSelect, /BELT_SCOPES/);
  // No hay lista de cinturones cableada a mano en la UI.
  assert.doesNotMatch(beltSelect, /WHITE|GREEN|BROWN|BLACK/);
});

test('el selector de club usa el endpoint real y no acepta texto libre', () => {
  const clubSelect = source(SPORTS_CLUB_SELECT);
  assert.match(clubSelect, /sportsClubOptions\(/);
  assert.match(clubSelect, /SPORTS_CLUB_INDEPENDENT/);
  assert.doesNotMatch(clubSelect, /TextInput/);
});

test('el perfil usa los endpoints reales de perfil, afiliación e independencia', () => {
  const profile = source(COMPETITOR_PROFILE);
  for (const call of [
    'getBelts(',
    'getSportsClubs(',
    'getCompetitorAffiliations(',
    'updateCompetitorProfile(',
    'assignAffiliation(',
    'goIndependent(',
  ]) {
    assert.match(profile, new RegExp(call.replace('(', '\\(')), `el perfil no usa ${call}`);
  }
  assert.match(profile, /sexOptions\(COMPETITOR_SEXES\)/);
  assert.match(profile, /dateOfBirthError\(/);
  assert.match(profile, /affiliationAction\(/);
  assert.match(profile, /actionScope\(\[/);
});

test('el logo valida el fichero y usa el endpoint real de subida', () => {
  const logo = source(SPORTS_CLUB_LOGO);
  assert.match(logo, /logoFileError\(/);
  assert.match(logo, /sportsClubLogoDisplay\(/);
  assert.match(logo, /setSportsClubLogo\(/);
  assert.match(logo, /CLUB_LOGO_MAX_BYTES/);
});

test('la pantalla de competidores integra el perfil sin inventar el club', () => {
  const page = source(COMPETITORS_PAGE);
  assert.match(page, /CompetitorSportsProfile/);
  assert.match(page, /competitors_profile_button/);
  // El club nunca se envía desde la interfaz como texto ni como identificador suelto.
  assert.doesNotMatch(page, /sports_club_id/);
});

/* --------------------------------------------------------------------------------- i18n (§19) */

const NEW_KEYS = [
  'f3_sex_label',
  'f3_sex_male',
  'f3_sex_female',
  'f3_profile_title',
  'f3_profile_description',
  'f3_profile_loading',
  'f3_profile_dob_label',
  'f3_profile_dob_description',
  'f3_profile_dob_invalid',
  'f3_profile_dob_future',
  'f3_profile_dob_required',
  'f3_profile_belt_label',
  'f3_profile_belt_description',
  'f3_profile_belt_loading',
  'f3_profile_belt_empty',
  'f3_profile_belt_none',
  'f3_profile_belt_scope_juvenile',
  'f3_profile_belt_scope_adult',
  'f3_belt_white',
  'f3_belt_grey',
  'f3_belt_yellow',
  'f3_belt_orange',
  'f3_belt_green',
  'f3_belt_blue',
  'f3_belt_purple',
  'f3_belt_brown',
  'f3_belt_black',
  'f3_profile_club_label',
  'f3_profile_club_description',
  'f3_profile_club_loading',
  'f3_club_independent_option',
  'f3_profile_affiliation_success',
  'f3_profile_independence_success',
  'f3_profile_success',
  'f3_profile_save_button',
  'f3_club_logo_manage_title',
  'f3_club_logo_manage_note',
  'f3_club_logo_upload_button',
  'f3_club_logo_replace_button',
  'f3_club_logo_remove_button',
  'f3_club_logo_upload_success',
  'f3_club_logo_remove_success',
  'f3_club_logo_too_large',
  'f3_club_logo_wrong_type',
  'competitors_profile_button',
];

test('las claves nuevas existen, sin vacíos, en los 12 locales; y no hay clave de sexo MIXED', () => {
  for (const language of LOCALES) {
    const locale = loadLocale(language);
    for (const key of NEW_KEYS) {
      assert.equal(typeof locale[key], 'string', `${language}: falta ${key}`);
      assert.ok(locale[key].trim().length > 0, `${language}: ${key} está vacía`);
    }
    for (const forbidden of ['f3_sex_mixed', 'f3_sex_other']) {
      assert.equal(locale[forbidden], undefined, `${language}: no debe existir ${forbidden}`);
    }
  }
});
