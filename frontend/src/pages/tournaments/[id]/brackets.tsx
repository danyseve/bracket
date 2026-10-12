import { Anchor, Badge, Card, Grid, Skeleton, Stack, Table, Text, Title } from '@mantine/core';
import { useTranslation } from 'react-i18next';

import F3ErrorAlert from '@components/f3/f3_error_alert';
import { NoContent } from '@components/no_content/empty_table_info';
import { BracketRow, bracketRows, bracketStateKey } from '@components/utils/bracket_view';
import { describeF3Failure } from '@components/utils/f3_errors';
import { capitalize, getTournamentIdFromRouter } from '@components/utils/util';
import { Court } from '@openapi';
import TournamentLayout from '@pages/tournaments/_tournament_layout';
import { getCourts, getStages, getTournamentById } from '@services/adapter';

const BRACKET_STATE_COLORS: Record<string, string> = {
  not_generated: 'gray',
  generated: 'blue',
  running: 'orange',
  finished: 'green',
};

function TatamiList({ courtIds, courts }: { courtIds: number[]; courts: Court[] }) {
  const { t } = useTranslation();
  const names = courtIds
    .map((courtId) => {
      const index = courts.findIndex((court) => court.id === courtId);
      return index >= 0 ? `Tatami ${index + 1}` : t('brackets_tatami_unknown');
    })
    .filter((name) => name !== '');
  if (names.length === 0) {
    return (
      <Badge color="gray" variant="light">
        {t('brackets_tatami_unassigned')}
      </Badge>
    );
  }
  return <Text size="sm">{names.join(', ')}</Text>;
}

/**
 * Cuadros generados (S4.1 §11).
 *
 * Lista los cuadros tal como los publica el backend (`GET tournaments/{id}/stages`): categoría,
 * participantes, combates y estado derivado del marcador. No se reconstruye la estructura del
 * cuadro en el frontend: se muestra lo que ya existe.
 */
export default function BracketsPage() {
  const { t } = useTranslation();
  const { tournamentData } = getTournamentIdFromRouter();
  const swrStagesResponse = getStages(tournamentData.id);
  const swrCourtsResponse = getCourts(tournamentData.id);
  const swrTournamentResponse = getTournamentById(tournamentData.id);

  const stages = swrStagesResponse.data != null ? swrStagesResponse.data.data : [];
  const courts = swrCourtsResponse.data != null ? swrCourtsResponse.data.data : [];
  const rows: BracketRow[] = bracketRows(stages);
  const isLoading = swrStagesResponse.isLoading || swrCourtsResponse.isLoading;

  return (
    <TournamentLayout tournament_id={tournamentData.id}>
      <Grid justify="space-between" align="center">
        <Grid.Col span="auto">
          <Title>{capitalize(t('brackets_title'))}</Title>
        </Grid.Col>
      </Grid>

      <Card withBorder radius="md" padding="md" mt="md">
        <Text size="sm" c="dimmed">
          {t('brackets_source_notice')}{' '}
          <Anchor href={`/tournaments/${tournamentData.id}/registrations`}>
            {t('registrations_title')}
          </Anchor>
        </Text>
        <Text size="xs" c="dimmed" mt="xs">
          {t('brackets_derived_state_notice')}
        </Text>
      </Card>

      {swrStagesResponse.error != null ? (
        <F3ErrorAlert failure={describeF3Failure(swrStagesResponse.error)} />
      ) : null}
      {swrTournamentResponse.error != null ? (
        <F3ErrorAlert failure={describeF3Failure(swrTournamentResponse.error)} />
      ) : null}

      <div style={{ overflowX: 'auto' }}>
        <Table striped highlightOnHover withTableBorder mt="md" data-brackets-table>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>{t('brackets_category_column')}</Table.Th>
              <Table.Th>{t('brackets_stage_column')}</Table.Th>
              <Table.Th>{t('brackets_entrants_column')}</Table.Th>
              <Table.Th>{t('brackets_matches_column')}</Table.Th>
              <Table.Th>{t('brackets_state_column')}</Table.Th>
              <Table.Th>{t('brackets_tatami_column')}</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {isLoading
              ? Array.from({ length: 4 }, (_, index) => (
                  <Table.Tr key={`skeleton-${index}`}>
                    <Table.Td colSpan={6}>
                      <Skeleton height={20} />
                    </Table.Td>
                  </Table.Tr>
                ))
              : rows.map((row) => (
                  <Table.Tr key={row.stage_item_id} data-bracket-row={row.stage_item_id}>
                    <Table.Td>{row.category_name}</Table.Td>
                    <Table.Td>{row.stage_name}</Table.Td>
                    <Table.Td>{row.entrant_count}</Table.Td>
                    <Table.Td>
                      {row.played_count}/{row.match_count}
                    </Table.Td>
                    <Table.Td>
                      <Badge color={BRACKET_STATE_COLORS[row.state]} variant="light">
                        {t(bracketStateKey(row.state))}
                      </Badge>
                    </Table.Td>
                    <Table.Td>
                      <TatamiList courtIds={row.court_ids} courts={courts} />
                    </Table.Td>
                  </Table.Tr>
                ))}
          </Table.Tbody>
        </Table>
      </div>

      {!isLoading && rows.length === 0 ? (
        <Stack align="center" mt="md">
          <NoContent
            title={t('brackets_empty_title')}
            description={t('brackets_empty_description')}
          />
        </Stack>
      ) : null}
    </TournamentLayout>
  );
}
