import useSWR, { SWRResponse } from 'swr';

import {
  Belt,
  beltsUrl,
  CompetitorAffiliationsList,
  competitorAffiliationsUrl,
  competitorIndependenceUrl,
  CompetitorProfileBody,
  competitorProfileUrl,
  SportsClub,
  sportsClubLogoUrl,
  sportsClubsUrl,
} from '@components/utils/f3_competitor_contract';
import {
  BracketReadiness,
  bracketReadinessUrl,
  closeRegistrationsUrl,
  competitorActionUrl,
  CompetitorCreateBody,
  CompetitorList,
  competitorsQuery,
  competitorsUrl,
  competitorUrl,
  GenerateBracketResult,
  generateBracketUrl,
  IDEMPOTENCY_KEY_HEADER,
  LifecycleBody,
  Registration,
  RegistrationAction,
  registrationActionUrl,
  RegistrationCreateBody,
  registrationsQuery,
  RegistrationStatus,
  registrationsUrl,
  RegistrationWindow,
  registrationWindowUrl,
  StageItemCourtBody,
  StageItemCourtResult,
  stageItemCourtUrl,
} from '@components/utils/f3_contract';
import { describeF3Failure, F3Failure } from '@components/utils/f3_errors';
import type { CompetitorProfileTarget } from '@components/utils/f3_sports_profile';
import { createAxios } from '@services/adapter';

/**
 * Servicio F3 del frontend del organizador.
 *
 * Diferencias deliberadas con los servicios antiguos del repositorio:
 *
 * - las **escrituras no** llaman a `handleRequestError`: el aviso genérico "An error occurred:
 *   F3_WRITE_DISABLED" es exactamente lo que el operador no debe ver. Devuelven un resultado
 *   tipado (`{ok:true}`/`{ok:false, failure}`) y la pantalla decide el mensaje;
 * - toda escritura exige la `Idempotency-Key` como parámetro obligatorio: no hay forma de escribir
 *   sin pasar por el protocolo.
 */

export type F3WriteOutcome<T> = { ok: true; data: T } | { ok: false; failure: F3Failure };

const fetcher = (url: string) =>
  createAxios()
    .get(url)
    .then((response) => response.data);

async function write<T>(perform: () => Promise<{ data: T }>): Promise<F3WriteOutcome<T>> {
  try {
    const response = await perform();
    return { ok: true, data: response.data };
  } catch (error) {
    return { ok: false, failure: describeF3Failure(error) };
  }
}

function idempotentHeaders(key: string) {
  return { headers: { [IDEMPOTENCY_KEY_HEADER]: key } };
}

/* ------------------------------------------------------------------ lectura ------------------- */

export function getCompetitors(
  club_id: number | null,
  params: { display_name?: string; include_inactive?: boolean; limit?: number; offset?: number }
): SWRResponse<CompetitorList> {
  const query = competitorsQuery(params);
  return useSWR(
    club_id == null || club_id === -1 ? null : `${competitorsUrl(club_id)}?${query}`,
    fetcher
  );
}

export function getRegistrations(
  tournament_id: number,
  params: { registration_status?: RegistrationStatus; limit?: number; offset?: number }
): SWRResponse<{ count: number; registrations: Registration[] }> {
  const query = registrationsQuery(params);
  return useSWR(
    tournament_id === -1 ? null : `${registrationsUrl(tournament_id)}?${query}`,
    fetcher
  );
}

export function getRegistrationWindow(tournament_id: number): SWRResponse<RegistrationWindow> {
  return useSWR(tournament_id === -1 ? null : registrationWindowUrl(tournament_id), fetcher);
}

export function getBracketReadiness(tournament_id: number): SWRResponse<BracketReadiness> {
  return useSWR(tournament_id === -1 ? null : bracketReadinessUrl(tournament_id), fetcher);
}

/** Catálogo real de cinturones del club (S4.8a-4 §3): la UI no hardcodea el orden. */
export function getBelts(club_id: number | null): SWRResponse<Belt[]> {
  return useSWR(club_id == null || club_id === -1 ? null : beltsUrl(club_id), fetcher);
}

/** Academias del club (S4.8a-4 §6): población real del selector de club. */
export function getSportsClubs(club_id: number | null): SWRResponse<SportsClub[]> {
  return useSWR(club_id == null || club_id === -1 ? null : sportsClubsUrl(club_id), fetcher);
}

/** Afiliaciones del competidor (S4.8a-4 §6): precarga la academia vigente. */
export function getCompetitorAffiliations(
  club_id: number | null,
  competitor_id: number
): SWRResponse<CompetitorAffiliationsList> {
  return useSWR(
    club_id == null || club_id === -1 || competitor_id === -1
      ? null
      : competitorAffiliationsUrl(club_id, competitor_id),
    fetcher
  );
}

/** Perfil deportivo declarado del competidor (S4.8a-4 §5): origen de los valores iniciales. */
export function getCompetitorSportsProfile(
  club_id: number | null,
  competitor_id: number
): SWRResponse<CompetitorProfileTarget> {
  return useSWR(
    club_id == null || club_id === -1 || competitor_id === -1
      ? null
      : competitorUrl(club_id, competitor_id),
    fetcher
  );
}

/* ------------------------------------------------------------------ escritura ----------------- */

export async function createCompetitor(
  club_id: number,
  body: CompetitorCreateBody,
  key: string
): Promise<F3WriteOutcome<{ id: number; display_name: string; active: boolean }>> {
  return write(() => createAxios().post(competitorsUrl(club_id), body, idempotentHeaders(key)));
}

