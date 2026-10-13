import {
  Alert,
  Badge,
  Button,
  Card,
  Divider,
  Group,
  List,
  Radio,
  Stack,
  Text,
  Title,
} from '@mantine/core';
import { IconInfoCircle } from '@tabler/icons-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import F3ErrorAlert from '@components/f3/f3_error_alert';
import {
  classificationBlockerKeys,
  classificationState,
  classificationWarningKeys,
  modalityLabelKey,
  preselectedCategoryKey,
} from '@components/utils/f3_classification';
import type { RegistrationClassification } from '@components/utils/f3_competitor_contract';
import { categoryDisplayName } from '@components/utils/f3_contract';
import { F3Failure, describeF3Failure } from '@components/utils/f3_errors';
import { beltLabel, sexLabelKey } from '@components/utils/f3_sports_profile';
import {
  getBelts,
  getCompetitorSportsProfile,
  getRegistration,
  getRegistrationClassification,
} from '@services/f3';

/**
 * Clasificación mostrada al organizador (S4.8a-4 §10-§14, §18).
 *
 * Reúne las tres piezas que exige la revisión de una inscripción: la **ficha del competidor**
 * (nombre, cinturón, sexo, club), los **datos declarados** de la inscripción (peso, modalidad) y la
 * **clasificación** que devuelve el motor del backend.
 *
 * El motor es la única autoridad: este panel **no** reproduce reglas de categoría. Solo traduce los
 * códigos cerrados a texto humano con `classificationBlockerKey`/`classificationWarningKey`
 * (§12/§13) y proyecta el veredicto a tres estados. Una única compatible se preselecciona; con
 * varias compatibles no se elige la primera (§14) y la asignación de categoría (con su override)
 * llega en el flujo del organizador: aquí la superficie queda preparada pero **sin** llamar al
 * endpoint de asignación.
 */
export default function RegistrationClassificationPanel({
  tournament_id,
  registration_id,
  club_id,
}: {
  tournament_id: number;
  registration_id: number;
  club_id: number | null;
}) {
  const { t, i18n } = useTranslation();

  const swrRegistration = getRegistration(tournament_id, registration_id);
  const swrClassification = getRegistrationClassification(tournament_id, registration_id);
  const competitor_id = swrRegistration.data != null ? swrRegistration.data.competitor_id : null;
  const swrProfile = getCompetitorSportsProfile(club_id, competitor_id ?? -1);
  const swrBelts = getBelts(club_id);

  const registration = swrRegistration.data;
  const classification = swrClassification.data;
  const failure: F3Failure | null =
    swrRegistration.error != null
      ? describeF3Failure(swrRegistration.error)
      : swrClassification.error != null
        ? describeF3Failure(swrClassification.error)
        : null;

  if (registration == null) {
    return (
      <Stack gap="sm">
        <F3ErrorAlert failure={failure} />
        {failure == null ? (
          <Text size="sm" c="dimmed">
            {t('classification_loading')}
          </Text>
        ) : null}
      </Stack>
    );
  }

  const profile = swrProfile.data;
  const belt =
    profile != null && profile.belt_id != null
      ? ((swrBelts.data ?? []).find((item) => item.id === profile.belt_id) ?? null)
      : null;
  const beltName =
    belt != null ? beltLabel(belt, (key) => (i18n.exists(key) ? t(key) : null)) : null;
  const modalityKey = modalityLabelKey(registration.modality);

  return (
    <Stack gap="md">
      <F3ErrorAlert failure={failure} />

      <Card withBorder radius="md" padding="md">
        <Title order={5}>{t('classification_competitor_title')}</Title>
        <Stack gap={4} mt="xs">
          <Group gap="xs">
            <Text size="xs" c="dimmed" w={130}>
              {t('registrations_competitor_column')}
            </Text>
            <Text size="sm">{registration.competitor_name_snapshot}</Text>
          </Group>
          <Group gap="xs">
            <Text size="xs" c="dimmed" w={130}>
              {t('f3_profile_belt_label')}
            </Text>
            <Text size="sm">{beltName ?? t('classification_declared_none')}</Text>
          </Group>
          <Group gap="xs">
            <Text size="xs" c="dimmed" w={130}>
              {t('f3_sex_label')}
            </Text>
            <Text size="sm">
              {profile != null && profile.sex != null
                ? t(sexLabelKey(profile.sex))
                : t('classification_declared_none')}
            </Text>
          </Group>
          <Group gap="xs">
            <Text size="xs" c="dimmed" w={130}>
              {t('f3_profile_club_label')}
            </Text>
            <Text size="sm">
              {registration.sports_club_name_snapshot ?? t('classification_declared_none')}
            </Text>
          </Group>
        </Stack>
      </Card>

      <Card withBorder radius="md" padding="md">
        <Title order={5}>{t('classification_declared_title')}</Title>
        <Stack gap={4} mt="xs">
          <Group gap="xs">
            <Text size="xs" c="dimmed" w={130}>
              {t('f3_registration_weight_label')}
            </Text>
            <Text size="sm">
              {registration.declared_weight_kg != null
                ? `${registration.declared_weight_kg} kg`
                : t('classification_declared_none')}
            </Text>
          </Group>
          <Group gap="xs">
            <Text size="xs" c="dimmed" w={130}>
              {t('f3_registration_modality_label')}
            </Text>
            <Text size="sm">
              {modalityKey != null ? t(modalityKey) : t('classification_declared_none')}
            </Text>
          </Group>
        </Stack>
      </Card>

      <Card withBorder radius="md" padding="md">
        <Title order={5}>{t('classification_title')}</Title>

        {classification == null ? (
          <Text size="sm" c="dimmed" mt="xs">
            {t('classification_loading')}
          </Text>
        ) : (
          <ClassificationBody classification={classification} />
        )}
      </Card>
    </Stack>
  );
}

