import { Alert, Badge, Card, Group, List, SimpleGrid, Stack, Text, Title } from '@mantine/core';
import { IconInfoCircle } from '@tabler/icons-react';
import { useTranslation } from 'react-i18next';

import { TatamiStateBadge } from '@components/f3/status_badge';
import { TatamiOverview, tatamiSubtitle } from '@components/utils/tatami_board';

/**
 * Tablero de tatamis (S4.1 §12-§14).
 *
 * Representa el dominio tal como está: el tatami cuelga del **combate**, no del cuadro. El panel
 * declara en pantalla el hueco en vez de fingir una asignación por cuadro que el backend no guarda.
 */
export default function TatamiBoardView({ overview }: { overview: TatamiOverview }) {
  const { t } = useTranslation();

  return (
    <Stack gap="md">
      <Alert
        icon={<IconInfoCircle size={22} />}
        color="blue"
        variant="light"
        title={t('tatami_assignment_title')}
      >
        <Stack gap={4}>
          <Text size="sm">{t('tatami_assignment_granularity_notice')}</Text>
          <Text size="sm">{t('tatami_assignment_gap_notice')}</Text>
        </Stack>
      </Alert>

      <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }} spacing="md">
        {overview.tatamis.map((tatami, index) => {
          const subtitle = tatamiSubtitle(index, tatami.court_name);
          return (
            <Card key={tatami.court_id} withBorder radius="md" padding="md">
              <Stack gap="xs">
                <Group justify="space-between" align="center">
                  <Stack gap={0}>
                    <Title order={4}>{tatami.title}</Title>
                    {subtitle != null ? (
                      <Text size="xs" c="dimmed">
                        {subtitle}
                      </Text>
                    ) : null}
                  </Stack>
                  <TatamiStateBadge state={tatami.state} />
                </Group>

                <Group gap="lg">
                  <Stack gap={0}>
                    <Text size="xs" c="dimmed">
                      {t('tatami_matches_label')}
                    </Text>
                    <Text fw={600}>{tatami.match_count}</Text>
                  </Stack>
                  <Stack gap={0}>
                    <Text size="xs" c="dimmed">
                      {t('tatami_scheduled_label')}
                    </Text>
                    <Text fw={600}>{tatami.placed_count}</Text>
                  </Stack>
                  <Stack gap={0}>
                    <Text size="xs" c="dimmed">
                      {t('tatami_played_label')}
                    </Text>
                    <Text fw={600}>{tatami.played_count}</Text>
                  </Stack>
                </Group>

                {tatami.categories.length === 0 ? (
                  <Text size="sm" c="dimmed">
                    {t('tatami_no_categories')}
                  </Text>
                ) : (
                  <List size="sm" spacing={2}>
                    {tatami.categories.map((category) => (
                      <List.Item key={category.stage_item_id}>
                        {category.stage_item_name}{' '}
                        <Text span size="xs" c="dimmed">
                          ({category.placed_count}/{category.match_count})
                        </Text>{' '}
                        {category.complete ? (
                          <Badge color="green" variant="light" size="xs">
                            {t('tatami_category_complete')}
                          </Badge>
                        ) : (
                          <Badge color="yellow" variant="light" size="xs">
                            {t('tatami_category_partial')}
                          </Badge>
                        )}
                      </List.Item>
                    ))}
                  </List>
                )}
              </Stack>
            </Card>
          );
        })}
      </SimpleGrid>

      <Card withBorder radius="md" padding="md">
        <Stack gap="xs">
          <Title order={4}>{t('tatami_unassigned_title')}</Title>
          {overview.unassigned.length === 0 ? (
            <Text size="sm" c="dimmed">
              {t('tatami_unassigned_empty')}
            </Text>
          ) : (
            <List size="sm" spacing={2}>
              {overview.unassigned.map((category) => (
                <List.Item key={category.stage_item_id}>
                  {category.stage_item_name} —{' '}
                  {category.generated
                    ? t('tatami_unassigned_matches', { count: category.match_count })
                    : t('tatami_unassigned_not_generated')}{' '}
                  <Text span size="xs" c="dimmed">
                    {category.stage_name}
                  </Text>
                </List.Item>
              ))}
            </List>
          )}
        </Stack>
      </Card>
    </Stack>
  );
}
