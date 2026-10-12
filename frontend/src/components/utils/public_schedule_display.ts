/**
 * Derivación pura de la pantalla pública del planificador (S4.5).
 *
 * La pantalla **no decide nada**: los estados (`NOW`, `NEXT`, `UPCOMING`, `WAITING`, `COMPLETED`,
 * `DELAYED`), la hora estimada (que ya lleva el retraso aplicado) y lo que falta para poder pelear
 * vienen calculados por el backend. Este módulo solo coloca ese cuerpo en filas y en claves de
 * traducción, para que la TV no derive nada por su cuenta.
 *
 * No importa nada (ni React, ni axios, ni i18next) para poder ejercerlo con `node --test` sin red
 * y sin navegador.
 */
import type {
  PublicScheduleCourt,
  PublicScheduleMatch,
  PublicSchedulePayload,
} from './f3_contract.ts';
import { publicStateKey } from './f3_contract.ts';

/** Una fila de la pantalla, ya resuelta a partir de la ficha pública del combate. */
export interface DisplayRow {
  matchId: number;
  /** Estado que ha decidido el servidor. La pantalla lo pinta tal cual. */
  state: string;
  /** Clave de traducción del estado (`public_state_now`). */
  stateKey: string;
  /** `Categoría · Ronda`, tal y como lo publica el backend. */
  title: string;
  /** `A vs B`, o cadena vacía si todavía no hay rivales. */
  fighters: string;
  /** Nombres que faltan para poder pelear; vacío si el combate ya tiene rivales. */
  waitingFor: string[];
  /** Hora estimada de salida, tal y como la publica el backend (ISO-8601). */
  estimatedStart: string | null;
  /** Hora planificada, sin retraso aplicado (ISO-8601). */
  scheduledStart: string | null;
}

function rowFor(match: PublicScheduleMatch): DisplayRow {
  return {
    matchId: match.match_id,
    state: String(match.state),
    stateKey: publicStateKey(match.state),
    title: `${match.category} · ${match.round_name}`,
    fighters: fightersLabel(match),
    waitingFor: waitingNames(match),
    estimatedStart: match.estimated_start,
    scheduledStart: match.scheduled_start,
  };
}

/** `A vs B`; vacío cuando el combate aún no tiene rivales (los publica `waiting_for`). */
export function fightersLabel(match: PublicScheduleMatch): string {
  const a = match.fighter_a;
  const b = match.fighter_b;
  if (a == null || b == null || a === '' || b === '') {
    return '';
  }
  return `${a} vs ${b}`;
}

/** Nombres que faltan para poder pelear, ya como lista lista para unir con comas. */
export function waitingNames(match: PublicScheduleMatch): string[] {
  return match.waiting_for.filter((name) => name !== '');
}

/** ¿Lleva retraso este tatami? */
export function isLateCourt(court: PublicScheduleCourt): boolean {
  return court.lag_minutes > 0;
}

/** Los combates que la pantalla enseña como cola del tatami: AHORA, SIGUIENTE y los siguientes. */
export function rowsFor(court: PublicScheduleCourt): DisplayRow[] {
  const rows: DisplayRow[] = [];
  if (court.current != null) {
    rows.push(rowFor(court.current));
  }
  if (court.next != null) {
    rows.push(rowFor(court.next));
  }
  for (const match of court.upcoming) {
    rows.push(rowFor(match));
  }
  return rows;
}

/** Lo último terminado en el tatami, para que la grada vea por dónde va la cola. */
export function resultsFor(court: PublicScheduleCourt): DisplayRow[] {
  return court.completed.map((match) => rowFor(match));
}

/** Combates que le quedan al tatami (sin contar los terminados). */
export function pendingCount(court: PublicScheduleCourt): number {
  return rowsFor(court).length;
}

/** ¿No hay nada que enseñar en todo el torneo? */
export function isEmptySchedule(payload: PublicSchedulePayload): boolean {
  return payload.courts.every((court) => pendingCount(court) === 0);
}

/** El tatami sin combates visibles tampoco se borra de la pantalla: se queda con su cartel. */
export function courtsToShow(payload: PublicSchedulePayload): PublicScheduleCourt[] {
  return payload.courts;
}
