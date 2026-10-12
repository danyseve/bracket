import { Button, Card, Grid, Stack, Text, TextInput, Title } from '@mantine/core';
import { showNotification } from '@mantine/notifications';
import { IconCalendarTime, IconLock } from '@tabler/icons-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { SWRResponse } from 'swr';

import F3ErrorAlert from '@components/f3/f3_error_alert';
import OrganizerOnly from '@components/f3/organizer_only';
import { RegistrationWindowBadge } from '@components/f3/status_badge';
import { DateTime } from '@components/utils/datetime';
import { RegistrationWindow, windowAdmitsNewRegistrations } from '@components/utils/f3_contract';
import { F3Failure, describeF3Failure } from '@components/utils/f3_errors';
import { actionScope } from '@components/utils/f3_write';
import useF3Write from '@components/utils/f3_write_hook';
import { closeRegistrations, setRegistrationDeadline } from '@services/f3';

/**
 * Ventana de inscripción (S4.1 §6).
 *
 * Deja explícito en pantalla lo que el backend separa: **cerrar las inscripciones no genera los
 * cuadros**. Y solo permite al OWNER tocar la fecha límite o cerrar; el resto de usuarios ve el
 * estado y el aviso.
 */
export default function RegistrationWindowCard({
  tournament_id,
  swrWindow,
  onChanged,
}: {
  tournament_id: number;
  swrWindow: SWRResponse<RegistrationWindow>;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const { run, inflight } = useF3Write();
  const [failure, setFailure] = useState<F3Failure | null>(null);
  const [deadline, setDeadline] = useState('');

  const window = swrWindow.data;
  const admitsNewRegistrations = window != null && windowAdmitsNewRegistrations(window.state);

  async function saveDeadline() {
    if (window == null) {
      return;
    }
    const iso = deadline === '' ? null : new Date(deadline).toISOString();
    setFailure(null);
    const outcome = await run(actionScope(['window', tournament_id, iso ?? 'none']), (key) =>
      setRegistrationDeadline(tournament_id, iso, key)
    );
    if (outcome.status === 'ignored') {
      return;
    }
    if (outcome.value != null && outcome.value.ok) {
      showNotification({ color: 'green', message: t('registration_window_deadline_saved') });
      await swrWindow.mutate();
      onChanged();
      return;
    }
    setFailure(outcome.value != null && !outcome.value.ok ? outcome.value.failure : null);
  }

  async function closeWindow() {
    setFailure(null);
    const outcome = await run(actionScope(['close', tournament_id]), (key) =>
      closeRegistrations(tournament_id, key)
    );
    if (outcome.status === 'ignored') {
      return;
    }
    if (outcome.value != null && outcome.value.ok) {
      showNotification({ color: 'green', message: t('registration_window_closed_success') });
      await swrWindow.mutate();
      onChanged();
      return;
    }
    setFailure(outcome.value != null && !outcome.value.ok ? outcome.value.failure : null);
  }

  return (
    <Card withBorder radius="md" padding="lg">
      <Stack gap="sm">
        <Grid align="center">
          <Grid.Col span="auto">
            <Title order={4}>{t('registration_window_title')}</Title>
          </Grid.Col>
          <Grid.Col span="content">
            {window != null ? <RegistrationWindowBadge state={window.state} /> : null}
          </Grid.Col>
        </Grid>

        {window == null ? (
          <Text size="sm" c="dimmed">
            {t('f3_loading')}
          </Text>
        ) : (
          <Stack gap={4}>
            <Text size="sm">
              {admitsNewRegistrations
                ? t('registration_window_admits_new')
                : t('registration_window_does_not_admit_new')}
            </Text>
            <Text size="sm" c="dimmed">
              {t('registration_window_deadline_label')}:{' '}
              {window.registration_deadline != null ? (
                <DateTime datetime={window.registration_deadline} />
              ) : (
                t('registration_window_no_deadline')
              )}
            </Text>
            {window.registrations_closed_at != null ? (
              <Text size="sm" c="dimmed">
                {t('registration_window_closed_at_label')}:{' '}
                <DateTime datetime={window.registrations_closed_at} />
              </Text>
            ) : null}
          </Stack>
        )}

        <Text size="xs" c="dimmed">
          {t('registration_window_close_vs_generate_notice')}
        </Text>

        <OrganizerOnly>
          <TextInput
            type="datetime-local"
            label={t('registration_window_deadline_input_label')}
            value={deadline}
            onChange={(event) => setDeadline(event.currentTarget.value)}
            leftSection={<IconCalendarTime size={16} />}
            w={{ base: '100%', sm: '18rem' }}
          />
          <Button
            variant="light"
            color="blue"
            onClick={saveDeadline}
            loading={inflight}
            disabled={inflight || deadline === ''}
          >
            {t('registration_window_deadline_save_button')}
          </Button>
          <Button
            variant="light"
            color="red"
            leftSection={<IconLock size={16} />}
            onClick={closeWindow}
            loading={inflight}
            disabled={inflight || (window != null && window.state === 'CLOSED')}
          >
            {t('registration_window_close_button')}
          </Button>
        </OrganizerOnly>

        {swrWindow.error != null ? (
          <F3ErrorAlert failure={describeF3Failure(swrWindow.error)} />
        ) : null}
        <F3ErrorAlert failure={failure} />
      </Stack>
    </Card>
  );
}
