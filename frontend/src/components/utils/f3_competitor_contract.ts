/**
 * Contrato frontend del organizador para la unidad S4.8a-4: perfil deportivo del competidor,
 * catálogo de cinturones, modalidad, peso declarado, clasificación (blockers/warnings) y logo de
 * club.
 *
 * Por qué un fichero nuevo y no `f3_contract.ts`: `f3_contract.ts` es el contrato del cliente ya
 * validado (S4.1) y no se reescribe; esta unidad añade las proyecciones de S4.8a-3 y se separa para
 * no tocar el contrato vivo. Aquí solo se **tipan** los cuerpos que el backend ya acepta y las
 * respuestas que ya publica: la interfaz no ofrece nada que el backend rechace y **no duplica**
 * ninguna regla deportiva (eso es el motor de `bracket/logic/classification`).
 *
 * Fuente de verdad (no se inventa nada):
 *   - `bracket/models/db/competitor_api.py`        (perfil, afiliación)
 *   - `bracket/models/db/registration_api.py`      (peso declarado, modalidad, clasificación)
 *   - `bracket/models/db/sports_club_api.py`       (logo)
 *   - `bracket/models/db/domain.py`                (Belt, CompetitorSex, Modality, BeltScope)
 *   - `bracket/routes/domain_competitors.py`       (rutas de perfil y afiliación)
 *   - `bracket/routes/domain_registrations.py`     (rutas de clasificación y categoría)
 *   - `bracket/routes/domain_sports_clubs.py`      (ruta de logo, límites 2 MB y tipos)
 *   - `bracket/logic/classification/vocabulary.py` (códigos cerrados de blocker/warning)
 *
 * El módulo no importa nada (ni React, ni axios, ni i18next): los tests lo ejercen con
 * `node --test` sin red y sin navegador. Los textos visibles son claves de traducción, nunca
 * frases: el copy vive en `public/locales`.
 */

/* --- vocabularios cerrados (domain.py) -------------------------------------------------------- */

/** Sexo de la **persona**: `MIXED` describe una categoría, no a nadie (domain.py:52). */
export type CompetitorSex = 'MALE' | 'FEMALE';

/** Restricción de sexo de una **categoría**; `MIXED` no restringe (domain.py:53). */
export type CategorySexConstraint = 'MALE' | 'FEMALE' | 'MIXED';

/** Modalidad de una inscripción: GI y NO_GI son inscripciones distintas (domain.py:56). */
export type Modality = 'GI' | 'NO_GI';

/** Escala del cinturón: el mismo color existe en la juvenil y en la adulta (domain.py:59). */
export type BeltScope = 'JUVENILE' | 'ADULT';

/** Valores del sexo de un competidor, en orden canónico. */
export const COMPETITOR_SEXES: CompetitorSex[] = ['MALE', 'FEMALE'];

/** Modalidades reales. No existe `GI_AND_NO_GI`: una inscripción es de una sola modalidad. */
export const MODALITIES: Modality[] = ['GI', 'NO_GI'];

/** Escalas del catálogo de cinturones, comparables solo dentro de la suya. */
export const BELT_SCOPES: BeltScope[] = ['JUVENILE', 'ADULT'];

/* --- catálogo de cinturones (tabla `belts`) --------------------------------------------------- */

/**
 * Cinturón del catálogo de plataforma (`belts`, domain.py:149).
 *
 * `code` es la clave lógica estable y `label` la etiqueta canónica de respaldo (la traducción vive
 * en el frontend). `rank` solo es comparable **dentro** de `scope`.
 *
 * Nota: a-3 **no** publica una ruta HTTP de catálogo (solo existe el lector `get_belt` interno,
 * `sql/domain_reads.py:74`); este tipo describe la forma de la tabla y queda listo para cuando el
 * catálogo se exponga. El backend **tampoco asocia modalidad al cinturón**, así que no hay campo
 * `modality` aquí.
 */
export interface Belt {
  id: number;
  code: string;
  label: string;
  rank: number;
  scope: BeltScope;
  active: boolean;
  created: string;
}

/* --- perfil deportivo del competidor (competitor_api.py) -------------------------------------- */

/**
 * Perfil deportivo tal como viaja en `CompetitorResponse` (competitor_api.py:129).
 *
 * Los tres campos son nulables: `null` es «dato no declarado» y un valor legítimo, no un campo sin
 * mencionar. El club representado no es un campo del competidor: es una afiliación temporal.
 */
