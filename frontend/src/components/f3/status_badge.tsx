import { Badge, Code, Group } from '@mantine/core';
import { useTranslation } from 'react-i18next';

import { registrationStatusKey, registrationWindowKey } from '@components/utils/f3_contract';
import { TatamiState, tatamiStateKey } from '@components/utils/tatami_board';

const STATUS_COLORS: Record<string, string> = {
  DRAFT: 'gray',
  CONFIRMED: 'green',
  WITHDRAWN: 'orange',
  DISQUALIFIED: 'red',
  CORRECTED: 'blue',
};

const WINDOW_COLORS: Record<string, string> = {
  OPEN: 'green',
  DEADLINE_PASSED: 'orange',
  CLOSED: 'red',
};

const TATAMI_COLORS: Record<TatamiState, string> = {
  free: 'gray',
  planned: 'blue',
  partially_planned: 'yellow',
  running: 'green',
};

/**
 * Etiqueta de estado en español para la persona que organiza. El código del backend viaja en
 * secundario (`title`) para soporte, nunca como dato principal.
 */
export function RegistrationStatusBadge({ status }: { status: string }) {
  const { t } = useTranslation();
  return (
    <Badge color={STATUS_COLORS[status] ?? 'gray'} variant="light" title={status} radius="sm">
      {t(registrationStatusKey(status))}
    </Badge>
  );
}

export function RegistrationWindowBadge({ state }: { state: string }) {
  const { t } = useTranslation();
  return (
    <Badge color={WINDOW_COLORS[state] ?? 'gray'} variant="filled" title={state} radius="sm">
      {t(registrationWindowKey(state))}
    </Badge>
  );
}

export function TatamiStateBadge({ state }: { state: TatamiState }) {
  const { t } = useTranslation();
  return (
    <Badge color={TATAMI_COLORS[state]} variant="light" radius="sm">
      {t(tatamiStateKey(state))}
    </Badge>
  );
}

/** Código técnico en secundario, para que soporte pueda reproducir el caso. */
export function TechnicalCode({ code }: { code: string | null }) {
  const { t } = useTranslation();
  if (code == null) {
    return null;
  }
  return (
    <Group gap={4} wrap="nowrap">
      <span>{t('f3_error_technical_code')}</span>
      <Code>{code}</Code>
    </Group>
  );
}
