/**
 * Configuración compartida de i18next (P2.8B F2C).
 *
 * Vive fuera de `i18n.ts` para que los tests puedan verificar exactamente la MISMA configuración
 * que usa la aplicación (namespace, idioma de respaldo y URL de los locales) sin arrancar el
 * backend HTTP.
 *
 * `LOCALES_REVISION`: los ficheros de `public/locales/<idioma>/common.json` se sirven sin
 * `Cache-Control` y su URL no estaba versionada, así que un release podía encontrarse el JSON
 * antiguo guardado en la caché del navegador y mostrar las claves crudas
 * (`generate_bracket_modal_title`) en lugar del texto. Versionar la URL (`?v=<hash de los locales>`)
 * y revalidar por ETag elimina ese estado inconsistente.
 *
 * El test `frontend/tests/i18n_locales.test.ts` recalcula el hash sobre los ficheros de locales y
 * falla si alguno cambia sin actualizar la constante: así el versionado no puede quedarse atrás.
 */
export const LOCALES_REVISION = '774e67cc8391';

export const i18nInitOptions = {
  fallbackLng: 'en',
  defaultNS: 'common',
  backend: {
    loadPath: `/locales/{{lng}}/{{ns}}.json?v=${LOCALES_REVISION}`,
    requestOptions: { cache: 'no-cache' as RequestCache },
  },
  interpolation: {
    escapeValue: false,
  },
};
