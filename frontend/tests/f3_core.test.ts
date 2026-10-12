/**
 * Contrato F3 del frontend (P2.8C/F3C S4.1): rutas, máquina de estados, ventana, categorías,
 * errores del backend e idempotencia de las escrituras.
 *
 * Estos tests ejercen los módulos puros que la UI usa de verdad (los mismos que importan las
 * páginas): si el contrato con `bracket/` cambia, fallan aquí antes de que el organizador vea una
 * pantalla rota. No hay red ni backend: los fallos se construyen con la forma que devuelve axios.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
  allowedTransitions,
  bracketReadinessUrl,
  CATEGORY_KEY_MAX_LENGTH,
  categoryDisplayName,
  categoryKeyFromLabel,
  closeRegistrationsUrl,
  competitorActionUrl,
  competitorsQuery,
  competitorsUrl,
  competitorUrl,
  generateBracketUrl,
  IDEMPOTENCY_KEY_HEADER,
  isTerminalStatus,
  isValidCategoryKey,
  MINIMUM_ENTRANTS,
  readinessBlockerKey,
  REGISTRATION_REASON_CODES,
  REGISTRATION_STATUSES,
  registrationActionKey,
  registrationActionUrl,
  registrationsQuery,
  registrationStatusKey,
  registrationsUrl,
  registrationUrl,
  registrationWindowKey,
  registrationWindowUrl,
  sortCategories,
  windowAdmitsNewRegistrations,
} from '../src/components/utils/f3_contract.ts';
import {
  describeF3Failure,
  detailCode,
  isBracketNotReady,
  isOperationInProgress,
  isWriteDisabled,
} from '../src/components/utils/f3_errors.ts';
import {
  actionScope,
  KEY_MIN_LENGTH,
  KEY_PATTERN,
  newKey,
  resolvePending,
  WriteGate,
} from '../src/components/utils/f3_write.ts';

/** Respuesta de error con la forma de axios (lo único que ve `describeF3Failure`). */
function httpError(status: number, detail?: unknown, headers?: Record<string, unknown>) {
  return { isAxiosError: true, response: { status, headers, data: { detail } } };
}

/* --------------------------------------------------------------------------------------- rutas */

test('las rutas del contrato son las del OpenAPI de F3', () => {
  assert.equal(competitorsUrl(4), 'clubs/4/competitors');
  assert.equal(competitorUrl(4, 9), 'clubs/4/competitors/9');
  assert.equal(competitorActionUrl(4, 9, 'rename'), 'clubs/4/competitors/9/rename');
  assert.equal(competitorActionUrl(4, 9, 'deactivate'), 'clubs/4/competitors/9/deactivate');
  assert.equal(competitorActionUrl(4, 9, 'activate'), 'clubs/4/competitors/9/activate');

  assert.equal(registrationsUrl(7), 'tournaments/7/registrations');
  assert.equal(registrationUrl(7, 3), 'tournaments/7/registrations/3');
  assert.equal(registrationActionUrl(7, 3, 'confirm'), 'tournaments/7/registrations/3/confirm');
  assert.equal(
    registrationActionUrl(7, 3, 'disqualify'),
    'tournaments/7/registrations/3/disqualify'
  );
  assert.equal(closeRegistrationsUrl(7), 'tournaments/7/registrations/close');
  assert.equal(registrationWindowUrl(7), 'tournaments/7/registration-window');

  assert.equal(bracketReadinessUrl(7), 'tournaments/7/bracket-readiness');
  assert.equal(generateBracketUrl(7), 'tournaments/7/generate-bracket');
});

test('los filtros de listado viajan como el backend los espera', () => {
  const competitors = new URLSearchParams(
    competitorsQuery({ display_name: 'ana', include_inactive: true, limit: 10, offset: 20 })
  );
  assert.equal(competitors.get('display_name'), 'ana');
  assert.equal(competitors.get('include_inactive'), 'true');
  assert.equal(competitors.get('limit'), '10');
  assert.equal(competitors.get('offset'), '20');

  const defaults = new URLSearchParams(competitorsQuery({}));
  assert.equal(defaults.get('include_inactive'), 'false');
  assert.equal(defaults.get('display_name'), null);

  const registrations = new URLSearchParams(
    registrationsQuery({ registration_status: 'CONFIRMED', competitor_id: 5 })
  );
  assert.equal(registrations.get('registration_status'), 'CONFIRMED');
  assert.equal(registrations.get('competitor_id'), '5');
});

