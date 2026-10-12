/**
 * Tablero de tatamis del organizador (S4.1 y S4.3).
 *
 * Desde S4.2 el backend **sí** guarda el tatami del cuadro: `stage_items.court_id` (nullable, con la
 * clave foránea a `courts` y `ON DELETE SET NULL`), y esa es la unidad de asignación que se opera
 * (`PUT tournaments/{id}/stage-items/{stage_item_id}/court`). El tatami del combate sigue existiendo
 * (`matches.court_id`) porque el backend lo propaga al cuadro dentro de la misma transacción.
 *
 * Reglas de este módulo:
 *
 * - la asignación se lee de `stage_item.court_id`, nunca se infiere de los combates;
 * - la derivación desde `matches.court_id` queda **solo** como compatibilidad declarada con un
 *   backend que no publique el campo (se marca en `assignment_source` y la pantalla lo avisa);
 * - el estado de un cuadro o de un tatami se deriva de sus combates, porque el backend no publica un
 *   estado de combate ni de tatami: es una aproximación declarada.
 */

/** Granularidad real de la asignación que se opera: el cuadro entero (S4.2). */
export const TATAMI_ASSIGNMENT_GRANULARITY = 'stage_item';

export interface BoardCourt {
  id: number;
  name: string;
}

export interface BoardMatch {
  id: number;
  /** Tatami del combate; `null` = sin asignar; ausente = el payload no lo trae (compatibilidad). */
  court_id?: number | null;
  start_time: string | null;
  position_in_schedule: number | null;
  stage_item_input1_score: number;
  stage_item_input2_score: number;
  /** Deportistas de cada casilla; el backend los publica en el `input` del combate. */
  input1_team_id: number | null;
  input2_team_id: number | null;
}

