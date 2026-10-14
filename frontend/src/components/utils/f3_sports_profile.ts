/**
 * Lógica pura del perfil deportivo del competidor y del club (S4.8a-4 §3-§7).
 *
 * Aquí vive **solo** lo que la UI decide sin hablar con el backend: cómo ordenar y etiquetar el
 * catálogo de cinturones, qué opciones de sexo ofrecer, cómo validar la fecha de nacimiento como
 * UX, qué acción de afiliación corresponde al club elegido y cómo validar el logo antes de subirlo.
 *
 * Reglas duras que respeta este módulo:
 *
 * - **no duplica reglas deportivas**: no decide categoría, no impone qué campos son obligatorios por
 *   reglamento; el backend es la autoridad (`bracket/logic/classification`). La **edad** que calcula
 *   `derivedAge` es solo presentación: se deriva de `date_of_birth` + fecha de referencia, no se
 *   envía nunca al backend y no decide escala de cinturón ni categoría (S4.8d §3);
 * - **no hardcodea el orden del catálogo**: el orden de cinturones sale del `rank`/`code` que publica
 *   el backend y del orden de escalas (`BELT_SCOPES`) del contrato; el módulo recibe esos datos;
 * - `MIXED` **nunca** es una opción de sexo de competidor: describe una categoría, no a una persona
 *   (`bracket/models/db/domain.py:52-54`);
 * - el módulo **no importa valores** de otros módulos en tiempo de ejecución (solo `import type`, que
 *   se borra), para que `node --test` lo ejerza sin red y sin navegador, igual que `f3_contract.ts`.
 *
 * Fuente de verdad:
 *   - `bracket/models/db/catalog_api.py`      (forma de `BeltResponse`)
 *   - `bracket/models/db/sports_club_api.py`  (forma de `SportsClubResponse`)
 *   - `bracket/routes/domain_competitors.py`  (rutas de perfil, afiliación e independencia)
 *   - `bracket/routes/domain_sports_clubs.py` (límites del logo: 2 MB, PNG/JPEG/WEBP)
 *   - `bracket/logic/audit_reasons.py:98`     (motivo `DATA_CORRECTION` del alcance `competitor`)
 *   - `bracket/models/db/domain.py:53-59`     (CompetitorSex, BeltScope)
 */

import type { Belt, BeltScope, CompetitorSex, SportsClub } from './f3_competitor_contract.ts';

/* --------------------------------------------------------------------------------- sexo (§4) --- */

/** Valor de la opción «independiente» en el selector de club. Nunca es un `sports_club_id`. */
export const SPORTS_CLUB_INDEPENDENT = 'independent';

/**
 * Motivo de auditoría del reemplazo de perfil. El catálogo cerrado del alcance `competitor`
 * admite `DATA_CORRECTION` y `AFFILIATION_CHANGE` (`audit_reasons.py:98`); el perfil es una
 * corrección de datos.
 */
export const COMPETITOR_PROFILE_REASON_CODE = 'DATA_CORRECTION';

/** Clave i18n del sexo de la persona. Solo dos: `MIXED` no es un sexo de competidor. */
export const COMPETITOR_SEX_KEYS: Record<CompetitorSex, string> = {
  MALE: 'f3_sex_male',
  FEMALE: 'f3_sex_female',
};

/** Clave i18n de un sexo de competidor. */
export function sexLabelKey(sex: CompetitorSex): string {
  return COMPETITOR_SEX_KEYS[sex];
}

export interface SexOption {
  value: CompetitorSex;
  labelKey: string;
}

/**
 * Opciones del selector de sexo. Filtra explícitamente lo que no sea `MALE`/`FEMALE`: aunque una
 * versión futura del backend publicara más valores, `MIXED` no puede llegar a la pantalla como sexo
 * de una persona.
 */
export function sexOptions(sexes: readonly string[]): SexOption[] {
  return sexes
    .filter((sex): sex is CompetitorSex => sex === 'MALE' || sex === 'FEMALE')
    .map((sex) => ({ value: sex, labelKey: sexLabelKey(sex) }));
}

/* ------------------------------------------------------------------ cinturones (§3) ------------- */

/** Clave i18n de la etiqueta de una escala. */
export function beltScopeLabelKey(scope: BeltScope): string {
  return `f3_profile_belt_scope_${scope.toLowerCase()}`;
}

/** Clave i18n del nombre de un cinturón, derivada del `code` estable del catálogo. */
export function beltLabelKey(code: string): string {
  return `f3_belt_${code.toLowerCase()}`;
}

/**
 * Ordena el catálogo **según los datos**: primero por escala (en el orden de `scopeOrder`, que es el
 * que fija el contrato) y, dentro de cada escala, por `rank` y `code` desempatando. No conoce ningún
 * orden de cinturones: si el backend reordena el catálogo, esta función lo respeta.
 */
