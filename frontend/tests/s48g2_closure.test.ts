/**
 * P2.8C/F3C-S4.8g — CIERRE DE LOS 4 PUNTOS (LAB ONLY; sin despliegue ni migración en producción).
 *
 * Fija los tres invariantes que no deben deshacerse solos después del cierre:
 *
 *  A) la modal de inscripción NO vuelve a entrar en bucle de render: el lector de clasificación
 *     viaja en un `ref`, no en las dependencias del efecto (era React #185 con el árbol caído);
 *  B) la pantalla de gestión de Clubes mira activos **e** inactivos; los selectores funcionales
 *     solo activos, y la afiliación del backend sigue rechazando un club de baja;
 *  C) la organización (tenant) no se llama «Club» en ninguna de las 12 lenguas, mientras el club
 *     deportivo sigue llamándose «Club».
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { sportsClubsUrl } from '../src/components/utils/f3_competitor_contract.ts';

const LOCALES = ['de', 'el', 'en', 'es', 'fa', 'fr', 'it', 'ja', 'nl', 'pt', 'sv', 'zh'] as const;

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8');
}

function locale(language: string): Record<string, string> {
  return JSON.parse(
    readFileSync(new URL(`../public/locales/${language}/common.json`, import.meta.url), 'utf8')
  );
}

const MODAL = source('../src/components/modals/registration_modal.tsx');
const REGISTRATIONS_PAGE = source('../src/pages/tournaments/[id]/registrations.tsx');
const CONTRACT = source('../src/components/utils/f3_competitor_contract.ts');
const SERVICES = source('../src/services/f3.ts');
const CLUBS_PAGE = source('../src/pages/tournaments/[id]/clubs.tsx');
const COMPETITORS_PAGE = source('../src/pages/tournaments/[id]/competitors.tsx');
const COMPETITOR_PROFILE = source('../src/components/f3/competitor_sports_profile.tsx');
const CLUB_MODAL = source('../src/components/modals/club_modal.tsx');
const CLUBS_TABLE = source('../src/components/tables/clubs.tsx');

/** Claves de la superficie del **tenant**: organización en el producto, `clubs` en la DB. */
const TENANT_KEYS = [
  'create_club_button',
  'edit_club_button',
  'delete_club_button',
  'club_name_input_placeholder',
  'club_select_label',
  'club_select_placeholder',
  'club_choose_title',
  'clubs_title',
  'clubs_spotlight_description',
] as const;

/* ------------------------------------------------------------------ A · sin bucle de render */