export interface CompetitorSportsProfile {
  date_of_birth: string | null;
  sex: CompetitorSex | null;
  belt_id: number | null;
}

/**
 * Cuerpo del reemplazo explícito del perfil (`CompetitorProfileBody`, competitor_api.py:78).
 *
 * Los tres campos viajan **siempre**, aunque sean `null`: es un reemplazo, no un parche.
 */
export interface CompetitorProfileBody {
  date_of_birth: string | null;
  sex: CompetitorSex | null;
  belt_id: number | null;
  reason_code?: string | null;
  reason_note?: string | null;
}

/** Cuerpo de la asignación de afiliación: solo el club destino (competitor_api.py:160). */
export interface CompetitorAffiliationBody {
  sports_club_id: number;
}

/** Afiliación vigente o histórica (competitor_api.py:169). */
export interface CompetitorAffiliation {
  id: number;
  competitor_id: number;
  sports_club_id: number;
  is_primary: boolean;
  valid_from: string;
  valid_to: string | null;
}

/** Lista de afiliaciones de un competidor (competitor_api.py:190). */
export interface CompetitorAffiliationsList {
  competitor_id: number;
  affiliations: CompetitorAffiliation[];
}

/* --- peso declarado y modalidad de la inscripción (registration_api.py) ------------------------ */

/**
 * Campos de clasificación que el competidor declara en la inscripción.
 *
 * `declared_weight_kg` viaja como **cadena decimal** en la respuesta (`NUMERIC(5,2)` serializado;
 * `openapi.json` lo publica como `string` con patrón): se conserva como texto para no perder
 * precisión con un `number`. En el cuerpo de alta el backend acepta número o cadena.
 */
export interface RegistrationDeclaredFields {
  declared_weight_kg: string | null;
  modality: Modality | null;
}

/* --- clasificación (vocabulary.py + registration_api.py) --------------------------------------- */

/**
 * Motivos por los que una inscripción no tiene categoría resoluble (vocabulary.py:26).
 *
 * Orden canónico = orden de declaración del vocabulario. No se renombra ninguno.
 */
export type ClassificationBlocker =
  | 'MISSING_DATE_OF_BIRTH'
  | 'MISSING_SEX'
  | 'MISSING_BELT'
  | 'MISSING_WEIGHT'
  | 'MISSING_MODALITY'
  | 'CATEGORY_NOT_RESOLVED'
  | 'CATEGORY_INCOMPATIBLE'
  | 'NO_COMPATIBLE_CATEGORY'
  | 'INVALID_CATEGORY_BELT_RANGE';

/** Situaciones **no** bloqueantes que el organizador debería conocer (vocabulary.py:52). */
export type ClassificationWarning = 'CATEGORY_RULES_PARTIAL' | 'MULTIPLE_COMPATIBLE_CATEGORIES';

/** Códigos de blocker, en orden canónico (el de la declaración del vocabulario). */
export const CLASSIFICATION_BLOCKERS: ClassificationBlocker[] = [
  'MISSING_DATE_OF_BIRTH',
  'MISSING_SEX',
  'MISSING_BELT',
  'MISSING_WEIGHT',
  'MISSING_MODALITY',
  'CATEGORY_NOT_RESOLVED',
  'CATEGORY_INCOMPATIBLE',
  'NO_COMPATIBLE_CATEGORY',
  'INVALID_CATEGORY_BELT_RANGE',
];

/** Códigos de warning, en orden canónico. */
export const CLASSIFICATION_WARNINGS: ClassificationWarning[] = [
  'CATEGORY_RULES_PARTIAL',
  'MULTIPLE_COMPATIBLE_CATEGORIES',
];

/**
 * Clave i18n de cada blocker. El código es el contrato; el texto humano vive en los locales.
 * (Las entradas de los locales las añade otro trozo de la unidad; aquí solo se fija la clave.)
 */
export const CLASSIFICATION_BLOCKER_KEYS: Record<ClassificationBlocker, string> = {
  MISSING_DATE_OF_BIRTH: 'classification_blocker_missing_date_of_birth',
  MISSING_SEX: 'classification_blocker_missing_sex',
  MISSING_BELT: 'classification_blocker_missing_belt',
  MISSING_WEIGHT: 'classification_blocker_missing_weight',
  MISSING_MODALITY: 'classification_blocker_missing_modality',
  CATEGORY_NOT_RESOLVED: 'classification_blocker_category_not_resolved',
  CATEGORY_INCOMPATIBLE: 'classification_blocker_category_incompatible',
  NO_COMPATIBLE_CATEGORY: 'classification_blocker_no_compatible_category',
  INVALID_CATEGORY_BELT_RANGE: 'classification_blocker_invalid_category_belt_range',
};

