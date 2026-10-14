/**
 * Contrato HTTP de F3 que consume el frontend del organizador (S4.1).
 *
 * Fuente de verdad: `bracket/routes/domain_registrations.py` y `bracket/routes/domain_competitors.py`
 * del release F3. Aquí viven las rutas, los cuerpos, los vocabularios y las reglas de transición que
 * la UI **no** puede inventar: la interfaz solo ofrece lo que el backend acepta.
 *
 * El módulo no importa nada **en tiempo de ejecución** (ni React, ni axios, ni i18next; el único
 * `import` es de tipos, que se borra) para que los tests puedan ejercerlo sin red y sin navegador.
 * Los textos visibles son claves de traducción, nunca frases: el copy vive en `public/locales`.
 */

import type { CompetitorSex, Modality } from './f3_competitor_contract.ts';

/** Cabecera del protocolo de idempotencia (S3.3b). No se muestra al usuario. */
export const IDEMPOTENCY_KEY_HEADER = 'Idempotency-Key';

/** Mínimo de inscritos confirmados que exige la generación de cuadros (`MINIMUM_ENTRANTS`). */
export const MINIMUM_ENTRANTS = 2;

export type RegistrationStatus = 'DRAFT' | 'CONFIRMED' | 'WITHDRAWN' | 'DISQUALIFIED' | 'CORRECTED';

/** Estados de inscripción que el backend publica hoy en el listado. */
export const REGISTRATION_STATUSES: RegistrationStatus[] = [
  'DRAFT',
  'CONFIRMED',
  'WITHDRAWN',
  'DISQUALIFIED',
];

export type RegistrationWindowState = 'OPEN' | 'CLOSED' | 'DEADLINE_PASSED';
export type RegistrationIdentityStatus = 'UNVERIFIED' | 'AMBIGUOUS' | 'VERIFIED';
export type RegistrationRepresentation = 'INDEPENDENT' | 'CLUB';

/** Bloqueos de la revisión: vocabulario cerrado que el cliente traduce, código a código. */
export type BracketReadinessBlocker =
  | 'REGISTRATION_WINDOW_OPEN'
  | 'REGISTRATIONS_PENDING'
  | 'CATEGORY_NOT_ENOUGH_COMPETITORS'
  | 'COMPETITOR_INACTIVE'
  | 'REGISTRATION_INCONSISTENT'
  | 'INVALID_CATEGORY'
  | 'BRACKET_ALREADY_EXISTS'
  // S4.8a-4 §17: bloqueos de clasificación (a-3 §9), el mismo vocabulario cerrado del backend.
  | 'MISSING_DATE_OF_BIRTH'
  | 'MISSING_SEX'
  | 'MISSING_BELT'
  | 'MISSING_WEIGHT'
  | 'MISSING_MODALITY'
  | 'CATEGORY_NOT_RESOLVED'
  | 'CATEGORY_INCOMPATIBLE'
  | 'NO_COMPATIBLE_CATEGORY'
  | 'INVALID_CATEGORY_BELT_RANGE';

/**
 * Competidor tal y como lo devuelve `GET /clubs/{club_id}/competitors`
 * (`bracket/models/db/competitor_api.py`). Incluye el perfil deportivo desde S4.8a: `date_of_birth`,
 * `sex` y `belt_id` — la lista ya los trae, así que el formulario de inscripción puede etiquetar
 * cada opción y el perfil puede abrirse con los valores reales de la fila en lugar de nulls (S4.8d
 * §3 y §6). La **edad** no está aquí a propósito: se deriva en el cliente y nunca se persiste.
 *
 * Desde S4.8d §3 la **misma** respuesta publica además la afiliación **actual** del competidor
 * (`affiliation_mode`, `sports_club_id`, `sports_club_name`), que es lo único que hace falta para
 * mostrar "club / independiente" en el desplegable sin una consulta por competidor. Es el
 * estado actual, no una decisión: el club representado de cada inscripción se sigue eligiendo en
 * el alta. `INDEPENDENT` con los dos campos en `null` = sin afiliación abierta.
 */
export interface Competitor {
  id: number;
  display_name: string;
  active: boolean;
  date_of_birth: string | null;
  sex: CompetitorSex | null;
  belt_id: number | null;
  affiliation_mode: RegistrationRepresentation;
  sports_club_id: number | null;
  sports_club_name: string | null;
  created: string;
  updated_at: string | null;
}

