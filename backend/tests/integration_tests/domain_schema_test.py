# pylint: disable=redefined-outer-name  # `env` es el fixture de este modulo (igual que conftest).
"""Invariantes de esquema del modelo de dominio F3A (competidor / academia / inscripcion).

Estas tablas todavia NO tienen codigo de aplicacion que las use (eso es F3B/F3C). Lo que se
comprueba aqui son las restricciones **reales de PostgreSQL**: CHECK, indices UNIQUE
parciales y claves foraneas con su politica ON DELETE. Los tests no simulan invariantes que
la base de datos no garantiza: las que dependen de la capa de aplicacion se documentan, de
forma explicita, en `TestKnownLimitationsEnforcedByApplication` (y por tanto no se dan por
buenas aqui).

Requiere PostgreSQL: los CHECK, los indices parciales y los tipos enum no existen en SQLite.
Se ejecuta contra la base de tests (`bracket_test`), que la fixture de sesion de
`tests/integration_tests/conftest.py` recrea a partir de los modelos.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError

from bracket.database import engine

NOW = "2026-01-01T00:00:00+00:00"
# Posterior a `now()` del servidor (fecha real de ejecucion): valida `valid_to > valid_from`.
LATER = "2030-01-01T00:00:00+00:00"


def _scalar(sql: str, params: Mapping[str, object]) -> int:
    with engine.begin() as connection:
        return int(connection.execute(text(sql), params).scalar_one())


def _execute(sql: str, params: Mapping[str, object]) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params)


@dataclass
class Env:
    """Objetos minimos de un tenant y ayudantes de insercion con limpieza garantizada."""

    club_id: int
    tournament_id: int
    academy_id: int
    competitor_id: int
    user_id: int
    created: list[tuple[str, int]] = field(default_factory=list)

    def track(self, table: str, row_id: int) -> int:
        self.created.append((table, row_id))
        return row_id

    def sql(self, sql: str, params: Mapping[str, object] | None = None) -> int:
        return _scalar(sql, params or {})

    def fail(self, sql: str, params: Mapping[str, object] | None = None) -> None:
        with pytest.raises(IntegrityError):
            _execute(sql, params or {})

    def fail_type(self, sql: str, params: Mapping[str, object] | None = None) -> None:
        with pytest.raises(DataError):
            _execute(sql, params or {})

    def ok(self, sql: str, params: Mapping[str, object] | None = None) -> None:
        """Ejecuta una sentencia que debe aceptarse (p. ej. un DELETE ordenado)."""
        _execute(sql, params or {})

    def registration(self, **overrides: object) -> int:
        params: dict[str, object] = {
            "tournament_id": self.tournament_id,
            "competitor_id": self.competitor_id,
            "identity_status": "VERIFIED",
            "representation": "INDEPENDENT",
            "sports_club_id": None,
            "category_key": None,
            "category_label": None,
            "competitor_name_snapshot": "Competidor F3A",
            "sports_club_name_snapshot": None,
            "status": "CONFIRMED",
            "revision": 1,
            "corrects_registration_id": None,
            "superseded_by_registration_id": None,
        }
        params.update(overrides)
        columns = ", ".join(params)
        values = ", ".join(f":{key}" for key in params)
        return self.track(
            "tournament_registrations",
            _scalar(
                f"INSERT INTO tournament_registrations ({columns}) VALUES ({values}) RETURNING id",
                params,
            ),
        )


@pytest.fixture()
def env() -> Iterator[Env]:
    club_id = _scalar("INSERT INTO clubs (name) VALUES (:name) RETURNING id", {"name": "f3a-club"})
    tournament_id = _scalar(
        "INSERT INTO tournaments (name, start_time, club_id, dashboard_public)"
        " VALUES (:name, :start_time, :club_id, false) RETURNING id",
        {"name": "f3a-torneo", "start_time": NOW, "club_id": club_id},
    )
    academy_id = _scalar(
        "INSERT INTO sports_clubs (name, tenant_club_id) VALUES (:name, :club_id) RETURNING id",
        {"name": "f3a-academia", "club_id": club_id},
    )
    competitor_id = _scalar(
        "INSERT INTO competitors (display_name, managed_by_club_id)"
        " VALUES (:name, :club_id) RETURNING id",
        {"name": "f3a-competidor", "club_id": club_id},
    )
    user_id = _scalar(
        "INSERT INTO users (email, name, password_hash, account_type)"
        " VALUES (:email, :name, :password_hash, 'REGULAR') RETURNING id",
        {"email": "f3a-test@example.org", "name": "F3A", "password_hash": "x"},
    )
    env = Env(club_id, tournament_id, academy_id, competitor_id, user_id)
    try:
        yield env
    finally:
        # Orden inverso al de creacion: respeta las FK RESTRICT sin cascadas silenciosas.
        for table, row_id in reversed(env.created):
            _execute(f"DELETE FROM {table} WHERE id = :id", {"id": row_id})
        for statement, params in (
            ("DELETE FROM users WHERE id = :id", {"id": user_id}),
            ("DELETE FROM competitors WHERE id = :id", {"id": competitor_id}),
            ("DELETE FROM sports_clubs WHERE id = :id", {"id": academy_id}),
            ("DELETE FROM tournaments WHERE id = :id", {"id": tournament_id}),
            ("DELETE FROM clubs WHERE id = :id", {"id": club_id}),
        ):
            _execute(statement, params)


# --------------------------------------------------------------------------------------
# Inscripciones: cardinalidad y unicidad de la vigente
# --------------------------------------------------------------------------------------


def test_varias_inscripciones_en_categorias_distintas_son_validas(env: Env) -> None:
    """Una persona compite en peso, absoluto, Gi y No-Gi del mismo torneo."""
    for category in ("gi-peso-83", "gi-absoluto", "nogi-peso-83", "nogi-absoluto"):
        env.registration(category_key=category, category_label=category)

    assert (
        env.sql(
            "SELECT count(*) FROM tournament_registrations"
            " WHERE tournament_id = :tournament_id AND competitor_id = :competitor_id",
            {"tournament_id": env.tournament_id, "competitor_id": env.competitor_id},
        )
        == 4
    )


def test_inscripcion_vigente_unica_por_categoria(env: Env) -> None:
    env.registration(category_key="gi-absoluto")
    env.fail(
        "INSERT INTO tournament_registrations"
        " (tournament_id, competitor_id, identity_status, representation, competitor_name_snapshot,"
        "  category_key, status)"
        " VALUES (:tournament_id, :competitor_id, 'VERIFIED', 'INDEPENDENT', 'Competidor F3A',"
        "  'gi-absoluto', 'DRAFT')",
        {"tournament_id": env.tournament_id, "competitor_id": env.competitor_id},
    )


def test_correccion_no_choca_con_la_unicidad_de_la_vigente(env: Env) -> None:
    """La inscripcion sustituida pasa a CORRECTED y deja libre la categoria."""
    original = env.registration(category_key="gi-peso-83")
    sustituta = env.registration(category_key="nogi-peso-83")
    _execute(
        "UPDATE tournament_registrations"
        " SET status = 'CORRECTED', superseded_by_registration_id = :sustituta, revision = 1"
        " WHERE id = :original",
        {"sustituta": sustituta, "original": original},
    )
    nueva_revision = env.registration(
        category_key="gi-peso-83", revision=2, corrects_registration_id=original
    )

    assert nueva_revision > 0
    assert (
        env.sql(
            "SELECT count(*) FROM tournament_registrations"
            " WHERE tournament_id = :tournament_id AND competitor_id = :competitor_id"
            " AND category_key = 'gi-peso-83' AND status <> 'CORRECTED'",
            {"tournament_id": env.tournament_id, "competitor_id": env.competitor_id},
        )
        == 1
    )


def test_inscripciones_sin_categoria_tambien_son_unicas_por_persona(env: Env) -> None:
    """Los NULL no colisionan entre si: la unicidad legacy necesita su propio indice."""
    env.registration(category_key=None)
    env.fail(
        "INSERT INTO tournament_registrations"
        " (tournament_id, competitor_id, identity_status, representation, competitor_name_snapshot,"
        "  category_key, status)"
        " VALUES (:tournament_id, :competitor_id, 'VERIFIED', 'INDEPENDENT', 'Competidor F3A',"
        "  NULL, 'CONFIRMED')",
        {"tournament_id": env.tournament_id, "competitor_id": env.competitor_id},
    )


def test_varias_inscripciones_sin_identidad_verificada_no_colisionan(env: Env) -> None:
    """Participantes legacy sin competidor enlazado: no se deduplica por nombre."""
    for _ in range(2):
        env.track(
            "tournament_registrations",
            env.sql(
                "INSERT INTO tournament_registrations"
                " (tournament_id, competitor_id, identity_status, representation,"
                "  competitor_name_snapshot, category_key, status)"
                " VALUES (:tournament_id, NULL, 'UNVERIFIED', 'INDEPENDENT',"
                "  'Nombre repetido', NULL, 'CONFIRMED') RETURNING id",
                {"tournament_id": env.tournament_id},
            ),
        )

    assert (
        env.sql(
            "SELECT count(*) FROM tournament_registrations"
            " WHERE tournament_id = :tournament_id AND competitor_id IS NULL",
            {"tournament_id": env.tournament_id},
        )
        == 2
    )


# --------------------------------------------------------------------------------------
# Representacion: independiente vs academia
# --------------------------------------------------------------------------------------


def test_inscripcion_independiente_sin_academia(env: Env) -> None:
    registration = env.registration(representation="INDEPENDENT", sports_club_id=None)

    assert (
        env.sql(
            "SELECT sports_club_id IS NULL FROM tournament_registrations WHERE id = :id",
            {"id": registration},
        )
        == 1
    )


def test_representacion_de_club_exige_academia(env: Env) -> None:
    env.fail(
        "INSERT INTO tournament_registrations"
        " (tournament_id, competitor_id, identity_status, representation, competitor_name_snapshot,"
        "  sports_club_id, sports_club_name_snapshot, status)"
        " VALUES (:tournament_id, :competitor_id, 'VERIFIED', 'CLUB', 'Competidor F3A', NULL,"
        "  'f3a-academia', 'CONFIRMED')",
        {"tournament_id": env.tournament_id, "competitor_id": env.competitor_id},
    )


def test_representacion_de_club_exige_snapshot_del_nombre(env: Env) -> None:
    env.fail(
        "INSERT INTO tournament_registrations"
        " (tournament_id, competitor_id, identity_status, representation, competitor_name_snapshot,"
        "  sports_club_id, sports_club_name_snapshot, status)"
        " VALUES (:tournament_id, :competitor_id, 'VERIFIED', 'CLUB', 'Competidor F3A',"
        "  :sports_club_id, NULL, 'CONFIRMED')",
        {
            "tournament_id": env.tournament_id,
            "competitor_id": env.competitor_id,
            "sports_club_id": env.academy_id,
        },
    )


def test_inscripcion_independiente_no_admite_snapshot_de_academia(env: Env) -> None:
    env.fail(
        "INSERT INTO tournament_registrations"
        " (tournament_id, competitor_id, identity_status, representation, competitor_name_snapshot,"
        "  sports_club_id, sports_club_name_snapshot, status)"
        " VALUES (:tournament_id, :competitor_id, 'VERIFIED', 'INDEPENDENT', 'Competidor F3A',"
        "  NULL, 'f3a-academia', 'CONFIRMED')",
        {"tournament_id": env.tournament_id, "competitor_id": env.competitor_id},
    )


def test_representacion_de_club_valida_con_academia_y_snapshot(env: Env) -> None:
    registration = env.registration(
        representation="CLUB",
        sports_club_id=env.academy_id,
        sports_club_name_snapshot="f3a-academia",
    )

    assert registration > 0


# --------------------------------------------------------------------------------------
# Identidad: verificada / no verificada, snapshots
# --------------------------------------------------------------------------------------


def test_identidad_verificada_exige_competidor(env: Env) -> None:
    env.fail(
        "INSERT INTO tournament_registrations"
        " (tournament_id, competitor_id, identity_status, representation, competitor_name_snapshot,"
        "  status)"
        " VALUES (:tournament_id, NULL, 'VERIFIED', 'INDEPENDENT', 'Competidor F3A', 'CONFIRMED')",
        {"tournament_id": env.tournament_id},
    )


def test_identidad_no_verificada_no_puede_traer_competidor(env: Env) -> None:
    env.fail(
        "INSERT INTO tournament_registrations"
        " (tournament_id, competitor_id, identity_status, representation, competitor_name_snapshot,"
        "  status)"
        " VALUES (:tournament_id, :competitor_id, 'UNVERIFIED', 'INDEPENDENT', 'Competidor F3A',"
        "  'CONFIRMED')",
        {"tournament_id": env.tournament_id, "competitor_id": env.competitor_id},
    )


def test_snapshot_de_nombre_no_puede_estar_vacio(env: Env) -> None:
    env.fail(
        "INSERT INTO tournament_registrations"
        " (tournament_id, identity_status, representation, competitor_name_snapshot, status)"
        " VALUES (:tournament_id, 'UNVERIFIED', 'INDEPENDENT', '   ', 'DRAFT')",
        {"tournament_id": env.tournament_id},
    )


def test_valor_de_representacion_fuera_del_enum_es_rechazado(env: Env) -> None:
    env.fail_type(
        "INSERT INTO tournament_registrations"
        " (tournament_id, identity_status, representation, competitor_name_snapshot, status)"
        " VALUES (:tournament_id, 'UNVERIFIED', 'ACADEMY', 'Competidor F3A', 'DRAFT')",
        {"tournament_id": env.tournament_id},
    )


# --------------------------------------------------------------------------------------
# Revisiones y auditoria
# --------------------------------------------------------------------------------------


def test_una_revision_mayor_exige_inscripcion_corregida(env: Env) -> None:
    env.fail(
        "INSERT INTO tournament_registrations"
        " (tournament_id, identity_status, representation, competitor_name_snapshot,"
        "  status, revision)"
        " VALUES (:tournament_id, 'UNVERIFIED', 'INDEPENDENT', 'Competidor F3A', 'DRAFT', 2)",
        {"tournament_id": env.tournament_id},
    )


def test_auditoria_guarda_nombres_de_campo_y_sobrevive_al_actor(env: Env) -> None:
    log_id = env.track(
        "domain_change_log",
        env.sql(
            "INSERT INTO domain_change_log"
            " (entity, entity_id, action, changed_fields, actor_user_id, actor_label, reason)"
            " VALUES ('tournament_registration', :entity_id, 'CORRECT',"
            "  ARRAY['representation', 'sports_club_id'], :user_id, 'F3A', 'rectificacion')"
            " RETURNING id",
            {"entity_id": env.tournament_id, "user_id": env.user_id},
        ),
    )
    _execute("DELETE FROM users WHERE id = :id", {"id": env.user_id})

    assert (
        env.sql(
            "SELECT count(*) FROM domain_change_log WHERE id = :id AND actor_user_id IS NULL",
            {"id": log_id},
        )
        == 1
    )
    assert (
        env.sql(
            "SELECT array_length(changed_fields, 1) FROM domain_change_log WHERE id = :id",
            {"id": log_id},
        )
        == 2
    )


# --------------------------------------------------------------------------------------
# Afiliaciones
# --------------------------------------------------------------------------------------


def test_afiliaciones_multiples_con_una_sola_primaria_vigente(env: Env) -> None:
    segunda_academia = env.track(
        "sports_clubs",
        env.sql(
            "INSERT INTO sports_clubs (name, tenant_club_id) VALUES ('f3a-academia-2', :club_id)"
            " RETURNING id",
            {"club_id": env.club_id},
        ),
    )
    primera = env.track(
        "competitors_x_sports_clubs",
        env.sql(
            "INSERT INTO competitors_x_sports_clubs"
            " (competitor_id, sports_club_id, is_primary)"
            " VALUES (:competitor_id, :sports_club_id, true) RETURNING id",
            {"competitor_id": env.competitor_id, "sports_club_id": env.academy_id},
        ),
    )
    # Afiliacion simultanea no primaria: permitida (entrena en dos academias).
    env.track(
        "competitors_x_sports_clubs",
        env.sql(
            "INSERT INTO competitors_x_sports_clubs"
            " (competitor_id, sports_club_id, is_primary)"
            " VALUES (:competitor_id, :sports_club_id, false) RETURNING id",
            {"competitor_id": env.competitor_id, "sports_club_id": segunda_academia},
        ),
    )
    # Segunda primaria vigente: rechazada.
    env.fail(
        "INSERT INTO competitors_x_sports_clubs"
        " (competitor_id, sports_club_id, is_primary)"
        " VALUES (:competitor_id, :sports_club_id, true)",
        {"competitor_id": env.competitor_id, "sports_club_id": segunda_academia},
    )
    # Cerrada la primera, otra puede pasar a primaria.
    _execute(
        "UPDATE competitors_x_sports_clubs SET valid_to = :valid_to WHERE id = :id",
        {"valid_to": LATER, "id": primera},
    )
    assert (
        env.track(
            "competitors_x_sports_clubs",
            env.sql(
                "INSERT INTO competitors_x_sports_clubs"
                " (competitor_id, sports_club_id, is_primary)"
                " VALUES (:competitor_id, :sports_club_id, true) RETURNING id",
                {"competitor_id": env.competitor_id, "sports_club_id": segunda_academia},
            ),
        )
        > 0
    )


def test_afiliacion_con_rango_invalido_es_rechazada(env: Env) -> None:
    env.fail(
        "INSERT INTO competitors_x_sports_clubs"
        " (competitor_id, sports_club_id, valid_from, valid_to)"
        " VALUES (:competitor_id, :sports_club_id, :valid_from, :valid_to)",
        {
            "competitor_id": env.competitor_id,
            "sports_club_id": env.academy_id,
            "valid_from": LATER,
            "valid_to": NOW,
        },
    )


def test_historial_de_nombres_con_rango_invalido_es_rechazado(env: Env) -> None:
    env.fail(
        "INSERT INTO sports_clubs_name_history (sports_club_id, name, valid_from, valid_to)"
        " VALUES (:sports_club_id, 'f3a-academia', :valid_from, :valid_to)",
        {"sports_club_id": env.academy_id, "valid_from": LATER, "valid_to": NOW},
    )


# --------------------------------------------------------------------------------------
# Conservacion historica: politica de borrado
# --------------------------------------------------------------------------------------


def test_borrar_torneo_con_inscripciones_esta_restringido(env: Env) -> None:
    env.registration(category_key="gi-absoluto")
    env.fail("DELETE FROM tournaments WHERE id = :id", {"id": env.tournament_id})


def test_borrar_competidor_con_inscripciones_esta_restringido(env: Env) -> None:
    env.registration(category_key="gi-absoluto")
    env.fail("DELETE FROM competitors WHERE id = :id", {"id": env.competitor_id})


def test_borrar_academia_con_afiliaciones_esta_restringido(env: Env) -> None:
    env.track(
        "competitors_x_sports_clubs",
        env.sql(
            "INSERT INTO competitors_x_sports_clubs (competitor_id, sports_club_id, is_primary)"
            " VALUES (:competitor_id, :sports_club_id, true) RETURNING id",
            {"competitor_id": env.competitor_id, "sports_club_id": env.academy_id},
        ),
    )
    env.fail("DELETE FROM sports_clubs WHERE id = :id", {"id": env.academy_id})


def test_historial_de_nombres_desaparece_con_su_entidad(env: Env) -> None:
    historia = env.track(
        "sports_clubs_name_history",
        env.sql(
            "INSERT INTO sports_clubs_name_history (sports_club_id, name, valid_from)"
            " VALUES (:sports_club_id, 'f3a-academia', :valid_from) RETURNING id",
            {"sports_club_id": env.academy_id, "valid_from": NOW},
        ),
    )
    _execute("DELETE FROM sports_clubs WHERE id = :id", {"id": env.academy_id})

    assert (
        env.sql("SELECT count(*) FROM sports_clubs_name_history WHERE id = :id", {"id": historia})
        == 0
    )


# --------------------------------------------------------------------------------------
# Legacy: vinculos opcionales y sin efecto sobre las relaciones existentes
# --------------------------------------------------------------------------------------


def test_vinculos_opcionales_en_players_no_alteran_el_legacy(env: Env) -> None:
    """`players` sigue siendo la participacion legacy por torneo: los vinculos son opcionales."""
    player_id = env.track(
        "players",
        env.sql(
            "INSERT INTO players (name, tournament_id, elo_score, swiss_score, wins, draws, losses)"
            " VALUES ('f3a-jugador', :tournament_id, 0, 0, 0, 0, 0) RETURNING id",
            {"tournament_id": env.tournament_id},
        ),
    )
    assert (
        env.sql(
            "SELECT (registration_id IS NULL AND competitor_id IS NULL)::int FROM players"
            " WHERE id = :id",
            {"id": player_id},
        )
        == 1
    )

    registration = env.registration(category_key="gi-absoluto")
    _execute(
        "UPDATE players SET registration_id = :registration_id, competitor_id = :competitor_id"
        " WHERE id = :id",
        {"registration_id": registration, "competitor_id": env.competitor_id, "id": player_id},
    )
    # Al desaparecer la inscripcion el vinculo se anula; la fila legacy y su torneo siguen.
    _execute("DELETE FROM tournament_registrations WHERE id = :id", {"id": registration})
    env.created = [(table, row) for table, row in env.created if row != registration]

    assert (
        env.sql(
            "SELECT (registration_id IS NULL AND tournament_id = :tournament_id)::int FROM players"
            " WHERE id = :id",
            {"tournament_id": env.tournament_id, "id": player_id},
        )
        == 1
    )


def test_cuota_configurable_unica_por_tenant_y_clave(env: Env) -> None:
    env.track(
        "tenant_quota_overrides",
        env.sql(
            "INSERT INTO tenant_quota_overrides (club_id, quota_key, quota_value)"
            " VALUES (:club_id, 'max_competitors', 500) RETURNING id",
            {"club_id": env.club_id},
        ),
    )
    env.fail(
        "INSERT INTO tenant_quota_overrides (club_id, quota_key, quota_value)"
        " VALUES (:club_id, 'max_competitors', 900)",
        {"club_id": env.club_id},
    )


# --------------------------------------------------------------------------------------
# Invariantes que NO garantiza la base de datos (aplicacion: F3B/F3C)
# --------------------------------------------------------------------------------------


class TestKnownLimitationsEnforcedByApplication:
    """Documenta lo que PostgreSQL acepta hoy y debe validar la capa de aplicacion.

    No son fallos: son limites conscientes del esquema F3A. Si alguno cambia de fase,
    su test debe moverse arriba (a la seccion de invariantes garantizadas).
    """

    def test_correccion_de_otro_torneo_no_esta_bloqueada_por_la_base(self, env: Env) -> None:
        otro_torneo = env.track(
            "tournaments",
            env.sql(
                "INSERT INTO tournaments (name, start_time, club_id, dashboard_public)"
                " VALUES ('f3a-torneo-2', :start_time, :club_id, false) RETURNING id",
                {"start_time": NOW, "club_id": env.club_id},
            ),
        )
        original = env.registration(category_key="gi-absoluto")
        revision = env.registration(
            category_key="gi-absoluto",
            tournament_id=otro_torneo,
            revision=2,
            corrects_registration_id=original,
        )

        # Aceptado por la base: la pertenencia al mismo torneo es invariante de aplicacion.
        assert revision > 0

    def test_ciclo_de_revisiones_no_esta_bloqueado_por_la_base(self, env: Env) -> None:
        primera = env.registration(category_key="gi-absoluto")
        segunda = env.registration(
            category_key="gi-absoluto-2", revision=2, corrects_registration_id=primera
        )
        _execute(
            "UPDATE tournament_registrations SET status = 'CORRECTED',"
            " superseded_by_registration_id = :segunda WHERE id = :primera",
            {"segunda": segunda, "primera": primera},
        )
        _execute(
            "UPDATE tournament_registrations SET status = 'CORRECTED',"
            " superseded_by_registration_id = :primera WHERE id = :segunda",
            {"primera": primera, "segunda": segunda},
        )

        # Aceptado por la base: la ausencia de ciclos es invariante de aplicacion.
        assert (
            env.sql(
                "SELECT count(*) FROM tournament_registrations WHERE status = 'CORRECTED'",
                {},
            )
            >= 2
        )

    def test_identidad_no_verificada_no_puede_enlazar_competidor(self, env: Env) -> None:
        """El CHECK de identidad es bicondicional: solo VERIFIED enlaza competidor.

        AMBIGUOUS sin competidor se acepta (la ambiguedad se resuelve en F3B/F3C), pero
        no puede enlazarse: la base no admite un vinculo sin identidad verificada.
        """
        sin_competidor = env.registration(identity_status="AMBIGUOUS", competitor_id=None)
        assert sin_competidor > 0
        env.fail(
            "INSERT INTO tournament_registrations"
            " (tournament_id, competitor_id, identity_status, representation,"
            "  competitor_name_snapshot, category_key, status)"
            " VALUES (:tournament_id, :competitor_id, 'AMBIGUOUS', 'INDEPENDENT',"
            "  'Competidor F3A', 'gi-peso-83', 'CONFIRMED')",
            {"tournament_id": env.tournament_id, "competitor_id": env.competitor_id},
        )

    def test_una_inscripcion_corrected_sin_sustituta_no_la_bloquea_la_bbd(self, env: Env) -> None:
        """La invariante "CORRECTED => superseded_by" NO la garantiza la base (F3B/F3C).

        No es expresable a la vez que el ON DELETE SET NULL que hace posible el borrado
        ordenado de la cadena de revisiones (desviacion documentada de docs/21 v3 §12.3).
        """
        primera = env.registration(category_key="gi-peso-83", category_label="Gi -83")

        env.ok(
            "UPDATE tournament_registrations SET status = 'CORRECTED' WHERE id = :id",
            {"id": primera},
        )

        # Aceptado por la base: queda CORRECTED sin sustituta declarada.
        assert (
            env.sql(
                "SELECT (superseded_by_registration_id IS NULL)::int"
                " FROM tournament_registrations WHERE id = :id",
                {"id": primera},
            )
            == 1
        )


# --------------------------------------------------------------------------------------
# Cadena de revisiones: el origen queda protegido (RESTRICT), el borrado ordenado es posible
# --------------------------------------------------------------------------------------


def test_borrar_el_origen_de_una_revision_esta_restringido(env: Env) -> None:
    """El original no se borra mientras exista una revision que lo corrija.

    El borrado explicito sigue siendo posible en orden (hoja -> raiz), que es la via
    autorizada que docs/21 v3 §12.2 exige para una retirada real.
    """
    original = env.registration(category_key="gi-peso-83", category_label="Gi -83")
    # La vigente pasa a CORRECTED (sustituida) antes de crear la revision: el indice
    # unico parcial solo admite una inscripcion vigente por categoria.
    env.ok(
        "UPDATE tournament_registrations SET status = 'CORRECTED' WHERE id = :id",
        {"id": original},
    )
    revision = env.registration(
        category_key="gi-peso-83",
        category_label="Gi -83",
        revision=2,
        corrects_registration_id=original,
    )

    env.fail("DELETE FROM tournament_registrations WHERE id = :id", {"id": original})

    env.ok("DELETE FROM tournament_registrations WHERE id = :id", {"id": revision})
    env.ok("DELETE FROM tournament_registrations WHERE id = :id", {"id": original})

    assert (
        env.sql(
            "SELECT count(*)::int FROM tournament_registrations WHERE id IN (:a, :b)",
            {"a": original, "b": revision},
        )
        == 0
    )
