import { Alert, Button, Grid, Group, Skeleton, Stack, Text, Title } from '@mantine/core';
import { IconCheck } from '@tabler/icons-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import F3ErrorAlert from '@components/f3/f3_error_alert';
import TatamiBoardView from '@components/f3/tatami_board_view';
import TatamiControlBoardView from '@components/f3/tatami_control_board';
import CourtModal from '@components/modals/create_court_modal';
import { NoContent } from '@components/no_content/empty_table_info';
import { boardStageItems } from '@components/utils/bracket_view';
import { describeF3Failure, F3Failure } from '@components/utils/f3_errors';
import useF3Write from '@components/utils/f3_write_hook';
import { runAssignment } from '@components/utils/tatami_actions';
import {
  AssignmentFeedback,
  assignmentFeedbackKey,
  BoardCourt,
  buildTatamiControlBoard,
  buildTatamiOverview,
  ControlCard,
  courtLabel,
  hasStageItemCourtContract,
  TatamiAssignmentSource,
} from '@components/utils/tatami_board';
import { capitalize, getTournamentIdFromRouter } from '@components/utils/util';
import TournamentLayout from '@pages/tournaments/_tournament_layout';
import { getCourts, getStages, getTournamentById } from '@services/adapter';
import { setStageItemCourt } from '@services/f3';

/**
 * Tatamis del torneo (S4.1 §12-§14 y S4.3).
 *
 * La palabra visible es «Tatami» aunque el modelo se llame `court`. Los tatamis se crean con el modal
 * que ya existía (`CourtModal` → `POST tournaments/{id}/courts`): no se añade una segunda vía de alta.
 *
 * El tablero de control asigna **cuadros enteros** a un tatami por el contrato de S4.2
 * (`PUT tournaments/{id}/stage-items/{stage_item_id}/court`), con la puerta de escritura y la
 * `Idempotency-Key`. La asignación que se pinta sale siempre del estado publicado por el backend: tras
 * cada cambio se releen `stages` y `courts`, y la pantalla no guarda ninguna copia optimista.
 *
 * Si el entorno no publica el tatami del cuadro (backend anterior a S4.2), se enseña el tablero
 * derivado de los combates —con su aviso— en vez de ofrecer una acción que el backend no sabe guardar.
 */
export default function TatamisPage() {
  const { t } = useTranslation();
  const { tournamentData } = getTournamentIdFromRouter();
  const swrCourtsResponse = getCourts(tournamentData.id);
  const swrStagesResponse = getStages(tournamentData.id);
  const swrTournamentResponse = getTournamentById(tournamentData.id);
  const { run, inflight, gate } = useF3Write();
  const [feedback, setFeedback] = useState<AssignmentFeedback | null>(null);
  const [failure, setFailure] = useState<F3Failure | null>(null);

  const courts = swrCourtsResponse.data != null ? swrCourtsResponse.data.data : [];
  const stages = swrStagesResponse.data != null ? swrStagesResponse.data.data : [];
  const boardCourts: BoardCourt[] = courts.map((court) => ({ id: court.id, name: court.name }));
  const stageItems = boardStageItems(stages);

  // Capacidad derivada del **contrato publicado**, no de una versión declarada: si el campo
  // `court_id` viaja en la respuesta, la asignación por cuadro existe.
  const publishesAssignment = hasStageItemCourtContract(
    stages.flatMap((stage) => stage.stage_items)
  );
  const assignmentSource: TatamiAssignmentSource = publishesAssignment ? 'stage_item' : 'match';
  const board = buildTatamiControlBoard(stageItems, boardCourts, assignmentSource);
  const overview = buildTatamiOverview(boardCourts, stageItems);
  const isLoading = swrCourtsResponse.isLoading || swrStagesResponse.isLoading;

  const refetchState = async () => {
    await Promise.all([swrStagesResponse.mutate(), swrCourtsResponse.mutate()]);
  };

  const handleAssign = async (card: ControlCard, target: BoardCourt | null) => {
    setFailure(null);
    const outcome = await runAssignment(
      { run, restorePending: (scope, key) => gate.restorePending(scope, key) },
      {
        tournament_id: tournamentData.id,
        stage_item_id: card.stage_item_id,
        current_court_id: card.court_id,
        target_court_id: target != null ? target.id : null,
      },
      {
        // El servicio real de S4.2: el cuerpo va explícito porque `court_id: null` es «sin tatami».
        write: (tournament_id, stage_item_id, court_id, key) =>
          setStageItemCourt(tournament_id, stage_item_id, { court_id }, key),
        refresh: refetchState,
      }
    );

    if (outcome.status === 'applied') {
      setFeedback(outcome.feedback);
      return;
    }
    if (outcome.status === 'failed') {
      setFeedback(null);
      setFailure(outcome.failure);
    }
    // `ignored` (doble clic) y `rejected` (no-op) no cambian nada en pantalla: el estado publicado
    // sigue siendo la única fuente de verdad.
  };

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
      {failure != null ? <F3ErrorAlert failure={failure} /> : null}

      {feedback != null ? (
        <Alert
          color="green"
          variant="light"
          icon={<IconCheck size={22} />}
          mt="sm"
          data-assignment-feedback={feedback.stage_item_id}
        >
          <Text size="sm">
            {t(assignmentFeedbackKey(feedback), {
              court: courtLabel(boardCourts, feedback.court_id, feedback.court_name),
              count: feedback.affected_match_count,
            })}
          </Text>
        </Alert>
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
          {publishesAssignment ? (
            <TatamiControlBoardView
              board={board}
              courts={boardCourts}
              inflight={inflight}
              onAssign={handleAssign}
            />
          ) : (
            <TatamiBoardView overview={overview} />
          )}
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
