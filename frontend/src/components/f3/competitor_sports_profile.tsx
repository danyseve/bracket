import { Button, Modal, Select, Stack, Text, TextInput } from '@mantine/core';
import { showNotification } from '@mantine/notifications';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import BeltSelect from '@components/f3/belt_select';
import F3ErrorAlert from '@components/f3/f3_error_alert';
import SportsClubLogo from '@components/f3/sports_club_logo';
import SportsClubSelect from '@components/f3/sports_club_select';
import { COMPETITOR_SEXES, CompetitorSex } from '@components/utils/f3_competitor_contract';
import { describeF3Failure, F3Failure } from '@components/utils/f3_errors';
import {
  affiliationAction,
  CompetitorProfileTarget,
  currentSportsClubId,
  dateOfBirthError,
  profileBody,
  profileChanged,
  sexOptions,
  SPORTS_CLUB_INDEPENDENT,
  toIsoDate,
} from '@components/utils/f3_sports_profile';
import { actionScope } from '@components/utils/f3_write';
import useF3Write from '@components/utils/f3_write_hook';
import {
  assignAffiliation,
  getBelts,
  getCompetitorAffiliations,
  getCompetitorSportsProfile,
  getSportsClubs,
  goIndependent,
  updateCompetitorProfile,
} from '@services/f3';

/**
 * Perfil deportivo del competidor y su club (S4.8a-4 §3-§6).
 *
 * Un solo formulario para lo que el backend separa en dos acciones de dominio:
 *
 * - el **perfil** (`date_of_birth`, `sex`, `belt_id`) se reemplaza con `POST .../profile`;
 * - el **club** se resuelve con las rutas de afiliación (`POST .../affiliations`) o de
 *   independencia (`POST .../independence`), nunca con texto libre y sin tocar el histórico de
 *   inscripciones.
 *
 * Los catálogos (cinturones, academias) y la afiliación vigente se leen del backend; la UI no
 * inventa orden de cinturones ni reglas de edad, y sólo escribe lo que cambió de verdad.
 */
