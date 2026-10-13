/**
 * Contrato frontend de la unidad S4.8a-4: perfil deportivo del competidor, catalogo de cinturones,
 * modalidad, peso declarado, clasificacion (blockers/warnings) y logo de academia; mas la
 * separacion de privacidad (§20) entre el contrato del organizador y el publico.
 *
 * Ejercen modulos puros (sin red, sin navegador, sin DOM). Fuente de verdad, no supuesta:
 * `bracket/models/db/competitor_api.py`, `registration_api.py`, `sports_club_api.py`,
 * `routes/domain_competitors.py`, `routes/domain_registrations.py`, `routes/domain_sports_clubs.py`
 * y `logic/classification/vocabulary.py`.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
  BELT_SCOPES,
  CLASSIFICATION_BLOCKER_FALLBACK_KEY,
  CLASSIFICATION_BLOCKERS,
  CLASSIFICATION_BLOCKER_KEYS,
  CLASSIFICATION_WARNING_FALLBACK_KEY,
  CLASSIFICATION_WARNINGS,
  CLASSIFICATION_WARNING_KEYS,
  CLUB_LOGO_EXTENSIONS,
  CLUB_LOGO_MAX_BYTES,
  CLUB_LOGO_MIME_TYPES,
  COMPETITOR_SEXES,
  classificationBlockerKey,
  classificationWarningKey,
  competitorAffiliationCloseUrl,
  competitorAffiliationPrimaryUrl,
  competitorAffiliationsUrl,
  competitorIndependenceUrl,
  competitorProfileUrl,
  MODALITIES,
  registrationCategoryUrl,
  registrationClassificationUrl,
  sportsClubLogoUrl,
} from '../src/components/utils/f3_competitor_contract.ts';
import { toPublicCompetitor, toPublicRegistration } from '../src/components/utils/f3_privacy.ts';

/** Lee el fuente de un modulo del contrato (para comprobaciones de lo que NO debe nombrar). */
function source(relative: string): string {
  return readFileSync(new URL(relative, import.meta.url), 'utf8');
}

/* ----------------------------------------------------------------- codigos del motor (a-2) */

test('los codigos de blocker son exactamente los del vocabulario cerrado del motor', () => {
  // Orden canonico = orden de declaracion de `ClassificationBlocker` en vocabulary.py.
  assert.deepEqual(CLASSIFICATION_BLOCKERS, [
    'MISSING_DATE_OF_BIRTH',
    'MISSING_SEX',
    'MISSING_BELT',
    'MISSING_WEIGHT',
    'MISSING_MODALITY',
    'CATEGORY_NOT_RESOLVED',
    'CATEGORY_INCOMPATIBLE',
    'NO_COMPATIBLE_CATEGORY',
    'INVALID_CATEGORY_BELT_RANGE',
  ]);
});

test('los warnings son los dos del vocabulario cerrado', () => {
  assert.deepEqual(CLASSIFICATION_WARNINGS, [
    'CATEGORY_RULES_PARTIAL',
    'MULTIPLE_COMPATIBLE_CATEGORIES',
  ]);
});

test('cada codigo tiene su clave i18n escrita en el mapa', () => {
  for (const code of CLASSIFICATION_BLOCKERS) {
    assert.equal(
      CLASSIFICATION_BLOCKER_KEYS[code],
      `classification_blocker_${code.toLowerCase()}`,
      `blocker ${code}`
    );
  }
  for (const code of CLASSIFICATION_WARNINGS) {
    assert.equal(
      CLASSIFICATION_WARNING_KEYS[code],
      `classification_warning_${code.toLowerCase()}`,
      `warning ${code}`
    );
  }
  // Muestra explicita de la clave elegida para el aviso mas relevante de la clasificacion.
  assert.equal(
    CLASSIFICATION_WARNING_KEYS.MULTIPLE_COMPATIBLE_CATEGORIES,
    'classification_warning_multiple_compatible_categories'
  );
});

test('un codigo desconocido cae en el fallback tecnico, nunca en una clave inexistente', () => {
  assert.equal(classificationBlockerKey('MISSING_WEIGHT'), 'classification_blocker_missing_weight');
  assert.equal(
    classificationBlockerKey('CODIGO_QUE_NO_EXISTE'),
    CLASSIFICATION_BLOCKER_FALLBACK_KEY
  );
  assert.equal(
    classificationWarningKey('AVISO_QUE_NO_EXISTE'),
    CLASSIFICATION_WARNING_FALLBACK_KEY
  );
  // El fallback no es la clave del codigo crudo: esa clave no existe en los locales y se veria
  // en crudo en pantalla.
  assert.notEqual(classificationBlockerKey('CODIGO_QUE_NO_EXISTE'), 'classification_blocker_codigo_que_no_existe');
});

