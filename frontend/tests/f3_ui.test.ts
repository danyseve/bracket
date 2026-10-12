/**
 * Integración de la UI de S4.1 (F3 core): rutas, navegación, cableado de las páginas del
 * organizador y el tablero de tatamis derivado.
 *
 * Los componentes React no se montan (no hay DOM): se comprueba lo que decide el comportamiento y
 * que es verificable sin navegador — el routing declarado, los endpoints que llama cada pantalla,
 * las guardas de UX (botón deshabilitado, doble clic) y la derivación pura del tablero. La
 * derivación de cuadros y tatamis sí se ejerce de verdad, con datos de entrada reales.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
  boardStageItems,
  bracketRows,
  bracketStateKey,
} from '../src/components/utils/bracket_view.ts';
import {
  buildTatamiOverview,
  hasStageItemCourtContract,
  TATAMI_ASSIGNMENT_CONTRACT,
  TATAMI_ASSIGNMENT_GRANULARITY,
  tatamiStateKey,
  tatamiSubtitle,
  tatamiTitle,
} from '../src/components/utils/tatami_board.ts';

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8');
}

const MAIN = '../src/main.tsx';
const LINKS = '../src/components/navbar/_main_links.tsx';
const COMPETITORS_PAGE = '../src/pages/tournaments/[id]/competitors.tsx';
const REGISTRATIONS_PAGE = '../src/pages/tournaments/[id]/registrations.tsx';
const BRACKETS_PAGE = '../src/pages/tournaments/[id]/brackets.tsx';
const TATAMIS_PAGE = '../src/pages/tournaments/[id]/tatamis.tsx';
const TATAMI_VIEW = '../src/components/f3/tatami_board_view.tsx';
const READINESS_PANEL = '../src/components/f3/readiness_panel.tsx';
const WINDOW_CARD = '../src/components/f3/registration_window_card.tsx';
const ERROR_ALERT = '../src/components/f3/f3_error_alert.tsx';
const SERVICES = '../src/services/f3.ts';

/* ------------------------------------------------------------------------------- routing (§3) */

test('las cuatro pantallas nuevas son rutas del router existente', () => {
  const main = source(MAIN);

  for (const [route, page] of [
    ['competitors', 'CompetitorsPage'],
    ['registrations', 'RegistrationsPage'],
    ['brackets', 'BracketsPage'],
    ['tatamis', 'TatamisPage'],
  ]) {
    assert.match(
      main,
      new RegExp(`path="${route}"\\s+element=\\{<${page} />\\}`),
      `falta la ruta ${route} en main.tsx`
    );
    assert.match(
      main,
      new RegExp(`import ${page} from './pages/tournaments/\\[id\\]/${route}'`),
      `falta el import de ${page}`
    );
  }

  // Las rutas cuelgan del layout de torneo que ya existía: no se crea un router paralelo.
  const tournamentBlock = main.slice(main.indexOf('<Route path="/tournaments">'));
  assert.match(tournamentBlock, /<Route path=":id">/);
  assert.equal(main.match(/<Routes>/g)?.length, 1, 'solo debe haber un <Routes>');
  // Un único router montado (apertura y cierre) y ningún router paralelo.
  assert.equal(main.match(/<BrowserRouter>/g)?.length, 1, 'un único <BrowserRouter> montado');
  assert.doesNotMatch(
    main,
    /createBrowserRouter|RouterProvider|<HashRouter>|<MemoryRouter>/,
    'sin router paralelo'
  );
});

/* ---------------------------------------------------------------------------- navegación (§4) */