test('S4.8g-A: el preview no depende de la identidad del lector inyectado (no hay bucle)', () => {
  // Causa exacta: la página inyecta el lector como arrow inline, de modo que su identidad cambia
  // en cada render; con él en las dependencias, cada `setPreview` programaba otro preview.
  assert.match(
    REGISTRATIONS_PAGE,
    /loadPreview=\{\(body\) => previewRegistrationClassification\(/,
    'la página lo sigue pasando inline: la modal no puede depender de su identidad'
  );

  assert.match(MODAL, /const loadPreviewRef = useRef\(loadPreview\);/);
  assert.match(MODAL, /loadPreviewRef\.current = loadPreview;/);
  assert.match(MODAL, /const loader = loadPreviewRef\.current;/);
  assert.match(MODAL, /void loader\(\{/, 'el preview se dispara con el ref, no con la prop');

  assert.match(MODAL, /}, \[hasPreviewLoader, competitorId, weightInput, modalityInput\]\);/);
  assert.doesNotMatch(
    MODAL,
    /\}, \[hasPreviewLoader[^\]]*\b(form|loadPreview)\b/,
    'ni el objeto de useForm ni el lector inline pueden ser dependencias: cambian en cada render'
  );
  assert.match(MODAL, /const formRef = useRef\(form\);/);
  assert.match(MODAL, /formRef\.current = form;/);
  assert.match(MODAL, /formRef\.current\.setFieldValue\('category_key'/);
  assert.doesNotMatch(MODAL, /void loadPreview\(\{/, 'el cuerpo del efecto no llama a la prop');
});

/* ------------------------------------------------------------ B · activos frente a inactivos */

test('S4.8g-B: la URL de clubes solo pide inactivos cuando se le dice explícitamente', () => {
  assert.equal(sportsClubsUrl(7), 'clubs/7/sports-clubs');
  assert.equal(sportsClubsUrl(7, true), 'clubs/7/sports-clubs?include_inactive=true');
  assert.match(
    CONTRACT,
    /export function sportsClubsUrl\(club_id: number, includeInactive = false\): string/
  );
});

test('S4.8g-B: la gestión lista activos e inactivos; los selectores solo activos', () => {
  assert.match(CLUBS_PAGE, /getSportsClubs\(club_id, true\)/);
  assert.match(SERVICES, /includeInactive = false/);

  for (const [surface, text] of [
    ['competidores', COMPETITORS_PAGE],
    ['perfil deportivo', COMPETITOR_PROFILE],
  ] as const) {
    assert.match(text, /getSportsClubs\(club_id\)/);
    assert.doesNotMatch(
      text,
      /getSportsClubs\(club_id, true\)/,
      `${surface}: un selector no debe ofrecer clubes dados de baja`
    );
  }
});

test('S4.8g-B: un club de baja no se puede asignar (contrato del backend intacto)', () => {
  const affiliations = source('../../backend/bracket/logic/competitor_affiliations.py');
  assert.match(affiliations, /if club is None or not club\.active:/);
  assert.match(
    source('../../backend/tests/integration_tests/api/competitor_affiliation_http_test.py'),
    /test_unknown_inactive_or_foreign_club_is_rejected/,
    'la cobertura de la regla ya existe: no se amplía alcance en esta fase'
  );
});

/* ------------------------------------------------------------- C · organización ≠ Club (copy) */

test('S4.8g-C: la superficie del tenant no se llama «Club» en ninguna de las 12 lenguas', () => {
  for (const language of LOCALES) {
    const entries = locale(language);
    for (const key of TENANT_KEYS) {
      const value = entries[key];
      assert.ok(typeof value === 'string' && value.length > 0, `${language}: falta ${key}`);
      assert.doesNotMatch(value, /club/i, `${language}: ${key} sigue diciendo «Club»: "${value}"`);
      assert.notEqual(
        value,
        entries.f3_profile_club_label,
        `${language}: ${key} usa el nombre del club deportivo`
      );
    }
  }
});

test('S4.8g-C: el copy del tenant es el definitivo y el del club no se toca', () => {
  const es = locale('es');
  assert.equal(es.f3_organizations_title, 'Mis organizaciones');
  assert.equal(es.create_club_button, 'Nueva organización');
  assert.equal(es.edit_club_button, 'Editar organización');
  assert.equal(es.delete_club_button, 'Eliminar organización');
  assert.equal(es.club_name_input_placeholder, 'Nombre de la organización');
  assert.equal(es.club_select_label, 'Organización');
  assert.equal(es.club_select_placeholder, 'Elige una organización para este torneo');
  assert.equal(es.club_choose_title, 'Por favor, elige una organización');
  assert.equal(es.clubs_title, 'Organizaciones');
  assert.equal(es.clubs_spotlight_description, 'Ver, añadir o eliminar organizaciones');

  // Dentro del torneo el club deportivo sigue siendo «Club».
  assert.equal(es.f3_clubs_title, 'Clubes');
  assert.equal(es.f3_club_new_button, 'Nuevo club');
  assert.equal(es.f3_club_edit_button, 'Editar club');
  assert.equal(es.f3_club_create_title, 'Nuevo club');
  assert.equal(es.f3_profile_club_label, 'Club');
});

test('S4.8g-C: las claves del tenant solo las usa la superficie del tenant', () => {
  assert.match(CLUB_MODAL, /t\('create_club_button'\)/);
  assert.match(CLUB_MODAL, /t\('edit_club_button'\)/);
  assert.match(CLUBS_TABLE, /t\('delete_club_button'\)/);
  assert.doesNotMatch(
    CLUBS_PAGE,
    /t\('create_club_button'\)/,
    'la pantalla de Clubes tiene su propio copy (f3_club_*)'
  );
  assert.doesNotMatch(CLUBS_PAGE, /t\('f3_organizations_title'\)/);
});
