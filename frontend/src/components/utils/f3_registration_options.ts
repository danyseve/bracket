/**
 * Opciones y resumen del competidor en la inscripción (S4.8d §4/§6).
 *
 * Módulo **puro**: sin red, sin React, sin i18next. Compone lo que ve el organizador con los datos
 * que ya viajan en la lista de competidores del club (`GET /clubs/:club_id/competitors`:
 * `display_name`, `date_of_birth`, `sex`, `belt_id` y, desde S4.8d §3, la afiliación actual
 * `affiliation_mode` + `sports_club_name`), más los traductores que inyecta la página: catálogo de
 * cinturones, formato de la edad derivada, nombre del sexo y etiqueta de «Independiente».
 *
 * Reglas:
 *
 * - nunca inventa datos: si falta el cinturón, la fecha de nacimiento o el nombre del club, el
 *   dato simplemente no aparece;
 * - `affiliation_mode: 'INDEPENDENT'` se dice **explícitamente** («Independiente»), no se omite: es
 *   información, no ausencia de dato;
 * - el club representado **no** se compone aquí ni viaja a la clasificación previa: la
 *   representación pertenece a la inscripción y se elige en el alta.
 */
import type { CompetitorSex } from './f3_competitor_contract.ts';
import { derivedAge } from './f3_sports_profile.ts';

export interface CompetitorOptionSource {
  display_name: string;
  belt_id: number | null;
  date_of_birth: string | null;
  /** Afiliación **actual** proyectada por el listado del club (S4.8d §3). */
  affiliation_mode?: 'CLUB' | 'INDEPENDENT';
  sports_club_name?: string | null;
}

export interface CompetitorOptionLabelContext {
  /** Fecha de referencia de la edad derivada (`YYYY-MM-DD`). */
  referenceDate: string;
  /** Etiqueta del cinturón por su id, o `null` si no se conoce. */
  beltLabel: (beltId: number) => string | null;
  /** Formato de la edad derivada (p. ej. «34 años»). */
  ageLabel: (age: number) => string;
  /**
   * Etiqueta de «Independiente» (§4). Un competidor afiliado a un club se pinta con el nombre real
   * del club, no con una etiqueta genérica; sin esta etiqueta, `INDEPENDENT` no se pinta.
   */
  independentLabel?: string;
}

/** Contexto del resumen de la modal: el de la etiqueta más el nombre del sexo. */
export interface CompetitorSummaryContext extends CompetitorOptionLabelContext {
  sexLabel: (sex: CompetitorSex) => string;
}

/** Fila del listado del club con lo que necesita el resumen: identidad, perfil y afiliación. */
export interface CompetitorSummarySource extends CompetitorOptionSource {
  id: number;
  sex?: CompetitorSex | null;
}

/** Una fila del resumen: clave i18n de la etiqueta y valor ya legible. */
export interface CompetitorSummaryRow {
  labelKey: string;
  value: string;
}

/** Separador de los datos del competidor en una sola línea: `Nombre · Azul · 34 años`. */
export const COMPETITOR_LABEL_SEPARATOR = ' · ';

/**
 * Etiqueta de la afiliación actual: el nombre del club, «Independiente», o `null` cuando el
 * listado no publica afiliación (no se deduce ninguna).
 *
 * Un `CLUB` sin nombre no se pinta: mejor vacío que un club inventada.
 */
export function affiliationLabel(
  competitor: Pick<CompetitorOptionSource, 'affiliation_mode' | 'sports_club_name'>,
  context: Pick<CompetitorOptionLabelContext, 'independentLabel'> = {}
): string | null {
  if (competitor.affiliation_mode === 'CLUB') {
    const name = (competitor.sports_club_name ?? '').trim();
    return name === '' ? null : name;
  }
  if (competitor.affiliation_mode === 'INDEPENDENT') {
    return context.independentLabel ?? null;
  }
  return null;
}

export function competitorOptionLabel(
  competitor: CompetitorOptionSource,
  context: CompetitorOptionLabelContext
): string {
  const parts = [competitor.display_name];

  if (competitor.belt_id != null) {
    const belt = context.beltLabel(competitor.belt_id);
    if (belt != null && belt.length > 0) {
      parts.push(belt);
    }
  }

  const age = derivedAge(competitor.date_of_birth, context.referenceDate);
  if (age != null) {
    parts.push(context.ageLabel(age));
  }

  const affiliation =
    context.independentLabel == null
      ? affiliationLabel(competitor)
      : affiliationLabel(competitor, { independentLabel: context.independentLabel });
  if (affiliation != null) {
    parts.push(affiliation);
  }

  return parts.join(COMPETITOR_LABEL_SEPARATOR);
}

/**
 * Resumen del competidor elegido (§4.2): fecha de nacimiento, edad derivada, sexo, cinturón y
 * afiliación actual, en ese orden. Sólo se incluyen los datos que existen de verdad.
 */
export function competitorSummaryRows(
  competitor: CompetitorSummarySource,
  context: CompetitorSummaryContext
): CompetitorSummaryRow[] {
  const rows: CompetitorSummaryRow[] = [];

  if (competitor.date_of_birth != null && competitor.date_of_birth !== '') {
    rows.push({ labelKey: 'f3_profile_dob_label', value: competitor.date_of_birth });
  }

  const age = derivedAge(competitor.date_of_birth, context.referenceDate);
  if (age != null) {
    rows.push({ labelKey: 'f3_profile_age_label', value: context.ageLabel(age) });
  }

  if (competitor.sex != null) {
    rows.push({ labelKey: 'f3_sex_label', value: context.sexLabel(competitor.sex) });
  }

  if (competitor.belt_id != null) {
    const belt = context.beltLabel(competitor.belt_id);
    if (belt != null && belt.length > 0) {
      rows.push({ labelKey: 'f3_profile_belt_label', value: belt });
    }
  }

  const affiliation =
    context.independentLabel == null
      ? affiliationLabel(competitor)
      : affiliationLabel(competitor, { independentLabel: context.independentLabel });
  if (affiliation != null) {
    rows.push({ labelKey: 'f3_profile_club_label', value: affiliation });
  }

  return rows;
}