export interface BoardStageItem {
  id: number;
  name: string;
  type: string;
  stage_id: number;
  stage_name: string;
  /**
   * Tatami del cuadro entero según el contrato (S4.2). `null` = sin asignar; `undefined` = el estado
   * publicado no trae el campo (backend anterior a S4.2), y entonces el tablero reintroduce la
   * derivación desde los combates, declarada como compatibilidad.
   */
  court_id?: number | null;
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

/* ---------------------------------------------------------------------------------------------- */
/* S4.3: tablero de control (asignación por cuadro)                                                */
/* ---------------------------------------------------------------------------------------------- */

/**
 * Contrato del que sale la asignación cuando el backend lo publica: `stage_items[].court_id`
 * (`GET tournaments/{id}/stages`, S4.2). Es el tatami del **cuadro entero**, no el de cada combate.
 */
export const TATAMI_ASSIGNMENT_CONTRACT = 'stage_item.court_id';

/** De dónde sale la asignación que se está mostrando. Se enseña al organizador cuando es derivada. */
export type TatamiAssignmentSource = 'stage_item' | 'match';

/**
 * ¿Publica este backend el tatami del cuadro? Se mira el **dato**, no una versión declarada: si el
 * campo viaja en la respuesta (aunque su valor sea `null`), el contrato de asignación por cuadro
 * está disponible. Un backend anterior simplemente no manda el campo.
 */
export function hasStageItemCourtContract(payload: readonly object[]): boolean {
  return payload.some((item) => 'court_id' in item);
}

/** Estado derivado de un cuadro a partir de sus combates (mismo criterio que el resto del tablero). */
export type CategoryState = 'not_generated' | 'generated' | 'running' | 'finished';

export function categoryState(matches: BoardMatch[]): CategoryState {
  if (matches.length === 0) {
    return 'not_generated';
  }
  const played = matches.filter(hasResult).length;
  if (played === 0) {
    return 'generated';
  }
  return played === matches.length ? 'finished' : 'running';
}

export function categoryStateKey(state: CategoryState): string {
  return `bracket_state_${state}`;
}

/** Tarjeta del tablero: un cuadro con lo mínimo que necesita el organizador para repartirlo. */
export interface ControlCard {
  stage_item_id: number;
  category_name: string;
  stage_name: string;
  /** Deportistas distintos que aparecen en los combates del cuadro. */
  participants: number;
  match_count: number;
  played_count: number;
  /** Tatami actual del cuadro según el contrato (S4.2). `null` = sin asignar. */
  court_id: number | null;
  state: CategoryState;
  /** Primer combate sin resultado (el que el marcador pondría en marcha), si se puede derivar. */
  next_match_id: number | null;
  next_match_position: number | null;
}

/** Columna del tablero: la bandeja de «sin asignar» o un tatami real del torneo. */
export interface ControlColumn {
  kind: 'unassigned' | 'court';
  court_id: number | null;
  title: string;
  subtitle: string | null;
  cards: ControlCard[];
  /** Cuadros asignados (avance del marcador de S4.4). */
  bracket_count: number;
  /** Combates que caen en la columna. */
  match_count: number;
  played_count: number;
  /** Siguiente combate de la columna, para el avance del marcador. */
  next_match_id: number | null;
  next_match_position: number | null;
}

export interface TatamiControlBoard {
  columns: ControlColumn[];
  assignment_source: TatamiAssignmentSource;
}

function participantsOf(matches: BoardMatch[]): number {
  const entrants = new Set<number>();
  for (const match of matches) {
    if (match.input1_team_id != null) {
      entrants.add(match.input1_team_id);
    }
    if (match.input2_team_id != null) {
      entrants.add(match.input2_team_id);
    }
  }
  return entrants.size;
}

/** Primer combate pendiente en orden de planificación (el que se pondría en marcha). */
function nextPending(matches: BoardMatch[]): BoardMatch | null {
  return sortMatches(matches).find((match) => !hasResult(match)) ?? null;
}

function toControlCard(item: BoardStageItem): ControlCard {
  const next = nextPending(item.matches);
  return {
    stage_item_id: item.id,
    category_name: item.name,
    stage_name: item.stage_name,
    participants: participantsOf(item.matches),
    match_count: item.matches.length,
    played_count: item.matches.filter(hasResult).length,
    court_id: item.court_id ?? null,
    state: categoryState(item.matches),
    next_match_id: next != null ? next.id : null,
    next_match_position: next != null ? next.position_in_schedule : null,
  };
}

function byCategoryName(left: ControlCard, right: ControlCard): number {
  return left.category_name.localeCompare(right.category_name);
}

function columnOf(
  kind: ControlColumn['kind'],
  court_id: number | null,
  items: BoardStageItem[],
  title: string,
  subtitle: string | null
): ControlColumn {
  const matches = items.flatMap((item) => item.matches);
  const next = nextPending(matches);
  return {
    kind,
    court_id,
    title,
    subtitle,
    cards: items.map(toControlCard).sort(byCategoryName),
    bracket_count: items.length,
    match_count: matches.length,
    played_count: matches.filter(hasResult).length,
    next_match_id: next != null ? next.id : null,
    next_match_position: next != null ? next.position_in_schedule : null,
  };
}

/**
 * Tablero de control: la bandeja de «sin asignar» y una columna por tatami **real** del torneo.
 *
 * - la asignación del cuadro sale de `stage_item.court_id` (S4.2);
 * - un tatami sin cuadros aparece igualmente (columna vacía): el organizador necesita ver los que
 *   tiene, no solo los que usa;
 * - si un cuadro apunta a un tatami que no está en el listado recibido, se le abre su propia columna
 *   en vez de hacerlo desaparecer;
 * - el orden de las columnas es el del listado de tatamis, que ya viene ordenado por el backend.
 */
export function buildTatamiControlBoard(
  stageItems: BoardStageItem[],
  courts: BoardCourt[],
  assignment_source: TatamiAssignmentSource = 'stage_item'
): TatamiControlBoard {
  const unassigned = stageItems.filter((item) => item.court_id == null);
  const columns: ControlColumn[] = [
    columnOf('unassigned', null, unassigned, '', null),
    ...courts.map((court, index) =>
      columnOf(
        'court',
        court.id,
        stageItems.filter((item) => item.court_id === court.id),
        tatamiTitle(index),
        court.name
      )
    ),
  ];

  const known = new Set(courts.map((court) => court.id));
  const orphans = new Set(
    stageItems
      .map((item) => item.court_id)
      .filter((court_id): court_id is number => court_id != null && !known.has(court_id))
  );
  let orphanIndex = courts.length;
  for (const court_id of orphans) {
    orphanIndex += 1;
    columns.push(
      columnOf(
        'court',
        court_id,
        stageItems.filter((item) => item.court_id === court_id),
        tatamiTitle(orphanIndex),
        null
      )
    );
  }

  return { columns, assignment_source };
}

/* ---------------------------------------------------------------------------------------------- */
/* Acciones de asignación (lógica pura: la ejercen los tests y la usa el componente)               */
/* ---------------------------------------------------------------------------------------------- */

export type AssignmentAction = 'assign' | 'reassign' | 'unassign' | 'noop';

/** Operación que representa una petición, según el tatami que tiene hoy el cuadro. */
export function assignmentAction(current: number | null, target: number | null): AssignmentAction {
  if (current === target) {
    return 'noop';
  }
  if (target == null) {
    return 'unassign';
  }
  return current == null ? 'assign' : 'reassign';
}

/**
 * Cambiar de tatami mueve combates ya planificados: se pregunta antes de enviarlo. Asignar y quitar
 * son acciones directas (no hay nada planificado que mover).
 */
export function needsReassignmentConfirmation(
  current: number | null,
  target: number | null
): boolean {
  return assignmentAction(current, target) === 'reassign';
}

/** Tatamis a los que se puede mandar el cuadro: todos menos en el que ya está. */
export function assignmentTargets(courts: BoardCourt[], current: number | null): BoardCourt[] {
  return courts.filter((court) => court.id !== current);
}

/* ---------------------------------------------------------------------------------------------- */
/* Resultado confirmado: lo que se le enseña al organizador                                        */
/* ---------------------------------------------------------------------------------------------- */

/** Respuesta confirmada por el backend para una operación de asignación. */
export interface AssignmentFeedback {
  stage_item_id: number;
  court_id: number | null;
  court_name: string | null;
  affected_match_count: number;
}

/**
 * Clave del mensaje. Tres casos distintos y ninguno se disfraza de otro:
 *
 * - quitar el tatami → «fuera del tatami»;
 * - asignar/reasignar con efecto → «asignada a X, N combates actualizados»;
 * - confirmado sin efecto (la operación no movió nada, p. ej. el cuadro ya estaba completo) →
 *   se dice que no hubo cambios; **afected_match_count = 0 es un dato, no un fallo**.
 */
export function assignmentFeedbackKey(feedback: AssignmentFeedback): string {
  if (feedback.court_id == null) {
    return 'tatami_feedback_unassigned';
  }
  return feedback.affected_match_count > 0
    ? 'tatami_feedback_assigned'
    : 'tatami_feedback_unchanged';
}

/**
 * Nombre del tatami para los mensajes: el organizador lee «Tatami 2» (posición en el listado), no el
 * nombre crudo del modelo ni un identificador. Si el tatami no está en el listado, se usa el nombre
 * que devolvió el backend y, en último caso, no se enseña identificador ninguno.
 */
export function courtLabel(
  courts: BoardCourt[],
  court_id: number | null,
  court_name: string | null = null
): string {
  if (court_id == null) {
    return '';
  }
  const index = courts.findIndex((court) => court.id === court_id);
  if (index >= 0) {
    return tatamiTitle(index);
  }
  return court_name ?? '';
}
