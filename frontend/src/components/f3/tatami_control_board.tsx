import {
  Alert,
  Badge,
  Button,
  Card,
  Group,
  Menu,
  ScrollArea,
  Stack,
  Text,
  Title,
} from '@mantine/core';
import { IconArrowsExchange, IconInfoCircle, IconX } from '@tabler/icons-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  BoardCourt,
  ControlCard,
  ControlColumn,
  TatamiControlBoard,
  assignmentTargets,
  categoryStateKey,
  courtLabel,
  needsReassignmentConfirmation,
} from '@components/utils/tatami_board';

/**
 * Tablero de control de tatamis (S4.3 §1-§14).
 *
 * Una columna por tatami real del torneo más la bandeja de «sin asignar». El organizador mueve
 * cuadros enteros: el backend propaga el tatami a todos sus combates. Sin arrastrar y soltar todavía
 * (§4): cada tarjeta ofrece los tatamis disponibles.
 *
 * El componente no decide nada del dominio: recibe el tablero ya construido, avisa de la intención
 * (`onAssign`) y pinta el resultado que le pasa la pantalla. La fuente de verdad es el estado
 * publicado por el backend, que la pantalla relee tras cada cambio.
 */

export interface TatamiControlBoardProps {
  board: TatamiControlBoard;
  courts: BoardCourt[];
  /** Escritura en vuelo: deshabilita los controles (el doble clic no debe llegar al servicio). */
  inflight: boolean;
  /** Tatami destino pendiente de confirmación, para pintarlo mientras se pregunta. */
  onAssign: (card: ControlCard, target: BoardCourt | null) => void;
}

function CardField({ label, value }: { label: string; value: string | number }) {
  return (
    <Stack gap={0}>
      <Text size="xs" c="dimmed">
        {label}
      </Text>
      <Text size="sm" fw={600}>
        {value}
      </Text>
    </Stack>
  );
}

function ControlCardView({
  card,
  courts,
  inflight,
  onAssign,
}: {
  card: ControlCard;
  courts: BoardCourt[];
  inflight: boolean;
  onAssign: TatamiControlBoardProps['onAssign'];
}) {
  const { t } = useTranslation();
  const [confirmTarget, setConfirmTarget] = useState<BoardCourt | null>(null);
  const targets = assignmentTargets(courts, card.court_id);
  const nextMatch = card.next_match_position ?? card.next_match_id;

  const chooseTarget = (court: BoardCourt) => {
    if (needsReassignmentConfirmation(card.court_id, court.id)) {
      // Reasignar mueve combates ya planificados: se pregunta antes de enviarlo (§5).
      setConfirmTarget(court);
      return;
    }
    onAssign(card, court);
  };

  return (
    <Card withBorder radius="md" padding="sm" data-stage-item-card={card.stage_item_id}>
      <Stack gap="xs">
        <Group justify="space-between" align="flex-start" wrap="nowrap">
          <Stack gap={0}>
            <Text fw={600}>{card.category_name}</Text>
            <Text size="xs" c="dimmed">
              {card.stage_name}
            </Text>
          </Stack>
          <Badge variant="light" size="sm">
            {t(categoryStateKey(card.state))}
          </Badge>
        </Group>

        <Group gap="lg">
          <CardField label={t('tatami_participants_label')} value={card.participants} />
          <CardField
            label={t('tatami_matches_label')}
            value={`${card.played_count}/${card.match_count}`}
          />
        </Group>

        {confirmTarget != null ? (
          <Stack gap={4} data-reassign-confirm={card.stage_item_id}>
            <Text size="xs">
              {t('tatami_reassign_confirm', {
                from: courtLabel(courts, card.court_id, null),
                to: courtLabel(courts, confirmTarget.id, confirmTarget.name),
              })}
            </Text>
            <Group gap="xs">
              <Button
                size="xs"
                disabled={inflight}
                onClick={() => {
                  setConfirmTarget(null);
                  onAssign(card, confirmTarget);
                }}
              >
                {t('tatami_move_confirm')}
              </Button>
              <Button size="xs" variant="subtle" onClick={() => setConfirmTarget(null)}>
                {t('close_button')}
              </Button>
            </Group>
          </Stack>
        ) : (
          <Group gap="xs" wrap="wrap">
            <Menu shadow="md" width={240} disabled={inflight || targets.length === 0}>
              <Menu.Target>
                <Button
                  size="xs"
                  variant="light"
                  disabled={inflight || targets.length === 0}
                  leftSection={<IconArrowsExchange size={14} />}
                  data-assign-menu={card.stage_item_id}
                >
                  {card.court_id == null ? t('tatami_assign_action') : t('tatami_move_action')}
                </Button>
              </Menu.Target>
              <Menu.Dropdown>
                {targets.map((court) => (
                  <Menu.Item
                    key={court.id}
                    onClick={() => {
                      chooseTarget(court);
                    }}
                  >
                    {card.court_id == null
                      ? t('tatami_assign_to', { court: courtLabel(courts, court.id, court.name) })
                      : t('tatami_move_to', { court: courtLabel(courts, court.id, court.name) })}
                  </Menu.Item>
                ))}
              </Menu.Dropdown>
            </Menu>

            {card.court_id != null ? (
              <Button
                size="xs"
                variant="subtle"
                color="red"
                disabled={inflight}
                leftSection={<IconX size={14} />}
                data-unassign-button={card.stage_item_id}
                onClick={() => {
                  onAssign(card, null);
                }}
              >
                {t('tatami_remove_action')}
              </Button>
            ) : null}
          </Group>
        )}

        {nextMatch != null ? (
          <Text size="xs" c="dimmed">
            {t('tatami_next_match_label')} #{nextMatch}
          </Text>
        ) : null}
      </Stack>
    </Card>
  );
}