/** Clave i18n de cada warning. */
export const CLASSIFICATION_WARNING_KEYS: Record<ClassificationWarning, string> = {
  CATEGORY_RULES_PARTIAL: 'classification_warning_category_rules_partial',
  MULTIPLE_COMPATIBLE_CATEGORIES: 'classification_warning_multiple_compatible_categories',
};

/** Fallback de un blocker desconocido: nunca se pinta la clave del código crudo. */
export const CLASSIFICATION_BLOCKER_FALLBACK_KEY = 'classification_blocker_unknown';
/** Fallback de un warning desconocido. */
export const CLASSIFICATION_WARNING_FALLBACK_KEY = 'classification_warning_unknown';

/**
 * Clave i18n de un blocker. Un código desconocido (versión futura del motor) cae en el **fallback
 * técnico**: la UI no debe pintar una clave que no existe en los locales. El código crudo viaja
 * aparte y no se pierde.
 */
export function classificationBlockerKey(code: string): string {
  return (
    CLASSIFICATION_BLOCKER_KEYS[code as ClassificationBlocker] ??
    CLASSIFICATION_BLOCKER_FALLBACK_KEY
  );
}

/** Clave i18n de un warning, con el mismo fallback técnico que un blocker. */
export function classificationWarningKey(code: string): string {
  return (
    CLASSIFICATION_WARNING_KEYS[code as ClassificationWarning] ??
    CLASSIFICATION_WARNING_FALLBACK_KEY
  );
}

/** Veredicto del motor para una categoría (`ClassificationCategoryVerdict`, registration_api.py). */
export interface ClassificationCategoryVerdict {
  category_key: string;
  category_label: string | null;
  compatible: boolean;
  blockers: ClassificationBlocker[];
  warnings: ClassificationWarning[];
}

/**
 * Respuesta de `GET tournaments/{id}/registrations/{id}/classification` (§6).
 *
 * Cuatro campos: compatibles, recomendada (solo si es **única**), bloqueos y avisos. Nada más.
 */
export interface RegistrationClassification {
  compatible_categories: ClassificationCategoryVerdict[];
  recommended_category: ClassificationCategoryVerdict | null;
  blockers: ClassificationBlocker[];
  warnings: ClassificationWarning[];
}

/** Cuerpo de la asignación de categoría (§7/§8). `override` es explícito y OWNER-only. */
export interface RegistrationCategoryBody {
  category_key: string | null;
  override: boolean;
  reason_code?: string | null;
  reason_note?: string | null;
}

/**
 * Cuerpo de la **clasificación previa** al alta (S4.8d §2).
 *
 * Sólo los tres datos que deciden: el competidor y lo que se declara en el formulario. No viaja la
 * representación ni el club (no intervienen en la compatibilidad deportiva) ni ningún estado: la
 * previa es precisamente antes de que exista la inscripción. Peso y modalidad pueden ir nulos
 * mientras el usuario todavía no los ha rellenado; el motor responderá `MISSING_WEIGHT` /
 * `MISSING_MODALITY`, que es información y no un error.
 */
export interface RegistrationClassificationPreviewBody {
  competitor_id: number;
  declared_weight_kg: string | null;
  modality: Modality | null;
}

/* --- logo de club (domain_sports_clubs.py) ------------------------------------------------- */

/** Tamaño máximo del logo (`MAX_LOGO_BYTES`, domain_sports_clubs.py:63). */
export const CLUB_LOGO_MAX_BYTES = 2 * 1024 * 1024;

/** Tipos reales admitidos: se detectan por contenido; SVG queda fuera aunque se declare PNG. */
export const CLUB_LOGO_MIME_TYPES: string[] = ['image/png', 'image/jpeg', 'image/webp'];

/** Extensiones declaradas que el backend acepta y coteja con el tipo real. */
export const CLUB_LOGO_EXTENSIONS: string[] = ['.png', '.jpg', '.jpeg', '.webp'];

/** Proyección pública de un club: identidad mínima y logo (sports_club_api.py). */
export interface SportsClub {
  id: number;
  name: string;
  active: boolean;
  logo_path: string | null;
}

/**
 * Cuerpo del alta de club (S4.8g-2 §7/§14). `name` es obligatorio; `active` nace en alta; los
 * motivos de auditoria son opcionales (el backend resuelve el codigo por defecto del catalogo).
 */
