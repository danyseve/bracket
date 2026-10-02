import { showNotification } from '@mantine/notifications';
import { IconCheck } from '@tabler/icons-react';
import { NavigateFunction } from 'react-router';

import { Translator } from '@components/utils/types';

const ACCOUNT_TYPE_KEY = 'account_type';

export function performLogout() {
  localStorage.removeItem('login');
  localStorage.removeItem(ACCOUNT_TYPE_KEY);
}

export function performLogoutAndRedirect(t: Translator, navigate: NavigateFunction) {
  performLogout();

  showNotification({
    color: 'green',
    title: t('logout_success_title'),
    icon: <IconCheck />,
    message: '',
    autoClose: 10000,
  });
  navigate('/login', { replace: true });
}

export function getLogin() {
  const login = localStorage.getItem('login');
  return login != null ? JSON.parse(login) : {};
}

export function tokenPresent() {
  return localStorage.getItem('login') != null;
}

/**
 * Tipo de cuenta del usuario con sesion iniciada, cacheado para poder mostrar u
 * ocultar la navegacion de administracion sin esperar a una peticion. La
 * autorizacion real la decide siempre el backend (dependencia user_is_admin).
 * No es un secreto y nunca contiene credenciales.
 */
export function setAccountType(accountType: string | null | undefined) {
  if (accountType == null) {
    localStorage.removeItem(ACCOUNT_TYPE_KEY);
    return;
  }

  localStorage.setItem(ACCOUNT_TYPE_KEY, accountType);
}

export function getAccountType(): string | null {
  return localStorage.getItem(ACCOUNT_TYPE_KEY);
}

export function isAdmin() {
  return getAccountType() === 'ADMIN';
}