/**
 * Cuerpo de la clasificación: los tres estados que puede devolver el motor (`unclassifiable`,
 * `single`, `multiple`) más los avisos. Los códigos crudos jamás se pintan: se traducen siempre con
 * las claves humanas (§12) y un aviso nunca se pinta como error bloqueante (§13).
 */
function ClassificationBody({ classification }: { classification: RegistrationClassification }) {
  const { t } = useTranslation();
  const [choice, setChoice] = useState<string | null>(null);
  const state = classificationState(classification);
  const blockerKeys = classificationBlockerKeys(classification.blockers);
  const warningKeys = classificationWarningKeys(classification.warnings);
  // Con una única compatible se preselecciona; con varias, nada marcado por defecto (§14).
  const selected = choice ?? preselectedCategoryKey(classification);

  return (
    <Stack gap="sm" mt="xs">
      {state.kind === 'unclassifiable' ? (
        <Alert icon={<IconInfoCircle size={20} />} color="orange" variant="light">
          <Text fw={600}>{t('classification_unclassifiable_title')}</Text>
          <Text size="sm" mt={4}>
            {t('classification_unclassifiable_description')}
          </Text>
          <List size="sm" spacing={2} mt={4}>
            {blockerKeys.map((key, index) => (
              <List.Item key={`${key}-${index}`}>{t(key)}</List.Item>
            ))}
          </List>
        </Alert>
      ) : null}

      {state.kind === 'single' ? (
        <Group gap="xs">
          <Badge color="green" variant="light">
            {t('classification_recommended_badge')}
          </Badge>
          <Text size="sm" fw={600}>
            {categoryDisplayName(state.category.category_key, state.category.category_label)}
          </Text>
        </Group>
      ) : null}

      {state.kind === 'multiple' ? (
        <Stack gap={4}>
          <Text size="sm" fw={600}>
            {t('classification_compatible_title')}
          </Text>
          <Radio.Group value={selected} onChange={setChoice}>
            <Stack gap={4}>
              {state.categories.map((category) => (
                <Radio
                  key={category.category_key}
                  value={category.category_key}
                  label={categoryDisplayName(category.category_key, category.category_label)}
                />
              ))}
            </Stack>
          </Radio.Group>
        </Stack>
      ) : null}

      {warningKeys.length > 0 ? (
        <>
          <Divider />
          <Stack gap={2}>
            <Text size="sm" fw={600}>
              {t('classification_warnings_title')}
            </Text>
            <List size="sm" spacing={2}>
              {warningKeys.map((key, index) => (
                <List.Item key={`${key}-${index}`}>{t(key)}</List.Item>
              ))}
            </List>
          </Stack>
        </>
      ) : null}

      <Divider />
      <Stack gap={4}>
        <Text size="sm" fw={600}>
          {t('classification_actions_title')}
        </Text>
        <Group gap="sm">
          <Button disabled title={t('classification_actions_pending')}>
            {t('classification_assign_button')}
          </Button>
          <Text size="xs" c="dimmed">
            {t('classification_actions_pending')}
          </Text>
        </Group>
      </Stack>
    </Stack>
  );
}
