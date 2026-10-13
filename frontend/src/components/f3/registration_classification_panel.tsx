import {
  Alert,
  Badge,
  Button,
  Card,
  Divider,
  Group,
  List,
  Radio,
  Select,
  Stack,
  Text,
  Title,
} from '@mantine/core';
import { showNotification } from '@mantine/notifications';
import { IconInfoCircle } from '@tabler/icons-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import F3ErrorAlert from '@components/f3/f3_error_alert';
import OrganizerOnly from '@components/f3/organizer_only';
import ReasonModal from '@components/f3/reason_modal';
import {
  CATEGORY_ASSIGNMENT_REASON_CODES,
  CategoryAssignmentBody,
  buildCategoryAssignment,
  compatibleCategoryKeys,
  isCompatibleChoice,
} from '@components/utils/f3_category_assignment';
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
import { actionScope } from '@components/utils/f3_write';
import useF3Write from '@components/utils/f3_write_hook';
import {
  assignRegistrationCategory,
  getBelts,
  getCompetitorSportsProfile,
  getRegistration,
  getRegistrationClassification,
} from '@services/f3';

/** Opción de categoría del torneo para el override (clave real + etiqueta visible). */
export interface CategoryOption {
  value: string;
  label: string;
}

/**
 * Clasificación y asignación de categoría mostradas al organizador (S4.8a-4 §10-§16, §18, §22).
 *
 * Reúne las tres piezas que exige la revisión de una inscripción: la **ficha del competidor**
 * (nombre, cinturón, sexo, club), los **datos declarados** de la inscripción (peso, modalidad) y la
 * **clasificación** que devuelve el motor del backend, más la **asignación** por el endpoint real.
 *
 * El motor es la única autoridad: este panel **no** reproduce reglas de categoría. Solo traduce los
 * códigos cerrados a texto humano con `classificationBlockerKey`/`classificationWarningKey`
 * (§12/§13) y proyecta el veredicto a tres estados. Una única compatible se preselecciona; con
 * varias compatibles no se elige la primera (§14). La asignación normal (§15) usa las categorías
 * compatibles del motor; el **override** (§16) es un flujo explícito reservado al organizador que
 * exige confirmación y motivo, y nunca se dispara solo: el endpoint no se llama sin una acción del
 * usuario.
 */
