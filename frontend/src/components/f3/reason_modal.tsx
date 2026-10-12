import { Button, Modal, Select, Stack, Text, Textarea } from '@mantine/core';
import { useForm } from '@mantine/form';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import F3ErrorAlert from '@components/f3/f3_error_alert';
import { F3Failure } from '@components/utils/f3_errors';

/**
 * Confirmación de una operación de ciclo de vida con motivo del catálogo.
 *
 * El backend exige un motivo cerrado para retirar, readmitir y descalificar (y lo acepta para
 * activar/desactivar un competidor), así que la UI no lo improvisa: ofrece exactamente los códigos
 * admitidos y traduce cada uno a lenguaje humano. La nota es opcional y tiene el máximo del catálogo.
 */
export default function ReasonModal({
  opened,
  onClose,
  title,
  description,
  confirmLabel,
  reasonCodes,
  color = 'red',
  error,
  inflight = false,
  onSubmit,
}: {
  opened: boolean;
  onClose: () => void;
  title: string;
  description: string;
  confirmLabel: string;
  reasonCodes: string[];
  color?: string;
  error?: F3Failure | null;
  inflight?: boolean;
  onSubmit: (reasonCode: string, reasonNote: string | null) => void;
}) {
  const { t } = useTranslation();
  const form = useForm({
    initialValues: {
      reason_code: reasonCodes[0] ?? 'ADMINISTRATIVE',
      reason_note: '',
    },
    validate: {
      reason_code: (value) => (value.length > 0 ? null : t('f3_reason_required_validation')),
      reason_note: (value) =>
        value.length <= 200 ? null : t('f3_reason_note_too_long_validation'),
    },
  });

  return (
    <Modal opened={opened} onClose={onClose} title={title}>
      <form
        onSubmit={form.onSubmit((values) => {
          onSubmit(
            values.reason_code,
            values.reason_note.trim() === '' ? null : values.reason_note
          );
        })}
      >
        <Stack gap="sm">
          <Text size="sm">{description}</Text>

          <Select
            withAsterisk
            label={t('f3_reason_code_label')}
            data={reasonCodes.map((code) => ({
              value: code,
              label: t(`f3_reason_${code.toLowerCase()}`),
            }))}
            allowDeselect={false}
            {...form.getInputProps('reason_code')}
          />

          <Textarea
            label={t('f3_reason_note_label')}
            description={t('f3_reason_note_description')}
            autosize
            minRows={2}
            maxLength={200}
            {...form.getInputProps('reason_note')}
          />

          <F3ErrorAlert failure={error ?? null} />

          <Button type="submit" color={color} loading={inflight} disabled={inflight} fullWidth>
            {confirmLabel}
          </Button>
        </Stack>
      </form>
    </Modal>
  );
}

/** Estado del modal de motivo: el componente que lo usa solo tiene que abrirlo y cerrarlo. */
export function useReasonModalState(initiallyOpen = false) {
  const [opened, setOpened] = useState(initiallyOpen);
  return {
    opened,
    open: () => setOpened(true),
    close: () => setOpened(false),
  };
}