export async function renameCompetitor(
  club_id: number,
  competitor_id: number,
  body: CompetitorCreateBody,
  key: string
): Promise<F3WriteOutcome<{ id: number; display_name: string; active: boolean }>> {
  return write(() =>
    createAxios().post(
      competitorActionUrl(club_id, competitor_id, 'rename'),
      body,
      idempotentHeaders(key)
    )
  );
}

export async function setCompetitorActive(
  club_id: number,
  competitor_id: number,
  active: boolean,
  key: string,
  reason_code: string = 'ADMINISTRATIVE'
): Promise<F3WriteOutcome<{ id: number; display_name: string; active: boolean }>> {
  return write(() =>
    createAxios().post(
      competitorActionUrl(club_id, competitor_id, active ? 'activate' : 'deactivate'),
      { reason_code },
      idempotentHeaders(key)
    )
  );
}

export async function createRegistration(
  tournament_id: number,
  body: RegistrationCreateBody,
  key: string
): Promise<F3WriteOutcome<Registration>> {
  return write(() =>
    createAxios().post(registrationsUrl(tournament_id), body, idempotentHeaders(key))
  );
}

export async function changeRegistrationState(
  tournament_id: number,
  registration_id: number,
  action: RegistrationAction,
  body: LifecycleBody,
  key: string
): Promise<F3WriteOutcome<Registration>> {
  return write(() =>
    createAxios().post(
      registrationActionUrl(tournament_id, registration_id, action),
      body,
      idempotentHeaders(key)
    )
  );
}

export async function setRegistrationDeadline(
  tournament_id: number,
  registration_deadline: string | null,
  key: string
): Promise<F3WriteOutcome<RegistrationWindow>> {
  return write(() =>
    createAxios().post(
      registrationWindowUrl(tournament_id),
      { registration_deadline },
      idempotentHeaders(key)
    )
  );
}

export async function closeRegistrations(
  tournament_id: number,
  key: string
): Promise<F3WriteOutcome<RegistrationWindow>> {
  return write(() =>
    createAxios().post(closeRegistrationsUrl(tournament_id), {}, idempotentHeaders(key))
  );
}

/**
 * Fija (o quita) el tatami de un cuadro (S4.2). El cuerpo va explícito —`court_id` siempre presente—
 * porque `null` significa «sin tatami» y no «no cambiar nada».
 */
export async function setStageItemCourt(
  tournament_id: number,
  stage_item_id: number,
  body: StageItemCourtBody,
  key: string
): Promise<F3WriteOutcome<StageItemCourtResult>> {
  return write(() =>
    createAxios().put(stageItemCourtUrl(tournament_id, stage_item_id), body, idempotentHeaders(key))
  );
}

export async function generateTournamentBrackets(
  tournament_id: number,
  key: string
): Promise<F3WriteOutcome<GenerateBracketResult>> {
  return write(() =>
    createAxios().post(generateBracketUrl(tournament_id), {}, idempotentHeaders(key))
  );
}

/** Competidor concreto (ficha): se usa para resolver el club del torneo cuando hace falta. */
export async function fetchCompetitor(
  club_id: number,
  competitor_id: number
): Promise<F3WriteOutcome<{ id: number; display_name: string; active: boolean }>> {
  return write(() => createAxios().get(competitorUrl(club_id, competitor_id)));
}

/**
 * Reemplaza el perfil deportivo del competidor (S4.8a-4 §3-§5). Los tres campos viajan siempre
 * (es un reemplazo); el motivo de auditoría entra por el vocabulario cerrado del contrato.
 */
export async function updateCompetitorProfile(
  club_id: number,
  competitor_id: number,
  body: CompetitorProfileBody,
  key: string
): Promise<F3WriteOutcome<CompetitorProfileTarget>> {
  return write(() =>
    createAxios().post(competitorProfileUrl(club_id, competitor_id), body, idempotentHeaders(key))
  );
}

/** Asigna una academia al competidor (S4.8a-4 §6): solo el `sports_club_id` destino. */
export async function assignAffiliation(
  club_id: number,
  competitor_id: number,
  sports_club_id: number,
  key: string
): Promise<F3WriteOutcome<CompetitorAffiliationsList>> {
  return write(() =>
    createAxios().post(
      competitorAffiliationsUrl(club_id, competitor_id),
      { sports_club_id },
      idempotentHeaders(key)
    )
  );
}

/** Pasa al competidor a independiente (S4.8a-4 §6). No toca el histórico de inscripciones. */
export async function goIndependent(
  club_id: number,
  competitor_id: number,
  key: string
): Promise<F3WriteOutcome<CompetitorAffiliationsList>> {
  return write(() =>
    createAxios().post(
      competitorIndependenceUrl(club_id, competitor_id),
      {},
      idempotentHeaders(key)
    )
  );
}

/**
 * Sube, reemplaza (con fichero) o quita (sin fichero) el logo de la academia (S4.8a-4 §7). El
 * backend valida tipo y tamaño por contenido; la UI valida antes solo como UX.
 */
export async function setSportsClubLogo(
  club_id: number,
  sports_club_id: number,
  file: File | null,
  key: string
): Promise<F3WriteOutcome<SportsClub>> {
  const form = new FormData();
  if (file != null) {
    form.append('file', file);
  }
  return write(() =>
    createAxios().post(sportsClubLogoUrl(club_id, sports_club_id), form, idempotentHeaders(key))
  );
}
