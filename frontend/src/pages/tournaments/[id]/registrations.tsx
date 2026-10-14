import {
  Button,
  Card,
  Grid,
  Group,
  Modal,
  Pagination,
  Select,
  Skeleton,
  Stack,
  Table,
  Text,
  Title,
} from '@mantine/core';
import { showNotification } from '@mantine/notifications';
import { IconPlus } from '@tabler/icons-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import F3ErrorAlert from '@components/f3/f3_error_alert';
import ReadinessPanel from '@components/f3/readiness_panel';
import ReasonModal from '@components/f3/reason_modal';
import RegistrationClassificationPanel from '@components/f3/registration_classification_panel';
import RegistrationWindowCard from '@components/f3/registration_window_card';
import { RegistrationStatusBadge } from '@components/f3/status_badge';
import RegistrationModal, { SelectOption } from '@components/modals/registration_modal';
import { NoContent } from '@components/no_content/empty_table_info';
import { tournamentCategoryOptions } from '@components/utils/f3_category_catalog';
import {
  REGISTRATION_REASON_CODES,
  Registration,
  RegistrationAction,
  RegistrationCreateBody,
  RegistrationStatus,
  allowedTransitions,
  categoryDisplayName,
  registrationActionKey,
  windowAdmitsNewRegistrations,
} from '@components/utils/f3_contract';
import { F3Failure, describeF3Failure } from '@components/utils/f3_errors';
import {
  CompetitorSummaryContext,
  CompetitorSummarySource,
  competitorOptionLabel,
} from '@components/utils/f3_registration_options';
import { beltLabel, sexLabelKey, toIsoDate } from '@components/utils/f3_sports_profile';
import { actionScope } from '@components/utils/f3_write';
import useF3Write from '@components/utils/f3_write_hook';
import { capitalize, getTournamentIdFromRouter } from '@components/utils/util';
import TournamentLayout from '@pages/tournaments/_tournament_layout';
import { getStages, getTournamentById } from '@services/adapter';
import {
  changeRegistrationState,
  createRegistration,
  getBelts,
  getBracketReadiness,
  getCompetitors,
  getRegistrationWindow,
  getRegistrations,
  previewRegistrationClassification,
} from '@services/f3';

const PAGE_SIZE = 25;

/**
 * Tope de competidores del desplegable de inscripción. El backend valida `limit <= 100`
 * (`bracket/utils/pagination.py:50`): pedir 200 devolvía 422 y dejaba el desplegable vacío.
 */
const COMPETITOR_PAGE_SIZE = 100;

const STATUS_FILTERS: RegistrationStatus[] = ['DRAFT', 'CONFIRMED', 'WITHDRAWN', 'DISQUALIFIED'];

/**
 * Inscripciones del torneo (S4.1 §5-§9).
 *
 * Reúne las tres piezas del flujo: la ventana de inscripción, la preparación de los cuadros y el
 * listado de inscripciones con las transiciones que el backend permite. Cada acción viaja con su
 * `Idempotency-Key` y el botón queda bloqueado mientras está en vuelo.
 */
