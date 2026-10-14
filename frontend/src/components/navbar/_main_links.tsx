import { Center, Divider, Group, Text, Tooltip, UnstyledButton } from '@mantine/core';
import {
  Icon,
  IconBook,
  IconBrackets,
  IconBrandGithub,
  IconBrowser,
  IconBuildingStore,
  IconCalendar,
  IconClipboardList,
  IconDots,
  IconEye,
  IconEyeOff,
  IconHome,
  IconLayoutGrid,
  IconScoreboard,
  IconSettings,
  IconShieldLock,
  IconTournament,
  IconTrophy,
  IconUser,
  IconUsers,
  IconUsersGroup,
} from '@tabler/icons-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation } from 'react-router';

import PreloadLink from '@components/utils/link';
import { capitalize } from '@components/utils/util';
import { getBaseApiUrl } from '@services/adapter';
import { isAdmin } from '@services/local_storage';
import classes from './_main_links.module.css';

interface MainLinkProps {
  icon: Icon;
  label: string;
  link: string;
  links?: MainLinkProps[] | null;
}

function MainLinkMobile({ item, pathName }: { item: MainLinkProps; pathName: String }) {
  return (
    <>
      <UnstyledButton
        hiddenFrom="sm"
        component={PreloadLink}
        href={item.link}
        className={classes.mobileLink}
        style={{ width: '100%' }}
        data-active={pathName === item.link || undefined}
      >
        <Group className={classes.mobileLinkGroup}>
          <item.icon stroke={1.5} />
          <p style={{ marginLeft: '0.5rem' }}>{item.label}</p>
        </Group>
        <Divider />
      </UnstyledButton>
    </>
  );
}

function MainLink({ item, pathName }: { item: MainLinkProps; pathName: String }) {
  return (
    <>
      <Tooltip position="right" label={item.label} transitionProps={{ duration: 0 }}>
        <UnstyledButton
          visibleFrom="sm"
          component={PreloadLink}
          href={item.link}
          className={classes.link}
          data-active={pathName.startsWith(item.link) || undefined}
        >
          <item.icon stroke={1.5} />
        </UnstyledButton>
      </Tooltip>
      <MainLinkMobile item={item} pathName={pathName} />
    </>
  );
}

export function getBaseLinksDict() {
  const { t } = useTranslation();

  const links = [
    { link: '/clubs', label: capitalize(t('f3_organizations_title')), links: [], icon: IconUsers },
    { link: '/', label: capitalize(t('tournaments_title')), links: [], icon: IconHome },
    {
      link: '/user',
      label: t('user_title'),
      links: [],
      icon: IconUser,
    },
    {
      icon: IconDots,
      link: '',
      label: t('more_title'),
      links: [
        { link: 'https://docs.bracketapp.nl/', label: t('website_title'), icon: IconBrowser },
        {
          link: 'https://github.com/evroon/bracket',
          label: t('github_title'),
          icon: IconBrandGithub,
        },
        { link: `${getBaseApiUrl()}/docs`, label: t('api_docs_title'), icon: IconBook },
      ],
    },
  ];

  // Gestion de usuarios: solo para administradores (el backend devuelve 403 al
  // resto, la ocultacion es solo de interfaz).
  if (isAdmin()) {
    links.push({
      icon: IconShieldLock,
      link: '',
      label: t('admin_nav_label'),
      links: [{ link: '/admin/users', label: t('admin_users_title'), icon: IconUsers }],
    });
  }

  return links;
}

export function getBaseLinks() {
  const location = useLocation();
  const { t } = useTranslation();
  const pathName = location.pathname.replace(/\/+$/, '');
  const links = getBaseLinksDict().filter((link) => link.links.length < 1);

  if (isAdmin()) {
    links.push({
      link: '/admin/users',
      label: capitalize(t('admin_users_title')),
      links: [],
      icon: IconShieldLock,
    });
  }

  return links.map((link) => <MainLinkMobile key={link.label} item={link} pathName={pathName} />);
}

/**
 * Botón que muestra u oculta las secciones legacy del torneo (S4.8g §3).
 *
 * El menú del torneo enseña el flujo BJJ recomendado; jugadores y equipos siguen existiendo (rutas y
 * componentes intactos, deep links incluidos) pero se piden explícitamente, y con la marca `legacy`
 * para que no parezcan el camino recomendado.
 */