/* ------------------------------------------------------------- máquina de estados de inscripción */

test('la UI ofrece exactamente las transiciones que permite el backend', () => {
  assert.deepEqual(
    allowedTransitions('DRAFT').map((transition) => transition.action),
    ['confirm', 'withdraw']
  );
  assert.deepEqual(
    allowedTransitions('CONFIRMED').map((transition) => transition.action),
    ['withdraw', 'disqualify']
  );
  assert.deepEqual(
    allowedTransitions('WITHDRAWN').map((transition) => transition.action),
    ['reinstate']
  );
  assert.deepEqual(allowedTransitions('DISQUALIFIED'), []);
  assert.deepEqual(allowedTransitions('CORRECTED'), []);
  assert.deepEqual(allowedTransitions('ESTADO_INVENTADO'), []);
});

test('solo las acciones destructivas exigen OWNER y descalificar es terminal', () => {
  const confirmed = allowedTransitions('CONFIRMED');
  assert.ok(confirmed.every((transition) => transition.requires_owner));
  assert.ok(allowedTransitions('DRAFT').every((transition) => !transition.requires_owner));
  assert.ok(allowedTransitions('WITHDRAWN').every((transition) => !transition.requires_owner));

  assert.equal(isTerminalStatus('DISQUALIFIED'), true);
  assert.equal(isTerminalStatus('WITHDRAWN'), false);
  assert.equal(isTerminalStatus('DRAFT'), false);
});

test('cada acción de auditoría tiene su catálogo cerrado de motivos', () => {
  for (const action of ['confirm', 'withdraw', 'reinstate', 'disqualify'] as const) {
    assert.ok(REGISTRATION_REASON_CODES[action].length > 0, `${action} sin motivos`);
  }
  assert.deepEqual(REGISTRATION_REASON_CODES.confirm, ['READY', 'DATA_VERIFIED']);
  assert.ok(REGISTRATION_REASON_CODES.disqualify.includes('RULE_VIOLATION'));
});

test('la lista de estados que la UI pinta son los que el backend publica', () => {
  assert.deepEqual(REGISTRATION_STATUSES, ['DRAFT', 'CONFIRMED', 'WITHDRAWN', 'DISQUALIFIED']);
});

/* ------------------------------------------------------------------- ventana de inscripción */

test('la ventana solo admite altas nuevas en OPEN', () => {
  assert.equal(windowAdmitsNewRegistrations('OPEN'), true);
  assert.equal(windowAdmitsNewRegistrations('CLOSED'), false);
  assert.equal(windowAdmitsNewRegistrations('DEADLINE_PASSED'), false);
  assert.equal(windowAdmitsNewRegistrations('LO_QUE_SEA'), false);
});

/* -------------------------------------------------------------------------------- categorías */

test('la clave de categoría se deriva de la etiqueta visible', () => {
  assert.equal(categoryKeyFromLabel('Gi -83 kg'), 'gi-83-kg');
  assert.equal(categoryKeyFromLabel('Infantil A'), 'infantil-a');
  assert.equal(categoryKeyFromLabel('  Juvenil Ñ  '), 'juvenil-n');
  assert.equal(categoryKeyFromLabel('No-Gi / +95kg'), 'no-gi-95kg');
  assert.ok(categoryKeyFromLabel('a'.repeat(120)).length <= CATEGORY_KEY_MAX_LENGTH);
  assert.equal(isValidCategoryKey(categoryKeyFromLabel('Gi -83 kg')), true);
  assert.equal(isValidCategoryKey('Gi -83 kg'), false);
  assert.equal(isValidCategoryKey(''), false);
});

