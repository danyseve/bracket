import type { MatchWithDetails, StageWithStageItems } from '@openapi';

import type { BoardMatch, BoardStageItem } from '@components/utils/tatami_board';

/**
 * Adaptador entre las APIs existentes del cuadro (`GET tournaments/{id}/stages`) y las vistas del
 * frontend F3.
 *
 * Aquí está el límite de tipos: los tipos generados (`@openapi`) son la única fuente y no se
 * reconstruye la estructura del cuadro (el backend ya la expone). Lo que sí se **deriva** es el
 * avance, porque el backend no publica un estado de combate: se lee del marcador. Es una
 * aproximación declarada — un combate decidido sin puntos puede contarse como pendiente.
 */

export type BracketState = 'not_generated' | 'generated' | 'running' | 'finished';

export interface BracketRow {
  stage_item_id: number;
  category_name: string;
  stage_name: string;
  stage_id: number;
  entrant_count: number;
  match_count: number;
  played_count: number;
  state: BracketState;
  /** Tatamis por los que está repartido el cuadro (vacío = sin asignar). */
  court_ids: number[];
}

export function bracketStateKey(state: BracketState): string {
  return `bracket_state_${state}`;
}

function matchesOf(item: StageWithStageItems['stage_items'][number]): MatchWithDetails[] {
  return item.rounds.flatMap((round) => round.matches);
}

function slotTeamId(match: MatchWithDetails, slot: 1 | 2): number | null {
  const input = slot === 1 ? match.stage_item_input1 : match.stage_item_input2;
  // Las tres variantes del input (`final`, `tentative`, `empty`) declaran `team_id`; la vacía lo
  // publica como `null`, así que esta lectura no necesita discriminador.
  return input != null ? input.team_id : null;
}

function hasResult(match: MatchWithDetails): boolean {
  return (
    slotTeamId(match, 1) != null &&
    slotTeamId(match, 2) != null &&
    (match.stage_item_input1_score !== 0 || match.stage_item_input2_score !== 0)
  );
}

function entrantCount(matches: MatchWithDetails[]): number {
  const entrants = new Set<number>();
  for (const match of matches) {
    const first = slotTeamId(match, 1);
    const second = slotTeamId(match, 2);
    if (first != null) {
      entrants.add(first);
    }
    if (second != null) {
      entrants.add(second);
    }
  }
  return entrants.size;
}

function stateOf(matches: MatchWithDetails[], entrantCount_value: number): BracketState {
  if (matches.length === 0 || entrantCount_value === 0) {
    return 'not_generated';
  }
  const played = matches.filter(hasResult).length;
  if (played === 0) {
    return 'generated';
  }
  return played === matches.length ? 'finished' : 'running';
}

function toBoardMatch(match: MatchWithDetails): BoardMatch {
  return {
    id: match.id,
    court_id: match.court_id,
    start_time: match.start_time,
    position_in_schedule: match.position_in_schedule,
    stage_item_input1_score: match.stage_item_input1_score,
    stage_item_input2_score: match.stage_item_input2_score,
  };
}

/** Todas las categorías (stage items) de un torneo, con su stage y sus combates. */
export function boardStageItems(stages: StageWithStageItems[]): BoardStageItem[] {
  return stages.flatMap((stage) =>
    stage.stage_items.map((item) => ({
      id: item.id,
      name: item.name,
      type: item.type,
      stage_id: stage.id,
      stage_name: stage.name,
      matches: matchesOf(item).map(toBoardMatch),
    }))
  );
}

/** Filas del listado de cuadros generados. */
export function bracketRows(stages: StageWithStageItems[]): BracketRow[] {
  return stages
    .flatMap((stage) =>
      stage.stage_items.map((item) => {
        const matches = matchesOf(item);
        const entrants = entrantCount(matches);
        const courtIds = Array.from(
          new Set(
            matches
              .map((match) => match.court_id)
              .filter((courtId): courtId is number => courtId != null)
          )
        ).sort((left, right) => left - right);
        return {
          stage_item_id: item.id,
          category_name: item.name,
          stage_name: stage.name,
          stage_id: stage.id,
          entrant_count: entrants,
          match_count: matches.length,
          played_count: matches.filter(hasResult).length,
          state: stateOf(matches, entrants),
          court_ids: courtIds,
        };
      })
    )
    .sort((left, right) => left.category_name.localeCompare(right.category_name));
}
