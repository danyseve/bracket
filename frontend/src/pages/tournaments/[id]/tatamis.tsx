import { Button, Grid, Group, Skeleton, Stack, Title } from '@mantine/core';
import { useTranslation } from 'react-i18next';

import F3ErrorAlert from '@components/f3/f3_error_alert';
import TatamiBoardView from '@components/f3/tatami_board_view';
import CourtModal from '@components/modals/create_court_modal';
import { NoContent } from '@components/no_content/empty_table_info';
import { boardStageItems } from '@components/utils/bracket_view';
import { describeF3Failure } from '@components/utils/f3_errors';
import { buildTatamiOverview } from '@components/utils/tatami_board';
import { capitalize, getTournamentIdFromRouter } from '@components/utils/util';
import TournamentLayout from '@pages/tournaments/_tournament_layout';
import { getCourts, getStages, getTournamentById } from '@services/adapter';

/**
 * Tatamis del torneo (S4.1 §12-§14).
 *
 * La palabra visible es «Tatami» aunque el modelo se llame `court`. Los tatamis se crean con el
 * modal que ya existía (`CourtModal` → `POST tournaments/{id}/courts`): no se añade una segunda vía
 * de alta. El tablero **deriva** qué hay en cada tatami desde los combates, porque la única
 * asociación persistida es combate → tatami (ver `tatami_board.ts` y el informe de la fase).
 */
export default function TatamisPage() {
  const { t } = useTranslation();
  const { tournamentData } = getTournamentIdFromRouter();
  const swrCourtsResponse = getCourts(tournamentData.id);
  const swrStagesResponse = getStages(tournamentData.id);
  const swrTournamentResponse = getTournamentById(tournamentData.id);

  const courts = swrCourtsResponse.data != null ? swrCourtsResponse.data.data : [];
  const stages = swrStagesResponse.data != null ? swrStagesResponse.data.data : [];
  const overview = buildTatamiOverview(
    courts.map((court) => ({ id: court.id, name: court.name })),
    boardStageItems(stages)
  );
  const isLoading = swrCourtsResponse.isLoading || swrStagesResponse.isLoading;

  return (
    <TournamentLayout tournament_id={tournamentData.id}>
      <Grid justify="space-between" align="center">
        <Grid.Col span="auto">
          <Title>{capitalize(t('tatamis_title'))}</Title>
        </Grid.Col>
        <Grid.Col span="content">
          <Group gap="sm">
            <CourtModal
              swrCourtsResponse={swrCourtsResponse}
              tournamentId={tournamentData.id}
              buttonSize="lg"
            />
          </Group>
        </Grid.Col>
      </Grid>

      {swrCourtsResponse.error != null ? (
        <F3ErrorAlert failure={describeF3Failure(swrCourtsResponse.error)} />
      ) : null}
      {swrStagesResponse.error != null ? (
        <F3ErrorAlert failure={describeF3Failure(swrStagesResponse.error)} />
      ) : null}
      {swrTournamentResponse.error != null ? (
        <F3ErrorAlert failure={describeF3Failure(swrTournamentResponse.error)} />
      ) : null}

      {isLoading ? (
        <Stack gap="sm" mt="md">
          <Skeleton height={120} />
          <Skeleton height={120} />
        </Stack>
      ) : courts.length === 0 ? (
        <Stack align="center" mt="md">
          <NoContent
            title={t('tatamis_empty_title')}
            description={t('tatamis_empty_description')}
          />
        </Stack>
      ) : (
        <div style={{ marginTop: '1rem' }} data-tatami-board>
          <TatamiBoardView overview={overview} />
        </div>
      )}

      {!isLoading && courts.length > 0 && overview.unassigned.length > 0 ? (
        <Button variant="subtle" component="a" href={`/tournaments/${tournamentData.id}/schedule`}>
          {t('tatamis_open_planning')}
        </Button>
      ) : null}
    </TournamentLayout>
  );
}
