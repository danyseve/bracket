/**
 * Tablero de tatamis: representación honesta del dominio real (S4.1 §12-§14).
 *
 * Lo que el backend almacena hoy (inventario en `references`/informe de la fase):
 *
 * - `courts` es una entidad **del torneo** (nombre + torneo): es lo que el organizador llama tatami.
 * - la **única** asociación persistida es `matches.court_id`, es decir **combate → tatami**;
 * - `stage_items` (el cuadro de una categoría) **no** tiene tatami: no existe `stage_item.court_id`;
 *   la convención del planificador automático es que todos los combates de un cuadro caigan en el
 *   mismo tatami, pero eso es consecuencia del plan, no una fila;
 * - no hay orden ni prioridad por tatami más allá de `matches.position_in_schedule`, ni estado
 *   `IN_PROGRESS` de tatami: el estado se deriva de los combates.
 *
 * Por eso el tablero **deriva** tatami → cuadros desde los combates y no inventa una asignación que
 * el backend no puede guardar. La asignación por cuadro es un hueco de backend declarado.
 */

/** Granularidad real de la asociación persistida. */
export const TATAMI_ASSIGNMENT_GRANULARITY = 'match';

/**
 * ¿Puede el frontend asignar un cuadro entero a un tatami con el contrato actual? No: solo existe
 * `PUT tournaments/{id}/matches/{match_id}` (que acepta `court_id`) y el planificador por lotes.
 */
export const TATAMI_BRACKET_ASSIGNMENT_SUPPORTED = false;

export interface BoardCourt {
  id: number;
  name: string;
}

export interface BoardMatch {
  id: number;
  court_id: number | null;
  start_time: string | null;
  position_in_schedule: number | null;
  stage_item_input1_score: number;
  stage_item_input2_score: number;
}

export interface BoardStageItem {
  id: number;
  name: string;
  type: string;
  stage_id: number;
  stage_name: string;
  matches: BoardMatch[];
}

export type TatamiState = 'free' | 'planned' | 'partially_planned' | 'running';

export interface TatamiCategory {
  stage_item_id: number;
  stage_item_name: string;
  stage_name: string;
  match_count: number;
  /** Combates del cuadro con resultado anotado (el marcador es la única señal de avance real). */
  played_count: number;
  /** Combates del cuadro ya colocados en este tatami. */
  placed_count: number;
  /** `true` si el cuadro entero está en este tatami. */
  complete: boolean;
}

export interface TatamiBoard {
  court_id: number;
  court_name: string;
  title: string;
  state: TatamiState;
  match_count: number;
  placed_count: number;
  played_count: number;
  categories: TatamiCategory[];
}

export interface UnassignedCategory {
  stage_item_id: number;
  stage_item_name: string;
  stage_name: string;
  match_count: number;
  /** `true` cuando el cuadro todavía no tiene combates: falta generarlo. */
  generated: boolean;
}

export interface TatamiOverview {
  tatamis: TatamiBoard[];
  /** Cuadros con combates sin tatami (o sin cuadro generado todavía). */
  unassigned: UnassignedCategory[];
  /** Cuadros repartidos entre más de un tatami: el planificador no lo hace, pero el contrato lo permite. */
  shared_category_ids: number[];
}

function hasResult(match: BoardMatch): boolean {
  return match.stage_item_input1_score !== 0 || match.stage_item_input2_score !== 0;
}

function sortMatches(matches: BoardMatch[]): BoardMatch[] {
  return [...matches].sort((left, right) => {
    const leftPosition = left.position_in_schedule ?? Number.MAX_SAFE_INTEGER;
    const rightPosition = right.position_in_schedule ?? Number.MAX_SAFE_INTEGER;
    return leftPosition - rightPosition || left.id - right.id;
  });
}

function stateOf(matches: BoardMatch[]): TatamiState {
  if (matches.length === 0) {
    return 'free';
  }
  if (matches.some(hasResult)) {
    return 'running';
  }
  const placed = matches.filter((match) => match.start_time != null).length;
  if (placed === matches.length) {
    return 'planned';
  }
  return 'partially_planned';
}

/** Título visible: el organizador lee "Tatami 1", no el nombre crudo del modelo. */
export function tatamiTitle(index: number): string {
  return `Tatami ${index + 1}`;
}

/** Nombre propio del tatami, solo si aporta algo distinto del título visible. */
export function tatamiSubtitle(index: number, name: string): string | null {
  const trimmed = name.trim();
  if (trimmed === '' || trimmed.toLowerCase() === tatamiTitle(index).toLowerCase()) {
    return null;
  }
  return trimmed;
}

/**
 * Construye el tablero. Los cuadros se cuelgan del tatami por sus **combates**; un cuadro puede
 * aparecer en más de un tatami si el plan lo reparte, y eso se marca (`complete: false`).
 */
export function buildTatamiOverview(
  courts: BoardCourt[],
  stageItems: BoardStageItem[]
): TatamiOverview {
  const matchesByCourt = new Map<number, BoardMatch[]>();
  const courtIds = new Set(courts.map((court) => court.id));

  for (const stageItem of stageItems) {
    for (const match of stageItem.matches) {
      if (match.court_id == null || !courtIds.has(match.court_id)) {
        continue;
      }
      const bucket = matchesByCourt.get(match.court_id) ?? [];
      bucket.push(match);
      matchesByCourt.set(match.court_id, bucket);
    }
  }

  const tatamis = courts.map((court, index) => {
    const matches = sortMatches(matchesByCourt.get(court.id) ?? []);
    const categories: TatamiCategory[] = stageItems
      .map((stageItem) => {
        const placed = matches.filter((match) =>
          stageItem.matches.some((candidate) => candidate.id === match.id)
        );
        return {
          stage_item_id: stageItem.id,
          stage_item_name: stageItem.name,
          stage_name: stageItem.stage_name,
          match_count: stageItem.matches.length,
          played_count: stageItem.matches.filter(hasResult).length,
          placed_count: placed.length,
          complete: placed.length > 0 && placed.length === stageItem.matches.length,
        };
      })
      .filter((category) => category.placed_count > 0)
      .sort((left, right) => left.stage_item_name.localeCompare(right.stage_item_name));

    return {
      court_id: court.id,
      court_name: court.name,
      title: tatamiTitle(index),
      state: stateOf(matches),
      match_count: matches.length,
      placed_count: matches.filter((match) => match.start_time != null).length,
      played_count: matches.filter(hasResult).length,
      categories,
    };
  });

  const unassigned = stageItems
    .filter((stageItem) =>
      stageItem.matches.some((match) => match.court_id == null || !courtIds.has(match.court_id))
    )
    .map((stageItem) => ({
      stage_item_id: stageItem.id,
      stage_item_name: stageItem.name,
      stage_name: stageItem.stage_name,
      match_count: stageItem.matches.length,
      generated: stageItem.matches.length > 0,
    }))
    .sort((left, right) => left.stage_item_name.localeCompare(right.stage_item_name));

  const shared = stageItems
    .filter((stageItem) => {
      const seen = new Set(
        stageItem.matches
          .map((match) => match.court_id)
          .filter((courtId): courtId is number => courtId != null && courtIds.has(courtId))
      );
      return seen.size > 1;
    })
    .map((stageItem) => stageItem.id);

  return { tatamis, unassigned, shared_category_ids: shared };
}

/** Resumen legible del estado del tatami (clave de traducción). */
export function tatamiStateKey(state: TatamiState): string {
  return `tatami_state_${state}`;
}