export default function RegistrationsPage() {
  const { t } = useTranslation();
  const { tournamentData } = getTournamentIdFromRouter();
  const swrTournamentResponse = getTournamentById(tournamentData.id);
  const club_id =
    swrTournamentResponse.data != null ? swrTournamentResponse.data.data.club_id : null;

  const [statusFilter, setStatusFilter] = useState<RegistrationStatus | null>(null);
  const [page, setPage] = useState(1);
  const [organizerActions, setOrganizerActions] = useState(false);
  const [failure, setFailure] = useState<F3Failure | null>(null);
  const [createOpened, setCreateOpened] = useState(false);
  const [classificationTarget, setClassificationTarget] = useState<Registration | null>(null);
  const [actionTarget, setActionTarget] = useState<{
    registration: Registration;
    action: RegistrationAction;
  } | null>(null);

  const { run, inflight } = useF3Write();

  const swrRegistrations = getRegistrations(tournamentData.id, {
    registration_status: statusFilter ?? undefined,
    limit: PAGE_SIZE,
    offset: (page - 1) * PAGE_SIZE,
  });
  const swrWindow = getRegistrationWindow(tournamentData.id);
  const swrReadiness = getBracketReadiness(tournamentData.id);
  // Catálogo real de categorías del torneo (stage_items). La readiness NO sirve para esto: sólo
  // agrupa las inscripciones que ya existen, así que un torneo sin inscripciones publica [].
  const swrStages = getStages(tournamentData.id);
  const swrBelts = getBelts(club_id);
  const swrActiveCompetitors = getCompetitors(club_id, {
    include_inactive: false,
    limit: COMPETITOR_PAGE_SIZE,
    offset: 0,
  });

  const registrations = swrRegistrations.data != null ? swrRegistrations.data.registrations : [];
  const count = swrRegistrations.data != null ? swrRegistrations.data.count : 0;
  const pages = count > 0 ? Math.ceil(count / PAGE_SIZE) : 1;
  const windowState = swrWindow.data != null ? swrWindow.data.state : null;
  const canRegister = windowState != null && windowAdmitsNewRegistrations(windowState);

  // El desplegable de competidores muestra nombre · cinturón · edad derivada (§6). El cinturón sale
  // del catálogo del club y la edad se calcula en el cliente: ninguno de los dos se envía.
  const today = toIsoDate(new Date());
  const beltsById = new Map((swrBelts.data ?? []).map((belt) => [belt.id, belt]));
  const lookup = (key: string): string | null => {
    const translated = t(key);
    return translated === key ? null : translated;
  };

  const competitorOptions: SelectOption[] =
    swrActiveCompetitors.data != null
      ? swrActiveCompetitors.data.competitors.map((competitor) => ({
          value: String(competitor.id),
          label: competitorOptionLabel(competitor, {
            referenceDate: today,
            beltLabel: (beltId) => {
              const belt = beltsById.get(beltId);
              return belt == null ? null : beltLabel(belt, lookup);
            },
            ageLabel: (age) => t('f3_profile_age_value', { age }),
            independentLabel: t('f3_club_independent_option'),
          }),
        }))
      : [];

  // Resumen del competidor elegido en la modal (§4.2): mismos traductores que la etiqueta, más el
  // nombre del sexo. La afiliación sale de la proyección del listado; si no viene, no se pinta.
  const competitorSummaries: CompetitorSummarySource[] =
    swrActiveCompetitors.data != null ? swrActiveCompetitors.data.competitors : [];

  const summaryContext: CompetitorSummaryContext = {
    referenceDate: today,
    beltLabel: (beltId) => {
      const belt = beltsById.get(beltId);
      return belt == null ? null : beltLabel(belt, lookup);
    },
    ageLabel: (age) => t('f3_profile_age_value', { age }),
    independentLabel: t('f3_club_independent_option'),
    sexLabel: (sex) => t(sexLabelKey(sex)),
  };

  const categoryOptions: SelectOption[] = tournamentCategoryOptions(
    swrStages.data != null ? swrStages.data.data : null
  ).map((category) => ({ value: category.category_key, label: category.label }));

  // Mismas categorías reales, con la **clave contractual** (`si:<stage_item_id>`) como valor: es
  // lo que compara el motor para decidir compatibilidad y lo que acepta el override (§16).
  const classificationCategoryOptions: SelectOption[] = categoryOptions;

  async function refreshAll() {
    await Promise.all([
      swrRegistrations.mutate(),
      swrReadiness.mutate(),
      swrWindow.mutate(),
      swrStages.mutate(),
      swrActiveCompetitors.mutate(),
    ]);
  }

  async function handleCreate(body: RegistrationCreateBody) {
    setFailure(null);
    const outcome = await run(
      actionScope([
        'registration-create',
        tournamentData.id,
        body.competitor_id,
        body.category_key,
      ]),
      (key) => createRegistration(tournamentData.id, body, key)
    );
    if (outcome.status === 'ignored') {
      return;
    }
    if (outcome.value != null && outcome.value.ok) {
      setCreateOpened(false);
      showNotification({ color: 'green', message: t('registrations_create_success') });
      await refreshAll();
      return;
    }
    setFailure(outcome.value != null && !outcome.value.ok ? outcome.value.failure : null);
  }

  async function handleAction(reasonCode: string, reasonNote: string | null) {
    if (actionTarget == null) {
      return;
    }
    setFailure(null);
    const outcome = await run(
      actionScope([
        'registration-action',
        actionTarget.registration.id,
        actionTarget.action,
        actionTarget.registration.revision,
      ]),
      (key) =>
        changeRegistrationState(
          tournamentData.id,
          actionTarget.registration.id,
          actionTarget.action,
          { reason_code: reasonCode, reason_note: reasonNote },
          key
        )
    );
    if (outcome.status === 'ignored') {
      return;
    }
    if (outcome.value != null && outcome.value.ok) {
      const action = actionTarget.action;
      setActionTarget(null);
      showNotification({ color: 'green', message: t(`registration_action_success_${action}`) });
      await refreshAll();
      return;
    }
    setFailure(outcome.value != null && !outcome.value.ok ? outcome.value.failure : null);
  }

  return (
    <TournamentLayout tournament_id={tournamentData.id}>
      <Grid justify="space-between" align="center">
        <Grid.Col span="auto">
          <Title>{capitalize(t('registrations_title'))}</Title>
        </Grid.Col>
        <Grid.Col span="content">
          <Button
            leftSection={<IconPlus size={16} />}
            disabled={!canRegister}
            onClick={() => {
              setFailure(null);
              setCreateOpened(true);
            }}
            title={canRegister ? undefined : t('registrations_create_needs_open_window')}
          >
            {t('registrations_create_button')}
          </Button>
        </Grid.Col>
      </Grid>

      {!canRegister && windowState != null ? (
        <Text size="sm" c="dimmed" mt="xs">
          {t('registrations_create_needs_open_window')}
        </Text>
      ) : null}

      <Stack gap="md" mt="md">
        <RegistrationWindowCard
          tournament_id={tournamentData.id}
          swrWindow={swrWindow}
          onChanged={refreshAll}
        />

        <ReadinessPanel
          tournament_id={tournamentData.id}
          swrReadiness={swrReadiness}
          onChanged={refreshAll}
        />
      </Stack>

      <Card withBorder radius="md" padding="md" mt="md">
        <Group gap="md" align="center" wrap="wrap">
          <Text fw={600}>{t('registrations_list_title')}</Text>
          <Select
            clearable
            placeholder={t('registrations_filter_placeholder')}
            value={statusFilter}
            onChange={(value) => {
              setStatusFilter((value as RegistrationStatus) ?? null);
              setPage(1);
            }}
            data={STATUS_FILTERS.map((status) => ({
              value: status,
              label: t(`registration_status_${status.toLowerCase()}`),
            }))}
            w={{ base: '100%', sm: '14rem' }}
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

      {swrRegistrations.error != null ? (
        <F3ErrorAlert failure={describeF3Failure(swrRegistrations.error)} />
      ) : null}
      <F3ErrorAlert failure={failure} />

      <div style={{ overflowX: 'auto' }}>
        <Table striped highlightOnHover withTableBorder mt="md" data-registrations-table>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>{t('registrations_competitor_column')}</Table.Th>
              <Table.Th>{t('registrations_category_column')}</Table.Th>
              <Table.Th>{t('registrations_state_column')}</Table.Th>
              <Table.Th>{t('registrations_actions_column')}</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {swrRegistrations.isLoading
              ? Array.from({ length: 5 }, (_, index) => (
                  <Table.Tr key={`skeleton-${index}`}>
                    <Table.Td colSpan={4}>
                      <Skeleton height={20} />
                    </Table.Td>
                  </Table.Tr>
                ))
              : registrations.map((registration) => {
                  const transitions = allowedTransitions(registration.status);
                  const visible = transitions.filter(
                    (transition) => !transition.requires_owner || organizerActions
                  );
                  return (
                    <Table.Tr key={registration.id} data-registration-row={registration.id}>
                      <Table.Td>{registration.competitor_name_snapshot}</Table.Td>
                      <Table.Td>
                        {categoryDisplayName(
                          registration.category_key,
                          registration.category_label
                        )}
                      </Table.Td>
                      <Table.Td>
                        <RegistrationStatusBadge status={registration.status} />
                      </Table.Td>
                      <Table.Td>
                        <Group gap="xs">
                          <Button
                            variant="subtle"
                            size="compact-sm"
                            color="gray"
                            onClick={() => setClassificationTarget(registration)}
                          >
                            {t('classification_open_button')}
                          </Button>
                          {visible.length === 0 ? (
                            <Text size="xs" c="dimmed">
                              {t('registrations_no_actions')}
                            </Text>
                          ) : (
                            visible.map((transition) => (
                              <Button
                                key={transition.action}
                                variant="light"
                                size="compact-sm"
                                color={
                                  transition.action === 'disqualify'
                                    ? 'red'
                                    : transition.action === 'reinstate' ||
                                        transition.action === 'confirm'
                                      ? 'green'
                                      : 'orange'
                                }
                                onClick={() => {
                                  setFailure(null);
                                  setActionTarget({
                                    registration,
                                    action: transition.action,
                                  });
                                }}
                              >
                                {t(registrationActionKey(transition.action))}
                              </Button>
                            ))
                          )}
                        </Group>
                      </Table.Td>
                    </Table.Tr>
                  );
                })}
          </Table.Tbody>
        </Table>
      </div>

      {!swrRegistrations.isLoading && registrations.length === 0 ? (
        <Stack align="center" mt="md">
          <NoContent
            title={
              statusFilter != null
                ? t('registrations_empty_filtered_title')
                : t('registrations_empty_title')
            }
            description={t('registrations_empty_description')}
          />
        </Stack>
      ) : null}

      {pages > 1 ? (
        <Group justify="center" mt="md">
          <Pagination value={page} onChange={setPage} total={pages} />
        </Group>
      ) : null}

      <RegistrationModal
        opened={createOpened}
        onClose={() => setCreateOpened(false)}
        onSubmit={handleCreate}
        competitorOptions={competitorOptions}
        competitorDetails={competitorSummaries}
        summaryContext={summaryContext}
        loadPreview={(body) => previewRegistrationClassification(tournamentData.id, body)}
        categoryOptions={categoryOptions}
        inflight={inflight}
        error={failure}
      />

      <Modal
        opened={classificationTarget != null}
        onClose={() => setClassificationTarget(null)}
        size="lg"
        title={
          classificationTarget != null
            ? `${t('classification_modal_title')} · ${classificationTarget.competitor_name_snapshot}`
            : t('classification_modal_title')
        }
      >
        {classificationTarget != null ? (
          <RegistrationClassificationPanel
            tournament_id={tournamentData.id}
            registration_id={classificationTarget.id}
            club_id={club_id}
            overrideCategoryOptions={classificationCategoryOptions}
          />
        ) : null}
      </Modal>

      <ReasonModal
        opened={actionTarget != null}
        onClose={() => setActionTarget(null)}
        title={
          actionTarget != null
            ? t('registrations_action_title', {
                action: t(registrationActionKey(actionTarget.action)),
              })
            : ''
        }
        description={
          actionTarget != null
            ? t('registrations_action_description', {
                name: actionTarget.registration.competitor_name_snapshot,
              })
            : ''
        }
        confirmLabel={
          actionTarget != null ? t(registrationActionKey(actionTarget.action)) : t('f3_confirm')
        }
        color={actionTarget != null && actionTarget.action === 'reinstate' ? 'green' : 'red'}
        reasonCodes={actionTarget != null ? REGISTRATION_REASON_CODES[actionTarget.action] : []}
        inflight={inflight}
        error={failure}
        onSubmit={handleAction}
      />
    </TournamentLayout>
  );
}