test('la persona que organiza lee siempre una etiqueta, nunca un identificador', () => {
  assert.equal(categoryDisplayName('gi-83-kg', 'Gi -83 kg'), 'Gi -83 kg');
  assert.equal(categoryDisplayName('gi-83-kg', null), 'gi-83-kg');
  assert.equal(categoryDisplayName(null, null), '');
  assert.equal(categoryDisplayName('', 'Infantil'), 'Infantil');
});

test('las categorías se ordenan por su nombre visible', () => {
  const sorted = sortCategories([
    { category_key: 'nogi-adulto', category_label: 'No-Gi Adulto' },
    { category_key: 'gi-adulto', category_label: 'Gi Adulto' },
    { category_key: 'sin-etiqueta', category_label: null },
    { category_key: 'aaa', category_label: 'Gi Adulto' },
  ]);
  assert.deepEqual(
    sorted.map((category) => category.category_key),
    // Primero por nombre visible; a igual nombre, por clave (desempate estable).
    ['aaa', 'gi-adulto', 'nogi-adulto', 'sin-etiqueta']
  );
});

/* ------------------------------------------------------------------------------- claves i18n */

test('las claves de traducción dinámicas son estables', () => {
  assert.equal(registrationStatusKey('CONFIRMED'), 'registration_status_confirmed');
  assert.equal(registrationWindowKey('DEADLINE_PASSED'), 'registration_window_deadline_passed');
  assert.equal(registrationActionKey('withdraw'), 'registration_action_withdraw');
  assert.equal(
    readinessBlockerKey('CATEGORY_NOT_ENOUGH_COMPETITORS'),
    'readiness_blocker_category_not_enough_competitors'
  );
});

test('el mínimo de participantes del copy es el del backend', () => {
  assert.equal(MINIMUM_ENTRANTS, 2);
});

/* ----------------------------------------------------------------------------------- errores */

test('503 F3_WRITE_DISABLED se reconoce y no se confunde con una caída', () => {
  const failure = describeF3Failure(httpError(503, 'F3_WRITE_DISABLED'));

  assert.equal(failure.kind, 'write_disabled');
  assert.equal(failure.messageKey, 'f3_error_write_disabled');
  assert.equal(failure.technicalCode, 'F3_WRITE_DISABLED');
  assert.equal(isWriteDisabled(failure), true);
  assert.equal(isBracketNotReady(failure), false);
  assert.equal(isOperationInProgress(failure), false);
});

test('409 BRACKET_NOT_READY conserva los bloqueos que devuelve el backend', () => {
  const failure = describeF3Failure(
    httpError(409, {
      code: 'BRACKET_NOT_READY',
      blocking_issues: [{ code: 'REGISTRATION_WINDOW_OPEN' }, { code: 'COMPETITOR_INACTIVE' }],
    })
  );

  assert.equal(failure.kind, 'bracket_not_ready');
  assert.equal(failure.messageKey, 'f3_error_bracket_not_ready');
  assert.equal(failure.technicalCode, 'BRACKET_NOT_READY');
  assert.equal(isBracketNotReady(failure), true);
  assert.equal(failure.blockingIssues.length, 2);

  assert.deepEqual(describeF3Failure(httpError(409, 'BRACKET_NOT_READY')).blockingIssues, []);
});

test('409 OPERATION_IN_PROGRESS y IDEMPOTENCY_KEY_REUSED se distinguen', () => {
  const inProgress = describeF3Failure(httpError(409, 'OPERATION_IN_PROGRESS'));
  assert.equal(inProgress.kind, 'operation_in_progress');
  assert.equal(inProgress.messageKey, 'f3_error_operation_in_progress');
  assert.equal(isOperationInProgress(inProgress), true);

  const reused = describeF3Failure(httpError(409, 'IDEMPOTENCY_KEY_REUSED'));
  assert.equal(reused.kind, 'idempotency_reused');
  assert.equal(reused.messageKey, 'f3_error_idempotency_reused');
  assert.equal(isOperationInProgress(reused), false);
});