export function sortBelts(belts: readonly Belt[], scopeOrder: readonly BeltScope[]): Belt[] {
  const order = new Map(scopeOrder.map((scope, index) => [scope, index]));
  return [...belts].sort((a, b) => {
    const scopeA = order.get(a.scope) ?? scopeOrder.length;
    const scopeB = order.get(b.scope) ?? scopeOrder.length;
    if (scopeA !== scopeB) {
      return scopeA - scopeB;
    }
    if (a.rank !== b.rank) {
      return a.rank - b.rank;
    }
    return a.code.localeCompare(b.code);
  });
}

/**
 * Etiqueta visible de un cinturón: la traducción si existe en el locale y, si no, la etiqueta
 * canónica que publica el backend (`BeltResponse.label`). El `label` del backend es el respaldo, no
 * un texto a conservar por encima de la traducción.
 */
export function beltLabel(belt: Belt, lookup: (key: string) => string | null): string {
  return lookup(beltLabelKey(belt.code)) ?? belt.label;
}

export interface BeltChoice {
  value: string;
  label: string;
  scope: BeltScope;
  code: string;
  rank: number;
}

/** Opciones del selector de cinturón: catálogo ordenado y etiquetado, con la escala a la vista. */
export function beltOptions(
  belts: readonly Belt[],
  scopeOrder: readonly BeltScope[],
  lookup: (key: string) => string | null
): BeltChoice[] {
  // Solo las escalas pedidas: el catálogo trae la escala juvenil y la adulta, y el perfil de
  // competición debe ofrecer únicamente la adulta (S4.8d §4). Antes se devolvían todas las escalas
  // y el filtrado dependía de que el componente las agrupara.
  const inScope = belts.filter((belt) => scopeOrder.includes(belt.scope));
  return sortBelts(inScope, scopeOrder).map((belt) => ({
    value: String(belt.id),
    label: beltLabel(belt, lookup),
    scope: belt.scope,
    code: belt.code,
    rank: belt.rank,
  }));
}

/* --------------------------------------------------------------------- club (§6) ---------------- */

export interface SportsClubChoice {
  value: string;
  name: string | null;
  logo_path: string | null;
}

/**
 * Opciones del selector de club: las academias disponibles seguido de la opción «Independiente».
 * La UI nunca acepta texto libre para el club; el valor viaja como el `id` de la academia o como el
 * centinela de independiente.
 */
export function sportsClubOptions(clubs: readonly SportsClub[]): SportsClubChoice[] {
  return [
    ...clubs.map((club) => ({
      value: String(club.id),
      name: club.name,
      logo_path: club.logo_path,
    })),
    { value: SPORTS_CLUB_INDEPENDENT, name: null, logo_path: null },
  ];
}

/** Academia vigente de un competidor: la afiliación sin `valid_to` (la respuesta por defecto). */
export function currentSportsClubId(
  affiliations: readonly { sports_club_id: number; valid_to?: string | null }[]
): number | null {
  const current = affiliations.find((affiliation) => affiliation.valid_to == null);
  return current != null ? current.sports_club_id : null;
}

export type AffiliationAction =
  | { kind: 'none' }
  | { kind: 'assign'; sports_club_id: number }
  | { kind: 'independent' };

/**
 * Qué endpoint de afiliación usar según el club elegido. La UI no toca histórico de inscripciones:
 * asignar afiliación e independencia son acciones del dominio (`domain_competitors.py`) y esta
 * función solo decide **cuál**, no la aplica. Si no hay cambio, no hay escritura.
 */
export function affiliationAction(
  currentSportsClubIdValue: number | null,
  chosen: string
): AffiliationAction {
  if (chosen === SPORTS_CLUB_INDEPENDENT) {
    return currentSportsClubIdValue == null ? { kind: 'none' } : { kind: 'independent' };
  }
  const target = Number(chosen);
  if (!Number.isFinite(target) || String(target) !== chosen.trim()) {
    return { kind: 'none' };
  }
  return currentSportsClubIdValue === target
    ? { kind: 'none' }
    : { kind: 'assign', sports_club_id: target };
}

/* ------------------------------------------------------- perfil deportivo (perfil, §3/§4/§5) ---- */

/** Campos del perfil deportivo que el organizador puede declarar (los tres son nulables). */
export interface CompetitorSportsProfileValues {
  date_of_birth: string | null;
  sex: CompetitorSex | null;
  belt_id: number | null;
}

/** Competidor objetivo del modal de perfil: identidad visible + perfil deportivo. */
export interface CompetitorProfileTarget extends CompetitorSportsProfileValues {
  id: number;
  display_name: string;
}

