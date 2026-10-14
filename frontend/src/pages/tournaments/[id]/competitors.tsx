import {
  Badge,
  Button,
  Card,
  Grid,
  Group,
  Pagination,
  Skeleton,
  Stack,
  Switch,
  Table,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import { useDebouncedValue } from '@mantine/hooks';
import { showNotification } from '@mantine/notifications';
import { IconPlus, IconSearch } from '@tabler/icons-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import CompetitorCreateWizard, {
  CompetitorCreateValues,
} from '@components/f3/competitor_create_wizard';
import CompetitorSportsProfile from '@components/f3/competitor_sports_profile';
import F3ErrorAlert from '@components/f3/f3_error_alert';
import ReasonModal from '@components/f3/reason_modal';
import CompetitorModal from '@components/modals/competitor_modal';
import { NoContent } from '@components/no_content/empty_table_info';
import { DateTime } from '@components/utils/datetime';
import {
  COMPETITOR_CREATE_REASON_CODE,
  COMPETITOR_LIFECYCLE_REASON_CODES,
  COMPETITOR_RENAME_REASON_CODE,
  Competitor,
} from '@components/utils/f3_contract';
import { F3Failure, describeF3Failure } from '@components/utils/f3_errors';
import {
  CompetitorProfileTarget,
  affiliationAction,
  affiliationSportsClubId,
  beltLabelForId,
  competitorCreateBody,
  derivedAge,
  sexLabelKey,
  toIsoDate,
} from '@components/utils/f3_sports_profile';
import { actionScope } from '@components/utils/f3_write';
import useF3Write from '@components/utils/f3_write_hook';
import { capitalize, getTournamentIdFromRouter } from '@components/utils/util';
import TournamentLayout from '@pages/tournaments/_tournament_layout';
import { getTournamentById } from '@services/adapter';
import {
  assignAffiliation,
  createCompetitor,
  getBelts,
  getCompetitors,
  getSportsClubs,
  renameCompetitor,
  setCompetitorActive,
} from '@services/f3';

const PAGE_SIZE = 25;

/**
 * Competidores del club (S4.1 §4).
 *
 * Listado con búsqueda y filtro de bajas, sin identificadores internos a la vista. Las acciones de
 * baja y reactivación exigen relación OWNER con el club y viven detrás de un desplegable explícito
 * (ver `OrganizerOnly`), porque el frontend no puede consultar hoy esa relación.
 */
