import {
  Badge,
  Button,
  Card,
  Grid,
  Group,
  Modal,
  Skeleton,
  Stack,
  Switch,
  Table,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import { showNotification } from '@mantine/notifications';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import F3ErrorAlert from '@components/f3/f3_error_alert';
import SportsClubLogo from '@components/f3/sports_club_logo';
import { NoContent } from '@components/no_content/empty_table_info';
import { SportsClub } from '@components/utils/f3_competitor_contract';
import { describeF3Failure, F3Failure } from '@components/utils/f3_errors';
import { actionScope } from '@components/utils/f3_write';
import useF3Write from '@components/utils/f3_write_hook';
import { capitalize, getTournamentIdFromRouter } from '@components/utils/util';
import TournamentLayout from '@pages/tournaments/_tournament_layout';
import { getTournamentById } from '@services/adapter';
import { createSportsClub, getSportsClubs, updateSportsClub } from '@services/f3';

/**
 * Clubes del torneo — S4.8g-2 §7/§15.
 *
 * La entidad deportiva visible es **una sola: Club** (el nombre técnico histórico de la tabla es
 * `sports_clubs`, y la organización/tenant es otra pantalla: «Mis organizaciones»). Un competidor
 * pertenece a un Club o es **Independiente**; el Club es estable y reutilizable, y muchos
 * competidores pueden compartirlo: nunca se crea un club por torneo, por inscripción ni por
 * competidor (§1).
 *
 * La página ofrece el ciclo de vida mínimo y sin borrado físico: listar, crear, renombrar,
 * activar/desactivar y gestionar el logo (subir, reemplazar, quitar). Reutiliza lo que ya está
 * desplegado y certificado: `GET /clubs/:club_id/sports-clubs` para el listado,
 * `POST /clubs/:club_id/sports-clubs` y `PATCH /clubs/:club_id/sports-clubs/:id` para las
 * mutaciones (protocolo de idempotencia: la clave la genera `useF3Write`) y `SportsClubLogo` para
 * el logo (PNG · JPEG · WEBP, máximo 2 MB, sin SVG: el backend valida tipo y tamaño).
 *
 * El control de escritura sólo se muestra con «acciones de organizador» activadas, igual que el
 * resto de pantallas. El backend sigue siendo la autoridad de permisos (`OWNER`): activar el
 * interruptor no concede nada, sólo muestra los controles.
 */
export default function TournamentClubs() {
  const { t } = useTranslation();
  const { tournamentData } = getTournamentIdFromRouter();
  const { run, inflight } = useF3Write();

  const [organizerActions, setOrganizerActions] = useState(false);
  const [createOpened, setCreateOpened] = useState(false);
  const [newName, setNewName] = useState('');
  const [newActive, setNewActive] = useState(true);
  const [editTarget, setEditTarget] = useState<SportsClub | null>(null);
  const [editName, setEditName] = useState('');
  const [editActive, setEditActive] = useState(true);
  const [failure, setFailure] = useState<F3Failure | null>(null);

  const swrTournamentResponse = getTournamentById(tournamentData.id);
  const club_id =
    swrTournamentResponse.data != null ? swrTournamentResponse.data.data.club_id : null;

  // Gestión de Clubes (§7/§15): aquí sí se listan los dados de baja para poder reactivarlos.
  const swrSportsClubs = getSportsClubs(club_id, true);
  const sportsClubs = swrSportsClubs.data != null ? swrSportsClubs.data : [];
  const isLoading = swrTournamentResponse.isLoading || swrSportsClubs.isLoading;

  function openCreate() {
    setFailure(null);
    setNewName('');
    setNewActive(true);
    setCreateOpened(true);
  }

  function openEdit(sportsClub: SportsClub) {
    setFailure(null);
    setEditTarget(sportsClub);
    setEditName(sportsClub.name);
    setEditActive(sportsClub.active);
  }

  async function handleCreate() {
    if (club_id == null) {
      return;
    }
    setFailure(null);
    const name = newName.trim();
    const outcome = await run(actionScope(['sports-club-create', club_id, name]), (key) =>
      createSportsClub(club_id, { name, active: newActive }, key)
    );
    if (outcome.status === 'ignored') {
      return;
    }
    if (outcome.value == null || !outcome.value.ok) {
      setFailure(outcome.value != null && !outcome.value.ok ? outcome.value.failure : null);
      return;
    }
    setCreateOpened(false);
    await swrSportsClubs.mutate();
    showNotification({ color: 'green', message: t('f3_club_create_success') });
  }

  async function handleUpdate() {
    if (club_id == null || editTarget == null) {
      return;
    }
    setFailure(null);
    const name = editName.trim();
    const outcome = await run(
      actionScope(['sports-club-update', club_id, editTarget.id, name, editActive]),
      (key) => updateSportsClub(club_id, editTarget.id, { name, active: editActive }, key)
    );
    if (outcome.status === 'ignored') {
      return;
    }
    if (outcome.value == null || !outcome.value.ok) {
      setFailure(outcome.value != null && !outcome.value.ok ? outcome.value.failure : null);
      return;
    }
    setEditTarget(null);
    await swrSportsClubs.mutate();
    showNotification({ color: 'green', message: t('f3_club_update_success') });
  }

  async function handleToggleActive(sportsClub: SportsClub) {
    if (club_id == null) {
      return;
    }
    setFailure(null);
    const outcome = await run(
      actionScope(['sports-club-active', club_id, sportsClub.id, !sportsClub.active]),
      (key) => updateSportsClub(club_id, sportsClub.id, { active: !sportsClub.active }, key)
    );
    if (outcome.status === 'ignored') {
      return;
    }
    if (outcome.value == null || !outcome.value.ok) {
      setFailure(outcome.value != null && !outcome.value.ok ? outcome.value.failure : null);
      return;
    }
    await swrSportsClubs.mutate();
    showNotification({ color: 'green', message: t('f3_club_update_success') });
  }

  const duplicateHint =
    failure != null && failure.technicalCode === 'DUPLICATE_CLUB' ? t('f3_club_duplicate') : null;
  const nameIsEmpty = createOpened && newName.trim().length < 1;

  return (
    <TournamentLayout tournament_id={tournamentData.id}>
      <Grid justify="space-between" align="center" data-clubs-page>
        <Grid.Col span="auto">
          <Title>{capitalize(t('f3_clubs_title'))}</Title>
        </Grid.Col>
        <Grid.Col span="content">
          <Group gap="sm">
            {organizerActions ? (
              <Button data-club-create onClick={openCreate}>
                {t('f3_club_new_button')}
              </Button>
            ) : null}
            <Button variant="light" onClick={() => setOrganizerActions(!organizerActions)}>
              {organizerActions ? t('organizer_actions_hide') : t('organizer_actions_show')}
            </Button>
          </Group>
        </Grid.Col>
      </Grid>

      <Card withBorder radius="md" padding="md" mt="md">
        <Text size="sm" c="dimmed">
          {t('f3_clubs_description')}
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
      <F3ErrorAlert failure={failure} />
      {duplicateHint != null ? (
        <Text size="xs" c="red" mt="xs" data-club-duplicate-hint>
          {duplicateHint}
        </Text>
      ) : null}

      <Stack mt="lg">
        {isLoading ? <Skeleton height={120} radius="md" /> : null}

        {!isLoading && sportsClubs.length < 1 ? (
          <Stack align="center" mt="md">
            <NoContent
              title={t('f3_clubs_empty_title')}
              description={t('f3_clubs_empty_description')}
            />
          </Stack>
        ) : null}

        {sportsClubs.length > 0 ? (
          <Card withBorder radius="md" padding="md">
            <Table highlightOnHover>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>{t('f3_club_name_label')}</Table.Th>
                  <Table.Th>{t('f3_club_logo_manage_title')}</Table.Th>
                  <Table.Th>{t('competitors_state_active')}</Table.Th>
                  {organizerActions ? <Table.Th>{t('f3_club_actions_label')}</Table.Th> : null}
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {sportsClubs.map((sportsClub) => (
                  <Table.Tr key={sportsClub.id} data-club-row={sportsClub.id}>
                    <Table.Td>
                      <Text fw={600}>{sportsClub.name}</Text>
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
                    <Table.Td>
                      <Badge color={sportsClub.active ? 'green' : 'gray'} variant="light">
                        {sportsClub.active
                          ? t('competitors_state_active')
                          : t('competitors_state_inactive')}
                      </Badge>
                    </Table.Td>
                    {organizerActions ? (
                      <Table.Td>
                        <Group gap="xs">
                          <Button
                            variant="light"
                            size="xs"
                            data-club-edit={sportsClub.id}
                            onClick={() => openEdit(sportsClub)}
                          >
                            {t('f3_club_edit_button')}
                          </Button>
                          <Button
                            variant="subtle"
                            size="xs"
                            data-club-toggle={sportsClub.id}
                            disabled={inflight}
                            onClick={() => void handleToggleActive(sportsClub)}
                          >
                            {sportsClub.active
                              ? t('f3_club_deactivate_button')
                              : t('f3_club_activate_button')}
                          </Button>
                        </Group>
                      </Table.Td>
                    ) : null}
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Card>
        ) : null}
      </Stack>

      <Modal
        opened={createOpened}
        onClose={() => setCreateOpened(false)}
        title={t('f3_club_create_title')}
        data-club-create-modal
      >
        <Stack>
          <Text size="sm" c="dimmed">
            {t('f3_club_create_description')}
          </Text>
          <TextInput
            label={t('f3_club_name_label')}
            data-club-name-input
            value={newName}
            onChange={(event) => setNewName(event.currentTarget.value)}
            error={nameIsEmpty ? t('f3_club_name_required') : null}
            required
          />
          <Switch
            label={t('f3_club_active_label')}
            data-club-active-input
            checked={newActive}
            onChange={(event) => setNewActive(event.currentTarget.checked)}
          />
          <Group justify="flex-end">
            <Button variant="light" onClick={() => setCreateOpened(false)}>
              {t('close_button')}
            </Button>
            <Button
              loading={inflight}
              disabled={newName.trim().length < 1}
              onClick={() => void handleCreate()}
            >
              {t('f3_club_new_button')}
            </Button>
          </Group>
        </Stack>
      </Modal>

      <Modal
        opened={editTarget != null}
        onClose={() => setEditTarget(null)}
        title={t('f3_club_edit_title')}
        data-club-edit-modal
      >
        <Stack>
          <TextInput
            label={t('f3_club_name_label')}
            data-club-edit-name-input
            value={editName}
            onChange={(event) => setEditName(event.currentTarget.value)}
            required
          />
          <Switch
            label={t('f3_club_active_label')}
            data-club-edit-active-input
            checked={editActive}
            onChange={(event) => setEditActive(event.currentTarget.checked)}
          />
          <Group justify="flex-end">
            <Button variant="light" onClick={() => setEditTarget(null)}>
              {t('close_button')}
            </Button>
            <Button
              loading={inflight}
              disabled={editName.trim().length < 1}
              onClick={() => void handleUpdate()}
            >
              {t('f3_club_edit_button')}
            </Button>
          </Group>
        </Stack>
      </Modal>
    </TournamentLayout>
  );
}