export interface CompetitorList {
  count: number;
  competitors: Competitor[];
}

export interface Registration {
  id: number;
  tournament_id: number;
  competitor_id: number | null;
  identity_status: RegistrationIdentityStatus;
  representation: RegistrationRepresentation;
  sports_club_id: number | null;
  category_key: string | null;
  category_label: string | null;
  /** Peso declarado en kg (S4.8a-4 §8). Pydantic serializa `Decimal` como cadena; puede ser nulo. */
  declared_weight_kg: string | null;
  /** Modalidad declarada (S4.8a-4 §9): `GI`/`NO_GI`, o nulo si no se declara. */
  modality: Modality | null;
  competitor_name_snapshot: string;
  sports_club_name_snapshot: string | null;
  status: RegistrationStatus;
  revision: number;
  created: string;
  updated_at: string | null;
}

export interface RegistrationList {
  count: number;
  registrations: Registration[];
}

export interface RegistrationWindow {
  tournament_id: number;
  registration_deadline: string | null;
  registrations_closed_at: string | null;
  state: RegistrationWindowState;
  registrations_open: boolean;
}

export interface ReadinessIssue {
  code: BracketReadinessBlocker;
  count: number | null;
  category_key: string | null;
  required_minimum: number | null;
}

export interface ReadinessCategory {
  category_key: string | null;
  category_label: string | null;
  confirmed_count: number;
  required_minimum: number;
  ready: boolean;
  blocking_issues: ReadinessIssue[];
}

export interface ReadinessSummary {
  total: number;
  confirmed: number;
  draft: number;
  withdrawn: number;
  disqualified: number;
}

export interface BracketReadiness {
  tournament_id: number;
  ready: boolean;
  registration_window: RegistrationWindowState;
  summary: ReadinessSummary;
  categories: ReadinessCategory[];
  blocking_issues: ReadinessIssue[];
}

export interface GeneratedStageItem {
  stage_item_id: number;
  entrant_count: number;
  bracket_size: number;
  bye_count: number;
  ghost_count: number;
}

export interface GenerateBracketResult {
  tournament_id: number;
  generated: boolean;
  stage_items: GeneratedStageItem[];
  skipped_stage_items: number;
}

export interface CompetitorCreateBody {
  display_name: string;
  reason_code?: string | null;
  reason_note?: string | null;
}

export interface RegistrationCreateBody {
  competitor_id: number;
  representation: RegistrationRepresentation;
  category_key: string;
  category_label: string;
  /** Club representada: obligatoria si `representation` es `CLUB`, ausente si es `INDEPENDENT`. */
  sports_club_id?: number | null;
  /** Peso declarado en kg (S4.8a-4 §8): decimal `>0` y `<=300`, u omitido si no se declara. */
  declared_weight_kg?: string | null;
  /** Modalidad (S4.8a-4 §9): `GI`/`NO_GI`, u omitida si no se declara. */
  modality?: Modality | null;
  reason_code?: string | null;
}

export interface LifecycleBody {
  reason_code?: string | null;
  reason_note?: string | null;
}

/* -------------------------------------------------------------------------------------------- */
/* Rutas. Se construyen una sola vez aquí: los servicios y los tests comparten la misma fuente.  */
/* -------------------------------------------------------------------------------------------- */

export function competitorsUrl(club_id: number): string {
  return `clubs/${club_id}/competitors`;
}

export function competitorUrl(club_id: number, competitor_id: number): string {
  return `clubs/${club_id}/competitors/${competitor_id}`;
}

export function competitorActionUrl(
  club_id: number,
  competitor_id: number,
  action: 'rename' | 'activate' | 'deactivate'
): string {
  return `${competitorUrl(club_id, competitor_id)}/${action}`;
}

export function registrationsUrl(tournament_id: number): string {
  return `tournaments/${tournament_id}/registrations`;
}

export function registrationUrl(tournament_id: number, registration_id: number): string {
  return `tournaments/${tournament_id}/registrations/${registration_id}`;
}

export function registrationActionUrl(
  tournament_id: number,
  registration_id: number,
  action: RegistrationAction
): string {
  return `${registrationUrl(tournament_id, registration_id)}/${action}`;
}

export function closeRegistrationsUrl(tournament_id: number): string {
  return `tournaments/${tournament_id}/registrations/close`;
}

export function registrationWindowUrl(tournament_id: number): string {
  return `tournaments/${tournament_id}/registration-window`;
}