export default function CompetitorSportsProfile({
  opened,
  onClose,
  club_id,
  competitor,
  canManageAcademy = false,
  onSaved,
}: {
  opened: boolean;
  onClose: () => void;
  club_id: number | null;
  competitor: CompetitorProfileTarget | null;
  canManageAcademy?: boolean;
  onSaved?: () => void;
}) {
  const { t } = useTranslation();
  const { run, inflight } = useF3Write();

  const competitor_id = competitor != null ? competitor.id : -1;
  const swrProfile = getCompetitorSportsProfile(club_id, competitor_id);
  const swrBelts = getBelts(club_id);
  const swrClubs = getSportsClubs(club_id);
  const swrAffiliations = getCompetitorAffiliations(club_id, competitor_id);

  const [dateOfBirth, setDateOfBirth] = useState('');
  const [sex, setSex] = useState<string | null>(null);
  const [beltId, setBeltId] = useState<number | null>(null);
  const [clubChoice, setClubChoice] = useState<string>(SPORTS_CLUB_INDEPENDENT);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [failure, setFailure] = useState<F3Failure | null>(null);

  const profile = swrProfile.data;
  const affiliations = swrAffiliations.data != null ? swrAffiliations.data.affiliations : [];
  const clubs = swrClubs.data != null ? swrClubs.data : [];

  useEffect(() => {
    if (!opened) {
      return;
    }
    setValidationError(null);
    setFailure(null);
  }, [opened, competitor_id]);

  useEffect(() => {
    if (!opened || profile == null) {
      return;
    }
    setDateOfBirth(profile.date_of_birth ?? '');
    setSex(profile.sex);
    setBeltId(profile.belt_id);
  }, [opened, profile]);

  useEffect(() => {
    if (!opened) {
      return;
    }
    const current = currentSportsClubId(affiliations);
    setClubChoice(current == null ? SPORTS_CLUB_INDEPENDENT : String(current));
  }, [opened, swrAffiliations.data]);

  const today = toIsoDate(new Date());
  const selectedClub = clubs.find((club) => String(club.id) === clubChoice) ?? null;

  async function handleSave() {
    if (club_id == null || competitor == null) {
      return;
    }
    const error = dateOfBirthError(dateOfBirth, today);
    if (error != null) {
      setValidationError(error);
      return;
    }
    setValidationError(null);
    setFailure(null);

    const initial = {
      date_of_birth: profile != null ? profile.date_of_birth : null,
      sex: profile != null ? profile.sex : null,
      belt_id: profile != null ? profile.belt_id : null,
    };
    const next = {
      date_of_birth: dateOfBirth.trim() === '' ? null : dateOfBirth.trim(),
      sex: sex == null ? null : (sex as CompetitorSex),
      belt_id: beltId,
    };

    if (profileChanged(initial, next)) {
      const outcome = await run(actionScope(['competitor-profile', competitor.id]), (key) =>
        updateCompetitorProfile(club_id, competitor.id, profileBody(next), key)
      );
      if (outcome.status === 'ignored') {
        return;
      }
      if (outcome.value == null || !outcome.value.ok) {
        setFailure(outcome.value != null && !outcome.value.ok ? outcome.value.failure : null);
        return;
      }
    }

    const action = affiliationAction(currentSportsClubId(affiliations), clubChoice);
    if (action.kind !== 'none') {
      const outcome = await run(
        actionScope(['competitor-affiliation', competitor.id, action.kind]),
        (key) =>
          action.kind === 'assign'
            ? assignAffiliation(club_id, competitor.id, action.sports_club_id, key)
            : goIndependent(club_id, competitor.id, key)
      );
      if (outcome.status === 'ignored') {
        return;
      }
      if (outcome.value == null || !outcome.value.ok) {
        setFailure(outcome.value != null && !outcome.value.ok ? outcome.value.failure : null);
        return;
      }
      showNotification({
        color: 'green',
        message:
          action.kind === 'assign'
            ? t('f3_profile_affiliation_success')
            : t('f3_profile_independence_success'),
      });
    }

    showNotification({ color: 'green', message: t('f3_profile_success') });
    await swrProfile.mutate();
    await swrAffiliations.mutate();
    if (onSaved != null) {
      onSaved();
    }
  }

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={t('f3_profile_title')}
      data-competitor-sports-profile
    >
      <Stack gap="sm">
        <Text size="sm" c="dimmed">
          {t('f3_profile_description')}
        </Text>

        <F3ErrorAlert
          failure={swrProfile.error != null ? describeF3Failure(swrProfile.error) : null}
        />

        {swrProfile.isLoading ? (
          <Text size="sm" c="dimmed">
            {t('f3_profile_loading')}
          </Text>
        ) : null}

        <TextInput
          type="date"
          label={t('f3_profile_dob_label')}
          description={t('f3_profile_dob_description')}
          max={today}
          value={dateOfBirth}
          onChange={(event) => setDateOfBirth(event.currentTarget.value)}
          error={validationError != null ? t(validationError) : null}
        />

        <Select
          label={t('f3_sex_label')}
          data={sexOptions(COMPETITOR_SEXES).map((option) => ({
            value: option.value,
            label: t(option.labelKey),
          }))}
          value={sex}
          onChange={setSex}
          clearable
          allowDeselect
          data-sex-select
        />

        <BeltSelect
          belts={swrBelts.data != null ? swrBelts.data : []}
          isLoading={swrBelts.isLoading}
          value={beltId}
          onChange={setBeltId}
        />
        <F3ErrorAlert failure={swrBelts.error != null ? describeF3Failure(swrBelts.error) : null} />

        <SportsClubSelect
          clubs={clubs}
          isLoading={swrClubs.isLoading}
          value={clubChoice}
          onChange={setClubChoice}
        />
        <SportsClubLogo
          sportsClub={selectedClub}
          club_id={club_id}
          canManage={canManageAcademy}
          onChanged={() => {
            void swrClubs.mutate();
          }}
        />
        <F3ErrorAlert failure={swrClubs.error != null ? describeF3Failure(swrClubs.error) : null} />

        <F3ErrorAlert failure={failure} />

        <Button onClick={handleSave} loading={inflight} disabled={inflight} fullWidth>
          {t('f3_profile_save_button')}
        </Button>
      </Stack>
    </Modal>
  );
}