test('la navegación del torneo enlaza las cuatro pantallas sin duplicar enlaces', () => {
  const links = source(LINKS);
  const tournamentLinks = links.slice(links.indexOf('export function TournamentLinks'));

  const expected: [string, string][] = [
    ['competitors', 'competitors_title'],
    ['registrations', 'registrations_title'],
    ['brackets', 'brackets_title'],
    ['tatamis', 'tatamis_title'],
  ];

  for (const [route, label] of expected) {
    assert.match(
      tournamentLinks,
      new RegExp(`link: \`\\$\\{tm_prefix\\}/${route}\``),
      `falta el enlace a ${route}`
    );
    assert.match(tournamentLinks, new RegExp(`t\\('${label}'\\)`), `falta la etiqueta ${label}`);
  }

  const hrefs = [...tournamentLinks.matchAll(/link: `\$\{tm_prefix\}([^`]*)`/g)].map(
    (match) => match[1]
  );
  assert.equal(new Set(hrefs).size, hrefs.length, 'hay enlaces duplicados en TournamentLinks');
  assert.ok(hrefs.includes('/players'), 'no se debe perder el enlace existente de jugadores');
});

/* ---------------------------------------------------------------------------- competidores (§8) */

test('la pantalla de competidores cubre alta, listado, renombrado y ciclo de vida', () => {
  const competitors = source(COMPETITORS_PAGE);

  for (const call of [
    'getCompetitors',
    'createCompetitor',
    'renameCompetitor',
    'setCompetitorActive',
  ]) {
    assert.match(competitors, new RegExp(`${call}\\(`), `competidores no usa ${call}`);
  }

  // Alta como INDEPENDENT: el club representado no se inventa desde la interfaz.
  assert.doesNotMatch(competitors, /sports_club_id/, 'la UI no debe enviar sports_club_id');
  assert.match(competitors, /f3_registration_representation_notice|competitors_create_description/);

  // La identidad del listado sale del backend, sin estado paralelo (la query se compone en el servicio).
  assert.match(competitors, /getCompetitors\(club_id/);
  assert.match(competitors, /include_inactive/);
});

test('la pantalla de competidores respeta la autorización del servidor y no la adivina', () => {
  const competitors = source(COMPETITORS_PAGE);

  // No hay heurística de rol: las acciones se ofrecen y el 403 se maneja con el error real.
  assert.match(competitors, /organizerActions|OrganizerOnly/);
  assert.match(competitors, /describeF3Failure\(/);
  assert.doesNotMatch(competitors, /isOwner|role ===|localStorage\.getItem\('role'\)/);
});

/* --------------------------------------------------------------------------- inscripciones (§9) */

test('la pantalla de inscripciones usa la FSM del contrato, no una copia propia', () => {
  const registrations = source(REGISTRATIONS_PAGE);

  assert.match(registrations, /allowedTransitions\(/);
  assert.match(registrations, /getBracketReadiness\(/);
  for (const call of ['getRegistrations', 'createRegistration', 'changeRegistrationState']) {
    assert.match(registrations, new RegExp(`${call}\\(`), `inscripciones no usa ${call}`);
  }

  // Ninguna lista de acciones cableada a mano: las transiciones válidas salen del contrato.
  assert.doesNotMatch(
    registrations,
    /\['confirm',\s*'withdraw',\s*'reinstate',\s*'disqualify'\]/,
    'la FSM no debe duplicarse en la página'
  );

  // El motivo de auditoría se elige de un catálogo cerrado del contrato.
  assert.match(registrations, /REGISTRATION_REASON_CODES/);
});

test('cada acción de ciclo de vida viaja con su alcance de idempotencia', () => {
  const registrations = source(REGISTRATIONS_PAGE);
  const competitors = source(COMPETITORS_PAGE);

  assert.match(registrations, /actionScope\(\[/);
  assert.match(competitors, /actionScope\(\[/);
  assert.match(registrations, /transition\.action/);

  // Doble clic: el botón se bloquea mientras la escritura está en vuelo.
  assert.match(registrations, /inflight=\{inflight\}/);
  assert.match(competitors, /inflight=\{inflight\}/);
});

/* ------------------------------------------------------------------------------------- ventana */

test('la ventana muestra estado, fija plazo y cierra — y avisa de que cerrar no genera el cuadro', () => {
  const card = source(WINDOW_CARD);

  assert.match(card, /setRegistrationDeadline\(/);
  assert.match(card, /closeRegistrations\(/);
  assert.match(card, /registration_window_close_vs_generate_notice/);
  assert.match(card, /windowAdmitsNewRegistrations\(|admitsNew/);
  assert.match(card, /RegistrationWindowBadge|registrationWindowKey/);
  assert.match(card, /inflight/);

  const registrations = source(REGISTRATIONS_PAGE);
  assert.match(registrations, /RegistrationWindowCard/);
});

/* ----------------------------------------------------------------------------------- readiness */

test('el panel de preparación traduce los bloqueos del backend y conserva el código técnico', () => {
  const panel = source(READINESS_PANEL);

  assert.match(panel, /readinessBlockerKey\(/);
  // La lectura la hace la página (SWR) y el panel la recibe: un único origen de datos.
  assert.match(panel, /swrReadiness/);
  assert.match(panel, /readiness\.ready/);
  assert.match(panel, /category_not_enough|readiness_minimum_entrants/);
});

test('generar brackets está deshabilitado sin readiness, pero el backend sigue mandando', () => {
  const panel = source(READINESS_PANEL);

  assert.match(panel, /generateTournamentBrackets\(/);
  assert.match(panel, /disabled=\{!readiness\.ready \|\| inflight\}/);
  assert.match(
    panel,
    /readiness_generate_server_authority_notice|readiness_generate_hint_not_ready/
  );
});

test('409 BRACKET_NOT_READY refresca la revisión en vez de reintentar a ciegas', () => {
  const panel = source(READINESS_PANEL);
  const notReadyBlock = panel.slice(panel.indexOf('isBracketNotReady'));

  assert.match(panel, /isBracketNotReady/);
  assert.match(notReadyBlock, /mutate\(\)/, 'tras un 409 debe recalcularse la preparación');
  assert.doesNotMatch(panel, /setTimeout|setInterval|for \(;;\)/, 'nada de reintentos automáticos');
});

test('una generación sin cuadros que repartir no se pinta como reparto ni como error', () => {
  const panel = source(READINESS_PANEL);

  // La post-condición `generated` es del torneo, no de la llamada: sin estructura de fases la
  // respuesta es 200 con `stage_items` vacío y la UI lo explica aparte.
  assert.match(panel, /stage_items\.length/);
  assert.match(panel, /skipped_stage_items/);
  assert.match(panel, /readiness_generation_empty/);
  assert.match(panel, /readiness_generation_success/);
});

/* ------------------------------------------------------------------------------ write disabled */

test('503 F3_WRITE_DISABLED se explica con copy de producto y sin reintento automático', () => {
  const alert = source(ERROR_ALERT);

  assert.match(alert, /failure\.messageKey/);
  assert.match(alert, /failure\.technicalCode/);
  assert.match(alert, /f3_error_technical_code/);
  assert.match(alert, /blockingIssues/);
  assert.doesNotMatch(
    alert,
    /setTimeout|setInterval|location\.reload/,
    'sin reintentos ni recargas'
  );

  // El texto de "edición deshabilitada" vive en los locales, no en el componente.
  assert.doesNotMatch(alert, /temporarily disabled|deshabilitad[ao]s?\./i);
});

/* ------------------------------------------------------------------------------- tatamis (§12) */

test('el contrato asigna el cuadro entero y se reconoce por el dato publicado (S4.2)', () => {
  assert.equal(TATAMI_ASSIGNMENT_GRANULARITY, 'stage_item');
  assert.equal(TATAMI_ASSIGNMENT_CONTRACT, 'stage_item.court_id');
  assert.equal(hasStageItemCourtContract([{ court_id: null }]), true, 'null ya es contrato');
  assert.equal(hasStageItemCourtContract([{ court_id: 10 }]), true);
  assert.equal(
    hasStageItemCourtContract([{ id: 1 }]),
    false,
    'un backend anterior no publica el campo'
  );
});

test('el tablero deriva los cuadros de cada tatami desde los combates', () => {
  const courts = [
    { id: 10, name: 'Tatami 1' },
    { id: 11, name: 'Tatami A' },
    { id: 12, name: 'Tatami 3' },
    { id: 13, name: 'Tatami 4' },
  ];
  const stageItems = [
    {
      id: 1,
      name: 'Adulto Azul',
      type: 'single_elimination',
      stage_id: 100,
      stage_name: 'Eliminatoria',
      matches: [
        {
          id: 1,
          court_id: 10,
          start_time: '2026-10-12T09:00:00',
          position_in_schedule: 1,
          stage_item_input1_score: 0,
          stage_item_input2_score: 0,
          input1_team_id: null,
          input2_team_id: null,
        },
        {
          id: 2,
          court_id: 10,
          start_time: null,
          position_in_schedule: 2,
          stage_item_input1_score: 0,
          stage_item_input2_score: 0,
          input1_team_id: null,
          input2_team_id: null,
        },
      ],
    },
    {
      id: 2,
      name: 'Infantil',
      type: 'single_elimination',
      stage_id: 100,
      stage_name: 'Eliminatoria',
      matches: [
        {
          id: 3,
          court_id: 11,
          start_time: '2026-10-12T09:00:00',
          position_in_schedule: 1,
          stage_item_input1_score: 4,
          stage_item_input2_score: 2,
          input1_team_id: null,
          input2_team_id: null,
        },
        {
          id: 4,
          court_id: 12,
          start_time: '2026-10-12T09:00:00',
          position_in_schedule: 2,
          stage_item_input1_score: 0,
          stage_item_input2_score: 0,
          input1_team_id: null,
          input2_team_id: null,
        },
      ],
    },
    {
      id: 3,
      name: 'Sin generar',
      type: 'single_elimination',
      stage_id: 100,
      stage_name: 'Eliminatoria',
      matches: [],
    },
    {
      id: 4,
      name: 'Sin tatami',
      type: 'single_elimination',
      stage_id: 100,
      stage_name: 'Eliminatoria',
      matches: [
        {
          id: 5,
          start_time: '2026-10-12T09:00:00',
          position_in_schedule: 1,
          stage_item_input1_score: 0,
          stage_item_input2_score: 0,
          input1_team_id: null,
          input2_team_id: null,
        },
      ],
    },
  ];

  const overview = buildTatamiOverview(courts, stageItems);

  assert.equal(overview.tatamis.length, 4);
  assert.equal(overview.tatamis[0].title, 'Tatami 1');
  assert.equal(overview.tatamis[0].match_count, 2);
  assert.equal(
    overview.tatamis[0].state,
    'partially_planned',
    'un combate sin hora es plan parcial'
  );
  assert.equal(overview.tatamis[0].categories.length, 1);
  assert.equal(overview.tatamis[0].categories[0].complete, true);
  assert.equal(overview.tatamis[0].categories[0].played_count, 0);
  assert.equal(overview.tatamis[0].categories[0].stage_name, 'Eliminatoria');

  assert.equal(overview.tatamis[1].state, 'running', 'un combate con marcador se está jugando');
  assert.equal(overview.tatamis[1].categories[0].complete, false, 'el cuadro está repartido');
  assert.equal(overview.tatamis[2].state, 'planned', 'todo colocado con hora es plan completo');
  assert.equal(overview.tatamis[3].state, 'free', 'un tatami sin combates está libre');
  assert.deepEqual(overview.tatamis[3].categories, []);

  // El cuadro repartido entre dos tatamis se marca: el planificador no lo hace, el contrato sí.
  assert.deepEqual(overview.shared_category_ids, [2]);

  // Un cuadro con combates sin tatami se lista aparte; el que no tiene combates todavía no existe.
  assert.deepEqual(
    overview.unassigned.map((category) => [category.stage_item_name, category.generated]),
    [['Sin tatami', true]]
  );
});

test('un tatami sin nombre propio no repite el título genérico', () => {
  assert.equal(tatamiTitle(0), 'Tatami 1');
  assert.equal(tatamiSubtitle(0, 'Tatami 1'), null);
  assert.equal(tatamiSubtitle(0, '  '), null);
  assert.equal(tatamiSubtitle(1, 'Tatami Central'), 'Tatami Central');
  assert.equal(tatamiStateKey('partially_planned'), 'tatami_state_partially_planned');
});

test('la pantalla de tatamis no ofrece asignación por cuadro ni drag&drop', () => {
  const page = source(TATAMIS_PAGE);
  const view = source(TATAMI_VIEW);

  for (const [name, code] of [
    ['tatamis.tsx', page],
    ['tatami_board_view.tsx', view],
  ]) {
    assert.doesNotMatch(code, /drag|DndContext|Drop/, `${name} no debe implementar drag&drop`);
    assert.doesNotMatch(
      code,
      /\.put\(|\.patch\(|\.post\(/,
      `${name} no debe escribir asignaciones`
    );
    assert.doesNotMatch(
      code,
      /stage_items\/\$\{[^}]+\}\/court/,
      `${name} no debe llamar a un endpoint inexistente`
    );
  }

  // El alta de tatamis reutiliza el modal que ya existía.
  assert.match(page, /CourtModal/);
  assert.match(view, /tatami_assignment_granularity_notice/);
});

/* -------------------------------------------------------------------------------- cuadros (§12) */

/** Un `stage` con la forma que devuelve `GET tournaments/{id}/stages`. */
function stageWith(stageItems: unknown[]) {
  return { id: 100, name: 'Eliminatoria', stage_items: stageItems } as never;
}

function elimItem(id: number, name: string, matches: unknown[]) {
  return { id, name, type: 'single_elimination', rounds: [{ id: id * 10, matches }] };
}

function match(
  id: number,
  input1: number | null,
  input2: number | null,
  scores: [number, number] = [0, 0],
  court_id: number | null = null
) {
  return {
    id,
    court_id,
    start_time: null,
    position_in_schedule: id,
    stage_item_input1: input1 == null ? null : { team_id: input1 },
    stage_item_input2: input2 == null ? null : { team_id: input2 },
    stage_item_input1_score: scores[0],
    stage_item_input2_score: scores[1],
  };
}

test('el listado de cuadros se deriva de stages con estado de avance explícito', () => {
  const stages = [
    stageWith([
      elimItem(1, 'Adulto Azul', [match(1, 11, 12, [0, 0], 10), match(2, 13, null)]),
      elimItem(2, 'Infantil', [match(3, 21, 22, [3, 0], 11)]),
      elimItem(3, 'Sin generar', []),
    ]),
  ];

  const rows = bracketRows(stages);
  const byItem = new Map(rows.map((row) => [row.stage_item_id, row]));

  assert.deepEqual(
    rows.map((row) => row.category_name),
    ['Adulto Azul', 'Infantil', 'Sin generar'],
    'las filas van ordenadas por categoría'
  );

  const adulto = byItem.get(1)!;
  assert.equal(adulto.entrant_count, 3, 'la cuenta de participantes son equipos distintos');
  assert.equal(adulto.match_count, 2);
  assert.equal(adulto.played_count, 0);
  assert.equal(adulto.state, 'generated');
  assert.deepEqual(adulto.court_ids, [10], 'el tatami se deriva de los combates');

  const infantil = byItem.get(2)!;
  assert.equal(infantil.state, 'finished');
  assert.equal(infantil.played_count, 1);
  assert.deepEqual(infantil.court_ids, [11]);

  const sinGenerar = byItem.get(3)!;
  assert.equal(sinGenerar.state, 'not_generated');
  assert.equal(sinGenerar.entrant_count, 0);
  assert.deepEqual(sinGenerar.court_ids, []);

  assert.equal(bracketStateKey('not_generated'), 'bracket_state_not_generated');
  assert.equal(bracketRows([]).length, 0);
});

test('el tablero comparte el adaptador de stages con el listado de cuadros', () => {
  const stages = [stageWith([elimItem(1, 'Adulto', [match(1, 11, 12, [0, 0], 10)])])];

  const boards = boardStageItems(stages);
  assert.equal(boards.length, 1);
  assert.equal(boards[0].stage_name, 'Eliminatoria');
  assert.equal(boards[0].matches.length, 1);
  assert.equal(boards[0].matches[0].court_id, 10);
});

/* ------------------------------------------------------------------------------------ servicio */

test('las escrituras del servicio son idempotentes y las lecturas no lo son', () => {
  const services = source(SERVICES);

  const callSites = [...services.matchAll(/createAxios\(\)\.(get|post|put|patch|delete)\(/g)];
  const writes = callSites.filter(([, method]) => method !== 'get');
  const reads = [...services.matchAll(/useSWR\(/g)];
  const axiosGets = [...services.matchAll(/createAxios\(\)\.get\(/g)];

  assert.ok(writes.length >= 5, 'se esperaban las escrituras de F3');
  assert.ok(reads.length >= 4, 'las lecturas de F3 van por SWR');
  assert.ok(axiosGets.length >= 1, 'la lectura por id usa el fetcher compartido');

  const bodyOf = (index: number) => {
    const next = callSites.find((other) => other.index !== undefined && other.index > index);
    return services.slice(index, next?.index ?? services.length);
  };

  for (const match of writes) {
    const index = match.index!;
    assert.match(
      bodyOf(index),
      /idempotentHeaders\(key\)/,
      `la escritura ${match[1]} debe llevar Idempotency-Key`
    );
  }
  for (const match of axiosGets) {
    const index = match.index!;
    const line = services.slice(index, services.indexOf('\n', index));
    assert.doesNotMatch(line, /idempotentHeaders/, 'una lectura no debe llevar Idempotency-Key');
  }
  assert.match(services, /IDEMPOTENCY_KEY_HEADER/);
  assert.match(services, /idempotentHeaders\(key: string\)/);
});