export function bracketReadinessUrl(tournament_id: number): string {
  return `tournaments/${tournament_id}/bracket-readiness`;
}

export interface StageItemCourtBody {
  court_id: number | null;
}

/** Respuesta de la asignacion: el efecto de **esa** operacion (S4.2). */
export interface StageItemCourtResult {
  stage_item_id: number;
  court_id: number | null;
  court_name: string | null;
  affected_match_count: number;
}

export function generateBracketUrl(tournament_id: number): string {
  return `tournaments/${tournament_id}/generate-bracket`;
}

/**
 * Asignación de un cuadro entero a un tatami (S4.2). `null` desasigna.
 *
 * Es la única vía soportada: el cuadro es la unidad de asignación y el backend propaga el tatami a
 * todos sus combates dentro de la misma transacción.
 */
export function stageItemCourtUrl(tournament_id: number, stage_item_id: number): string {
  return `tournaments/${tournament_id}/stage-items/${stage_item_id}/court`;
}

/** Listado de competidores: los filtros son los del backend (`display_name`, `include_inactive`). */
export function competitorsQuery(params: {
  display_name?: string;
  include_inactive?: boolean;
  limit?: number;
  offset?: number;
}): string {
  const query = new URLSearchParams();
  if (params.display_name != null && params.display_name.length > 0) {
    query.set('display_name', params.display_name);
  }
  query.set('include_inactive', String(params.include_inactive ?? false));
  query.set('limit', String(params.limit ?? 25));
  query.set('offset', String(params.offset ?? 0));
  return query.toString();
}

export function registrationsQuery(params: {
  registration_status?: RegistrationStatus;
  competitor_id?: number;
  limit?: number;
  offset?: number;
}): string {
  const query = new URLSearchParams();
  if (params.registration_status != null) {
    query.set('registration_status', params.registration_status);
  }
  if (params.competitor_id != null) {
    query.set('competitor_id', String(params.competitor_id));
  }
  query.set('limit', String(params.limit ?? 25));
  query.set('offset', String(params.offset ?? 0));
  return query.toString();
}

/* -------------------------------------------------------------------------------------------- */
/* Transiciones. La UI no ofrece una transición que el backend no permita.                        */
/* -------------------------------------------------------------------------------------------- */

export type RegistrationAction = 'confirm' | 'withdraw' | 'reinstate' | 'disqualify';

export interface RegistrationTransition {
  action: RegistrationAction;
  /** `true` cuando la operación exige relación OWNER con el club (el backend responde 403 si no). */
  requires_owner: boolean;
}

/**
 * Espejo de las guardas de `bracket/logic/registrations.py`:
 *
 * - un borrador se confirma (idempotente) y se retira;
 * - una inscripción confirmada solo la retira o la descalifica un OWNER;
 * - una retirada se readmite y vuelve a CONFIRMED;
 * - descalificar es terminal: no hay vuelta atrás.
 */
const TRANSITIONS: Record<RegistrationStatus, RegistrationTransition[]> = {
  DRAFT: [
    { action: 'confirm', requires_owner: false },
    { action: 'withdraw', requires_owner: false },
  ],
  CONFIRMED: [
    { action: 'withdraw', requires_owner: true },
    { action: 'disqualify', requires_owner: true },
  ],
  WITHDRAWN: [{ action: 'reinstate', requires_owner: false }],
  DISQUALIFIED: [],
  CORRECTED: [],
};

/** Transiciones disponibles para un estado. Un estado desconocido no ofrece ninguna. */
export function allowedTransitions(status: string): RegistrationTransition[] {
  return TRANSITIONS[status as RegistrationStatus] ?? [];
}

export function isTerminalStatus(status: string): boolean {
  return allowedTransitions(status).length === 0;
}

/** Claves de traducción de los estados visibles. */
export function registrationStatusKey(status: string): string {
  return `registration_status_${String(status).toLowerCase()}`;
}

export function registrationWindowKey(state: string): string {
  return `registration_window_${String(state).toLowerCase()}`;
}

export function registrationActionKey(action: RegistrationAction): string {
  return `registration_action_${action}`;
}

export function readinessBlockerKey(code: string): string {
  return `readiness_blocker_${String(code).toLowerCase()}`;
}