/* --------------------------------------------------------------------- vocabularios cerrados */

test('el sexo del competidor es MALE|FEMALE; MIXED pertenece a la categoria, no a la persona', () => {
  assert.deepEqual(COMPETITOR_SEXES, ['MALE', 'FEMALE']);
});

test('la modalidad es GI|NO_GI y no existe GI_AND_NO_GI', () => {
  assert.deepEqual(MODALITIES, ['GI', 'NO_GI']);
});

test('el catalogo de cinturones tiene dos escalas comparables solo dentro de su scope', () => {
  assert.deepEqual(BELT_SCOPES, ['JUVENILE', 'ADULT']);
});

/* ----------------------------------------------------------------------------------- rutas */

test('las rutas del contrato son las del OpenAPI de a-3', () => {
  assert.equal(competitorProfileUrl(4, 9), 'clubs/4/competitors/9/profile');
  assert.equal(competitorAffiliationsUrl(4, 9), 'clubs/4/competitors/9/affiliations');
  assert.equal(
    competitorAffiliationPrimaryUrl(4, 9, 12),
    'clubs/4/competitors/9/affiliations/12/primary'
  );
  assert.equal(
    competitorAffiliationCloseUrl(4, 9, 12),
    'clubs/4/competitors/9/affiliations/12/close'
  );
  assert.equal(competitorIndependenceUrl(4, 9), 'clubs/4/competitors/9/independence');

  assert.equal(
    registrationClassificationUrl(7, 3),
    'tournaments/7/registrations/3/classification'
  );
  assert.equal(registrationCategoryUrl(7, 3), 'tournaments/7/registrations/3/category');

  assert.equal(sportsClubLogoUrl(4, 5), 'clubs/4/sports-clubs/5/logo');
});

/* ------------------------------------------------------------------------------ logo de club */

test('el logo de academia acepta PNG/JPEG/WEBP y como maximo 2 MB', () => {
  assert.equal(CLUB_LOGO_MAX_BYTES, 2 * 1024 * 1024);
  assert.deepEqual(CLUB_LOGO_MIME_TYPES, ['image/png', 'image/jpeg', 'image/webp']);
  assert.deepEqual(CLUB_LOGO_EXTENSIONS, ['.png', '.jpg', '.jpeg', '.webp']);
  // SVG queda fuera a proposito: el backend lo rechaza aunque venga declarado como image/png.
  assert.ok(!CLUB_LOGO_MIME_TYPES.includes('image/svg+xml'));
});

/* ------------------------------------------------------------------------------- privacidad */

test('la proyeccion publica descarta el perfil deportivo y el peso declarado', () => {
  const organizerCompetitor = {
    id: 1,
    display_name: 'Ana',
    active: true,
    date_of_birth: '1990-05-01',
    sex: 'FEMALE',
    belt_id: 3,
  };
  const publicCompetitor = toPublicCompetitor(organizerCompetitor);
  assert.deepEqual(publicCompetitor, { id: 1, display_name: 'Ana' });
  assert.ok(!('date_of_birth' in publicCompetitor));
  assert.ok(!('belt_id' in publicCompetitor));

  const organizerRegistration = {
    id: 10,
    tournament_id: 7,
    competitor_name_snapshot: 'Ana',
    category_label: 'Gi -63 kg',
    sports_club_name_snapshot: 'Academia X',
    declared_weight_kg: '62.50',
    modality: 'GI',
  };
  const publicRegistration = toPublicRegistration(organizerRegistration);
  assert.ok(!('declared_weight_kg' in publicRegistration));
  assert.ok(!('modality' in publicRegistration));
  assert.equal(publicRegistration.competitor_name_snapshot, 'Ana');
});

test('el modulo de privacidad no nombra los campos sensibles', () => {
  const privacy = source('../src/components/utils/f3_privacy.ts');
  assert.doesNotMatch(privacy, /date_of_birth/);
  assert.doesNotMatch(privacy, /declared_weight_kg/);
});

test('el contrato del organizador si declara los campos sensibles (solo el)', () => {
  const organizer = source('../src/components/utils/f3_competitor_contract.ts');
  assert.match(organizer, /date_of_birth/);
  assert.match(organizer, /declared_weight_kg/);
});
