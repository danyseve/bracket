import { Alert, Button, Group, List, Modal, Stack, Text } from '@mantine/core';
import { IconAlertTriangle, IconCircleCheck, IconSitemap } from '@tabler/icons-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { SWRResponse } from 'swr';

import {
  BracketGenerationSummary,
  countParticipants,
  getBlockerCode,
  getBracketSize,
  getExpectedByes,
  getGenerationMode,
  getResponseErrorDetail,
} from '@components/utils/generate_bracket';
import {
  StageItemInputOptionsResponse,
  StageItemWithRounds,
  StageRankingResponse,
  StagesWithStageItemsResponse,
  Tournament,
} from '@openapi';
import { generateBracket } from '@services/stage_item';

/**
 * The explicit "Generar cuadro" action of a single elimination stage item.
 *
 * It spreads the direct byes over the first round, which is why it is never triggered by assigning
 * a participant: the operator decides when the distribution is computed, sees what it would change
 * and gets the result (or the reason it was refused) in this modal.
 */
export default function GenerateBracketModal({
  tournament,
  stageItem,
  swrStagesResponse,
  swrAvailableInputsResponse,
  swrRankingsPerStageItemResponse,
}: {
  tournament: Tournament;
  stageItem: StageItemWithRounds;
  swrStagesResponse: SWRResponse<StagesWithStageItemsResponse>;
  swrAvailableInputsResponse: SWRResponse<StageItemInputOptionsResponse>;
  swrRankingsPerStageItemResponse: SWRResponse<StageRankingResponse>;
}) {
  const { t } = useTranslation();
  const [opened, setOpened] = useState(false);
  const [summary, setSummary] = useState<BracketGenerationSummary | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const mode = getGenerationMode(stageItem);
  const submitLabel =
    mode === 'regenerate' ? t('regenerate_bracket_button') : t('generate_bracket_button');

  const describeBlocker = (detail: string) => {
    const code = getBlockerCode(detail);
    return code == null ? detail : t(`generate_bracket_blocked_${code}`);
  };

  const close = () => {
    setOpened(false);
    setSummary(null);
    setErrorMessage(null);
  };

  const generate = async () => {
    setLoading(true);
    setErrorMessage(null);
    const response = await generateBracket(tournament.id, stageItem.id);
    setLoading(false);

    const detail = getResponseErrorDetail(response);
    if (detail != null) {
      setSummary(null);
      setErrorMessage(describeBlocker(detail));
      return;
    }

    setSummary((response as { data: BracketGenerationSummary }).data);
    await swrStagesResponse.mutate();
    await swrAvailableInputsResponse.mutate();
    await swrRankingsPerStageItemResponse.mutate();
  };

  return (
    <>
      <Modal opened={opened} onClose={close} title={t('generate_bracket_modal_title')} size="40rem">
        <Stack>
          <Alert color="gray" radius="lg">
            {t('generate_bracket_modal_description')}
            <List mt="sm">
              <List.Item>
                {t('generate_bracket_current_participants', {
                  count: countParticipants(stageItem),
                })}
              </List.Item>
              <List.Item>
                {t('generate_bracket_size', { count: getBracketSize(stageItem) })}
              </List.Item>
              <List.Item>
                {t('generate_bracket_expected_byes', { count: getExpectedByes(stageItem) })}
              </List.Item>
            </List>
          </Alert>

          {mode === 'regenerate' ? (
            <Alert color="orange" icon={<IconAlertTriangle size={16} />} radius="lg">
              {t('regenerate_bracket_modal_warning')}
            </Alert>
          ) : null}

          {errorMessage != null ? (
            <Alert color="red" icon={<IconAlertTriangle size={16} />} radius="lg">
              {errorMessage}
            </Alert>
          ) : null}

          {summary != null ? (
            <Alert color="green" icon={<IconCircleCheck size={16} />} radius="lg">
              <Text fw={700}>
                {summary.changed
                  ? t('generate_bracket_success_title')
                  : t('generate_bracket_no_change')}
              </Text>
              {summary.changed ? (
                <List mt="sm">
                  <List.Item>
                    {t('generate_bracket_success_participants', { count: summary.entrant_count })}
                  </List.Item>
                  <List.Item>
                    {t('generate_bracket_success_size', { count: summary.bracket_size })}
                  </List.Item>
                  <List.Item>
                    {t('generate_bracket_success_byes', { count: summary.bye_count })}
                  </List.Item>
                  <List.Item>
                    {t('generate_bracket_success_empty_matchups', { count: summary.ghost_count })}
                  </List.Item>
                </List>
              ) : null}
            </Alert>
          ) : null}

          <Group justify="flex-end">
            <Button variant="default" onClick={close}>
              {t('close_button')}
            </Button>
            <Button
              color="indigo"
              loading={loading}
              leftSection={<IconSitemap size={20} />}
              onClick={generate}
            >
              {submitLabel}
            </Button>
          </Group>
        </Stack>
      </Modal>

      <Button
        color="indigo"
        size="sm"
        variant="light"
        leftSection={<IconSitemap size={20} />}
        onClick={() => setOpened(true)}
      >
        {submitLabel}
      </Button>
    </>
  );
}