export default function RegistrationClassificationPanel({
  tournament_id,
  registration_id,
  club_id,
  overrideCategoryOptions = [],
}: {
  tournament_id: number;
  registration_id: number;
  club_id: number | null;
  /** Categorías reales del torneo, para ofrecer el override (§16). Sin ellas, no se ofrece. */
  overrideCategoryOptions?: CategoryOption[];
}) {
  const { t, i18n } = useTranslation();
  const { run, inflight } = useF3Write();
  const [failure, setFailure] = useState<F3Failure | null>(null);

  const swrRegistration = getRegistration(tournament_id, registration_id);
  const swrClassification = getRegistrationClassification(tournament_id, registration_id);
  const competitor_id = swrRegistration.data != null ? swrRegistration.data.competitor_id : null;
  const swrProfile = getCompetitorSportsProfile(club_id, competitor_id ?? -1);
  const swrBelts = getBelts(club_id);

  const registration = swrRegistration.data;
  const classification = swrClassification.data;
  const swrFailure: F3Failure | null =
    swrRegistration.error != null
      ? describeF3Failure(swrRegistration.error)
      : swrClassification.error != null
        ? describeF3Failure(swrClassification.error)
        : null;

  /**
   * Envía la asignación por el endpoint real. Solo se invoca desde un manejador de acción del
   * usuario (§22): el panel no llama al endpoint al montarse ni por efecto. Revalida la
   * clasificación y la inscripción después (§15). Devuelve `true` si la escritura fue aceptada.
   */
  async function assignCategory(body: CategoryAssignmentBody): Promise<boolean> {
    setFailure(null);
    const outcome = await run(
      actionScope([
        'registration-category',
        tournament_id,
        registration_id,
        body.category_key ?? '',
        body.override ? 'override' : 'normal',
      ]),
      (key) => assignRegistrationCategory(tournament_id, registration_id, body, key)
    );
    if (outcome.status === 'ignored') {
      return false;
    }
    if (outcome.value != null && outcome.value.ok) {
      showNotification({ color: 'green', message: t('classification_assign_success') });
      await swrClassification.mutate();
      await swrRegistration.mutate();
      return true;
    }
    setFailure(outcome.value != null && !outcome.value.ok ? outcome.value.failure : null);
    return false;
  }

  if (registration == null) {
    return (
      <Stack gap="sm">
        <F3ErrorAlert failure={swrFailure} />
        {swrFailure == null ? (
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
      <F3ErrorAlert failure={swrFailure} />

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
          <ClassificationBody
            classification={classification}
            onAssign={assignCategory}
            inflight={inflight}
            failure={failure}
            overrideCategoryOptions={overrideCategoryOptions}
          />
        )}
      </Card>
    </Stack>
  );
}

/**
 * Cuerpo de la clasificación: los tres estados que puede devolver el motor (`unclassifiable`,
 * `single`, `multiple`), los avisos y la superficie de asignación. Los códigos crudos jamás se
 * pintan: se traducen siempre con las claves humanas (§12). La asignación normal solo se ofrece
 * para una categoría **compatible**; el override (§16) va tras el desplegable del organizador y
 * exige confirmación y motivo, así que nunca se fuerza en silencio.
 */
function ClassificationBody({
  classification,
  onAssign,
  inflight,
  failure,
  overrideCategoryOptions,
}: {
  classification: RegistrationClassification;
  onAssign: (body: CategoryAssignmentBody) => Promise<boolean>;
  inflight: boolean;
  failure: F3Failure | null;
  overrideCategoryOptions: CategoryOption[];
}) {
  const { t } = useTranslation();
  const [choice, setChoice] = useState<string | null>(null);
  const [overrideChoice, setOverrideChoice] = useState<string | null>(null);
  const [overrideOpen, setOverrideOpen] = useState(false);
  const [validationKey, setValidationKey] = useState<string | null>(null);

  const state = classificationState(classification);
  const blockerKeys = classificationBlockerKeys(classification.blockers);
  const warningKeys = classificationWarningKeys(classification.warnings);
  // Con una única compatible se preselecciona; con varias, nada marcado por defecto (§14).
  const selected = choice ?? preselectedCategoryKey(classification);
  const selectedCompatible = isCompatibleChoice(classification, selected);
  // Candidatas al override: categorías reales del torneo que el motor NO da por compatibles.
  const compatible = compatibleCategoryKeys(classification);
  const overrideOptions = overrideCategoryOptions.filter(
    (option) => !compatible.includes(option.value)
  );

  async function submitNormal() {
    const built = buildCategoryAssignment({
      classification,
      category_key: selected,
      override: false,
    });
    if (!built.ok) {
      setValidationKey(built.errorKey);
      return;
    }
    setValidationKey(null);
    await onAssign(built.body);
  }

  async function submitOverride(reasonCode: string, reasonNote: string | null) {
    const built = buildCategoryAssignment({
      classification,
      category_key: overrideChoice,
      override: true,
      reason_code: reasonCode,
      reason_note: reasonNote,
    });
    if (!built.ok) {
      setValidationKey(built.errorKey);
      return;
    }
    setValidationKey(null);
    const accepted = await onAssign(built.body);
    if (accepted) {
      setOverrideOpen(false);
      setOverrideChoice(null);
      setChoice(null);
    }
  }

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

        <Group gap="sm" align="center">
          <Button
            disabled={!selectedCompatible || inflight}
            loading={inflight && selectedCompatible}
            onClick={() => {
              void submitNormal();
            }}
          >
            {t('classification_assign_button')}
          </Button>
          {selected != null && !selectedCompatible ? (
            <Text size="xs" c="dimmed">
              {t('classification_override_required')}
            </Text>
          ) : null}
        </Group>

        {validationKey != null ? (
          <Text size="xs" c="red">
            {t(validationKey)}
          </Text>
        ) : null}

        <OrganizerOnly>
          <Stack gap={6} w="100%">
            <Text size="xs" c="dimmed">
              {t('classification_override_disabled_hint')}
            </Text>
            {overrideOptions.length === 0 ? (
              <Text size="sm" c="dimmed">
                {t('classification_override_no_categories')}
              </Text>
            ) : (
              <Group gap="sm" align="flex-end" wrap="wrap">
                <Select
                  label={t('classification_override_category_label')}
                  data={overrideOptions}
                  value={overrideChoice}
                  onChange={setOverrideChoice}
                  allowDeselect
                  w={240}
                />
                <Button
                  color="orange"
                  variant="light"
                  disabled={overrideChoice == null}
                  onClick={() => setOverrideOpen(true)}
                >
                  {t('classification_override_button')}
                </Button>
              </Group>
            )}
            <Text size="xs" c="dimmed">
              {t('classification_override_notice')}
            </Text>
          </Stack>
        </OrganizerOnly>
      </Stack>

      <F3ErrorAlert failure={failure} />

      <ReasonModal
        opened={overrideOpen}
        onClose={() => setOverrideOpen(false)}
        title={t('classification_override_title')}
        description={t('classification_override_description')}
        confirmLabel={t('classification_override_confirm_label')}
        reasonCodes={CATEGORY_ASSIGNMENT_REASON_CODES}
        color="orange"
        inflight={inflight}
        onSubmit={(reasonCode: string, reasonNote: string | null) => {
          void submitOverride(reasonCode, reasonNote);
        }}
      />
    </Stack>
  );
}
