import { Alert, Anchor, Box, Center, Container, Group, Paper, Title } from '@mantine/core';
import { IconArrowLeft, IconInfoCircle } from '@tabler/icons-react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';

import classes from './create_account.module.css';

/**
 * El registro publico esta cerrado en este despliegue: las cuentas las crea un
 * administrador. Esta pagina es informativa y no envia ninguna peticion, asi que
 * no muestra formulario, ni widget de verificacion, ni boton de envio.
 */
export default function CreateAccountPage() {
  const navigate = useNavigate();
  const { t } = useTranslation();

  return (
    <Container size={460} my={30}>
      <Title className={classes.title} ta="center">
        {t('create_account_title')}
      </Title>
      <Paper withBorder shadow="md" p={30} radius="md" mt="xl">
        <Alert
          icon={<IconInfoCircle size={16} />}
          mb={16}
          title={t('create_account_alert_title')}
          color="blue"
          radius="md"
          data-testid="create-account-disabled"
        >
          {t('create_account_alert_description')}
        </Alert>
        <Group justify="center" mt="lg">
          <Anchor c="dimmed" size="sm" className={classes.control}>
            <Center inline>
              <IconArrowLeft size={12} stroke={1.5} />
              <Box ml={5} onClick={() => navigate('/login')} data-testid="back-to-login">
                {' '}
                {t('back_to_login_nav')}
              </Box>
            </Center>
          </Anchor>
        </Group>
      </Paper>
    </Container>
  );
}
