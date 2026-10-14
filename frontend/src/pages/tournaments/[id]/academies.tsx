import { Button, Card, Grid, Group, Skeleton, Stack, Table, Text, Title } from '@mantine/core';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import F3ErrorAlert from '@components/f3/f3_error_alert';
import SportsClubLogo from '@components/f3/sports_club_logo';
import { NoContent } from '@components/no_content/empty_table_info';
import { describeF3Failure } from '@components/utils/f3_errors';
import { capitalize, getTournamentIdFromRouter } from '@components/utils/util';
import TournamentLayout from '@pages/tournaments/_tournament_layout';
import { getTournamentById } from '@services/adapter';
import { getSportsClubs } from '@services/f3';

/**
 * Academias (clubes deportivos) del torneo — S4.8d §2/§5.
 *
 * Hasta ahora el logo del club deportivo sólo se podía gestionar **dentro** del perfil deportivo de
 * un competidor (`SportsClubSelect` → `SportsClubLogo`), así que no existía ninguna pantalla de
 * academias: quien buscaba «Clubes / Academias» no encontraba la opción. Esta página da esa
 * superficie reutilizando lo que ya está desplegado: `GET /clubs/:club_id/sports-clubs` para el
 * listado y los endpoints `logo` / `remove_logo` de S4.8a-4 a través de `SportsClubLogo`
 * (PNG · JPEG · WEBP, máximo 2 MB, sin SVG: el backend valida tipo y tamaño).
 *
 * El control de logo sólo se habilita con «acciones de organizador» activadas, igual que el resto de
 * escrituras de estas pantallas. El backend sigue siendo la autoridad de permisos (`OWNER`): activar
 * el interruptor no concede nada, sólo muestra el control.
 */
export default function TournamentAcademies() {
  const { t } = useTranslation();
  const { tournamentData } = getTournamentIdFromRouter();
  const [organizerActions, setOrganizerActions] = useState(false);

  const swrTournamentResponse = getTournamentById(tournamentData.id);
  const club_id =
    swrTournamentResponse.data != null ? swrTournamentResponse.data.data.club_id : null;

  const swrSportsClubs = getSportsClubs(club_id);
  const sportsClubs = swrSportsClubs.data != null ? swrSportsClubs.data : [];
  const isLoading = swrTournamentResponse.isLoading || swrSportsClubs.isLoading;

  return (
    <TournamentLayout tournament_id={tournamentData.id}>
      <Grid justify="space-between" align="center">
        <Grid.Col span="auto">
          <Title>{capitalize(t('f3_academies_title'))}</Title>
        </Grid.Col>
        <Grid.Col span="content">
          <Button variant="light" onClick={() => setOrganizerActions(!organizerActions)}>
            {organizerActions ? t('organizer_actions_hide') : t('organizer_actions_show')}
          </Button>
        </Grid.Col>
      </Grid>

      <Card withBorder radius="md" padding="md" mt="md">
        <Text size="sm" c="dimmed">
          {t('f3_academies_description')}
        </Text>
        {organizerActions ? (
          <Text size="xs" c="dimmed" mt="xs">
            {t('organizer_actions_note')}
          </Text>
        ) : null}
      </Card>

      {swrTournamentResponse.error != null ? (
        <F3ErrorAlert failure={describeF3Failure(swrTournamentResponse.error)} />
      ) : null}
      {swrSportsClubs.error != null ? (
        <F3ErrorAlert failure={describeF3Failure(swrSportsClubs.error)} />
      ) : null}

      <Stack mt="lg">
        {isLoading ? <Skeleton height={120} radius="md" /> : null}

        {!isLoading && sportsClubs.length < 1 ? (
          <Stack align="center" mt="md">
            <NoContent
              title={t('f3_academies_empty_title')}
              description={t('f3_academies_empty_description')}
            />
          </Stack>
        ) : null}

        {sportsClubs.length > 0 ? (
          <Card withBorder radius="md" padding="md">
            <Table highlightOnHover>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{t('f3_profile_club_label')}</Table.Th>
                  <Table.Th>{t('f3_club_logo_manage_title')}</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {sportsClubs.map((sportsClub) => (
                  <Table.Tr key={sportsClub.id} data-academy-row={sportsClub.id}>
                    <Table.Td>
                      <Group gap="xs">
                        <Text fw={600}>{sportsClub.name}</Text>
                      </Group>
                    </Table.Td>
                    <Table.Td>
                      <SportsClubLogo
                        sportsClub={sportsClub}
                        club_id={club_id}
                        canManage={organizerActions}
                        onChanged={() => {
                          void swrSportsClubs.mutate();
                        }}
                      />
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Card>
        ) : null}
      </Stack>
    </TournamentLayout>
  );
}
