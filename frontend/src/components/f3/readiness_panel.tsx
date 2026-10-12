import {
  Badge,
  Button,
  Card,
  Code,
  Grid,
  Group,
  List,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Title,
} from '@mantine/core';
import { showNotification } from '@mantine/notifications';
import { IconTrophy } from '@tabler/icons-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { SWRResponse } from 'swr';

import F3ErrorAlert from '@components/f3/f3_error_alert';
import {
  BracketReadiness,
  MINIMUM_ENTRANTS,
  ReadinessIssue,
  categoryDisplayName,
  readinessBlockerKey,
  sortCategories,
} from '@components/utils/f3_contract';
import { F3Failure, describeF3Failure, isBracketNotReady } from '@components/utils/f3_errors';
import { actionScope } from '@components/utils/f3_write';
import useF3Write from '@components/utils/f3_write_hook';
import { generateTournamentBrackets } from '@services/f3';

function BlockerList({ issues }: { issues: ReadinessIssue[] }) {
  const { t } = useTranslation();
  if (issues.length === 0) {
    return (
      <Text size="sm" c="green">
        {t('readiness_no_blockers')}
      </Text>
    );
  }
  return (
    <List size="sm" spacing={2}>
      {issues.map((issue, index) => (
        <List.Item key={`${issue.code}-${issue.category_key ?? 'all'}-${index}`}>
          {t(readinessBlockerKey(issue.code), {
            count: issue.count ?? 0,
            required_minimum: issue.required_minimum ?? MINIMUM_ENTRANTS,
          })}{' '}
          <Text span size="xs" c="dimmed">
            <Code>{issue.code}</Code>
          </Text>
        </List.Item>
      ))}
    </List>
  );
}

/**
 * Preparación de los cuadros (S4.1 §7-§8).
 *
 * Consume `GET bracket-readiness` y traduce cada bloqueo a una frase. El botón de generación solo se
 * habilita cuando la revisión dice `ready`, pero **el backend sigue mandando**: si el POST responde
 * `BRACKET_NOT_READY` se muestra el rechazo, se refresca la revisión y no se reintenta solo.
 */
