import { Select } from '@mantine/core';
import { useTranslation } from 'react-i18next';

import { BELT_SCOPES, Belt, BeltScope } from '@components/utils/f3_competitor_contract';
import { beltOptions, beltScopeLabelKey } from '@components/utils/f3_sports_profile';

/**
 * Selector de cinturón (S4.8a-4 §3).
 *
 * El catálogo viene del backend (`GET /clubs/:club_id/belts`, `list_belts_endpoint`): la UI no
 * hardcodea el orden deportivo, solo pinta lo que llega ordenado por `rank`/`code` dentro de cada
 * escala. La etiqueta es la traducción si existe y, si no, la canónica del backend.
 *
 * Es un componente «tonto»: no lee ni escribe, solo presenta el catálogo y avisa del cambio.
 *
 * `scopes` acota las escalas que se ofrecen. Por defecto las dos (`BELT_SCOPES`); el perfil del
 * competidor pasa solo la escala adulto, que es el catálogo de competición de este proyecto
 * (Blanco · Azul · Violeta · Marrón · Negro, S4.8d §4). La escala juvenil sigue en el catálogo del
 * backend (`scope = JUVENILE`, `active = true`): no se borra nada, solo se deja de ofrecer aquí.
 */
export default function BeltSelect({
  belts,
  isLoading = false,
  value,
  onChange,
  disabled = false,
  scopes = BELT_SCOPES,
}: {
  belts: Belt[];
  isLoading?: boolean;
  value: number | null;
  onChange: (beltId: number | null) => void;
  disabled?: boolean;
  scopes?: BeltScope[];
}) {
  const { t } = useTranslation();

  // i18next devuelve la propia clave cuando no hay traducción: ese caso usa el `label` del backend.
  const lookup = (key: string): string | null => {
    const translated = t(key);
    return translated === key ? null : translated;
  };

  const choices = beltOptions(belts, scopes, lookup);
  const data = scopes
    .map((scope) => ({
      group: t(beltScopeLabelKey(scope)),
      items: choices
        .filter((choice) => choice.scope === scope)
        .map((choice) => ({ value: choice.value, label: choice.label })),
    }))
    .filter((group) => group.items.length > 0);

  return (
    <Select
      label={t('f3_profile_belt_label')}
      description={t('f3_profile_belt_description')}
      placeholder={
        isLoading
          ? t('f3_profile_belt_loading')
          : data.length === 0
            ? t('f3_profile_belt_empty')
            : t('f3_profile_belt_none')
      }
      data={data}
      value={value == null ? null : String(value)}
      onChange={(selected) =>
        onChange(selected == null || selected === '' ? null : Number(selected))
      }
      disabled={disabled || isLoading}
      nothingFoundMessage={t('f3_profile_belt_empty')}
      searchable
      clearable
      allowDeselect
      data-belt-select
    />
  );
}
