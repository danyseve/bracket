/**
 * Asignación de categoría y override (S4.8a-4 §15-§16).
 *
 * Módulo **puro**: sin red, sin React, sin i18next. Traduce el veredicto del motor del backend a
 * una decisión de UI y construye el cuerpo que el endpoint real acepta
 * (`POST tournaments/{t}/registrations/{r}/category`, `RegistrationCategoryBody`).
 *
 * Reglas duras que este módulo protege:
 *
 * - nunca se envía una categoría **no compatible** sin `override=true`: sin él, la construcción
 *   falla con `classification_override_required` (§16, override nunca silencioso);
 * - el override **exige** un motivo del catálogo cerrado que acepta el backend; sin él, falla con
 *   `classification_override_reason_required`;
 * - una categoría vacía no se envía: el backend la rechazaría, así que la UI no la ofrece.
 *
 * La compatibilidad la decide el motor (`bracket/logic/classification`): aquí solo se consume su
 * `compatible_categories`. El frontend no reproduce ninguna regla de categoría (§15).
 */
import type {
  RegistrationCategoryBody,
  RegistrationClassification,
} from './f3_competitor_contract.ts';

/** Alias legible del cuerpo real que viaja a `POST .../category` (`RegistrationCategoryBody`). */
export type CategoryAssignmentBody = RegistrationCategoryBody;

/**
 * Códigos de motivo que el backend acepta para la acción `UPDATE` de una inscripción
 * (`logic/audit_reasons.py`: `DATA_CORRECTION`, `CATEGORY_CHANGE`, `REPRESENTATION_CHANGE`). Un
 * override fija una categoría, así que solo se ofrecen los dos que describen esa corrección.
 */
export const CATEGORY_ASSIGNMENT_REASON_CODES: string[] = ['CATEGORY_CHANGE', 'DATA_CORRECTION'];

/** Claves de las categorías **compatibles** que devolvió el motor, en el orden publicado. */
export function compatibleCategoryKeys(
  classification: Pick<RegistrationClassification, 'compatible_categories'>
): string[] {
  return classification.compatible_categories.map((category) => category.category_key);
}

/** ¿La categoría elegida está entre las compatibles que devolvió el motor? */
export function isCompatibleChoice(
  classification: Pick<RegistrationClassification, 'compatible_categories'>,
  category_key: string | null
): boolean {
  return category_key != null && compatibleCategoryKeys(classification).includes(category_key);
}

/**
 * ¿Elegir esta categoría exige un override explícito? Es decir: la categoría es real (no vacía) y
 * el motor **no** la lista como compatible. Con ella, la UI no puede asignar sin `override=true`.
 */
export function assignmentNeedsOverride(
  classification: Pick<RegistrationClassification, 'compatible_categories'>,
  category_key: string | null
): boolean {
  if (category_key == null || category_key.trim() === '') {
    return false;
  }
  return !isCompatibleChoice(classification, category_key);
}

/**
 * Error de validación del motivo del override, o `null` si es aceptable. El backend exige un motivo
 * del catálogo: la UI no manda un override sin él (§16).
 */
export function overrideReasonError(reason_code: string | null): string | null {
  if (reason_code == null || reason_code.trim() === '') {
    return 'classification_override_reason_required';
  }
  return CATEGORY_ASSIGNMENT_REASON_CODES.includes(reason_code)
    ? null
    : 'classification_override_reason_required';
}

/** Resultado de construir la asignación: el cuerpo listo, o la clave del error que la UI pinta. */
export type AssignmentBuild =
  | { ok: true; body: CategoryAssignmentBody }
  | { ok: false; errorKey: string };

/**
 * Construye el cuerpo de la asignación a partir de la elección del organizador y del veredicto del
 * motor. Es el único punto que decide entre asignación normal y override:
 *
 * - sin categoría -> `classification_assign_category_required`;
 * - sin `override` y categoría **no compatible** -> `classification_override_required` (la UI nunca
 *   fuerza en silencio);
 * - con `override` y sin motivo válido -> `classification_override_reason_required`;
 * - en cualquier otro caso, el cuerpo real para el endpoint.
 */
export function buildCategoryAssignment(params: {
  classification: Pick<RegistrationClassification, 'compatible_categories'>;
  category_key: string | null;
  override: boolean;
  reason_code?: string | null;
  reason_note?: string | null;
}): AssignmentBuild {
  const { classification, category_key, override } = params;

  if (category_key == null || category_key.trim() === '') {
    return { ok: false, errorKey: 'classification_assign_category_required' };
  }

  if (!override) {
    if (assignmentNeedsOverride(classification, category_key)) {
      return { ok: false, errorKey: 'classification_override_required' };
    }
    return {
      ok: true,
      body: { category_key, override: false, reason_code: null, reason_note: null },
    };
  }

  const reasonError = overrideReasonError(params.reason_code ?? null);
  if (reasonError != null) {
    return { ok: false, errorKey: reasonError };
  }

  return {
    ok: true,
    body: {
      category_key,
      override: true,
      reason_code: params.reason_code ?? null,
      reason_note: params.reason_note ?? null,
    },
  };
}