export default function ReadinessPanel({
  tournament_id,
  swrReadiness,
  onChanged,
}: {
  tournament_id: number;
  swrReadiness: SWRResponse<BracketReadiness>;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const { run, inflight } = useF3Write();
  const [failure, setFailure] = useState<F3Failure | null>(null);

  const readiness = swrReadiness.data;

  async function generateBrackets() {
    setFailure(null);
    const outcome = await run(actionScope(['generate-bracket', tournament_id]), (key) =>
      generateTournamentBrackets(tournament_id, key)
    );
    if (outcome.status === 'ignored') {
      return;
    }
    if (outcome.value != null && outcome.value.ok) {
      const generated = outcome.value.data.stage_items.length;
      if (generated === 0 && outcome.value.data.skipped_stage_items === 0) {
        // Sin cuadros que repartir: no es un error, pero tampoco un reparto. La estructura de
        // fases del torneo se crea antes de repartir las inscripciones.
        showNotification({ color: 'yellow', message: t('readiness_generation_empty') });
      } else {
        showNotification({
          color: 'green',
          message: t('readiness_generation_success', {
            generated,
            skipped: outcome.value.data.skipped_stage_items,
          }),
        });
      }
      await swrReadiness.mutate();
      onChanged();
      return;
    }
    const rejected = outcome.value != null && !outcome.value.ok ? outcome.value.failure : null;
    setFailure(rejected);
    if (rejected != null && isBracketNotReady(rejected)) {
      // El servidor ha recalculado la revisión dentro de la transacción: la nuestra ya no vale.
      await swrReadiness.mutate();
      onChanged();
    }
  }

  return (
    <Card withBorder radius="md" padding="lg">
      <Stack gap="sm">
        <Grid align="center">
          <Grid.Col span="auto">
            <Title order={4}>{t('readiness_title')}</Title>
          </Grid.Col>
          <Grid.Col span="content">
            {readiness != null ? (
              <Badge
                color={readiness.ready ? 'green' : 'red'}
                variant="filled"
                size="lg"
                title={readiness.ready ? 'ready' : 'not_ready'}
              >
                {readiness.ready ? t('readiness_ready') : t('readiness_not_ready')}
              </Badge>
            ) : null}
          </Grid.Col>
        </Grid>

        {swrReadiness.error != null ? (
          <F3ErrorAlert failure={describeF3Failure(swrReadiness.error)} />
        ) : null}

        {readiness == null ? (
          <Text size="sm" c="dimmed">
            {t('f3_loading')}
          </Text>
        ) : (
          <>
            <SimpleGrid cols={{ base: 2, sm: 5 }} spacing="xs">
              <Stack gap={0}>
                <Text size="xs" c="dimmed">
                  {t('readiness_total')}
                </Text>
                <Text fw={600}>{readiness.summary.total}</Text>
              </Stack>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">
                  {t('readiness_confirmed')}
                </Text>
                <Text fw={600}>{readiness.summary.confirmed}</Text>
              </Stack>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">
                  {t('readiness_draft')}
                </Text>
                <Text fw={600}>{readiness.summary.draft}</Text>
              </Stack>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">
                  {t('readiness_withdrawn')}
                </Text>
                <Text fw={600}>{readiness.summary.withdrawn}</Text>
              </Stack>
              <Stack gap={0}>
                <Text size="xs" c="dimmed">
                  {t('readiness_disqualified')}
                </Text>
                <Text fw={600}>{readiness.summary.disqualified}</Text>
              </Stack>
            </SimpleGrid>

            <Text size="xs" c="dimmed">
              {t('readiness_minimum_entrants', { minimum: MINIMUM_ENTRANTS })}
            </Text>

            <Table striped withTableBorder>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{t('registrations_category_column')}</Table.Th>
                  <Table.Th>{t('readiness_confirmed_column')}</Table.Th>
                  <Table.Th>{t('readiness_minimum_column')}</Table.Th>
                  <Table.Th>{t('readiness_state_column')}</Table.Th>
                  <Table.Th>{t('readiness_blockers_column')}</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {sortCategories(readiness.categories).map((category) => (
                  <Table.Tr key={category.category_key ?? 'sin-categoria'}>
                    <Table.Td>
                      {categoryDisplayName(category.category_key, category.category_label)}
                    </Table.Td>
                    <Table.Td>{category.confirmed_count}</Table.Td>
                    <Table.Td>{category.required_minimum}</Table.Td>
                    <Table.Td>
                      <Badge color={category.ready ? 'green' : 'orange'} variant="light">
                        {category.ready
                          ? t('readiness_category_ready')
                          : t('readiness_category_not_ready')}
                      </Badge>
                    </Table.Td>
                    <Table.Td>
                      <BlockerList issues={category.blocking_issues} />
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>

            <Stack gap={4}>
              <Text size="sm" fw={600}>
                {t('readiness_blockers_title')}
              </Text>
              <BlockerList issues={readiness.blocking_issues} />
            </Stack>

            <Group gap="sm" align="center">
              <Button
                leftSection={<IconTrophy size={18} />}
                disabled={!readiness.ready || inflight}
                loading={inflight}
                onClick={generateBrackets}
              >
                {t('readiness_generate_button')}
              </Button>
              <Text size="xs" c="dimmed">
                {readiness.ready
                  ? t('readiness_generate_hint_ready')
                  : t('readiness_generate_hint_not_ready')}
              </Text>
            </Group>

            <Text size="xs" c="dimmed">
              {t('readiness_generate_server_authority_notice')}
            </Text>
          </>
        )}

        <F3ErrorAlert failure={failure} />
      </Stack>
    </Card>
  );
}