test('el resto de rechazos del dominio se clasifican por código estable', () => {
  const cases: [number, unknown, string][] = [
    [401, 'Not authenticated', 'unauthorized'],
    [403, 'TENANT_NOT_AUTHORIZED', 'forbidden_tenant'],
    [403, 'ROLE_NOT_ALLOWED', 'forbidden_role'],
    [404, 'Not Found', 'not_found'],
    [422, 'Registration request is invalid', 'invalid'],
    [409, 'Registration already exists', 'duplicate'],
    [409, 'Registration state does not allow this operation', 'state_conflict'],
    [409, 'REGISTRATION_CLOSED', 'registration_closed'],
    [409, 'REGISTRATION_ALREADY_CLOSED', 'registration_already_closed'],
    [503, 'LOCK_TIMEOUT', 'lock_timeout'],
    [503, 'IDEMPOTENCY_NOT_CONFIGURED', 'unavailable'],
    [500, 'STATE_ERROR', 'server'],
  ];

  for (const [status, detail, kind] of cases) {
    const failure = describeF3Failure(httpError(status, detail));
    assert.equal(failure.kind, kind, `${status} ${String(detail)}`);
    assert.equal(failure.messageKey, `f3_error_${kind}`);
    assert.ok(failure.messageKey.length > 0);
  }
});

test('un rechazo por rol se deduce del 403 aunque el cuerpo no traiga código', () => {
  const failure = describeF3Failure(httpError(403));
  assert.equal(failure.kind, 'forbidden_role');
  assert.equal(failure.technicalCode, 'HTTP_403', 'sin código del cuerpo se conserva el HTTP');
  assert.equal(failure.messageKey, 'f3_error_forbidden_role');
});

test('sin respuesta HTTP el fallo es de red y el código del navegador no se pierde', () => {
  const offline = describeF3Failure({ isAxiosError: true, code: 'ERR_NETWORK' });
  assert.equal(offline.kind, 'network');
  assert.equal(offline.messageKey, 'f3_error_network');
  assert.equal(offline.technicalCode, 'ERR_NETWORK');

  assert.equal(describeF3Failure(null).kind, 'network');
  assert.equal(describeF3Failure(undefined).kind, 'network');
  assert.equal(describeF3Failure('vaya').kind, 'network');
});

test('Retry-After del servidor se lee para poder avisar, no para reintentar solo', () => {
  const throttled = describeF3Failure(httpError(503, 'LOCK_TIMEOUT', { 'retry-after': '3' }));
  assert.equal(throttled.retryAfterSeconds, 3);
  assert.equal(describeF3Failure(httpError(503, 'LOCK_TIMEOUT')).retryAfterSeconds, null);
  assert.equal(
    describeF3Failure(httpError(503, 'LOCK_TIMEOUT', { 'retry-after': '0' })).retryAfterSeconds,
    null
  );
  assert.equal(
    describeF3Failure(httpError(503, 'LOCK_TIMEOUT', { 'retry-after': 'ya' })).retryAfterSeconds,
    null
  );
});

test('el detalle en crudo del servidor nunca es el mensaje que se enseña', () => {
  const leaky = describeF3Failure(
    httpError(500, 'duplicate key value violates unique constraint "ix_registration"')
  );
  assert.equal(leaky.messageKey, 'f3_error_server');
  assert.ok(!JSON.stringify(leaky.messageKey).includes('constraint'));
  assert.equal(detailCode({ code: 'BRACKET_NOT_READY' }), 'BRACKET_NOT_READY');
  assert.equal(detailCode({ sin: 'codigo' }), null);
});

/* ------------------------------------------------------------------ idempotencia (WriteGate) */

test('una acción lógica tiene una clave y reintentar la conserva', () => {
  let generated = 0;
  const uuid = () => `uuid-${++generated}`;

  const first = resolvePending(null, 'register|7|ana', uuid);
  const retry = resolvePending(first, 'register|7|ana', uuid);
  const other = resolvePending(first, 'register|7|luis', uuid);

  assert.equal(first.key, retry.key);
  assert.equal(other.key === first.key, false);
  assert.equal(generated, 2, 'el reintento lógico no debe estrenar clave');
});

