import {
  Alert,
  Badge,
  Button,
  Code,
  Divider,
  Group,
  Modal,
  Select,
  Stack,
  Text,
  TextInput,
} from '@mantine/core';
import { useForm } from '@mantine/form';
import { IconAlertTriangle, IconInfoCircle } from '@tabler/icons-react';
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import F3ErrorAlert from '@components/f3/f3_error_alert';
import {
  classificationState,
  classificationWarningKeys,
  parseDeclaredWeightKg,
  preselectedCategoryKey,
  weightInputError,
} from '@components/utils/f3_classification';
import {
  MODALITIES,
  Modality,
  RegistrationClassification,
  RegistrationClassificationPreviewBody,
} from '@components/utils/f3_competitor_contract';
import {
  CATEGORY_LABEL_MAX_LENGTH,
  RegistrationCreateBody,
  categoryKeyFromLabel,
  isValidCategoryKey,
} from '@components/utils/f3_contract';
import { F3Failure } from '@components/utils/f3_errors';
import {
  CompetitorSummaryContext,
  CompetitorSummarySource,
  competitorSummaryRows,
} from '@components/utils/f3_registration_options';

export interface SelectOption {
  value: string;
  label: string;
}

/**
 * Resultado de la clasificación previa, en la forma que ya devuelve el servicio
 * (`services/f3.ts`): o el veredicto del motor, o el fallo tipado que pinta `F3ErrorAlert`.
 */
export type PreviewOutcome =
  | { ok: true; data: RegistrationClassification }
  | { ok: false; failure: F3Failure };

/** Estado de la llamada de previsualización; `idle` mientras no hay competidor elegido. */
type PreviewState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'ready'; classification: RegistrationClassification }
  | { kind: 'failed'; failure: F3Failure };

/**
 * Espera antes de pedir la clasificación previa: escribir un peso son varias pulsaciones y no
 * interesa una consulta por tecla (§4).
 */
const PREVIEW_DEBOUNCE_MS = 300;

/**
 * Alta de una inscripción (S4.1 §5 + S4.8a-4 §8/§9 + S4.8d §4/§6, flujo §1-§9).
 *
 * El flujo del organizador es:
 *
 * 1. elegir **competidor** (sólo activos: el backend rechaza inscribir a uno dado de baja);
 * 2. ver su **resumen**: nombre, fecha de nacimiento, edad derivada, sexo, cinturón y afiliación
 *    actual (academia o «Independiente») — todo de la lista del club, sin inventar nada;
 * 3. declarar **peso** (kg, admite decimales, §8) y **modalidad** (Gi / No-Gi, §9);
 * 4. pedir la **clasificación previa** al backend (`classification-preview`, solo lectura);
 * 5. ver **compatibles**, **recomendada**, **bloqueos** y **avisos** del motor;
 * 6. con **una** compatible, se preselecciona; con **varias**, elige el organizador; con **ninguna**,
 *    se muestra `NO_COMPATIBLE_CATEGORY` y no se deja confirmar;
 * 7. confirmar la inscripción.
 *
 * La compatibilidad **no** se decide aquí: el motor del backend es la autoridad (§15) y esta pantalla
 * sólo consume su veredicto. Por eso no hay ninguna regla de categorías en el cliente.
 *
 * La **representación** (club o independiente) no se elige aquí todavía: pertenece a la inscripción y
 * el alta la crea como independiente mientras esa elección no exista en el flujo del organizador.
 *
 * La categoría tiene dos caminos, y el orden importa:
 *
 * 1. **catálogo real del torneo** (`categoryOptions`, de los `stage_items`), filtrado por la
 *    clasificación previa cuando ésta responde: el valor que viaja es la clave contractual
 *    `si:<stage_item_id>` — la misma que compara el motor (`bracket/logic/classification/rules.py`).
 * 2. **etiqueta libre** (sólo si el torneo no tiene ninguna categoría declarada): el nombre que
 *    escribe el organizador se convierte en clave (`categoryKeyFromLabel`). En ese caso la previa no
 *    puede bloquear nada: sin categorías que comparar, el motor no tiene veredicto útil.
 *
 * Ni el peso ni la modalidad son obligatorios: el backend no los exige para inscribir (§18) y el
 * motor decide después qué falta (`MISSING_WEIGHT`, `MISSING_MODALITY`).
 */
