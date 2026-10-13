import { Select } from '@mantine/core';
import { useTranslation } from 'react-i18next';

import SportsClubLogo from '@components/f3/sports_club_logo';
import { SportsClub } from '@components/utils/f3_competitor_contract';
import { SPORTS_CLUB_INDEPENDENT, sportsClubOptions } from '@components/utils/f3_sports_profile';

/**
 * Selector de club / academia (S4.8a-4 §6).
 *
 * Las opciones salen del backend (`GET /clubs/:club_id/sports-clubs`) más la opción «Independiente»:
 * nunca hay campo de texto libre. El valor viaja como el `id` de la academia o como el centinela
 * `independent`; el componente que lo usa decide el endpoint real de afiliación o independencia.
 */
export default function SportsClubSelect({
  clubs,
  isLoading = false,
  value,
  onChange,
  disabled = false,
}: {
  clubs: SportsClub[];
  isLoading?: boolean;
  value: string | null;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation();

  const options = sportsClubOptions(clubs);
  const data = options.map((option) => ({
    value: option.value,
    label:
      option.value === SPORTS_CLUB_INDEPENDENT
        ? t('f3_club_independent_option')
        : (option.name ?? ''),
  }));
  const byValue = new Map(options.map((option) => [option.value, option]));

  return (
    <Select
      label={t('f3_profile_club_label')}
      description={t('f3_profile_club_description')}
      placeholder={isLoading ? t('f3_profile_club_loading') : t('f3_club_independent_option')}
      data={data}
      value={value}
      onChange={(selected) => onChange(selected ?? SPORTS_CLUB_INDEPENDENT)}
      disabled={disabled || isLoading}
      allowDeselect={false}
      searchable
      renderOption={({ option }) => {
        const club = byValue.get(option.value);
        if (club == null || club.value === SPORTS_CLUB_INDEPENDENT) {
          return option.label;
        }
        return (
          <SportsClubLogo
            sportsClub={{
              id: Number(club.value),
              name: club.name ?? '',
              active: true,
              logo_path: club.logo_path,
            }}
            size={24}
            compact
          />
        );
      }}
      data-sports-club-select
    />
  );
}
