import { Alert, Button, Code, Modal, Select, Stack, Text, TextInput } from '@mantine/core';
import { useForm } from '@mantine/form';
import { IconInfoCircle } from '@tabler/icons-react';
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';

import F3ErrorAlert from '@components/f3/f3_error_alert';
import { parseDeclaredWeightKg, weightInputError } from '@components/utils/f3_classification';
import { MODALITIES, Modality } from '@components/utils/f3_competitor_contract';
import {
  CATEGORY_LABEL_MAX_LENGTH,
  RegistrationCreateBody,
  categoryKeyFromLabel,
  isValidCategoryKey,
} from '@components/utils/f3_contract';
import { F3Failure } from '@components/utils/f3_errors';

export interface SelectOption {
  value: string;
  label: string;
}

/**
 * Alta de una inscripción (S4.1 §5 + S4.8a-4 §8/§9).
 *
 * El organizador elige competidor y categoría, y declara el **peso** (kg, admite decimales, §8) y la
 * **modalidad** (Gi / No-Gi, §9). La clave interna de la categoría se deriva de la etiqueta y se
 * muestra en secundario: es un detalle técnico que no se le debe pedir, pero tampoco se le oculta.
 *
 * Ni el peso ni la modalidad son obligatorios aquí: el backend no los exige para inscribir (§18) y
 * el motor de clasificación decide después qué falta (`MISSING_WEIGHT`, `MISSING_MODALITY`). Por eso
 * la modalidad no fuerza un valor inventado y el peso vacío no bloquea el envío.
 *
 * Solo se ofrecen competidores activos: el backend rechaza inscribir a un competidor dado de baja
 * (`CompetitorNotSelectableError`), así que la interfaz no ofrece esa puerta.
 *
 * La participación con academia representada (`representation: CLUB`) **no** se ofrece: exige una
 * academia elegible y no hay endpoint que las liste por torneo, así que pedirla obligaría al
 * organizador a teclear un identificador interno. El hueco queda declarado en el informe de la fase.
 */
export default function RegistrationModal({
  opened,
  onClose,
  onSubmit,
  competitorOptions,
  inflight = false,
  error = null,
  categoryOptions = [],
}: {
  opened: boolean;
  onClose: () => void;
  onSubmit: (body: RegistrationCreateBody) => void;
  competitorOptions: SelectOption[];
  inflight?: boolean;
  error?: F3Failure | null;
  categoryOptions?: SelectOption[];
}) {
  const { t } = useTranslation();
  const form = useForm({
    initialValues: {
      competitor_id: '',
      category_label: '',
      declared_weight_kg: '',
      modality: '',
    },
    validate: {
      competitor_id: (value) => (value !== '' ? null : t('f3_registration_competitor_required')),
      category_label: (value) => {
        const key = categoryKeyFromLabel(value);
        if (value.trim() === '') {
          return t('f3_registration_category_required');
        }
        if (value.trim().length > CATEGORY_LABEL_MAX_LENGTH) {
          return t('f3_registration_category_too_long');
        }
        return isValidCategoryKey(key) ? null : t('f3_registration_category_invalid');
      },
      declared_weight_kg: (value) => {
        const errorKey = weightInputError(value);
        return errorKey == null ? null : t(errorKey);
      },
    },
  });

  const derivedKey = useMemo(
    () => categoryKeyFromLabel(form.values.category_label),
    [form.values.category_label]
  );

  return (
    <Modal opened={opened} onClose={onClose} title={t('registrations_create_title')}>
      <form
        onSubmit={form.onSubmit((values) => {
          onSubmit({
            competitor_id: Number(values.competitor_id),
            representation: 'INDEPENDENT',
            category_key: categoryKeyFromLabel(values.category_label),
            category_label: values.category_label.trim(),
            declared_weight_kg: parseDeclaredWeightKg(values.declared_weight_kg),
            modality: values.modality === '' ? null : (values.modality as Modality),
            reason_code: 'PLANNED_ENTRY',
          });
        })}
      >
        <Stack gap="sm">
          <Select
            withAsterisk
            searchable
            label={t('registrations_competitor_column')}
            data={competitorOptions}
            nothingFoundMessage={t('f3_registration_no_competitors')}
            {...form.getInputProps('competitor_id')}
          />

          <TextInput
            withAsterisk
            label={t('registrations_category_column')}
            description={t('f3_registration_category_description')}
            maxLength={CATEGORY_LABEL_MAX_LENGTH}
            {...form.getInputProps('category_label')}
          />

          <TextInput
            label={t('f3_registration_weight_label')}
            description={t('f3_registration_weight_description')}
            placeholder="kg"
            inputMode="decimal"
            {...form.getInputProps('declared_weight_kg')}
          />

          <Select
            clearable
            label={t('f3_registration_modality_label')}
            description={t('f3_registration_modality_description')}
            data={MODALITIES.map((modality) => ({
              value: modality,
              label: t(`f3_registration_modality_${modality.toLowerCase()}`),
            }))}
            {...form.getInputProps('modality')}
          />

          {derivedKey !== '' ? (
            <Text size="xs" c="dimmed">
              {t('f3_registration_category_key_label')} <Code>{derivedKey}</Code>
            </Text>
          ) : null}

          {categoryOptions.length > 0 ? (
            <Select
              clearable
              label={t('f3_registration_category_existing_label')}
              data={categoryOptions}
              onChange={(value) => {
                if (value != null) {
                  form.setFieldValue('category_label', value);
                }
              }}
            />
          ) : null}

          <Alert icon={<IconInfoCircle size={20} />} color="gray" variant="light">
            <Text size="xs">{t('f3_registration_representation_notice')}</Text>
          </Alert>

          <F3ErrorAlert failure={error} />
          <Button type="submit" loading={inflight} disabled={inflight} fullWidth>
            {t('registrations_create_button')}
          </Button>
        </Stack>
      </form>
    </Modal>
  );
}
