/**
 * Catálogo real de categorías de un torneo (S4.8d §8).
 *
 * El motor de clasificación del backend identifica cada categoría con la clave contractual
 * `si:<stage_item_id>` (`bracket/logic/classification/rules.py`: `CATEGORY_KEY_PREFIX = "si:"`,
 * `category_key_for`). Esa es la clave que acepta el alta de inscripción y la que devuelve el
 * motor en `compatible_categories`; por eso el selector de categorías del formulario se alimenta
 * del **mismo** origen que usa el motor: los `stage_items` del torneo
 * (`GET tournaments/{id}/stages`, ya existente y de solo lectura).
 *
 * Antes de S4.8d el formulario se alimentaba de `bracket-readiness`, que solo enumera categorías
 * que **ya tienen inscripciones agrupadas**: en un torneo con el cuadro por generar la lista llega
 * vacía y el organizador no puede elegir ninguna de las categorías que sí existen
 * (torneos 6 y 9: seis categorías `-62 kg`…`-92 kg` cada uno, readiness `[]`).
 *
 * Módulo **puro**: sin red, sin React, sin i18next. No inventa el orden ni los nombres: lee
 * `stage_items[].id` y `stage_items[].name` tal cual llegan.
 */

/** Prefijo contractual de la clave de categoría del motor de clasificación. */
export const CATEGORY_KEY_PREFIX = 'si:';

/** Clave contractual de categoría a partir del identificador de un `stage_item`. */
export function categoryKeyFromStageItem(stageItemId: number): string {
  return `${CATEGORY_KEY_PREFIX}${stageItemId}`;
}

/** `stage_item_id` de una clave contractual, o `null` si la clave no es del motor. */
export function stageItemIdFromCategoryKey(category_key: string | null): number | null {
  if (category_key == null || !category_key.startsWith(CATEGORY_KEY_PREFIX)) {
    return null;
  }
  const suffix = category_key.slice(CATEGORY_KEY_PREFIX.length);
  return /^\d+$/.test(suffix) ? Number(suffix) : null;
}

/** Opción de un selector: la clave contractual como valor y el nombre del `stage_item` visible. */
export interface TournamentCategoryOption {
  category_key: string;
  label: string;
}

/** Forma mínima del `stage_item` que publica `GET tournaments/{id}/stages`. */
export interface StageItemLike {
  id: number;
  name?: string | null;
}

/** Forma mínima del `stage` que publica `GET tournaments/{id}/stages`. */
export interface StageLike {
  id?: number | null;
  stage_items?: StageItemLike[] | null;
}

/**
 * Categorías del torneo en el formato que espera el selector de Mantine, en el **orden declarado**
 * por el torneo (orden de `stage` y de `stage_item`): es el orden en el que el organizador las
 * creó y el que ve en el cuadro, no un orden alfabético inventado por la UI.
 *
 * Los `stage_items` sin nombre se omiten: una categoría sin etiqueta no es elegible por una
 * persona, y el backend tampoco puede nombrarla. Las claves duplicadas (no deberían existir) se
 * quedan con la primera aparición para no ofrecer dos veces la misma categoría.
 */
export function tournamentCategoryOptions(
  stages: readonly StageLike[] | null | undefined
): TournamentCategoryOption[] {
  const options = new Map<string, TournamentCategoryOption>();

  for (const stage of stages ?? []) {
    for (const item of stage.stage_items ?? []) {
      if (item == null || item.id == null) {
        continue;
      }
      const label = item.name == null ? '' : String(item.name).trim();
      if (label === '') {
        continue;
      }
      const category_key = categoryKeyFromStageItem(item.id);
      if (!options.has(category_key)) {
        options.set(category_key, { category_key, label });
      }
    }
  }

  return [...options.values()];
}

/**
 * ¿El torneo tiene alguna categoría declarada? El formulario lo usa para decidir entre selector
 * (catálogo real) y texto libre (torneo todavía sin `stage_items`, caso de los cuadros por
 * diseñar): así la ruta heredada sigue funcionando sin inventar categorías.
 */
export function hasTournamentCategories(stages: readonly StageLike[] | null | undefined): boolean {
  return tournamentCategoryOptions(stages).length > 0;
}