/** ¿Admite altas nuevas la ventana? Solo `OPEN`; el resto responde 409 REGISTRATION_CLOSED. */
export function windowAdmitsNewRegistrations(state: string): boolean {
  return state === 'OPEN';
}

/** Motivos de auditoría que el backend acepta por acción (catálogo cerrado de S3.3c-2). */
export const REGISTRATION_REASON_CODES: Record<RegistrationAction, string[]> = {
  confirm: ['READY', 'DATA_VERIFIED'],
  withdraw: ['WITHDRAWAL_REQUEST', 'DUPLICATE', 'ELIGIBILITY_LOST', 'ADMINISTRATIVE'],
  reinstate: ['MISTAKEN_WITHDRAWAL', 'ELIGIBILITY_REGAINED', 'ADMINISTRATIVE'],
  disqualify: ['SCHEDULING_NO_SHOW', 'RULE_VIOLATION', 'ELIGIBILITY', 'ADMINISTRATIVE'],
};

/** Motivos del alta de inscripción y del alta de competidor (un único código admitido). */
export const REGISTRATION_CREATE_REASON_CODE = 'PLANNED_ENTRY';
export const COMPETITOR_CREATE_REASON_CODE = 'PLANNED_ENTRY';
export const COMPETITOR_RENAME_REASON_CODE = 'DATA_CORRECTION';
export const COMPETITOR_LIFECYCLE_REASON_CODES: string[] = [
  'REQUESTED_BY_ATHLETE',
  'DUPLICATE',
  'DATA_ERROR',
  'ADMINISTRATIVE',
];

/** Etiqueta de una clave de categoría para la persona que organiza (nunca el identificador crudo). */
export function categoryDisplayName(
  category_key: string | null,
  category_label: string | null
): string {
  if (category_label != null && category_label.length > 0) {
    return category_label;
  }
  if (category_key != null && category_key.length > 0) {
    return category_key;
  }
  return '';
}

/** Formato que exige el backend para `category_key` (slug en minúsculas). */
export const CATEGORY_KEY_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

export const CATEGORY_KEY_MAX_LENGTH = 64;

export const CATEGORY_LABEL_MAX_LENGTH = 200;

/**
 * Deriva la clave estable a partir de la etiqueta que escribe el organizador ("Gi -83 kg" →
 * `gi-83-kg`). El organizador **no** debería tener que saber que existe una clave: la escribe la
 * interfaz y se le muestra en secundario para que la reconozca si la ve en otro sitio.
 */
export function categoryKeyFromLabel(label: string): string {
  return label
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, CATEGORY_KEY_MAX_LENGTH)
    .replace(/-+$/g, '');
}

export function isValidCategoryKey(key: string): boolean {
  return key.length > 0 && key.length <= CATEGORY_KEY_MAX_LENGTH && CATEGORY_KEY_PATTERN.test(key);
}

/** Orden estable del bloque de categorías: por etiqueta visible y, si empatan, por clave. */
export function sortCategories<
  T extends { category_key: string | null; category_label: string | null },
>(categories: T[]): T[] {
  return [...categories].sort((left, right) => {
    const leftName = categoryDisplayName(left.category_key, left.category_label);
    const rightName = categoryDisplayName(right.category_key, right.category_label);
    return (
      leftName.localeCompare(rightName) ||
      String(left.category_key).localeCompare(String(right.category_key))
    );
  });
}

/* -------------------------------------------------------------------------------------------- */
/* S4.8a-4 §17 — bloqueos de CLASIFICACIÓN dentro de la revisión de readiness.                   */
/* -------------------------------------------------------------------------------------------- */

/**
 * Códigos de `bracket-readiness` que describen un problema de **clasificación** de una inscripción
 * (a-3 §9). Salen del mismo `BracketReadinessBlocker` que la revisión publica: son el prefijo de
 * clasificación del vocabulario cerrado. La UI los aparta del resto para pintarlos con nombre y
 * motivo (§17), en vez de dejar solo un `ready=false`.
 */
