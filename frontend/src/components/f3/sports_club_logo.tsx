import { ActionIcon, Avatar, Button, FileButton, Group, Stack, Text } from '@mantine/core';
import { showNotification } from '@mantine/notifications';
import { IconPhoto, IconTrash, IconUpload } from '@tabler/icons-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import F3ErrorAlert from '@components/f3/f3_error_alert';
import {
  CLUB_LOGO_EXTENSIONS,
  CLUB_LOGO_MAX_BYTES,
  CLUB_LOGO_MIME_TYPES,
  SportsClub,
} from '@components/utils/f3_competitor_contract';
import { F3Failure } from '@components/utils/f3_errors';
import {
  LogoLimits,
  logoFileError,
  sportsClubLogoDisplay,
} from '@components/utils/f3_sports_profile';
import { actionScope } from '@components/utils/f3_write';
import useF3Write from '@components/utils/f3_write_hook';
import { getBaseApiUrl } from '@services/adapter';
import { setSportsClubLogo } from '@services/f3';

const LOGO_LIMITS: LogoLimits = {
  maxBytes: CLUB_LOGO_MAX_BYTES,
  mimeTypes: CLUB_LOGO_MIME_TYPES,
  extensions: CLUB_LOGO_EXTENSIONS,
};

/**
 * Logo de la academia (S4.8a-4 §7).
 *
 * Pintado con **fallback**: si hay `logo_path` se muestra la imagen; si no, las iniciales del nombre
 * y, si tampoco hay nombre, un icono neutro. Nunca queda un hueco roto.
 *
 * El flujo de subir/reemplazar/quitar aparece **solo** cuando la pantalla lo permite (`canManage`);
 * entonces usa el endpoint real (`domain_sports_clubs.py`) con la misma idempotencia que el resto de
 * escrituras de F3, y valida tamaño/tipo antes de enviar (el backend vuelve a validar por contenido).
 */
export default function SportsClubLogo({
  sportsClub,
  club_id,
  canManage = false,
  compact = false,
  size = 48,
  onChanged,
}: {
  sportsClub: SportsClub | null;
  club_id?: number | null;
  canManage?: boolean;
  compact?: boolean;
  size?: number;
  onChanged?: () => void;
}) {
  const { t } = useTranslation();
  const { run, inflight } = useF3Write();
  const [failure, setFailure] = useState<F3Failure | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);

  const display = sportsClubLogoDisplay(sportsClub, getBaseApiUrl());

  const avatar = (
    <Avatar src={display.hasLogo ? display.src : null} radius="sm" size={size} alt={display.name}>
      {display.initials !== '' ? display.initials : <IconPhoto size={Math.round(size / 2)} />}
    </Avatar>
  );

  async function submitLogo(file: File | null) {
    if (sportsClub == null || club_id == null) {
      return;
    }
    if (file != null) {
      const error = logoFileError(
        { name: file.name, size: file.size, type: file.type },
        LOGO_LIMITS
      );
      if (error != null) {
        setValidationError(error);
        return;
      }
    }
    setValidationError(null);
    setFailure(null);
    const outcome = await run(
      actionScope(['sports-club-logo', sportsClub.id, file != null ? file.name : 'remove']),
      (key) => setSportsClubLogo(club_id, sportsClub.id, file, key)
    );
    if (outcome.status === 'ignored') {
      return;
    }
    if (outcome.value != null && outcome.value.ok) {
      showNotification({
        color: 'green',
        message: file != null ? t('f3_club_logo_upload_success') : t('f3_club_logo_remove_success'),
      });
      if (onChanged != null) {
        onChanged();
      }
      return;
    }
    setFailure(outcome.value != null && !outcome.value.ok ? outcome.value.failure : null);
  }

  if (!canManage || sportsClub == null || club_id == null) {
    if (compact) {
      return (
        <Group gap="xs" wrap="nowrap">
          {avatar}
          <Text size="sm">{display.name}</Text>
        </Group>
      );
    }
    return avatar;
  }

  return (
    <Stack gap="xs" data-sports-club-logo-manager>
      <Group gap="sm" wrap="nowrap" align="center">
        {avatar}
        <Stack gap={0}>
          <Text size="sm" fw={500}>
            {t('f3_club_logo_manage_title')}
          </Text>
          <Text size="xs" c="dimmed">
            {sportsClub.name}
          </Text>
        </Stack>
      </Group>
      <Group gap="xs">
        <FileButton onChange={submitLogo} accept={CLUB_LOGO_MIME_TYPES.join(',')}>
          {(props) => (
            <Button
              {...props}
              size="compact-sm"
              variant="light"
              leftSection={<IconUpload size={16} />}
              loading={inflight}
              disabled={inflight}
            >
              {display.hasLogo ? t('f3_club_logo_replace_button') : t('f3_club_logo_upload_button')}
            </Button>
          )}
        </FileButton>
        {display.hasLogo ? (
          <ActionIcon
            color="red"
            variant="light"
            onClick={() => submitLogo(null)}
            disabled={inflight}
            aria-label={t('f3_club_logo_remove_button')}
          >
            <IconTrash size={16} />
          </ActionIcon>
        ) : null}
      </Group>
      {validationError != null ? (
        <Text size="xs" c="red">
          {t(validationError)}
        </Text>
      ) : null}
      <Text size="xs" c="dimmed">
        {t('f3_club_logo_manage_note')}
      </Text>
      <F3ErrorAlert failure={failure} />
    </Stack>
  );
}