export interface SportsClubCreateBody {
  name: string;
  active?: boolean;
  reason_code?: string | null;
  reason_note?: string | null;
}

/**
 * Cuerpo de la edicion de club (S4.8g-2 §7/§14): solo viajan los campos que cambian. El backend
 * mantiene el historico de nombres y no borra filas; con el mismo contenido no hay escritura.
 */
export interface SportsClubUpdateBody {
  name?: string | null;
  active?: boolean | null;
  reason_code?: string | null;
  reason_note?: string | null;
}

/* --- rutas (domain_competitors.py / domain_registrations.py / domain_sports_clubs.py) ---------- */

/** Reemplazo del perfil deportivo (`POST .../profile`). */
export function competitorProfileUrl(club_id: number, competitor_id: number): string {
  return `clubs/${club_id}/competitors/${competitor_id}/profile`;
}

/** Alta de afiliación (`POST .../affiliations`). */
export function competitorAffiliationsUrl(club_id: number, competitor_id: number): string {
  return `clubs/${club_id}/competitors/${competitor_id}/affiliations`;
}

/** Cambio de la afiliación primaria (`POST .../affiliations/{id}/primary`). */
export function competitorAffiliationPrimaryUrl(
  club_id: number,
  competitor_id: number,
  affiliation_id: number
): string {
  return `clubs/${club_id}/competitors/${competitor_id}/affiliations/${affiliation_id}/primary`;
}

/** Cierre de una afiliación (`POST .../affiliations/{id}/close`). */
export function competitorAffiliationCloseUrl(
  club_id: number,
  competitor_id: number,
  affiliation_id: number
): string {
  return `clubs/${club_id}/competitors/${competitor_id}/affiliations/${affiliation_id}/close`;
}

/** Paso a independiente (`POST .../independence`). */
export function competitorIndependenceUrl(club_id: number, competitor_id: number): string {
  return `clubs/${club_id}/competitors/${competitor_id}/independence`;
}

/** Clasificación de una inscripción (`GET .../classification`, solo lectura). */
export function registrationClassificationUrl(
  tournament_id: number,
  registration_id: number
): string {
  return `tournaments/${tournament_id}/registrations/${registration_id}/classification`;
}

/** Asignación de categoría (`POST .../category`). */
export function registrationCategoryUrl(tournament_id: number, registration_id: number): string {
  return `tournaments/${tournament_id}/registrations/${registration_id}/category`;
}

/**
 * Clasificación **previa** al alta (S4.8d §2): `POST .../registrations/classification-preview`.
 *
 * Es POST porque transporta un cuerpo, no porque escriba: el endpoint es funcionalmente de lectura
 * (no crea inscripción, no escribe auditoría, no consume clave de idempotencia), razón por la cual
 * aquí no hay `idempotentHeaders`.
 */
export function registrationClassificationPreviewUrl(tournament_id: number): string {
  return `tournaments/${tournament_id}/registrations/classification-preview`;
}

/** Logo de club: subir, reemplazar (con fichero) o quitar (sin fichero). */
export function sportsClubLogoUrl(club_id: number, sports_club_id: number): string {
  return `clubs/${club_id}/sports-clubs/${sports_club_id}/logo`;
}

/**
 * Catálogo real de cinturones del club (`GET /clubs/{club_id}/belts`, `list_belts_endpoint`,
 * `routes/domain_catalog.py:56`). Solo lectura: la UI no hardcodea el orden, lo recibe.
 */
export function beltsUrl(club_id: number): string {
  return `clubs/${club_id}/belts`;
}

/**
 * Clubes del club (`GET /clubs/{club_id}/sports-clubs`, `list_sports_clubs_endpoint`,
 * `routes/domain_catalog.py:75`). Solo lectura; el ámbito del tenant se aplica en el backend.
 */
export function sportsClubsUrl(club_id: number, includeInactive = false): string {
  const base = `clubs/${club_id}/sports-clubs`;
  // La gestión de Clubes necesita ver también los dados de baja (S4.8g-2 §7/§15); los selectores
  // funcionales se quedan con el defecto del backend: solo activos.
  return includeInactive ? `${base}?include_inactive=true` : base;
}

/** Club concreto (`PATCH /clubs/{club_id}/sports-clubs/{sports_club_id}`, S4.8g-2 §14). */
export function sportsClubUrl(club_id: number, sports_club_id: number): string {
  return `clubs/${club_id}/sports-clubs/${sports_club_id}`;
}