export const CLASSIFICATION_READINESS_BLOCKERS: BracketReadinessBlocker[] = [
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

/** ¿El código es un bloqueo de **clasificación** y no otro tipo de bloqueo de la revisión? */
export function isClassificationReadinessBlocker(code: string): boolean {
  return (CLASSIFICATION_READINESS_BLOCKERS as string[]).includes(code);
}

/** Bloqueos de clasificación agrupados por categoría, para pintarlos con nombre y motivo (§17). */
export interface ClassificationBlockedCategory {
  category_key: string | null;
  category_label: string | null;
  issues: ReadinessIssue[];
}

/**
 * Proyección de la revisión: los bloqueos de clasificación, agrupados por categoría (las
 * inscripciones sin clasificar viajan con `category_key` nulo). No inventa nombres: usa la etiqueta
 * que publica el backend. Devuelve `[]` cuando ninguna inscripción está bloqueada por clasificación.
 */
export function classificationBlockedCategories(
  readiness: Pick<BracketReadiness, 'blocking_issues' | 'categories'>
): ClassificationBlockedCategory[] {
  const rows: ClassificationBlockedCategory[] = [];

  const atTournamentLevel = readiness.blocking_issues.filter((issue) =>
    isClassificationReadinessBlocker(issue.code)
  );
  if (atTournamentLevel.length > 0) {
    rows.push({ category_key: null, category_label: null, issues: atTournamentLevel });
  }

  for (const category of sortCategories(readiness.categories)) {
    const issues = category.blocking_issues.filter((issue) =>
      isClassificationReadinessBlocker(issue.code)
    );
    if (issues.length > 0) {
      rows.push({
        category_key: category.category_key,
        category_label: category.category_label,
        issues,
      });
    }
  }

  return rows;
}

/**
 * Nº de inscripciones afectadas por los bloqueos de clasificación: la suma de los `count` que
 * publica cada bloqueo (1 cuando el bloqueo no cuenta inscripciones).
 */
export function classificationBlockedCount(rows: ClassificationBlockedCategory[]): number {
  return rows.reduce(
    (total, row) => total + row.issues.reduce((sum, issue) => sum + (issue.count ?? 1), 0),
    0
  );
}

/* -------------------------------------------------------------------------------------------- */
/* S4.5 — pantalla pública del planificador (la grada).                                          */
/* Los estados los decide el servidor: la pantalla los pinta, no los calcula.                    */
/* -------------------------------------------------------------------------------------------- */

/** Sondeo de la pantalla, en milisegundos: dentro de la ventana de 5–10 s que pide la grada. */
export const PUBLIC_SCHEDULE_POLL_MS = 5_000;

/** Estados que publica `GET /public/tournaments/{tid}/schedule` (vocabulario cerrado de S4.5). */
export type PublicScheduleState = 'NOW' | 'NEXT' | 'UPCOMING' | 'WAITING' | 'COMPLETED' | 'DELAYED';

export const PUBLIC_SCHEDULE_STATES: PublicScheduleState[] = [
  'NOW',
  'NEXT',
  'UPCOMING',
  'WAITING',
  'COMPLETED',
  'DELAYED',
];

/** Ficha pública de un combate: solo datos deportivos, nada de cuenta. */
export interface PublicScheduleMatch {
  match_id: number;
  category: string;
  round_name: string;
  fighter_a: string | null;
  fighter_b: string | null;
  scheduled_start: string | null;
  estimated_start: string | null;
  state: string;
  position_in_schedule: number | null;
  waiting_for: string[];
}

/** Bloque de un tatami: AHORA, SIGUIENTE, los siguientes y lo último terminado. */
export interface PublicScheduleCourt {
  court_id: number;
  court_name: string;
  lag_minutes: number;
  current: PublicScheduleMatch | null;
  next: PublicScheduleMatch | null;
  upcoming: PublicScheduleMatch[];
  completed: PublicScheduleMatch[];
}

/** Cuerpo público completo (`tournament`, `generated_at`, `courts`). */
export interface PublicSchedulePayload {
  tournament: { name: string; start_time: string | null };
  generated_at: string;
  courts: PublicScheduleCourt[];
}

/**
 * Ruta pública del calendario, sin sesión.
 *
 * La puerta la abre el panel público del torneo: si está apagado, el backend responde 401 y la
 * pantalla lo dice (no se cae).
 */
export function publicScheduleUrl(tournament_id: number): string {
  return `public/tournaments/${tournament_id}/schedule`;
}

/** Clave de traducción del estado visible: `NOW` → `public_state_now`. */
export function publicStateKey(state: string): string {
  return `public_state_${String(state).toLowerCase()}`;
}

/** Un estado que la pantalla no conoce no se pinta como si fuera uno de los suyos. */
export function isPublicScheduleState(state: string): boolean {
  return (PUBLIC_SCHEDULE_STATES as string[]).includes(state);
}
