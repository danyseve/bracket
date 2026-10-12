import { Button, Modal, Stack, TextInput } from '@mantine/core';
import { useForm } from '@mantine/form';
import { useTranslation } from 'react-i18next';

import F3ErrorAlert from '@components/f3/f3_error_alert';
import { F3Failure } from '@components/utils/f3_errors';

/**
 * Alta y renombrado de un competidor.
 *
 * El nombre visible es el único dato que se le pide al organizador: el identificador interno no se
 * pide, no se muestra y no se usa como dato principal en ninguna pantalla.
 */
export default function CompetitorModal({
  opened,
  onClose,
  onSubmit,
  title,
  description,
  confirmLabel,
  initialName = '',
  inflight = false,
  error = null,
}: {
  opened: boolean;
  onClose: () => void;
  onSubmit: (displayName: string) => void;
  title: string;
  description: string;
  confirmLabel: string;
  initialName?: string;
  inflight?: boolean;
  error?: F3Failure | null;
}) {
  const { t } = useTranslation();
  const form = useForm({
    initialValues: { display_name: initialName },
    validate: {
      display_name: (value) => {
        const trimmed = value.trim();
        if (trimmed === '') {
          return t('f3_competitor_name_required');
        }
        return trimmed.length <= 200 ? null : t('f3_competitor_name_too_long');
      },
    },
  });

  return (
    <Modal opened={opened} onClose={onClose} title={title}>
      <form
        onSubmit={form.onSubmit((values) => {
          onSubmit(values.display_name.trim());
        })}
      >
        <Stack gap="sm">
          <TextInput
            withAsterisk
            label={t('f3_competitor_name_label')}
            description={description}
            maxLength={200}
            data-autofocus
            {...form.getInputProps('display_name')}
          />
          <F3ErrorAlert failure={error} />
          <Button type="submit" loading={inflight} disabled={inflight} fullWidth>
            {confirmLabel}
          </Button>
        </Stack>
      </form>
    </Modal>
  );
}
