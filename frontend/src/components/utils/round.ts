import type { TFunction } from 'i18next';
import type { SWRResponse } from 'swr';

import type { RoundWithMatches, StageType, StagesWithStageItemsResponse } from '@openapi';

/**
 * i18n key of the label for a single elimination round/column.
 *
 * Today Bracket stores generic names in the database ("Round 01", "Round 02", ...), which say
 * nothing about how far a round is from the final. This helper maps a round position, in a bracket
 * with a known number of rounds, to the i18n key of its BJJ/judo name.
 *
 * The mapping is presentation only: `round.name` in the database is never modified.
 */
const ROUND_LABEL_SUFFIXES = [
  '', // index 0 unused: a bracket always has at least the final
  'final', // 1 round: just the final
  'semifinal', // 2 rounds: semifinal + final
  'quarterfinal', // 3 rounds: cuartos + semifinal + final
  'round_of_16', // 4 rounds: octavos + ...
  'round_of_32', // 5 rounds: dieciseisavos + ...
];

/** Stage type of a bracket whose rounds have a known distance to the final. */
const SINGLE_ELIMINATION: StageType = 'SINGLE_ELIMINATION';

/**
 * i18n key of the label for the round at `index` (0-based, ascending: the first round is 0) of a
 * single elimination bracket with `totalRounds` rounds.
 *
 * Returns `null` when the name cannot be determined with certainty: unknown bracket size (more
 * rounds than the known names), or an index outside the bracket. Callers must fall back to the
 * stored round name in that case.
 */
export function getRoundLabelKey(totalRounds: number, index: number): string | null {
  if (!Number.isInteger(totalRounds) || !Number.isInteger(index)) {
    return null;
  }
  if (totalRounds < 1 || totalRounds > ROUND_LABEL_SUFFIXES.length - 1) {
    return null;
  }
  if (index < 0 || index >= totalRounds) {
    return null;
  }

  // 1 round away from the start of the bracket is the final, 2 the semifinal, and so on.
  const suffix = ROUND_LABEL_SUFFIXES[totalRounds - index];
  return suffix === '' ? null : `round_label_${suffix}`;
}

/**
 * Name of a round for display: the translated BJJ name when it can be derived with certainty,
 * otherwise the name stored in the database.
 *
 * It falls back to `round.name` when:
 * - the stage item of the round is not in the loaded stages response,
 * - the stage item is not a single elimination bracket (round robin, swiss, ...),
 * - the round is not part of its stage item, or the bracket size is unknown,
 * - the label is not translated in the active language.
 */
export function getRoundDisplayName(
  round: RoundWithMatches,
  swrStagesResponse: SWRResponse<StagesWithStageItemsResponse>,
  t: TFunction
): string {
  const stageItem = swrStagesResponse.data?.data
    .flatMap((stage) => stage.stage_items)
    .find((item) => item.id === round.stage_item_id);

  if (stageItem == null || stageItem.type !== SINGLE_ELIMINATION) {
    return round.name;
  }

  const rounds = [...stageItem.rounds].sort((r1, r2) => r1.id - r2.id);
  const index = rounds.findIndex((r) => r.id === round.id);
  const key = getRoundLabelKey(rounds.length, index);

  if (key == null) {
    return round.name;
  }

  const label = t(key, { defaultValue: '' });
  return label === '' ? round.name : label;
}
