import { Button, Group, Modal, Select, Stack, Text, TextInput } from '@mantine/core';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import BeltSelect from '@components/f3/belt_select';
import F3ErrorAlert from '@components/f3/f3_error_alert';
import SportsClubSelect from '@components/f3/sports_club_select';
import {
  Belt,
  COMPETITOR_SEXES,
  CompetitorSex,
  SportsClub,
} from '@components/utils/f3_competitor_contract';
import { F3Failure } from '@components/utils/f3_errors';
import {
  COMPETITION_BELT_SCOPES,
  DERIVED_AGE_LABEL_KEY,
  SPORTS_CLUB_INDEPENDENT,
  dateOfBirthError,
  derivedAge,
  sexOptions,
  toIsoDate,
} from '@components/utils/f3_sports_profile';

/** Valores que produce el alta en un solo flujo (§5). */
export interface CompetitorCreateValues {
  display_name: string;
  date_of_birth: string | null;
  sex: CompetitorSex | null;
  belt_id: number | null;
  /** `id` del club elegido o el centinela de «Independiente»; nunca texto libre. */
  club_choice: string;
}

/**
 * Alta de competidor en un solo flujo (S4.8g §5).
 *
 * Dos pasos, una sola experiencia: el organizador no tiene que crear el nombre, cerrar, buscar la fila
 * y abrir el perfil para completar el resto. Paso 1 pide los datos deportivos (nombre, fecha de
 * nacimiento, sexo y cinturón); paso 2 pide el Club / Club representado o «Independiente».
 *
 * El componente **no escribe**: solo recoge valores y avisa. Quien lo usa decide la orquestación real
 * (`POST /clubs/:id/competitors` y, si procede, la afiliación) porque es ahí donde hay que distinguir
 * entre «no se pudo crear» y «se creó pero quedó un paso pendiente».
 *
 * La **edad** se muestra derivada y de solo lectura: no se persiste, no viaja al backend y no decide
 * nada (`f3_sports_profile.derivedAge`). El cinturón se ofrece de la escala adulto, que es el catálogo
 * de competición de este proyecto; la escala juvenil no se borra del catálogo del backend.
 *
 * La validación es de **UX** (nombre obligatorio, fecha con formato real y no futura): el backend sigue
 * siendo la autoridad y ninguna regla deportiva se duplica aquí.
 */
export default function CompetitorCreateWizard({
  opened,
  onClose,
  onSubmit,
  inflight = false,
  error = null,
  belts = [],
  beltsLoading = false,
  clubs = [],
  clubsLoading = false,
}: {
  opened: boolean;
  onClose: () => void;
  onSubmit: (values: CompetitorCreateValues) => void;
  inflight?: boolean;
  error?: F3Failure | null;
  belts?: Belt[];
  beltsLoading?: boolean;
  clubs?: SportsClub[];
  clubsLoading?: boolean;
}) {
  const { t } = useTranslation();
  const [step, setStep] = useState<1 | 2>(1);
  const [displayName, setDisplayName] = useState('');
  const [dateOfBirth, setDateOfBirth] = useState('');
  const [sex, setSex] = useState<string | null>(null);
  const [beltId, setBeltId] = useState<number | null>(null);
  const [clubChoice, setClubChoice] = useState<string>(SPORTS_CLUB_INDEPENDENT);
  const [validationError, setValidationError] = useState<string | null>(null);

  // Cada apertura empieza limpia: el asistente no arrastra los datos del alta anterior.
  useEffect(() => {
    if (!opened) {
      return;
    }
    setStep(1);
    setDisplayName('');
    setDateOfBirth('');
    setSex(null);
    setBeltId(null);
    setClubChoice(SPORTS_CLUB_INDEPENDENT);
    setValidationError(null);
  }, [opened]);

  const today = toIsoDate(new Date());
  const age = derivedAge(dateOfBirth, today);

  function goToClubStep() {
    const trimmed = displayName.trim();
    if (trimmed === '') {
      setValidationError('f3_competitor_name_required');
      return;
    }
    if (trimmed.length > 200) {
      setValidationError('f3_competitor_name_too_long');
      return;
    }
    const dobError = dateOfBirthError(dateOfBirth, today);
    if (dobError != null) {
      setValidationError(dobError);
      return;
    }
    setValidationError(null);
    setStep(2);
  }

  function submit() {
    onSubmit({
      display_name: displayName.trim(),
      date_of_birth: dateOfBirth.trim() === '' ? null : dateOfBirth.trim(),
      sex: sex == null ? null : (sex as CompetitorSex),
      belt_id: beltId,
      club_choice: clubChoice,
    });
  }

  return (
    <Modal opened={opened} onClose={onClose} title={t('competitors_create_title')}>
      <Stack gap="sm" data-competitor-create-wizard data-create-step={step}>
        <Text size="xs" c="dimmed" data-create-step-indicator>
          {t('f3_competitor_create_step_indicator', { step, total: 2 })}
        </Text>

        {step === 1 ? (
          <Text size="sm" c="dimmed">
            {t('competitors_create_description')}
          </Text>
        ) : null}

        {step === 1 ? (
          <>
            <Text fw={600}>{t('f3_competitor_create_step1_title')}</Text>
            <TextInput
              withAsterisk
              label={t('f3_competitor_name_label')}
              maxLength={200}
              data-autofocus
              value={displayName}
              onChange={(event) => setDisplayName(event.currentTarget.value)}
            />
            <TextInput
              type="date"
              label={t('f3_profile_dob_label')}
              description={t('f3_profile_dob_description')}
              max={today}
              value={dateOfBirth}
              onChange={(event) => setDateOfBirth(event.currentTarget.value)}
            />
            <Text size="sm" data-derived-age>
              {t(DERIVED_AGE_LABEL_KEY)}:{' '}
              {age != null ? t('f3_profile_age_value', { age }) : t('f3_profile_dob_none')}
            </Text>
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
              belts={belts}
              isLoading={beltsLoading}
              value={beltId}
              onChange={setBeltId}
              scopes={COMPETITION_BELT_SCOPES}
            />
          </>
        ) : null}

        {step === 2 ? (
          <>
            <Text fw={600}>{t('f3_competitor_create_step2_title')}</Text>
            <Text size="sm" c="dimmed">
              {t('f3_competitor_create_summary', { name: displayName.trim() })}
            </Text>
            <SportsClubSelect
              clubs={clubs}
              isLoading={clubsLoading}
              value={clubChoice}
              onChange={setClubChoice}
            />
            <Text size="xs" c="dimmed">
              {t('f3_competitor_create_club_note')}
            </Text>
          </>
        ) : null}

        <F3ErrorAlert failure={error} />
        {validationError != null ? (
          <Text size="sm" c="red" data-create-validation-error>
            {t(validationError)}
          </Text>
        ) : null}

        <Group grow>
          {step === 2 ? (
            <Button variant="default" onClick={() => setStep(1)} disabled={inflight}>
              {t('f3_competitor_create_back')}
            </Button>
          ) : null}
          {step === 1 ? (
            <Button onClick={goToClubStep} data-create-next>
              {t('f3_competitor_create_next')}
            </Button>
          ) : (
            <Button onClick={submit} loading={inflight} disabled={inflight} data-create-submit>
              {t('competitors_create_button')}
            </Button>
          )}
        </Group>
      </Stack>
    </Modal>
  );
}
