/**
 * Copy/logic for the "Generar cuadro" action of a single elimination stage item.
 *
 * Generating a bracket is the explicit step that spreads the direct byes (pases directos) over
 * the first round. It never happens on its own: assigning, removing or editing a participant does
 * not generate anything, so the operator always decides when the distribution is computed.
 *
 * Everything here is pure so it can be unit tested without a browser; the API decides what is
 * actually generated, this only decides what the operator reads before and after pressing the
 * button.
 */

/** The part of a stage item this module needs; a `StageItemWithRounds` satisfies it. */
export type BracketSlots = {
  team_count: number;
  inputs: { slot: number; team_id: number | null }[];
};

export type BracketGenerationSummary = {
  stage_item_id: number;
  entrant_count: number;
  bracket_size: number;
  bye_count: number;
  ghost_count: number;
  changed: boolean;
};

export type GenerationMode = 'generate' | 'regenerate';

export const BRACKET_BLOCKER_PREFIX = 'generate_bracket_blocked';
export const ERROR_WITHOUT_DETAIL = `${BRACKET_BLOCKER_PREFIX}: unknown`;

/** How many slots already hold a participant. */
export function countParticipants(stageItem: BracketSlots): number {
  return stageItem.inputs.filter((input) => input.team_id != null).length;
}

/** The number of slots of the bracket, fixed when the stage item was created. */
export function getBracketSize(stageItem: BracketSlots): number {
  return stageItem.team_count;
}

/** The direct byes: slots that stay empty, which means their opponent skips the first round. */
export function getExpectedByes(stageItem: BracketSlots): number {
  return Math.max(getBracketSize(stageItem) - countParticipants(stageItem), 0);
}

/**
 * Whether the bracket already looks generated, i.e. the byes are spread over the first round.
 *
 * Right after filling in the participants all empty slots are at the end; as soon as an empty slot
 * is followed by a filled one, the distribution was generated before. This only decides the
 * wording of the button ("Generar" versus "Regenerar cuadro"); the API stays authoritative about
 * what it does and the response reports whether anything actually changed.
 */
export function isBracketSpread(stageItem: BracketSlots): boolean {
  const slots = [...stageItem.inputs].sort((first, second) => first.slot - second.slot);
  const firstEmptyIndex = slots.findIndex((input) => input.team_id == null);
  if (firstEmptyIndex === -1) {
    return false;
  }
  return slots.slice(firstEmptyIndex).some((input) => input.team_id != null);
}

/** Generating again is only offered once the bracket holds a full or spread-out distribution. */
export function getGenerationMode(stageItem: BracketSlots): GenerationMode {
  const hasFullBracket = countParticipants(stageItem) >= 2 && getExpectedByes(stageItem) === 0;
  return isBracketSpread(stageItem) || hasFullBracket ? 'regenerate' : 'generate';
}

export function buildGenerateBracketUrl(tournamentId: number, stageItemId: number): string {
  return `tournaments/${tournamentId}/stage_items/${stageItemId}/generate_bracket`;
}

/** The reason the API refuses to generate, as a translation key suffix, or null if it succeeded. */
export function getBlockerCode(detail: string): string | null {
  const prefix = `${BRACKET_BLOCKER_PREFIX}:`;
  if (!detail.startsWith(prefix)) {
    return null;
  }
  const code = detail.slice(prefix.length).trim();
  return code === '' ? null : code;
}

type FailedRequest = { isAxiosError?: boolean; response?: { data?: { detail?: unknown } } };

function isFailedRequest(value: unknown): value is FailedRequest {
  if (typeof value !== 'object' || value === null) {
    return false;
  }
  const candidate = value as FailedRequest;
  return candidate.isAxiosError === true || candidate.response !== undefined;
}

/** The `detail` of a failed request, or null when the request did not fail. */
export function getResponseErrorDetail(response: unknown): string | null {
  if (!isFailedRequest(response)) {
    return null;
  }
  const { detail } = response.response?.data ?? {};
  return typeof detail === 'string' ? detail : ERROR_WITHOUT_DETAIL;
}