function MainLinkToggle({
  label,
  active,
  onClick,
  icon: Icon,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
  icon: Icon;
}) {
  return (
    <>
      <Tooltip position="right" label={label} transitionProps={{ duration: 0 }}>
        <UnstyledButton
          visibleFrom="sm"
          className={classes.link}
          onClick={onClick}
          data-advanced-toggle
          data-active={active || undefined}
        >
          <Icon stroke={1.5} />
        </UnstyledButton>
      </Tooltip>
      <UnstyledButton
        hiddenFrom="sm"
        className={classes.mobileLink}
        style={{ width: '100%' }}
        onClick={onClick}
        data-advanced-toggle
      >
        <Group className={classes.mobileLinkGroup}>
          <Icon stroke={1.5} />
          <p style={{ marginLeft: '0.5rem' }}>{label}</p>
        </Group>
        <Divider />
      </UnstyledButton>
    </>
  );
}

export function TournamentLinks({ tournament_id }: any) {
  const location = useLocation();
  const { t } = useTranslation();
  const tm_prefix = `/tournaments/${tournament_id}`;
  const pathName = location.pathname.replace('[id]', tournament_id).replace(/\/+$/, '');

  // Las secciones del motor legacy (jugadores y equipos) se conservan: sus rutas y componentes siguen
  // intactos y accesibles por URL, pero no forman parte del flujo recomendado, así que solo aparecen
  // si el organizador las pide (§3).
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const data = [
    {
      icon: IconTrophy,
      label: capitalize(t('stage_title')),
      link: `${tm_prefix}/stages`,
    },
    {
      icon: IconUsersGroup,
      label: capitalize(t('competitors_title')),
      link: `${tm_prefix}/competitors`,
    },
    {
      icon: IconBuildingStore,
      label: capitalize(t('f3_academies_title')),
      link: `${tm_prefix}/academies`,
    },
    {
      icon: IconClipboardList,
      label: capitalize(t('registrations_title')),
      link: `${tm_prefix}/registrations`,
    },
    {
      icon: IconTournament,
      label: capitalize(t('brackets_title')),
      link: `${tm_prefix}/brackets`,
    },
    {
      icon: IconLayoutGrid,
      label: capitalize(t('tatamis_title')),
      link: `${tm_prefix}/tatamis`,
    },
    {
      icon: IconCalendar,
      label: capitalize(t('planning_title')),
      link: `${tm_prefix}/schedule`,
    },
    {
      icon: IconBrackets,
      label: capitalize(t('results_title')),
      link: `${tm_prefix}/results`,
    },
    {
      icon: IconScoreboard,
      label: capitalize(t('rankings_title')),
      link: `${tm_prefix}/rankings`,
    },
    {
      icon: IconSettings,
      label: capitalize(t('tournament_setting_title')),
      link: `${tm_prefix}/settings`,
    },
  ];

  if (advancedOpen) {
    data.push(
      {
        icon: IconUser,
        label: `${capitalize(t('players_title'))} (${t('f3_nav_legacy_badge')})`,
        link: `${tm_prefix}/players`,
      },
      {
        icon: IconUsers,
        label: `${capitalize(t('teams_title'))} (${t('f3_nav_legacy_badge')})`,
        link: `${tm_prefix}/teams`,
      }
    );
  }

  const links = data.map((link) => <MainLink key={link.label} item={link} pathName={pathName} />);
  return (
    <>
      <Center hiddenFrom="sm">
        <h2>{capitalize(t('tournament_title'))}</h2>
      </Center>
      <Divider hiddenFrom="sm" />
      {links}
      <Divider mt="xs" />
      <MainLinkToggle
        label={advancedOpen ? t('f3_nav_advanced_hide') : t('f3_nav_advanced_show')}
        active={advancedOpen}
        onClick={() => setAdvancedOpen((current) => !current)}
        icon={advancedOpen ? IconEyeOff : IconEye}
      />
      {advancedOpen ? (
        <Center hiddenFrom="sm">
          <Text size="xs" c="dimmed">
            {t('f3_nav_legacy_section')}
          </Text>
        </Center>
      ) : null}
    </>
  );
}