export default function CompetitorsPage() {
  const { t } = useTranslation();
  const { tournamentData } = getTournamentIdFromRouter();
  const swrTournamentResponse = getTournamentById(tournamentData.id);
  const club_id =
    swrTournamentResponse.data != null ? swrTournamentResponse.data.data.club_id : null;

  const [nameFilter, setNameFilter] = useState('');
  const [search] = useDebouncedValue(nameFilter, 300);
  const [includeInactive, setIncludeInactive] = useState(true);
  const [page, setPage] = useState(1);
  const [organizerActions, setOrganizerActions] = useState(false);
  const [failure, setFailure] = useState<F3Failure | null>(null);
  const [createOpened, setCreateOpened] = useState(false);
  const [renameTarget, setRenameTarget] = useState<Competitor | null>(null);
  // El perfil deportivo recibe los valores **reales** de la fila (antes se forzaban a null, así que
  // el formulario sólo se rellenaba cuando respondía su propia petición). S4.8d §3.
  const [profileTarget, setProfileTarget] = useState<CompetitorProfileTarget | null>(null);
  const [lifecycleTarget, setLifecycleTarget] = useState<{
    competitor: Competitor;
    active: boolean;
  } | null>(null);
  // Fallo parcial del alta (§5): el competidor se creó pero quedó sin Club / Academia. Se conserva
  // para decirlo con el nombre del competidor y poder completarlo desde el perfil.
  const [partialFailure, setPartialFailure] = useState<{
    name: string;
    failure: F3Failure | null;
  } | null>(null);

  const { run, inflight } = useF3Write();

  const swrCompetitors = getCompetitors(club_id, {
    display_name: search,
    include_inactive: includeInactive,
    limit: PAGE_SIZE,
    offset: (page - 1) * PAGE_SIZE,
  });

  const competitors = swrCompetitors.data != null ? swrCompetitors.data.competitors : [];
  const count = swrCompetitors.data != null ? swrCompetitors.data.count : 0;
  const pages = count > 0 ? Math.ceil(count / PAGE_SIZE) : 1;

  // Catálogos de la superficie de personas (S4.8g §10/§5): cinturones, para traducir `belt_id` sin
  // enseñar identificadores, y academias, para el paso 2 del alta.
  const swrBelts = getBelts(club_id);
  const belts = swrBelts.data != null ? swrBelts.data : [];
  const swrSportsClubs = getSportsClubs(club_id);
  const sportsClubs = swrSportsClubs.data != null ? swrSportsClubs.data : [];

  const today = toIsoDate(new Date());

  // i18next devuelve la propia clave cuando no hay traducción: ese caso usa el nombre del backend.
  const lookup = (key: string): string | null => {
    const translated = t(key);
    return translated === key ? null : translated;
  };

  // Datos que faltan no son un error fatal (§10): se dice qué falta, en lugar de dejar el hueco.
  const ageLabel = (dateOfBirth: string | null): string => {
    const age = dateOfBirth != null ? derivedAge(dateOfBirth, today) : null;
    return age != null ? t('f3_profile_age_value', { age }) : t('f3_profile_dob_none');
  };
  const sexLabel = (value: Competitor['sex']): string =>
    value != null ? t(sexLabelKey(value)) : t('f3_sex_unspecified');
  const beltColumnLabel = (beltId: number | null): string =>
    beltLabelForId(belts, beltId, lookup) ?? t('f3_profile_belt_none');
  const clubColumnLabel = (competitor: Competitor): string =>
    competitor.sports_club_name != null
      ? competitor.sports_club_name
      : t('f3_club_independent_option');

  async function handleCreate(values: CompetitorCreateValues) {
    if (club_id == null) {
      return;
    }
    setFailure(null);
    setPartialFailure(null);
    // Paso 1 del alta: identidad y perfil en una sola escritura (el backend acepta fecha, sexo y
    // cinturón en el alta). No se inventan endpoints ni se duplican reglas del backend (§5/§15).
    const outcome = await run(
      actionScope(['competitor-create', club_id, values.display_name]),
      (key) =>
        createCompetitor(club_id, competitorCreateBody(values, COMPETITOR_CREATE_REASON_CODE), key)
    );
    if (outcome.status === 'ignored') {
      return;
    }
    if (outcome.value == null || !outcome.value.ok) {
      setFailure(outcome.value != null && !outcome.value.ok ? outcome.value.failure : null);
      return;
    }

    const created = outcome.value.data;
    setCreateOpened(false);
    await swrCompetitors.mutate();

    // Paso 2: el alta nace sin afiliación, así que «Independiente» no es una escritura; elegir una
    // academia sí lo es, y se hace con la acción de afiliación ya existente.
    const sportsClubId = affiliationSportsClubId(affiliationAction(null, values.club_choice));
    if (sportsClubId == null) {
      showNotification({ color: 'green', message: t('competitors_create_success') });
      return;
    }

    const affiliation = await run(
      actionScope(['competitor-affiliation', created.id, sportsClubId]),
      (key) => assignAffiliation(club_id, created.id, sportsClubId, key)
    );
    if (affiliation.status === 'ignored') {
      return;
    }
    if (affiliation.value != null && affiliation.value.ok) {
      showNotification({ color: 'green', message: t('competitors_create_success') });
      return;
    }

    // Fallo parcial (§5): el competidor existe pero quedó sin Club / Academia. No se oculta el fallo
    // ni se pierde el trabajo: se dice qué competidor es y se abre su perfil para completarlo.
    setPartialFailure({
      name: created.display_name,
      failure:
        affiliation.value != null && !affiliation.value.ok ? affiliation.value.failure : null,
    });
    setProfileTarget({
      id: created.id,
      display_name: created.display_name,
      date_of_birth: values.date_of_birth,
      sex: values.sex,
      belt_id: values.belt_id,
    });
  }

  async function handleRename(displayName: string) {
    if (club_id == null || renameTarget == null) {
      return;
    }
    setFailure(null);
    const outcome = await run(
      actionScope(['competitor-rename', renameTarget.id, displayName]),
      (key) =>
        renameCompetitor(
          club_id,
          renameTarget.id,
          { display_name: displayName, reason_code: COMPETITOR_RENAME_REASON_CODE },
          key
        )
    );
    if (outcome.status === 'ignored') {
      return;
    }
    if (outcome.value != null && outcome.value.ok) {
      setRenameTarget(null);
      showNotification({ color: 'green', message: t('competitors_rename_success') });
      await swrCompetitors.mutate();
      return;
    }
    setFailure(outcome.value != null && !outcome.value.ok ? outcome.value.failure : null);
  }

  async function handleLifecycle(reasonCode: string, reasonNote: string | null) {
    if (club_id == null || lifecycleTarget == null) {
      return;
    }
    setFailure(null);
    const outcome = await run(
      actionScope(['competitor-active', lifecycleTarget.competitor.id, lifecycleTarget.active]),
      (key) =>
        setCompetitorActive(
          club_id,
          lifecycleTarget.competitor.id,
          lifecycleTarget.active,
          key,
          reasonCode
        )
    );
    if (outcome.status === 'ignored') {
      return;
    }
    if (outcome.value != null && outcome.value.ok) {
      const active = lifecycleTarget.active;
      setLifecycleTarget(null);
      showNotification({
        color: 'green',
        message: active ? t('competitors_activate_success') : t('competitors_deactivate_success'),
      });
      await swrCompetitors.mutate();
      return;
    }
    setFailure(outcome.value != null && !outcome.value.ok ? outcome.value.failure : null);
  }

  const isFiltered = search !== '' || !includeInactive;

  return (
    <TournamentLayout tournament_id={tournamentData.id}>
      <Grid justify="space-between" align="center">
        <Grid.Col span="auto">
          <Title>{capitalize(t('competitors_title'))}</Title>
        </Grid.Col>
        <Grid.Col span="content">
          <Button
            leftSection={<IconPlus size={16} />}
            disabled={club_id == null}
            onClick={() => {
              setFailure(null);
              setCreateOpened(true);
            }}
          >
            {t('competitors_create_button')}
          </Button>
        </Grid.Col>
      </Grid>

      <Card withBorder radius="md" padding="md" mt="md">
        <Group gap="md" align="center" wrap="wrap">
          <TextInput
            placeholder={t('competitors_search_placeholder')}
            leftSection={<IconSearch size={16} />}
            value={nameFilter}
            onChange={(event) => {
              setNameFilter(event.currentTarget.value);
              setPage(1);
            }}
            w={{ base: '100%', sm: '20rem' }}
          />
          <Switch
            label={t('competitors_show_inactive')}
            checked={includeInactive}
            onChange={(event) => {
              setIncludeInactive(event.currentTarget.checked);
              setPage(1);
            }}
          />
          <Button
            variant="subtle"
            size="compact-sm"
            color="gray"
            onClick={() => setOrganizerActions((current) => !current)}
          >
            {organizerActions ? t('organizer_actions_hide') : t('organizer_actions_show')}
          </Button>
        </Group>
        {organizerActions ? (
          <Text size="xs" c="dimmed" mt="xs">
            {t('organizer_actions_note')}
          </Text>
        ) : null}
      </Card>

      {swrTournamentResponse.error != null ? (
        <F3ErrorAlert failure={describeF3Failure(swrTournamentResponse.error)} />
      ) : null}
      {swrCompetitors.error != null ? (
        <F3ErrorAlert failure={describeF3Failure(swrCompetitors.error)} />
      ) : null}
      <F3ErrorAlert failure={failure} />

      {partialFailure != null ? (
        <Card withBorder radius="md" padding="md" mt="md" data-create-partial-failure>
          <Text fw={600}>{t('f3_competitor_create_partial_title')}</Text>
          <Text size="sm" mt="xs">
            {t('f3_competitor_create_partial_failure', { name: partialFailure.name })}
          </Text>
          <F3ErrorAlert failure={partialFailure.failure} />
          <Button variant="light" size="compact-sm" mt="xs" onClick={() => setPartialFailure(null)}>
            {t('f3_competitor_create_partial_dismiss')}
          </Button>
        </Card>
      ) : null}

      <div style={{ overflowX: 'auto' }}>
        <Table striped highlightOnHover withTableBorder mt="md" data-competitors-table>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>{t('competitors_name_column')}</Table.Th>
              <Table.Th>{t('competitors_age_column')}</Table.Th>
              <Table.Th>{t('competitors_sex_column')}</Table.Th>
              <Table.Th>{t('competitors_belt_column')}</Table.Th>
              <Table.Th>{t('competitors_club_column')}</Table.Th>
              <Table.Th>{t('competitors_state_column')}</Table.Th>
              <Table.Th>{t('competitors_created_column')}</Table.Th>
              <Table.Th>{t('competitors_actions_column')}</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {swrCompetitors.isLoading
              ? Array.from({ length: 5 }, (_, index) => (
                  <Table.Tr key={`skeleton-${index}`}>
                    <Table.Td colSpan={8}>
                      <Skeleton height={20} />
                    </Table.Td>
                  </Table.Tr>
                ))
              : competitors.map((competitor) => (
                  <Table.Tr key={competitor.id} data-competitor-row={competitor.id}>
                    <Table.Td>{competitor.display_name}</Table.Td>
                    <Table.Td data-competitor-age={competitor.id}>
                      {ageLabel(competitor.date_of_birth)}
                    </Table.Td>
                    <Table.Td data-competitor-sex={competitor.id}>
                      {sexLabel(competitor.sex)}
                    </Table.Td>
                    <Table.Td data-competitor-belt={competitor.id}>
                      {beltColumnLabel(competitor.belt_id)}
                    </Table.Td>
                    <Table.Td data-competitor-club={competitor.id}>
                      {clubColumnLabel(competitor)}
                    </Table.Td>
                    <Table.Td>
                      <Badge
                        color={competitor.active ? 'green' : 'gray'}
                        variant="light"
                        title={competitor.active ? 'active' : 'inactive'}
                      >
                        {competitor.active
                          ? t('competitors_state_active')
                          : t('competitors_state_inactive')}
                      </Badge>
                    </Table.Td>
                    <Table.Td>
                      <DateTime datetime={competitor.created} />
                    </Table.Td>
                    <Table.Td>
                      <Group gap="xs">
                        <Button
                          variant="light"
                          size="compact-sm"
                          onClick={() => {
                            setFailure(null);
                            setRenameTarget(competitor);
                          }}
                        >
                          {t('competitors_rename_button')}
                        </Button>
                        <Button
                          variant="light"
                          size="compact-sm"
                          onClick={() => {
                            setFailure(null);
                            setProfileTarget(competitor);
                          }}
                        >
                          {t('competitors_profile_button')}
                        </Button>
                        {organizerActions ? (
                          <Button
                            variant="light"
                            size="compact-sm"
                            color={competitor.active ? 'red' : 'green'}
                            onClick={() => {
                              setFailure(null);
                              setLifecycleTarget({
                                competitor,
                                active: !competitor.active,
                              });
                            }}
                          >
                            {competitor.active
                              ? t('competitors_deactivate_button')
                              : t('competitors_activate_button')}
                          </Button>
                        ) : null}
                      </Group>
                    </Table.Td>
                  </Table.Tr>
                ))}
          </Table.Tbody>
        </Table>
      </div>

      {!swrCompetitors.isLoading && competitors.length === 0 ? (
        <Stack align="center" mt="md">
          <NoContent
            title={
              isFiltered ? t('competitors_empty_filtered_title') : t('competitors_empty_title')
            }
            description={t('competitors_empty_description')}
          />
        </Stack>
      ) : null}

      {pages > 1 ? (
        <Group justify="center" mt="md">
          <Pagination value={page} onChange={setPage} total={pages} />
        </Group>
      ) : null}

      <CompetitorCreateWizard
        opened={createOpened}
        onClose={() => setCreateOpened(false)}
        onSubmit={handleCreate}
        belts={belts}
        clubs={sportsClubs}
        inflight={inflight}
        error={failure}
      />

      <CompetitorModal
        opened={renameTarget != null}
        onClose={() => setRenameTarget(null)}
        onSubmit={handleRename}
        title={t('competitors_rename_title')}
        description={t('competitors_rename_description')}
        confirmLabel={t('competitors_rename_button')}
        initialName={renameTarget != null ? renameTarget.display_name : ''}
        inflight={inflight}
        error={failure}
      />

      <ReasonModal
        opened={lifecycleTarget != null}
        onClose={() => setLifecycleTarget(null)}
        title={
          lifecycleTarget != null && lifecycleTarget.active
            ? t('competitors_activate_title')
            : t('competitors_deactivate_title')
        }
        description={
          lifecycleTarget != null && lifecycleTarget.active
            ? t('competitors_activate_description')
            : t('competitors_deactivate_description')
        }
        confirmLabel={
          lifecycleTarget != null && lifecycleTarget.active
            ? t('competitors_activate_button')
            : t('competitors_deactivate_button')
        }
        color={lifecycleTarget != null && lifecycleTarget.active ? 'green' : 'red'}
        reasonCodes={COMPETITOR_LIFECYCLE_REASON_CODES}
        inflight={inflight}
        error={failure}
        onSubmit={handleLifecycle}
      />

      <CompetitorSportsProfile
        opened={profileTarget != null}
        onClose={() => setProfileTarget(null)}
        club_id={club_id}
        canManageAcademy={organizerActions}
        competitor={profileTarget}
        onSaved={() => {
          void swrCompetitors.mutate();
        }}
      />
    </TournamentLayout>
  );
}