export default function RegistrationModal({
  opened,
  onClose,
  onSubmit,
  competitorOptions,
  competitorDetails = [],
  summaryContext = null,
  loadPreview = null,
  inflight = false,
  error = null,
  categoryOptions = [],
}: {
  opened: boolean;
  onClose: () => void;
  onSubmit: (body: RegistrationCreateBody) => void;
  competitorOptions: SelectOption[];
  /** Filas del listado del club para el resumen del competidor elegido (§4.2). */
  competitorDetails?: CompetitorSummarySource[];
  /** Traductores con los que se compone el resumen (cinturón, edad, sexo, afiliación). */
  summaryContext?: CompetitorSummaryContext | null;
  /** Clasificación previa al alta; sin ella la modal no consulta nada. */
  loadPreview?: ((body: RegistrationClassificationPreviewBody) => Promise<PreviewOutcome>) | null;
  inflight?: boolean;
  error?: F3Failure | null;
  categoryOptions?: SelectOption[];
}) {
  const { t } = useTranslation();
  const hasCategoryCatalog = categoryOptions.length > 0;

  const [preview, setPreview] = useState<PreviewState>({ kind: 'idle' });

  const form = useForm({
    initialValues: {
      competitor_id: '',
      category_key: '',
      category_label: '',
      declared_weight_kg: '',
      modality: '',
    },
    validate: {
      competitor_id: (value) => (value !== '' ? null : t('f3_registration_competitor_required')),
      category_key: (value) =>
        hasCategoryCatalog && (value ?? '') === '' ? t('f3_registration_category_required') : null,
      category_label: (value) => {
        // Con catálogo, la categoría se elige de la lista: la etiqueta libre no participa.
        if (hasCategoryCatalog) {
          return null;
        }
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

  const competitorId = form.values.competitor_id;
  const weightInput = form.values.declared_weight_kg;
  const modalityInput = form.values.modality;

  // Clasificación previa (§4-§5): sólo cuando hay competidor y la página inyecta el lector.
  useEffect(() => {
    if (loadPreview == null || competitorId === '') {
      setPreview({ kind: 'idle' });
      return;
    }

    let cancelled = false;
    setPreview({ kind: 'loading' });

    const timer = setTimeout(() => {
      void loadPreview({
        competitor_id: Number(competitorId),
        declared_weight_kg: parseDeclaredWeightKg(weightInput),
        modality: modalityInput === '' ? null : (modalityInput as Modality),
      }).then((outcome) => {
        if (cancelled) {
          return;
        }
        if (!outcome.ok) {
          setPreview({ kind: 'failed', failure: outcome.failure });
          return;
        }
        setPreview({ kind: 'ready', classification: outcome.data });
        // §6-§7: una sola compatible se preselecciona; con varias no se elige por el organizador.
        form.setFieldValue('category_key', preselectedCategoryKey(outcome.data) ?? '');
      });
    }, PREVIEW_DEBOUNCE_MS);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [loadPreview, competitorId, weightInput, modalityInput, form]);

  const classification = preview.kind === 'ready' ? preview.classification : null;
  const state = useMemo(
    () => (classification == null ? null : classificationState(classification)),
    [classification]
  );

  /** Categorías que el motor admite, con su clave contractual. */
  const compatibleOptions = useMemo<SelectOption[]>(() => {
    if (classification == null) {
      return [];
    }
    return classification.compatible_categories.map((verdict) => ({
      value: verdict.category_key,
      label: verdict.category_label ?? verdict.category_key,
    }));
  }, [classification]);

  const recommendedKey = classification?.recommended_category?.category_key ?? null;
  const warningKeys = useMemo(
    () => (classification == null ? [] : classificationWarningKeys(classification.warnings)),
    [classification]
  );

  // §8: sin ninguna compatible no se confirma. Sólo puede bloquear cuando el torneo tiene catálogo:
  // con etiqueta libre el motor no tiene categorías declaradas que comparar.
  const previewBlocks = hasCategoryCatalog && state?.kind === 'unclassifiable';
  const selectableCategoryOptions = previewBlocks
    ? []
    : compatibleOptions.length > 0
      ? compatibleOptions
      : categoryOptions;

  const selectedCompetitor = useMemo(
    () => competitorDetails.find((competitor) => String(competitor.id) === competitorId) ?? null,
    [competitorDetails, competitorId]
  );

  const summaryRows = useMemo(
    () =>
      selectedCompetitor == null || summaryContext == null
        ? []
        : competitorSummaryRows(selectedCompetitor, summaryContext),
    [selectedCompetitor, summaryContext]
  );

  const derivedKey = useMemo(
    () => categoryKeyFromLabel(form.values.category_label),
    [form.values.category_label]
  );

  const catalogLabel = useMemo(() => {
    const selected = selectableCategoryOptions.find(
      (option) => option.value === form.values.category_key
    );
    return selected != null ? selected.label : '';
  }, [selectableCategoryOptions, form.values.category_key]);

  return (
    <Modal opened={opened} onClose={onClose} title={t('registrations_create_title')}>
      <form
        onSubmit={form.onSubmit((values) => {
          const chosen = selectableCategoryOptions.find(
            (option) => option.value === values.category_key
          );
          onSubmit({
            competitor_id: Number(values.competitor_id),
            representation: 'INDEPENDENT',
            category_key: hasCategoryCatalog
              ? values.category_key
              : categoryKeyFromLabel(values.category_label),
            category_label: (hasCategoryCatalog
              ? (chosen?.label ?? '')
              : values.category_label
            ).trim(),
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

          {selectedCompetitor != null && summaryRows.length > 0 ? (
            <Stack gap={2}>
              <Text size="xs" fw={600}>
                {t('classification_competitor_title')}
              </Text>
              <Text size="sm">{selectedCompetitor.display_name}</Text>
              {summaryRows.map((row) => (
                <Text key={row.labelKey} size="xs" c="dimmed">
                  {t(row.labelKey)}: {row.value}
                </Text>
              ))}
              <Divider my={4} />
            </Stack>
          ) : null}

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

          {preview.kind !== 'idle' ? (
            <Stack gap={2}>
              <Text size="xs" fw={600}>
                {t('f3_registration_preview_title')}
              </Text>
              <Text size="xs" c="dimmed">
                {t('f3_registration_preview_hint')}
              </Text>
            </Stack>
          ) : null}

          {preview.kind === 'loading' ? (
            <Text size="xs" c="dimmed">
              {t('classification_loading')}
            </Text>
          ) : null}

          {preview.kind === 'failed' ? <F3ErrorAlert failure={preview.failure} /> : null}

          {classification != null ? (
            <Stack gap={4}>
              <Divider my={4} />
              <Text size="xs" fw={600}>
                {t('classification_compatible_title')}
              </Text>
              {state?.kind === 'unclassifiable' ? (
                <Alert
                  icon={<IconAlertTriangle size={20} />}
                  color="red"
                  variant="light"
                  title={t('classification_blocker_no_compatible_category')}
                >
                  {state.blockerKeys.map((blockerKey) => (
                    <Text key={blockerKey} size="xs">
                      {t(blockerKey)}
                    </Text>
                  ))}
                </Alert>
              ) : (
                <Stack gap={2}>
                  {compatibleOptions.map((option) => (
                    <Group key={option.value} gap="xs">
                      <Text size="xs">{option.label}</Text>
                      <Code>{option.value}</Code>
                      {recommendedKey === option.value ? (
                        <Badge size="xs" variant="light">
                          {t('classification_recommended_badge')}
                        </Badge>
                      ) : null}
                    </Group>
                  ))}
                </Stack>
              )}

              {warningKeys.length > 0 ? (
                <>
                  <Text size="xs" fw={600}>
                    {t('classification_warnings_title')}
                  </Text>
                  {warningKeys.map((warningKey) => (
                    <Text key={warningKey} size="xs" c="dimmed">
                      {t(warningKey)}
                    </Text>
                  ))}
                </>
              ) : null}
            </Stack>
          ) : null}

          {hasCategoryCatalog ? (
            <>
              <Select
                withAsterisk
                searchable
                label={t('registrations_category_column')}
                description={t('f3_registration_category_description')}
                data={selectableCategoryOptions}
                nothingFoundMessage={t('f3_registration_category_invalid')}
                disabled={previewBlocks}
                {...form.getInputProps('category_key')}
              />
              <Text size="xs" c="dimmed">
                {t('f3_registration_category_catalog_note')}
              </Text>
              {form.values.category_key !== '' ? (
                <Text size="xs" c="dimmed">
                  {t('f3_registration_category_key_label')} <Code>{form.values.category_key}</Code>{' '}
                  {catalogLabel !== '' ? `· ${catalogLabel}` : ''}
                </Text>
              ) : null}
            </>
          ) : (
            <>
              <TextInput
                withAsterisk
                label={t('registrations_category_column')}
                description={t('f3_registration_category_description')}
                maxLength={CATEGORY_LABEL_MAX_LENGTH}
                {...form.getInputProps('category_label')}
              />
              {derivedKey !== '' ? (
                <Text size="xs" c="dimmed">
                  {t('f3_registration_category_key_label')} <Code>{derivedKey}</Code>
                </Text>
              ) : null}
            </>
          )}

          <Alert icon={<IconInfoCircle size={20} />} color="gray" variant="light">
            <Text size="xs">{t('f3_registration_representation_notice')}</Text>
          </Alert>

          <F3ErrorAlert failure={error} />
          <Button type="submit" loading={inflight} disabled={inflight || previewBlocks} fullWidth>
            {t('registrations_create_button')}
          </Button>
        </Stack>
      </form>
    </Modal>
  );
}
