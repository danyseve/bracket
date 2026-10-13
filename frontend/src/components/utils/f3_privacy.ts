/**
 * Proyección pública (§20): lo que cualquier superficie **no** organizadora puede mostrar.
 *
 * El contrato del organizador (`f3_competitor_contract.ts`) lleva el perfil deportivo y el peso
 * declarado; la proyección pública (grada, calendario, listados sin sesión) **no** los lleva. La
 * garantía no es «no pintarlos»: es una **lista blanca** que solo copia los campos permitidos, de
 * modo que un campo sensible que llegue en el cuerpo del backend jamás entra en la proyección.
 *
 * Por eso este fichero **no nombra** los campos sensibles: si los nombrara, la lista blanca sería
 * en realidad una lista negra y un campo nuevo del backend se filtraría solo. El test de privacidad
 * comprueba que esos identificadores no aparecen en este módulo.
 */

/** Datos mínimos que la proyección pública acepta de un competidor (lista blanca de entrada). */
export interface PublicCompetitorSource {
  id: number;
  display_name: string;
}

/** Ficha pública de una persona: sin datos personales ni deportivos sensibles. */
export interface PublicCompetitor {
  id: number;
  display_name: string;
}

/** Proyecta un competidor a su ficha pública copiando solo los campos permitidos. */
export function toPublicCompetitor(competitor: PublicCompetitorSource): PublicCompetitor {
  return {
    id: competitor.id,
    display_name: competitor.display_name,
  };
}

/** Campos permitidos en la proyección pública de una inscripción. */
export interface PublicRegistrationSource {
  id: number;
  tournament_id: number;
  competitor_name_snapshot: string;
  category_label: string | null;
  sports_club_name_snapshot: string | null;
}

/** Ficha pública de una inscripción: nombre, categoría y academia; nada del perfil ni del peso. */
export interface PublicRegistration {
  id: number;
  tournament_id: number;
  competitor_name_snapshot: string;
  category_label: string | null;
  sports_club_name_snapshot: string | null;
}

/** Proyecta una inscripción a su ficha pública copiando solo los campos permitidos. */
export function toPublicRegistration(registration: PublicRegistrationSource): PublicRegistration {
  return {
    id: registration.id,
    tournament_id: registration.tournament_id,
    competitor_name_snapshot: registration.competitor_name_snapshot,
    category_label: registration.category_label,
    sports_club_name_snapshot: registration.sports_club_name_snapshot,
  };
}
