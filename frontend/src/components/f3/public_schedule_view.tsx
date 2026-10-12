import { Badge, Card, Group, SimpleGrid, Stack, Text, Title } from '@mantine/core';
import { useTranslation } from 'react-i18next';

import { Time, formatTime } from '@components/utils/datetime';
import {
  PublicScheduleCourt,
  PublicSchedulePayload,
  isPublicScheduleState,
  publicStateKey,
} from '@components/utils/f3_contract';
import {
  DisplayRow,
  courtsToShow,
  isEmptySchedule,
  isLateCourt,
  resultsFor,
  rowsFor,
} from '@components/utils/public_schedule_display';

const STATE_COLORS: Record<string, string> = {
  NOW: 'green',
  NEXT: 'blue',
  UPCOMING: 'gray',
  WAITING: 'yellow',
  COMPLETED: 'gray',
  DELAYED: 'orange',
};

/**
 * Estado visible de un combate. El estado lo decide el servidor: aquí solo se le pone color y
 * etiqueta. Un estado que esta pantalla no conozca se enseña como código, nunca disfrazado de
 * uno conocido.
 */
export function PublicStateBadge({ state }: { state: string }) {
  const { t } = useTranslation();
  const known = isPublicScheduleState(state);
  return (
    <Badge
      color={STATE_COLORS[state] ?? 'gray'}
      variant={known ? 'filled' : 'outline'}
      size="lg"
      radius="sm"
      title={state}
    >
      {known ? t(publicStateKey(state)) : state}
    </Badge>
  );
}

function Row({ row, main }: { row: DisplayRow; main: boolean }) {
  const { t } = useTranslation();
  return (
    <Group justify="space-between" align="center" wrap="nowrap">
      <Stack gap={0}>
        <Text size={main ? 'xl' : 'sm'} fw={main ? 700 : 500}>
          {row.title}
        </Text>
        <Text size={main ? 'lg' : 'xs'} c={row.fighters === '' ? 'dimmed' : undefined}>
          {row.fighters !== ''
            ? row.fighters
            : t('public_schedule_waiting_for', { names: row.waitingFor.join(', ') })}
        </Text>
      </Stack>
      <Group gap="sm" align="center" wrap="nowrap">
        {row.estimatedStart != null ? (
          <Text size={main ? 'lg' : 'sm'} fw={main ? 600 : 400}>
            <Time datetime={row.estimatedStart} />
          </Text>
        ) : null}
        <PublicStateBadge state={row.state} />
      </Group>
    </Group>
  );
}

/** Bloque de un tatami: lo que está pasando, lo que viene y por dónde va la cola. */
function CourtCard({ court }: { court: PublicScheduleCourt }) {
  const { t } = useTranslation();
  const rows = rowsFor(court);
  const results = resultsFor(court);

  return (
    <Card withBorder radius="md" padding="md" style={{ height: '100%' }}>
      <Stack gap="sm">
        <Group justify="space-between" align="center" wrap="nowrap">
          <Title order={2}>{court.court_name}</Title>
          {isLateCourt(court) ? (
            <Badge color="orange" variant="light" size="lg" radius="sm">
              {t('public_schedule_lag', { minutes: court.lag_minutes })}
            </Badge>
          ) : null}
        </Group>

        {rows.length === 0 ? (
          <Text size="lg" c="dimmed">
            {t('public_schedule_empty')}
          </Text>
        ) : (
          rows.map((row, index) => <Row key={row.matchId} row={row} main={index === 0} />)
        )}

        {results.length > 0 ? (
          <Stack gap={2}>
            <Text size="xs" c="dimmed">
              {t('public_schedule_completed_title')}
            </Text>
            {results.map((row) => (
              <Row key={row.matchId} row={row} main={false} />
            ))}
          </Stack>
        ) : null}
      </Stack>
    </Card>
  );
}

/**
 * Pantalla estática para TV y tablet (S4.5).
 *
 * Es un componente tonto: recibe el cuerpo público tal cual lo sirve
 * `GET /public/tournaments/{tid}/schedule` y lo pinta. Ni calcula estados ni estima horas; el
 * sondeo lo lleva la página que lo monta.
 */
export default function PublicScheduleView({ payload }: { payload: PublicSchedulePayload }) {
  const { t } = useTranslation();

  return (
    <Stack gap="md">
      <Group justify="space-between" align="center" wrap="nowrap">
        <Title order={1}>{payload.tournament.name}</Title>
        <Text size="sm" c="dimmed">
          {t('public_schedule_updated', { time: formatTime(payload.generated_at) })}
        </Text>
      </Group>

      {isEmptySchedule(payload) ? (
        <Text size="lg" c="dimmed">
          {t('public_schedule_nothing')}
        </Text>
      ) : null}

      <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }} spacing="md">
        {courtsToShow(payload).map((court) => (
          <CourtCard key={court.court_id} court={court} />
        ))}
      </SimpleGrid>
    </Stack>
  );
}
