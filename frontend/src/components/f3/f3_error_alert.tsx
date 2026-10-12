import { Alert, Code, List, Stack, Text } from '@mantine/core';
import { IconAlertTriangle, IconInfoCircle } from '@tabler/icons-react';
import { useTranslation } from 'react-i18next';

import { readinessBlockerKey } from '@components/utils/f3_contract';
import { F3Failure } from '@components/utils/f3_errors';

interface BlockerLike {
  code?: unknown;
  count?: unknown;
  required_minimum?: unknown;
}

function blockersOf(failure: F3Failure): BlockerLike[] {
  return failure.blockingIssues.filter(
    (issue): issue is BlockerLike => issue != null && typeof issue === 'object'
  );
}

/**
 * Error de F3 en lenguaje humano.
 *
 * El mensaje sale siempre de una clave de traducción: nunca se pinta el `detail` en crudo del
 * servidor (ni SQL, ni nombres de restricción, ni trazas). El código estable se muestra **en
 * secundario** para que soporte pueda buscarlo, y los bloqueos derivados de un rechazo por revisión
 * se listan para que el organizador sepa qué arreglar.
 */
export default function F3ErrorAlert({ failure }: { failure: F3Failure | null }) {
  const { t } = useTranslation();

  if (failure == null) {
    return null;
  }

  const isDisabled = failure.kind === 'write_disabled';
  const blockers = blockersOf(failure);

  return (
    <Alert
      icon={isDisabled ? <IconInfoCircle size={22} /> : <IconAlertTriangle size={22} />}
      color={isDisabled ? 'yellow' : 'red'}
      variant="light"
      radius="md"
      title={t(failure.messageKey)}
      mt="sm"
    >
      <Stack gap="xs">
        {blockers.length > 0 ? (
          <List size="sm" spacing={2}>
            {blockers.map((blocker, index) => (
              <List.Item key={`${String(blocker.code)}-${index}`}>
                {t(readinessBlockerKey(String(blocker.code)), {
                  count: typeof blocker.count === 'number' ? blocker.count : 0,
                  required_minimum:
                    typeof blocker.required_minimum === 'number'
                      ? blocker.required_minimum
                      : undefined,
                })}
              </List.Item>
            ))}
          </List>
        ) : null}

        {failure.retryAfterSeconds != null ? (
          <Text size="xs">{t('f3_error_retry_after', { seconds: failure.retryAfterSeconds })}</Text>
        ) : null}

        {failure.technicalCode != null ? (
          <Text size="xs" c="dimmed">
            {t('f3_error_technical_code')} <Code>{failure.technicalCode}</Code>
          </Text>
        ) : null}
      </Stack>
    </Alert>
  );
}
