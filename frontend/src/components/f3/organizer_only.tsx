import { Button, Collapse, Group, Paper, Text } from '@mantine/core';
import { IconShieldLock } from '@tabler/icons-react';
import { ReactNode, useState } from 'react';
import { useTranslation } from 'react-i18next';

/**
 * Acciones reservadas al organizador (relación OWNER con el club).
 *
 * El frontend **no puede saber** la relación del usuario con el club: no hay endpoint que la
 * publique hoy (hueco declarado en el informe de la fase). Ante la duda, la UI no adivina: agrupa
 * estas acciones detrás de un desplegable explícito que explica el requisito, de modo que un COLLAB
 * no las ve como acciones disponibles y un OWNER las encuentra en un clic. El backend sigue siendo la
 * autoridad: un 403 `ROLE_NOT_ALLOWED` se muestra tal cual y no se reintenta.
 */
export default function OrganizerOnly({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const [opened, setOpened] = useState(false);

  return (
    <>
      <Button
        variant="subtle"
        size="compact-sm"
        color="gray"
        leftSection={<IconShieldLock size={16} />}
        onClick={() => setOpened((current) => !current)}
      >
        {opened ? t('organizer_actions_hide') : t('organizer_actions_show')}
      </Button>
      <Collapse in={opened}>
        <Paper withBorder radius="md" p="sm" mt="xs" bg="var(--mantine-color-gray-light)">
          <Group gap="xs" align="flex-start" wrap="nowrap">
            <Text size="xs" c="dimmed">
              {t('organizer_actions_note')}
            </Text>
          </Group>
          <Group gap="xs" mt="sm" wrap="wrap">
            {children}
          </Group>
        </Paper>
      </Collapse>
    </>
  );
}
