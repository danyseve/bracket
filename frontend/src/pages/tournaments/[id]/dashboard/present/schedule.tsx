import { Grid, Stack, Text, Title } from '@mantine/core';
import React from 'react';
import { useTranslation } from 'react-i18next';

import {
  TournamentLogo,
  TournamentQRCode,
  TournamentTitle,
  getTournamentHeadTitle,
} from '@components/dashboard/layout';
import PublicScheduleView from '@components/f3/public_schedule_view';
import { TableSkeletonTwoColumns } from '@components/utils/skeletons';
import { setTitle } from '@components/utils/util';
import { getPublicScheduleLive } from '@services/adapter';
import { getTournamentResponseByEndpointName } from '@services/dashboard';

/**
 * Pantalla pública del planificador para TV y tablet (S4.5).
 *
 * Se monta en la ruta de presentación del torneo, como `present/courts` y `present/standings`. La
 * única fuente es `GET /public/tournaments/{tid}/schedule`, que se sondea cada pocos segundos: el
 * display no habla con ninguna otra ruta y no calcula estados por su cuenta.
 *
 * Si el panel público del torneo está apagado, el backend responde 401 y aquí se dice; la pantalla
 * no se cae ni se queda en blanco.
 */
export default function SchedulePresentPage() {
  const { t } = useTranslation();
  const tournamentDataFull = getTournamentResponseByEndpointName();
  const tournamentValid = !React.isValidElement(tournamentDataFull);

  const swrScheduleResponse = getPublicScheduleLive(tournamentValid ? tournamentDataFull.id : null);

  if (!tournamentValid) {
    return tournamentDataFull;
  }

  if (swrScheduleResponse.data == null) {
    if (swrScheduleResponse.error == null) {
      return <TableSkeletonTwoColumns />;
    }
    return (
      <Grid style={{ margin: '1rem' }} gutter="2rem">
        <Grid.Col span="auto">
          <Stack gap="sm">
            <Title order={2}>{getTournamentHeadTitle(tournamentDataFull)}</Title>
            <Text size="lg">{t('public_schedule_unavailable')}</Text>
          </Stack>
        </Grid.Col>
      </Grid>
    );
  }

  setTitle(getTournamentHeadTitle(tournamentDataFull));

  return (
    <Grid style={{ margin: '1rem' }} gutter="2rem">
      <Grid.Col span={{ base: 12, lg: 2 }}>
        <TournamentTitle tournamentDataFull={tournamentDataFull} />
        <TournamentLogo tournamentDataFull={tournamentDataFull} />
        <TournamentQRCode tournamentDataFull={tournamentDataFull} />
      </Grid.Col>
      <Grid.Col span="auto">
        <PublicScheduleView payload={swrScheduleResponse.data} />
      </Grid.Col>
    </Grid>
  );
}
