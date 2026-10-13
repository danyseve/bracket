/**
 * Peso declarado, modalidad y proyección de la clasificación mostrada al organizador
 * (S4.8a-4 §8-§14, §18).
 *
 * Módulo puro: sin red, sin DOM, sin i18next. Su único trabajo es normalizar el peso, traducir los
 * códigos **cerrados** del motor a claves i18n y proyectar la respuesta del endpoint de clasificación
 * a un estado que la UI pueda pintar.
 *
 * Lo que este módulo **no** hace (y es deliberado): no reproduce las reglas de categoría ni decide la
 * compatibilidad. Esa autoridad es del motor del backend (`bracket/logic/classification`); aquí solo
 * se consume su veredicto, código a código. Los textos visibles son claves de traducción.
 */
import type {
  ClassificationCategoryVerdict,
  Modality,
  RegistrationClassification,
} from './f3_competitor_contract.ts';
import { classificationBlockerKey, classificationWarningKey } from './f3_competitor_contract.ts';

/* ------------------------------------------------------------------- peso declarado (§8) ------- */

/** Cota superior real de `declared_weight_kg` en el backend: `NUMERIC(5,2)`, `0 < kg <= 300`. */
export const WEIGHT_MAX_KG = 300;

/** Cota inferior real: el peso debe ser estrictamente positivo (el backend rechaza 0). */
export const WEIGHT_MIN_KG_EXCLUSIVE = 0;

/**
 * Normaliza y valida un peso declarado en kg. Acepta decimales (punto o coma, se normaliza a punto)
 * y exige el rango real del backend (`0 < kg <= 300`). Devuelve la forma canónica a enviar en
 * `declared_weight_kg`, o `null` si no es un peso válido.
 */
export function parseDeclaredWeightKg(value: string): string | null {
  const normalized = (value ?? '').trim().replace(',', '.');
  if (!/^\d+(\.\d+)?$/.test(normalized)) {
    return null;
  }
  const kg = Number(normalized);
  if (!Number.isFinite(kg) || kg <= WEIGHT_MIN_KG_EXCLUSIVE || kg > WEIGHT_MAX_KG) {
    return null;
  }
  return normalized;
}

/**
 * Error de UX del campo «Peso (kg)», o `null` si el valor es aceptable.
 *
 * Por defecto el peso **no** es obligatorio: el backend no lo exige para inscribir (§18) y su
 * ausencia la resuelve el motor con `MISSING_WEIGHT`. Solo si un flujo concreto lo pide (`required`)
 * la falta se marca como error de formulario.
 */
export function weightInputError(value: string, required = false): string | null {
  if ((value ?? '').trim() === '') {
    return required ? 'f3_registration_weight_required' : null;
  }
  return parseDeclaredWeightKg(value) == null ? 'f3_registration_weight_invalid' : null;
}

/* ------------------------------------------------------------------------ modalidad (§9) ------- */

/** Clave i18n del nombre de cada modalidad. No existe `GI_AND_NO_GI`. */
export const MODALITY_KEYS: Record<Modality, string> = {
  GI: 'f3_registration_modality_gi',
  NO_GI: 'f3_registration_modality_no_gi',
};

/**
 * Clave i18n del nombre de la modalidad, o `null` si no está declarada: una modalidad ausente no se
 * fuerza a un valor inventado (§9/§18).
 */
export function modalityLabelKey(modality: Modality | null): string | null {
  return modality == null ? null : MODALITY_KEYS[modality];
}

/* ------------------------------------------------------------------- clasificación (§10-§14) --- */

/** Traduce una lista de códigos de bloqueo a claves humanas (fallback técnico si son desconocidos). */
export function classificationBlockerKeys(blockers: readonly string[]): string[] {
  return blockers.map((code) => classificationBlockerKey(code));
}

/** Traduce una lista de códigos de aviso a claves humanas. Un aviso **no** bloquea (§13). */
export function classificationWarningKeys(warnings: readonly string[]): string[] {
  return warnings.map((code) => classificationWarningKey(code));
}

/** Estado de clasificación listo para pintar, sin volver a interpretar el veredicto del backend. */
export type ClassificationState =
  | { kind: 'unclassifiable'; blockerKeys: string[] }
  | { kind: 'single'; category: ClassificationCategoryVerdict }
  | { kind: 'multiple'; categories: ClassificationCategoryVerdict[] };

/**
 * Proyecta la respuesta del endpoint de clasificación a uno de tres estados:
 *
 * - `unclassifiable`: ninguna categoría compatible (bloqueos del motor, p. ej. falta de peso);
 * - `single`: **una** compatible -> el backend la recomienda y la UI la preselecciona (§14);
 * - `multiple`: varias compatibles -> no se elige ninguna, decide el organizador (§14).
 */
export function classificationState(
  classification: RegistrationClassification
): ClassificationState {
  const compatible = classification.compatible_categories;
  if (compatible.length === 0) {
    return {
      kind: 'unclassifiable',
      blockerKeys: classificationBlockerKeys(classification.blockers),
    };
  }
  if (compatible.length === 1) {
    return { kind: 'single', category: compatible[0] };
  }
  return { kind: 'multiple', categories: compatible };
}

/**
 * Clave de la categoría a preseleccionar, o `null` si no hay una única compatible (§14): con varias
 * compatibles el backend no recomienda ninguna y la UI **no** elige la primera.
 */
export function preselectedCategoryKey(
  classification: Pick<RegistrationClassification, 'compatible_categories'>
): string | null {
  return classification.compatible_categories.length === 1
    ? classification.compatible_categories[0].category_key
    : null;
}