/** Cuerpo del reemplazo de perfil: los tres campos viajan **siempre** (es un reemplazo). */
export function profileBody(
  values: CompetitorSportsProfileValues,
  reason_code: string | null = COMPETITOR_PROFILE_REASON_CODE
): {
  date_of_birth: string | null;
  sex: CompetitorSex | null;
  belt_id: number | null;
  reason_code: string | null;
  reason_note: string | null;
} {
  return {
    date_of_birth: values.date_of_birth,
    sex: values.sex,
    belt_id: values.belt_id,
    reason_code,
    reason_note: null,
  };
}

/** ¿Cambió algo del perfil declarado? Sin cambios reales no se escribe ni se audita. */
export function profileChanged(
  initial: CompetitorSportsProfileValues,
  next: CompetitorSportsProfileValues
): boolean {
  return (
    initial.date_of_birth !== next.date_of_birth ||
    initial.sex !== next.sex ||
    initial.belt_id !== next.belt_id
  );
}

/* ------------------------------------------------------------- fecha de nacimiento (§5) --------- */

/** Formato local del `<input type="date">`: `YYYY-MM-DD`, sin zona horaria. */
export const DATE_OF_BIRTH_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

/** ¿La cadena es un `YYYY-MM-DD` que existe en el calendario? (`2026-02-31` no lo es). */
export function isRealDate(value: string): boolean {
  if (!DATE_OF_BIRTH_PATTERN.test(value)) {
    return false;
  }
  const [year, month, day] = value.split('-').map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return (
    date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day
  );
}

/** Fecha local en el mismo formato que el input (`YYYY-MM-DD`), sin desplazar por zona horaria. */
export function toIsoDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

/**
 * Validación de **UX** de la fecha de nacimiento: formato local válido y no futura. Devuelve la
 * clave del mensaje o `null`. `required` es `false` por defecto: el frontend **no** impone la
 * obligatoriedad; es el motor de clasificación quien declara `MISSING_DATE_OF_BIRTH` cuando el
 * reglamento la exige. La comparación lexicográfica basta porque el formato es `YYYY-MM-DD`.
 */
export function dateOfBirthError(value: string, today: string, required = false): string | null {
  const trimmed = (value ?? '').trim();
  if (trimmed === '') {
    return required ? 'f3_profile_dob_required' : null;
  }
  if (!isRealDate(trimmed)) {
    return 'f3_profile_dob_invalid';
  }
  if (trimmed > today) {
    return 'f3_profile_dob_future';
  }
  return null;
}

/* ------------------------------------------------------------- edad derivada (S4.8d §3) -------- */

/**
 * Edad **derivada** del competidor, en años cumplidos.
 *
 * La edad no es un dato del competidor: se calcula aquí para mostrarla (read-only) a partir de
 * `date_of_birth` y de una fecha de referencia (hoy, o la fecha del torneo si se conoce). Devolver
 * `null` cuando no hay fecha o no es una fecha real es intencionado: la UI no inventa edades ni
 * las envía al backend (§3).
 *
 * Comparación por fecha local (`YYYY-MM-DD`), sin `Date` de zona horaria: el cumpleaños cuenta al
 * llegar el día, no antes.
 */
export function derivedAge(
  dateOfBirth: string | null | undefined,
  referenceDate: string
): number | null {
  const birth = (dateOfBirth ?? '').trim();
  if (!isRealDate(birth) || !isRealDate(referenceDate)) {
    return null;
  }

  const [birthYear, birthMonth, birthDay] = birth.split('-').map(Number);
  const [refYear, refMonth, refDay] = referenceDate.split('-').map(Number);

  let age = refYear - birthYear;
  const beforeBirthday = refMonth < birthMonth || (refMonth === birthMonth && refDay < birthDay);
  if (beforeBirthday) {
    age -= 1;
  }
  return age < 0 ? null : age;
}

/** Clave i18n de la etiqueta de edad mostrada junto a la fecha de nacimiento. */
export const DERIVED_AGE_LABEL_KEY = 'f3_profile_age_label';

/**
 * Escalas de cinturón que ofrece el perfil del competidor: la **adulto**, que es el catálogo de
 * competición de este proyecto (Blanco · Azul · Violeta · Marrón · Negro, S4.8d §4). La escala
 * juvenil no se borra del catálogo del backend: queda para un flujo juvenil futuro.
 */
export const COMPETITION_BELT_SCOPES: BeltScope[] = ['ADULT'];

/* ------------------------------------------------------------- logo de academia (§7) ------------ */

/** Iniciales para el fallback del logo: hasta dos letras, en mayúscula. Vacío si no hay nombre. */
export function logoInitials(name: string | null | undefined): string {
  if (name == null) {
    return '';
  }
  const words = name
    .trim()
    .split(/\s+/)
    .filter((word) => word.length > 0);
  return words
    .slice(0, 2)
    .map((word) => word[0].toUpperCase())
    .join('');
}