function BoardColumn({
  column,
  courts,
  inflight,
  onAssign,
}: {
  column: ControlColumn;
  courts: BoardCourt[];
  inflight: boolean;
  onAssign: TatamiControlBoardProps['onAssign'];
}) {
  const { t } = useTranslation();
  const title = column.kind === 'unassigned' ? t('tatami_unassigned_title') : column.title;

  return (
    <Card
      withBorder
      radius="md"
      padding="sm"
      data-column={column.court_id ?? 'unassigned'}
      style={{ minWidth: 320, flex: '1 1 320px' }}
    >
      <Stack gap="xs">
        <Group justify="space-between" align="flex-start">
          <Stack gap={0}>
            <Title order={5}>{title}</Title>
            {column.subtitle != null ? (
              <Text size="xs" c="dimmed">
                {column.subtitle}
              </Text>
            ) : null}
          </Stack>
          <Group gap="lg">
            <CardField label={t('tatami_bracket_count_label')} value={column.bracket_count} />
            <CardField label={t('tatami_matches_label')} value={column.match_count} />
          </Group>
        </Group>

        {column.cards.length === 0 ? (
          <Text size="sm" c="dimmed">
            {t('tatami_no_brackets')}
          </Text>
        ) : (
          column.cards.map((card) => (
            <ControlCardView
              key={card.stage_item_id}
              card={card}
              courts={courts}
              inflight={inflight}
              onAssign={onAssign}
            />
          ))
        )}
      </Stack>
    </Card>
  );
}

export default function TatamiControlBoardView({
  board,
  courts,
  inflight,
  onAssign,
}: TatamiControlBoardProps) {
  const { t } = useTranslation();
  const derived = board.assignment_source !== 'stage_item';

  return (
    <Stack gap="md">
      <Alert
        icon={<IconInfoCircle size={22} />}
        color={derived ? 'yellow' : 'blue'}
        variant="light"
        title={t('tatami_assignment_title')}
        data-assignment-source={board.assignment_source}
      >
        <Text size="sm">
          {derived ? t('tatami_control_legacy_notice') : t('tatami_control_notice')}
        </Text>
      </Alert>

      <ScrollArea type="auto" offsetScrollbars>
        <Group align="flex-start" gap="md" wrap="nowrap" pb="sm" data-tatami-control-board>
          {board.columns.map((column) => (
            <BoardColumn
              key={column.court_id ?? 'unassigned'}
              column={column}
              courts={courts}
              inflight={inflight}
              onAssign={onAssign}
            />
          ))}
        </Group>
      </ScrollArea>
    </Stack>
  );
}