test('el alcance distingue acciones distintas sobre el mismo recurso', () => {
  assert.notEqual(
    actionScope(['registration', 3, 'withdraw']),
    actionScope(['registration', 3, 'confirm'])
  );
  assert.equal(actionScope(['a', null, undefined]), 'a||');
  assert.notEqual(actionScope(['7']), actionScope([7, 0]));
});

test('la clave cumple el formato exigido por el backend', () => {
  const key = newKey(() => 'abc-123-def');
  assert.equal(key, 'f3-abc-123-def');
  assert.ok(key.length >= KEY_MIN_LENGTH);
  assert.ok(KEY_PATTERN.test(key));

  const raw = newKey();
  assert.ok(KEY_PATTERN.test(raw), `clave generada inválida: ${raw}`);
  assert.throws(() => newKey(() => 'no vale #'));
});

test('WriteGate: una sola escritura en vuelo y la segunda pulsación se ignora', async () => {
  const gate = new WriteGate(() => 'aaaaaaaaaaaa');
  const calls: string[] = [];
  let release: (() => void) | null = null;
  const blocked = new Promise<void>((resolve) => {
    release = resolve;
  });

  const first = gate.run('confirm|3', async (key) => {
    calls.push(key);
    await blocked;
    return 'ok';
  });
  const second = await gate.run('confirm|3', async (key) => {
    calls.push(key);
    return 'no debería llegar';
  });

  assert.deepEqual(second, { status: 'ignored' });
  assert.equal(gate.isInflight, true);
  assert.equal(calls.length, 1);

  release!();
  const done = await first;
  assert.equal(done.status, 'done');
  assert.equal(done.value, 'ok');
  assert.equal(gate.isInflight, false);
  assert.equal(gate.pendingKey, null, 'una acción completada libera su clave');
});

test('WriteGate: un fallo conserva la clave para el reintento del usuario', async () => {
  let generated = 0;
  const gate = new WriteGate(() => `aaaaaaaaaaa${++generated}`);

  await assert.rejects(
    gate.run('withdraw|3', async () => {
      throw new Error('503');
    })
  );
  const pendingAfterFailure = gate.pendingKey;
  assert.equal(pendingAfterFailure, 'f3-aaaaaaaaaaa1');
  assert.equal(gate.isInflight, false);

  const retry = await gate.run('withdraw|3', async (key) => key);
  assert.equal(retry.status, 'done');
  assert.equal(retry.key, pendingAfterFailure, 'el reintento reutiliza la clave pendiente');
  assert.equal(generated, 1);

  const fresh = await gate.run('withdraw|3', async (key) => key);
  assert.notEqual(
    fresh.key,
    pendingAfterFailure,
    'tras el éxito la acción siguiente estrena clave'
  );
  assert.equal(generated, 2);
});

test('WriteGate: cambiar de acción estrena clave y reset la descarta', async () => {
  let generated = 0;
  const gate = new WriteGate(() => `aaaaaaaaaaa${++generated}`);

  const withdraw = await gate.run('withdraw|3', async (key) => key);
  const confirm = await gate.run('confirm|3', async (key) => key);
  assert.notEqual(withdraw.key, confirm.key);
  assert.equal(gate.keyFor('confirm|3'), null, 'sin acción en vuelo no hay clave reservada');

  await assert.rejects(
    gate.run('disqualify|3', async () => {
      throw new Error('409');
    })
  );
  assert.ok(gate.pendingKey != null);
  gate.reset();
  assert.equal(gate.pendingKey, null);
  assert.equal(gate.isInflight, false);
});

test('la cabecera de idempotencia es la del backend y la clave no se enseña', () => {
  assert.equal(IDEMPOTENCY_KEY_HEADER, 'Idempotency-Key');

  for (const source of ['../src/services/f3.ts', '../src/components/utils/f3_write_hook.ts']) {
    const code = readFileSync(new URL(source, import.meta.url), 'utf8');
    assert.doesNotMatch(code, /showNotification\([^)]*key/i, `${source} no debe mostrar la clave`);
    assert.doesNotMatch(
      code,
      /console\.(log|debug|info)\([^)]*key/i,
      `${source} no debe trazar la clave`
    );
  }
});