export interface ClubLogoDisplay {
  hasLogo: boolean;
  src: string | null;
  initials: string;
  name: string;
}

/**
 * Proyección de pintado del logo. `logo_path` ya es la ruta **relativa** que publica el backend
 * (`static/sports-club-logos/<fichero>`), así que la URL se compone con la base de la API. Sin logo,
 * la UI pinta el fallback (iniciales o icono neutro), nunca una imagen rota.
 */
export function sportsClubLogoDisplay(club: SportsClub | null, baseUrl: string): ClubLogoDisplay {
  const name = club?.name ?? '';
  const path = club?.logo_path ?? null;
  const hasLogo = path != null && path.trim() !== '';
  return {
    hasLogo,
    src: hasLogo ? `${baseUrl.replace(/\/$/, '')}/${path}` : null,
    initials: logoInitials(name),
    name,
  };
}

/** Fichero tal como lo ve el navegador antes de subirlo. */
export interface LogoFileLike {
  name: string;
  size: number;
  type?: string;
}

/** Límites reales del backend (se pasan desde el contrato, no se copian aquí). */
export interface LogoLimits {
  maxBytes: number;
  mimeTypes: readonly string[];
  extensions: readonly string[];
}

/**
 * Validación de **UX** del logo antes de subirlo: tamaño y tipo declarado. El backend vuelve a
 * validar por **contenido** (`domain_sports_clubs.py`, sniffing + cotejo con la extensión), así que
 * esto es un aviso temprano, no la autoridad. Devuelve la clave del mensaje o `null`.
 */
export function logoFileError(file: LogoFileLike, limits: LogoLimits): string | null {
  if (!(file.size > 0) || file.size > limits.maxBytes) {
    return 'f3_club_logo_too_large';
  }
  const dot = file.name.lastIndexOf('.');
  const extension = dot >= 0 ? file.name.slice(dot).toLowerCase() : '';
  if (!limits.extensions.includes(extension)) {
    return 'f3_club_logo_wrong_type';
  }
  if (file.type != null && file.type !== '' && !limits.mimeTypes.includes(file.type)) {
    return 'f3_club_logo_wrong_type';
  }
  return null;
}

/* ------------------------------------------------------- alta en un solo flujo (S4.8g §5) ------- */

/** Datos del alta en un solo flujo: identidad visible + perfil declarado (§5). */
export interface CompetitorCreateInput extends CompetitorSportsProfileValues {
  display_name: string;
}

/**
 * Cuerpo del alta de competidor **con el perfil declarado**.
 *
 * El backend ya acepta `date_of_birth`, `sex` y `belt_id` en el alta (`CompetitorCreateBody`), así que
 * la UI no necesita encadenar un «alta» y un «reemplazo de perfil»: se pide una vez y se envía una vez.
 * El motivo del alta llega como parámetro para que este módulo no importe valores de otros módulos en
 * tiempo de ejecución (solo `import type`), igual que el resto del fichero.
 */
export function competitorCreateBody(
  values: CompetitorCreateInput,
  reason_code: string | null
): {
  display_name: string;
  date_of_birth: string | null;
  sex: CompetitorSex | null;
  belt_id: number | null;
  reason_code: string | null;
  reason_note: string | null;
} {
  return {
    display_name: values.display_name.trim(),
    date_of_birth:
      values.date_of_birth == null || values.date_of_birth.trim() === ''
        ? null
        : values.date_of_birth.trim(),
    sex: values.sex,
    belt_id: values.belt_id,
    reason_code,
    reason_note: null,
  };
}

/**
 * Academia que hay que asignar tras el alta, o `null` si no hay ninguna que asignar.
 *
 * Un competidor **nace sin afiliación**: elegir «Independiente» en el alta no es una escritura (no hay
 * nada que deshacer), y elegir una academia es la única acción de afiliación del flujo (§5/§6).
 */
export function affiliationSportsClubId(action: AffiliationAction): number | null {
  return action.kind === 'assign' ? action.sports_club_id : null;
}

/**
 * Etiqueta visible de un cinturón por su `id`, para listados y resúmenes.
 *
 * `null` significa «no hay cinturón declarado» (o el catálogo todavía no ha llegado): quien pinta
 * decide el texto de respaldo, nunca se inventa un cinturón ni se muestra el identificador.
 */
export function beltLabelForId(
  belts: readonly Belt[],
  belt_id: number | null,
  lookup: (key: string) => string | null
): string | null {
  if (belt_id == null) {
    return null;
  }
  const belt = belts.find((candidate) => candidate.id === belt_id);
  return belt == null ? null : beltLabel(belt, lookup);
}
