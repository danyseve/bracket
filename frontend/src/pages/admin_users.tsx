import {
  Alert,
  Badge,
  Button,
  Container,
  Group,
  Loader,
  Modal,
  PasswordInput,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import { showNotification } from '@mantine/notifications';
import { IconAlertCircle, IconCheck, IconShieldLock } from '@tabler/icons-react';
import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';

import { createAxios, handleRequestError } from '@services/adapter';
import { tokenPresent } from '@services/local_storage';

interface AdminUser {
  id: number;
  email: string;
  name: string;
  account_type: string;
  active: boolean;
}

const EMPTY_NEW_USER = { email: '', name: '', password: '', account_type: 'REGULAR' };
const ACCOUNT_TYPES = [
  { value: 'REGULAR', label: 'REGULAR' },
  { value: 'ADMIN', label: 'ADMIN' },
];

/**
 * Administracion -> Usuarios.
 *
 * Solo accesible para administradores: el backend responde 403 al resto con la
 * dependencia ``user_is_admin``. Nunca se muestran hashes ni contrasenas.
 */
export default function AdminUsersPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [createOpened, setCreateOpened] = useState(false);
  const [newUser, setNewUser] = useState({ ...EMPTY_NEW_USER });
  const [passwordTarget, setPasswordTarget] = useState<AdminUser | null>(null);
  const [newPassword, setNewPassword] = useState('');

  const loadUsers = useCallback(async () => {
    try {
      const response = await createAxios().get('users/admin');
      setUsers(response.data.data);
      setForbidden(false);
    } catch (error: any) {
      if (error?.response?.status === 403) {
        setForbidden(true);
      } else if (error?.response?.status !== 401) {
        handleRequestError(error);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!tokenPresent()) {
      setLoading(false);
      return;
    }

    loadUsers();
  }, [loadUsers]);

  async function createUser() {
    try {
      await createAxios().post('users/admin', newUser);
      setCreateOpened(false);
      setNewUser({ ...EMPTY_NEW_USER });
      showNotification({
        color: 'green',
        title: t('admin_users_created_title'),
        icon: <IconCheck />,
        message: t('admin_users_created_hint'),
        autoClose: 10000,
      });
      await loadUsers();
    } catch (error: any) {
      handleRequestError(error);
    }
  }

  async function setActive(user: AdminUser, active: boolean) {
    try {
      await createAxios().put(`users/admin/${user.id}/active`, { active });
      await loadUsers();
    } catch (error: any) {
      handleRequestError(error);
    }
  }

  async function setAccountType(user: AdminUser, accountType: string | null) {
    if (accountType == null || accountType === user.account_type) {
      return;
    }

    try {
      await createAxios().put(`users/admin/${user.id}/account-type`, {
        account_type: accountType,
      });
      await loadUsers();
    } catch (error: any) {
      handleRequestError(error);
    }
  }

  async function resetPassword() {
    if (passwordTarget == null) {
      return;
    }

    try {
      await createAxios().put(`users/admin/${passwordTarget.id}/password`, {
        password: newPassword,
      });
      setPasswordTarget(null);
      setNewPassword('');
      showNotification({
        color: 'green',
        title: t('admin_users_password_updated'),
        icon: <IconCheck />,
        message: t('admin_users_created_hint'),
        autoClose: 10000,
      });
    } catch (error: any) {
      handleRequestError(error);
    }
  }

  if (loading) {
    return (
      <Container size="lg" my="md">
        <Loader />
      </Container>
    );
  }

  if (forbidden) {
    return (
      <Container size="lg" my="md">
        <Alert icon={<IconAlertCircle size={16} />} color="red" radius="md">
          {t('admin_users_only_admins')}
        </Alert>
        <Button mt="md" variant="default" onClick={() => navigate('/')}>
          {t('back_to_login_nav')}
        </Button>
      </Container>
    );
  }

  return (
    <Container size="lg" my="md">
      <Group justify="space-between" mb="md">
        <Title order={2}>
          <Group gap="xs">
            <IconShieldLock size={24} />
            {t('admin_nav_label')} &rarr; {t('admin_users_title')}
          </Group>
        </Title>
        <Button onClick={() => setCreateOpened(true)}>{t('admin_users_create')}</Button>
      </Group>
      <Text c="dimmed" size="sm" mb="md">
        {t('admin_users_subtitle')}
      </Text>

      <Table striped highlightOnHover withTableBorder>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>{t('admin_users_col_id')}</Table.Th>
            <Table.Th>{t('admin_users_col_name')}</Table.Th>
            <Table.Th>{t('admin_users_col_email')}</Table.Th>
            <Table.Th>{t('admin_users_col_type')}</Table.Th>
            <Table.Th>{t('admin_users_col_status')}</Table.Th>
            <Table.Th>{t('admin_users_col_actions')}</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {users.map((user) => (
            <Table.Tr key={user.id}>
              <Table.Td>{user.id}</Table.Td>
              <Table.Td>{user.name}</Table.Td>
              <Table.Td>{user.email}</Table.Td>
              <Table.Td>
                <Select
                  data={ACCOUNT_TYPES}
                  value={user.account_type}
                  onChange={(value) => setAccountType(user, value)}
                  w={140}
                />
              </Table.Td>
              <Table.Td>
                <Badge color={user.active ? 'green' : 'red'}>
                  {user.active ? t('admin_users_active') : t('admin_users_inactive')}
                </Badge>
              </Table.Td>
              <Table.Td>
                <Group gap="xs">
                  <Button
                    size="xs"
                    variant={user.active ? 'light' : 'filled'}
                    color={user.active ? 'red' : 'green'}
                    onClick={() => setActive(user, !user.active)}
                  >
                    {user.active ? t('admin_users_deactivate') : t('admin_users_activate')}
                  </Button>
                  <Button size="xs" variant="default" onClick={() => setPasswordTarget(user)}>
                    {t('admin_users_reset_password')}
                  </Button>
                </Group>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>

      <Modal
        opened={createOpened}
        onClose={() => setCreateOpened(false)}
        title={t('admin_users_create_title')}
      >
        <Stack>
          <TextInput
            label={t('email_input_label')}
            required
            value={newUser.email}
            onChange={(event) => setNewUser({ ...newUser, email: event.currentTarget.value })}
          />
          <TextInput
            label={t('name_input_label')}
            required
            value={newUser.name}
            onChange={(event) => setNewUser({ ...newUser, name: event.currentTarget.value })}
          />
          <PasswordInput
            label={t('password_input_label')}
            required
            value={newUser.password}
            onChange={(event) => setNewUser({ ...newUser, password: event.currentTarget.value })}
          />
          <Select
            label={t('admin_users_col_type')}
            data={ACCOUNT_TYPES}
            value={newUser.account_type}
            onChange={(value) => setNewUser({ ...newUser, account_type: value ?? 'REGULAR' })}
          />
          <Button onClick={createUser}>{t('admin_users_create')}</Button>
        </Stack>
      </Modal>

      <Modal
        opened={passwordTarget != null}
        onClose={() => setPasswordTarget(null)}
        title={`${t('admin_users_password_title')}: ${passwordTarget?.email ?? ''}`}
      >
        <Stack>
          <PasswordInput
            label={t('password_input_label')}
            required
            value={newPassword}
            onChange={(event) => setNewPassword(event.currentTarget.value)}
          />
          <Button onClick={resetPassword}>{t('admin_users_reset_password')}</Button>
        </Stack>
      </Modal>
    </Container>
  );
}
