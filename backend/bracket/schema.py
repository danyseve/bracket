from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base  # type: ignore[attr-defined]
from sqlalchemy.sql.sqltypes import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    SmallInteger,
    Text,
)

Base = declarative_base()
metadata = Base.metadata
DateTimeTZ = DateTime(timezone=True)

clubs = Table(
    "clubs",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True, autoincrement=True),
    Column("name", String, nullable=False, index=True),
    Column("created", DateTimeTZ, nullable=False, server_default=func.now()),
)

tournaments = Table(
    "tournaments",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column("name", String, nullable=False, index=True),
    Column("created", DateTimeTZ, nullable=False, server_default=func.now()),
    Column("start_time", DateTimeTZ, nullable=False),
    Column("club_id", BigInteger, ForeignKey("clubs.id"), index=True, nullable=False),
    Column("dashboard_public", Boolean, nullable=False),
    Column("logo_path", String, nullable=True),
    Column("dashboard_endpoint", String, nullable=True, index=True, unique=True),
    Column("players_can_be_in_multiple_teams", Boolean, nullable=False, server_default="f"),
    Column("auto_assign_courts", Boolean, nullable=False, server_default="f"),
    Column("duration_minutes", Integer, nullable=False, server_default="15"),
    Column("margin_minutes", Integer, nullable=False, server_default="5"),
    Column(
        "status",
        Enum(
            "OPEN",
            "ARCHIVED",
            name="tournament_status",
        ),
        nullable=False,
        server_default="OPEN",
        index=True,
    ),
)

stages = Table(
    "stages",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column("name", String, nullable=False, index=True),
    Column("created", DateTimeTZ, nullable=False, server_default=func.now()),
    Column("tournament_id", BigInteger, ForeignKey("tournaments.id"), index=True, nullable=False),
    Column("is_active", Boolean, nullable=False, server_default="false"),
)

stage_items = Table(
    "stage_items",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column("name", Text, nullable=False),
    Column("created", DateTimeTZ, nullable=False, server_default=func.now()),
    Column("stage_id", BigInteger, ForeignKey("stages.id"), index=True, nullable=False),
    Column("team_count", Integer, nullable=False),
    Column("ranking_id", BigInteger, ForeignKey("rankings.id"), nullable=False),
    Column(
        "type",
        Enum(
            "SINGLE_ELIMINATION",
            "SWISS",
            "ROUND_ROBIN",
            name="stage_type",
        ),
        nullable=False,
    ),
)

stage_item_inputs = Table(
    "stage_item_inputs",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column("slot", Integer, nullable=False),
    Column("tournament_id", BigInteger, ForeignKey("tournaments.id"), index=True, nullable=False),
    Column(
        "stage_item_id",
        BigInteger,
        ForeignKey("stage_items.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    ),
    Column("team_id", BigInteger, ForeignKey("teams.id"), nullable=True),
    Column("winner_from_stage_item_id", BigInteger, ForeignKey("stage_items.id"), nullable=True),
    Column("winner_position", Integer, nullable=True),
    Column("points", Float, nullable=False, server_default="0"),
    Column("wins", Integer, nullable=False, server_default="0"),
    Column("draws", Integer, nullable=False, server_default="0"),
    Column("losses", Integer, nullable=False, server_default="0"),
    UniqueConstraint("stage_item_id", "team_id"),
    UniqueConstraint("stage_item_id", "winner_from_stage_item_id", "winner_position"),
)

rounds = Table(
    "rounds",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column("name", Text, nullable=False),
    Column("created", DateTimeTZ, nullable=False, server_default=func.now()),
    Column("is_draft", Boolean, nullable=False),
    Column("stage_item_id", BigInteger, ForeignKey("stage_items.id"), nullable=False),
)


matches = Table(
    "matches",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column("created", DateTimeTZ, nullable=False, server_default=func.now()),
    Column("start_time", DateTimeTZ, nullable=True),
    Column("duration_minutes", Integer, nullable=True),
    Column("margin_minutes", Integer, nullable=True),
    Column("custom_duration_minutes", Integer, nullable=True),
    Column("custom_margin_minutes", Integer, nullable=True),
    Column("round_id", BigInteger, ForeignKey("rounds.id"), nullable=False),
    Column("stage_item_input1_id", BigInteger, ForeignKey("stage_item_inputs.id"), nullable=True),
    Column("stage_item_input2_id", BigInteger, ForeignKey("stage_item_inputs.id"), nullable=True),
    Column("stage_item_input1_conflict", Boolean, nullable=False),
    Column("stage_item_input2_conflict", Boolean, nullable=False),
    Column(
        "stage_item_input1_winner_from_match_id",
        BigInteger,
        ForeignKey("matches.id"),
        nullable=True,
    ),
    Column(
        "stage_item_input2_winner_from_match_id",
        BigInteger,
        ForeignKey("matches.id"),
        nullable=True,
    ),
    Column("court_id", BigInteger, ForeignKey("courts.id"), nullable=True),
    Column("stage_item_input1_score", Integer, nullable=False),
    Column("stage_item_input2_score", Integer, nullable=False),
    Column("position_in_schedule", Integer, nullable=True),
)

teams = Table(
    "teams",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column("name", String, nullable=False, index=True),
    Column("created", DateTimeTZ, nullable=False, server_default=func.now()),
    Column("tournament_id", BigInteger, ForeignKey("tournaments.id"), index=True, nullable=False),
    Column("active", Boolean, nullable=False, index=True, server_default="t"),
    Column("elo_score", Float, nullable=False, server_default="0"),
    Column("swiss_score", Float, nullable=False, server_default="0"),
    Column("wins", Integer, nullable=False, server_default="0"),
    Column("draws", Integer, nullable=False, server_default="0"),
    Column("losses", Integer, nullable=False, server_default="0"),
    Column("logo_path", String, nullable=True),
)

players = Table(
    "players",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column("name", String, nullable=False, index=True),
    Column("created", DateTimeTZ, nullable=False, server_default=func.now()),
    Column("tournament_id", BigInteger, ForeignKey("tournaments.id"), index=True, nullable=False),
    # F3A: vinculos opcionales y nullable con el modelo nuevo de dominio. Se anaden
    # vacios a proposito: el backfill conservador es F3B y no se altera ninguna
    # relacion existente de `players` (participacion legacy por torneo).
    Column(
        "registration_id",
        BigInteger,
        ForeignKey("tournament_registrations.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    ),
    Column(
        "competitor_id",
        BigInteger,
        ForeignKey("competitors.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    ),
    Column("elo_score", Float, nullable=False),
    Column("swiss_score", Float, nullable=False),
    Column("wins", Integer, nullable=False),
    Column("draws", Integer, nullable=False),
    Column("losses", Integer, nullable=False),
    Column("active", Boolean, nullable=False, index=True, server_default="t"),
)

users = Table(
    "users",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column("email", String, nullable=False, index=True, unique=True),
    Column("name", String, nullable=False),
    Column("password_hash", String, nullable=False),
    Column("created", DateTimeTZ, nullable=False, server_default=func.now()),
    Column(
        "account_type",
        Enum(
            "REGULAR",
            "DEMO",
            "ADMIN",
            name="account_type",
        ),
        nullable=False,
    ),
    Column("active", Boolean, nullable=False, server_default="t"),
)

users_x_clubs = Table(
    "users_x_clubs",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column("club_id", BigInteger, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False),
    Column("user_id", BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column(
        "relation",
        Enum(
            "OWNER",
            "COLLABORATOR",
            name="user_x_club_relation",
        ),
        nullable=False,
        default="OWNER",
    ),
)

players_x_teams = Table(
    "players_x_teams",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column("player_id", BigInteger, ForeignKey("players.id", ondelete="CASCADE"), nullable=False),
    Column("team_id", BigInteger, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
)

courts = Table(
    "courts",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column("name", Text, nullable=False),
    Column("created", DateTimeTZ, nullable=False, server_default=func.now()),
    Column("tournament_id", BigInteger, ForeignKey("tournaments.id"), nullable=False, index=True),
)

rankings = Table(
    "rankings",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column("created", DateTimeTZ, nullable=False, server_default=func.now()),
    Column("tournament_id", BigInteger, ForeignKey("tournaments.id"), nullable=False, index=True),
    Column("position", Integer, nullable=False),
    Column("win_points", Float, nullable=False),
    Column("draw_points", Float, nullable=False),
    Column("loss_points", Float, nullable=False),
    Column("add_score_points", Boolean, nullable=False),
)

# --------------------------------------------------------------------------------------
# F3A — Fundamentos del modelo de dominio (Competitor / Academia / Inscripcion).
#
# Aditivo y sin backfill: ninguna tabla existente cambia de significado.
# ``clubs`` sigue siendo el tenant/organizacion, ``players`` la participacion legacy por
# torneo, ``teams`` la unidad competitiva y ``stage_item_inputs.team_id`` el entrant del
# motor (intacto). El Entrant explicito (F3E) esta aplazado y no se crea aqui.
#
# Politica de borrado (doc 21 v3, §12.2): el historico de competicion nunca se elimina en
# cascada desde ``tournaments``; las relaciones de identidad son RESTRICT y solo las
# relaciones auxiliares (SET NULL) o de configuracion (CASCADE) pueden desaparecer.
# --------------------------------------------------------------------------------------

sports_clubs = Table(
    "sports_clubs",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column("name", String, nullable=False),
    Column("active", Boolean, nullable=False, server_default="t", index=True),
    # Tenant/organizacion que administra el registro. NULL = academia sin cuenta
    # administrativa (solo mantenible por un administrador de plataforma).
    Column(
        "tenant_club_id",
        BigInteger,
        ForeignKey("clubs.id", ondelete="RESTRICT"),
        index=True,
        nullable=True,
    ),
    Column("created", DateTimeTZ, nullable=False, server_default=func.now()),
    Column("updated_at", DateTimeTZ, nullable=True),
    Index("ix_sports_clubs_name_normalized", func.lower(func.btrim(text("name")))),
)

sports_clubs_name_history = Table(
    "sports_clubs_name_history",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column(
        "sports_club_id",
        BigInteger,
        ForeignKey("sports_clubs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    ),
    Column("name", String, nullable=False),
    Column("valid_from", DateTimeTZ, nullable=False, server_default=func.now()),
    Column("valid_to", DateTimeTZ, nullable=True),
    Column(
        "changed_by_user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    ),
    CheckConstraint(
        "valid_to IS NULL OR valid_to > valid_from",
        name="ck_sports_clubs_name_history_range",
    ),
)

competitors = Table(
    "competitors",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column("display_name", String, nullable=False),
    Column("active", Boolean, nullable=False, server_default="t", index=True),
    # Tenant que administra el registro del competidor. NULL = registro de plataforma.
    Column(
        "managed_by_club_id",
        BigInteger,
        ForeignKey("clubs.id", ondelete="RESTRICT"),
        index=True,
        nullable=True,
    ),
    Column("created", DateTimeTZ, nullable=False, server_default=func.now()),
    Column("updated_at", DateTimeTZ, nullable=True),
    Index("ix_competitors_display_name_normalized", func.lower(func.btrim(text("display_name")))),
)

competitors_name_history = Table(
    "competitors_name_history",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column(
        "competitor_id",
        BigInteger,
        ForeignKey("competitors.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    ),
    Column("display_name", String, nullable=False),
    Column("valid_from", DateTimeTZ, nullable=False, server_default=func.now()),
    Column("valid_to", DateTimeTZ, nullable=True),
    Column(
        "changed_by_user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    ),
    CheckConstraint(
        "valid_to IS NULL OR valid_to > valid_from",
        name="ck_competitors_name_history_range",
    ),
)

competitors_x_sports_clubs = Table(
    "competitors_x_sports_clubs",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column(
        "competitor_id",
        BigInteger,
        ForeignKey("competitors.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    ),
    Column(
        "sports_club_id",
        BigInteger,
        ForeignKey("sports_clubs.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    ),
    Column("valid_from", DateTimeTZ, nullable=False, server_default=func.now()),
    Column("valid_to", DateTimeTZ, nullable=True),
    Column("is_primary", Boolean, nullable=False, server_default="f"),
    Column("note", Text, nullable=True),
    Column("created", DateTimeTZ, nullable=False, server_default=func.now()),
    CheckConstraint(
        "valid_to IS NULL OR valid_to > valid_from",
        name="ck_competitors_x_sports_clubs_range",
    ),
    # Una unica afiliacion primaria vigente por competidor (afiliaciones simultaneas
    # no primarias permitidas).
    Index(
        "uq_competitors_x_sports_clubs_primary",
        "competitor_id",
        unique=True,
        postgresql_where=text("valid_to IS NULL AND is_primary"),
    ),
)

tournament_registrations = Table(
    "tournament_registrations",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    # RESTRICT: eliminar un torneo no puede borrar inscripciones historicas en cascada.
    Column(
        "tournament_id",
        BigInteger,
        ForeignKey("tournaments.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    ),
    Column(
        "competitor_id",
        BigInteger,
        ForeignKey("competitors.id", ondelete="RESTRICT"),
        index=True,
        nullable=True,
    ),
    Column(
        "identity_status",
        Enum(
            "UNVERIFIED",
            "AMBIGUOUS",
            "VERIFIED",
            name="registration_identity_status",
        ),
        nullable=False,
        server_default="UNVERIFIED",
    ),
    Column(
        "representation",
        Enum("INDEPENDENT", "CLUB", name="registration_representation"),
        nullable=False,
        server_default="INDEPENDENT",
    ),
    Column(
        "sports_club_id",
        BigInteger,
        ForeignKey("sports_clubs.id", ondelete="RESTRICT"),
        index=True,
        nullable=True,
    ),
    Column(
        "affiliation_id",
        BigInteger,
        ForeignKey("competitors_x_sports_clubs.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("category_key", String, nullable=True, index=True),
    Column("category_label", String, nullable=True),
    Column("competitor_name_snapshot", String, nullable=False),
    Column("sports_club_name_snapshot", String, nullable=True),
    Column(
        "status",
        Enum(
            "DRAFT",
            "CONFIRMED",
            "WITHDRAWN",
            "DISQUALIFIED",
            "CORRECTED",
            name="registration_status",
        ),
        nullable=False,
        server_default="DRAFT",
        index=True,
    ),
    Column("revision", Integer, nullable=False, server_default="1"),
    # Cadena de revisiones (docs/21 v3 §12.3, con desviacion documentada en F3A):
    #  - `corrects_registration_id` RESTRICT: una revision no puede quedarse sin su origen,
    #    y el original no se borra mientras exista una revision que lo corrija.
    #  - `superseded_by_registration_id` SET NULL: permitir el borrado ordenado
    #    (hoja -> raiz). Con RESTRICT en ambos lados la pareja original/revision era
    #    indeleble, y el CHECK "CORRECTED => superseded_by" chocaba con el SET NULL.
    #    Esa invariante pasa a la capa de aplicacion (F3B/F3C), no se simula en la BBDD.
    Column(
        "corrects_registration_id",
        BigInteger,
        ForeignKey("tournament_registrations.id", ondelete="RESTRICT"),
        index=True,
        nullable=True,
    ),
    Column(
        "superseded_by_registration_id",
        BigInteger,
        ForeignKey("tournament_registrations.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column(
        "verified_by_user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("verified_at", DateTimeTZ, nullable=True),
    Column("created", DateTimeTZ, nullable=False, server_default=func.now()),
    Column("updated_at", DateTimeTZ, nullable=True),
    CheckConstraint(
        "(representation = 'CLUB') = (sports_club_id IS NOT NULL)",
        name="ck_tournament_registrations_representation",
    ),
    CheckConstraint(
        "(identity_status = 'VERIFIED') = (competitor_id IS NOT NULL)",
        name="ck_tournament_registrations_identity",
    ),
    CheckConstraint(
        "(representation = 'CLUB') = (sports_club_name_snapshot IS NOT NULL)",
        name="ck_tournament_registrations_club_snapshot",
    ),
    CheckConstraint(
        "length(btrim(competitor_name_snapshot)) > 0",
        name="ck_tournament_registrations_snapshot_not_empty",
    ),
    CheckConstraint(
        "revision = 1 OR corrects_registration_id IS NOT NULL",
        name="ck_tournament_registrations_revision",
    ),
    # Una sola inscripcion vigente por (torneo, competidor, categoria). Las filas
    # CORRECTED (sustituidas) quedan fuera del indice para no chocar con su revision.
    Index(
        "uq_tournament_registrations_current_category",
        "tournament_id",
        "competitor_id",
        "category_key",
        unique=True,
        postgresql_where=text("competitor_id IS NOT NULL AND status <> 'CORRECTED'"),
    ),
    # Inscripciones legacy sincronizadas sin categoria declarada: en PostgreSQL los
    # NULL no colisionan entre si, asi que la unicidad necesita un indice propio.
    Index(
        "uq_tournament_registrations_current_uncategorized",
        "tournament_id",
        "competitor_id",
        unique=True,
        postgresql_where=text(
            "competitor_id IS NOT NULL AND category_key IS NULL AND status <> 'CORRECTED'"
        ),
    ),
)

# Auditoria de rectificaciones. Sin FK hacia las entidades auditadas (el registro debe
# sobrevivir al objeto) y con el actor como SET NULL mas una etiqueta congelada.
# ``changed_fields`` guarda NOMBRES DE CAMPO, nunca valores (evita PII en la auditoria).
# S3.3c-2: motivo cerrado de la auditoria de dominio. ``reason`` conserva el texto canonico
# legible que deriva el catalogo (``bracket.logic.audit_reasons``); estas dos columnas guardan el
# motivo estructurado: ``reason_code`` (codigo cerrado, obligatorio en las escrituras nuevas que
# proceden del catalogo) y ``reason_note`` (nota libre **opcional** y solo en los codigos que la
# admiten). NULLABLE a proposito en ambas: NULL significa "fila anterior a esta migracion", y el
# escritor interno tambien audita entidades de plataforma que no tienen catalogo propio. La
# nulabilidad no debilita la garantia: los CHECK son los que cierran el conjunto de valores, y la
# coherencia "codigo permitido para la accion" se comprueba en la base, no solo en la aplicacion.
# La lista de codigos y de parejas se repite literalmente en la migracion (una migracion es una
# foto congelada y no debe importar codigo vivo); hay una prueba que compara ambas copias con el
# catalogo para que no puedan divergir en silencio.
_REASON_CODES: tuple[str, ...] = (
    "ADMINISTRATIVE",
    "CATEGORY_CHANGE",
    "DATA_CORRECTION",
    "DATA_ERROR",
    "DATA_VERIFIED",
    "DUPLICATE",
    "ELIGIBILITY",
    "ELIGIBILITY_LOST",
    "ELIGIBILITY_REGAINED",
    "MISTAKEN_WITHDRAWAL",
    "PLANNED_ENTRY",
    "READY",
    "REPRESENTATION_CHANGE",
    "REQUESTED_BY_ATHLETE",
    "RULE_VIOLATION",
    "SCHEDULING_NO_SHOW",
    "WITHDRAWAL_REQUEST",
)
_REASON_NOTE_ALLOWED_CODES: tuple[str, ...] = ("ADMINISTRATIVE", "DATA_ERROR", "RULE_VIOLATION")
_REASON_NOTE_MAX_LENGTH = 200
_REASON_CODE_ACTION_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("competitor", "CREATE", "PLANNED_ENTRY"),
    ("competitor", "UPDATE", "DATA_CORRECTION"),
    ("competitor", "DEACTIVATE", "REQUESTED_BY_ATHLETE"),
    ("competitor", "DEACTIVATE", "DUPLICATE"),
    ("competitor", "DEACTIVATE", "DATA_ERROR"),
    ("competitor", "DEACTIVATE", "ADMINISTRATIVE"),
    ("competitor", "ACTIVATE", "REQUESTED_BY_ATHLETE"),
    ("competitor", "ACTIVATE", "DUPLICATE"),
    ("competitor", "ACTIVATE", "DATA_ERROR"),
    ("competitor", "ACTIVATE", "ADMINISTRATIVE"),
    ("tournament_registration", "CREATE", "PLANNED_ENTRY"),
    ("tournament_registration", "UPDATE", "DATA_CORRECTION"),
    ("tournament_registration", "UPDATE", "CATEGORY_CHANGE"),
    ("tournament_registration", "UPDATE", "REPRESENTATION_CHANGE"),
    ("tournament_registration", "CONFIRM", "READY"),
    ("tournament_registration", "CONFIRM", "DATA_VERIFIED"),
    ("tournament_registration", "WITHDRAW", "WITHDRAWAL_REQUEST"),
    ("tournament_registration", "WITHDRAW", "DUPLICATE"),
    ("tournament_registration", "WITHDRAW", "ELIGIBILITY_LOST"),
    ("tournament_registration", "WITHDRAW", "ADMINISTRATIVE"),
    ("tournament_registration", "REINSTATE", "MISTAKEN_WITHDRAWAL"),
    ("tournament_registration", "REINSTATE", "ELIGIBILITY_REGAINED"),
    ("tournament_registration", "REINSTATE", "ADMINISTRATIVE"),
    ("tournament_registration", "DISQUALIFY", "SCHEDULING_NO_SHOW"),
    ("tournament_registration", "DISQUALIFY", "RULE_VIOLATION"),
    ("tournament_registration", "DISQUALIFY", "ELIGIBILITY"),
    ("tournament_registration", "DISQUALIFY", "ADMINISTRATIVE"),
)
_REASON_CODE_CATALOG_CHECK = (
    "reason_code IS NULL OR reason_code IN ("
    + ", ".join(f"'{code}'" for code in _REASON_CODES)
    + ")"
)
_REASON_CODE_ACTION_CHECK = (
    "reason_code IS NULL OR (entity, action, reason_code) IN ("
    + ", ".join(
        f"('{entity}', '{action}', '{code}')" for entity, action, code in _REASON_CODE_ACTION_PAIRS
    )
    + ")"
)
_REASON_NOTE_CHECK = (
    "reason_note IS NULL OR (reason_code IN ("
    + ", ".join(f"'{code}'" for code in _REASON_NOTE_ALLOWED_CODES)
    + f") AND char_length(reason_note) BETWEEN 1 AND {_REASON_NOTE_MAX_LENGTH}"
    " AND reason_note = btrim(reason_note) AND reason_note !~ '[[:cntrl:]]')"
)

domain_change_log = Table(
    "domain_change_log",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column("entity", String, nullable=False),
    Column("entity_id", BigInteger, nullable=False),
    Column("action", String, nullable=False),
    Column("changed_fields", ARRAY(String), nullable=False, server_default=text("'{}'")),
    Column(
        "actor_user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("actor_label", String, nullable=True),
    Column("reason", Text, nullable=False),
    Column("created", DateTimeTZ, nullable=False, server_default=func.now()),
    # S3.3a: atribucion explicita de tenant. NULLABLE a proposito: las filas historicas no
    # tienen tenant derivable y las entidades de plataforma no tienen tenant. NULL significa
    # "anterior a la migracion / sin tenant derivable", nunca "sin tenant". La coherencia
    # (tenant del evento == tenant de su entidad) no es expresable con un CHECK simple porque
    # cruza tablas: la garantiza ``sql_insert_domain_change_log`` y la verifica la suite.
    Column(
        "tenant_club_id",
        BigInteger,
        ForeignKey("clubs.id", ondelete="RESTRICT", name="fk_domain_change_log_tenant_club_id"),
        nullable=True,
    ),
    # S3.3c-2: motivo cerrado del evento. ``reason`` (texto canonico legible) se mantiene como
    # columna de lectura; el motivo estructurado vive aqui y lo valida la base.
    Column("reason_code", String(32), nullable=True),
    Column("reason_note", Text, nullable=True),
    CheckConstraint(_REASON_CODE_CATALOG_CHECK, name="ck_domain_change_log_reason_code_catalog"),
    CheckConstraint(_REASON_CODE_ACTION_CHECK, name="ck_domain_change_log_reason_code_action"),
    CheckConstraint(_REASON_NOTE_CHECK, name="ck_domain_change_log_reason_note"),
    Index("ix_domain_change_log_entity", "entity", "entity_id"),
    Index("ix_domain_change_log_tenant_created", "tenant_club_id", "created"),
)

# Cuotas configurables por tenant (override del valor por defecto de la cuenta). Es
# configuracion, no historico: se elimina con el tenant.
tenant_quota_overrides = Table(
    "tenant_quota_overrides",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column(
        "club_id",
        BigInteger,
        ForeignKey("clubs.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    ),
    Column("quota_key", String, nullable=False),
    Column("quota_value", Integer, nullable=False),
    Column(
        "updated_by_user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("updated_at", DateTimeTZ, nullable=False, server_default=func.now()),
    UniqueConstraint("club_id", "quota_key", name="uq_tenant_quota_overrides_club_key"),
)

# --- S3.3c-3: claves de idempotencia ---------------------------------------------
# Almacen de claves de idempotencia por tenant y actor. Guarda **como se reconocio** una peticion
# (huella HMAC), **en que estado** quedo y **que metadatos de respuesta** puede repetir: nunca el
# cuerpo de la peticion ni la respuesta completa.
#
# Estados cerrados: no existe un estado "confirmado" aparte. Una operacion confirmada **es** una
# fila COMPLETED con su respuesta: admitir un tercer estado dejaria una operacion COMMITTED sin su
# registro de idempotencia, que es justo lo que hay que impedir.
#
# Retencion: ``expires_at`` es obligatorio (provisional: 24 h). No hay purga automatica y una fila
# caducada **no** se reutiliza ni se borra: se declara caducada. La purga y la ventana real son
# decisiones de S3.3c-4.
_K_IDEMPOTENCY_STATES: tuple[str, ...] = ("IN_PROGRESS", "COMPLETED")
_K_IDEMPOTENCY_METHODS: tuple[str, ...] = ("POST", "PUT", "PATCH", "DELETE")
_K_IDEMPOTENCY_RESPONSE_KEYS: tuple[str, ...] = (
    "audit_event_id",
    "competitor_id",
    "registration_status",
    "resource_id",
    "resource_type",
)
_K_IDEMPOTENCY_KEY_MIN_LENGTH = 8
_K_IDEMPOTENCY_KEY_MAX_LENGTH = 255
_K_IDEMPOTENCY_RESPONSE_BODY_MAX_BYTES = 2048

_IDEMPOTENCY_STATE_CHECK = (
    "state IN (" + ", ".join(f"'{state}'" for state in _K_IDEMPOTENCY_STATES) + ")"
)
_IDEMPOTENCY_PENDING_SHAPE_CHECK = (
    "state <> 'IN_PROGRESS' OR (completed_at IS NULL AND response_status IS NULL"
    " AND response_body IS NULL AND resource_type IS NULL AND resource_id IS NULL)"
)
_IDEMPOTENCY_COMPLETED_SHAPE_CHECK = (
    "state <> 'COMPLETED' OR (completed_at IS NOT NULL AND response_status IS NOT NULL)"
)
_IDEMPOTENCY_RESOURCE_COHERENCE_CHECK = "(resource_type IS NULL) = (resource_id IS NULL)"
_IDEMPOTENCY_METHOD_CHECK = (
    "request_method IN (" + ", ".join(f"'{method}'" for method in _K_IDEMPOTENCY_METHODS) + ")"
)
_IDEMPOTENCY_PATH_CHECK = "char_length(request_path) BETWEEN 1 AND 255 AND request_path LIKE '/%'"
_IDEMPOTENCY_KEY_SHAPE_CHECK = (
    f"char_length(idempotency_key) BETWEEN {_K_IDEMPOTENCY_KEY_MIN_LENGTH} "
    f"AND {_K_IDEMPOTENCY_KEY_MAX_LENGTH} AND idempotency_key ~ '^[A-Za-z0-9._:-]+$'"
)
_IDEMPOTENCY_FINGERPRINT_CHECK = "request_fingerprint ~ '^[0-9a-f]{64}$'"
_IDEMPOTENCY_KEY_VERSION_CHECK = "fingerprint_key_version ~ '^v[0-9]{1,3}$'"
_IDEMPOTENCY_RESPONSE_STATUS_CHECK = (
    "response_status IS NULL OR response_status BETWEEN 100 AND 599"
)
_IDEMPOTENCY_RESPONSE_BODY_CHECK = (
    "response_body IS NULL OR (jsonb_typeof(response_body) = 'object'"
    " AND response_body - ARRAY["
    + ", ".join(f"'{key}'" for key in _K_IDEMPOTENCY_RESPONSE_KEYS)
    + "]::text[] = '{}'::jsonb"
    " AND octet_length(response_body::text) <= "
    + str(_K_IDEMPOTENCY_RESPONSE_BODY_MAX_BYTES)
    + ' AND NOT jsonb_path_exists(response_body, \'$.* ? (@.type() == "object"'
    ' || @.type() == "array")\'))'
)
_IDEMPOTENCY_EXPIRY_CHECK = "expires_at > created"

domain_idempotency_keys = Table(
    "domain_idempotency_keys",
    metadata,
    Column("id", BigInteger, primary_key=True, index=True),
    Column(
        "tenant_club_id",
        BigInteger,
        ForeignKey(
            "clubs.id", ondelete="CASCADE", name="fk_domain_idempotency_keys_tenant_club_id"
        ),
        index=True,
        nullable=False,
    ),
    Column(
        "actor_user_id",
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_domain_idempotency_keys_actor_user_id"),
        nullable=False,
    ),
    Column("idempotency_key", String(_K_IDEMPOTENCY_KEY_MAX_LENGTH), nullable=False),
    Column("request_fingerprint", String(64), nullable=False),
    Column("fingerprint_key_version", String(16), nullable=False),
    Column("request_method", String(10), nullable=False),
    Column("request_path", String(255), nullable=False),
    Column("state", String(16), nullable=False),
    Column("resource_type", String(32), nullable=True),
    Column("resource_id", BigInteger, nullable=True),
    Column("response_status", SmallInteger, nullable=True),
    Column("response_body", JSONB, nullable=True),
    Column("created", DateTimeTZ, nullable=False, server_default=func.now()),
    Column("expires_at", DateTimeTZ, nullable=False),
    Column("completed_at", DateTimeTZ, nullable=True),
    CheckConstraint(_IDEMPOTENCY_STATE_CHECK, name="ck_domain_idempotency_keys_state"),
    CheckConstraint(
        _IDEMPOTENCY_PENDING_SHAPE_CHECK, name="ck_domain_idempotency_keys_pending_shape"
    ),
    CheckConstraint(
        _IDEMPOTENCY_COMPLETED_SHAPE_CHECK, name="ck_domain_idempotency_keys_completed_shape"
    ),
    CheckConstraint(
        _IDEMPOTENCY_RESOURCE_COHERENCE_CHECK,
        name="ck_domain_idempotency_keys_resource_coherence",
    ),
    CheckConstraint(_IDEMPOTENCY_METHOD_CHECK, name="ck_domain_idempotency_keys_request_method"),
    CheckConstraint(_IDEMPOTENCY_PATH_CHECK, name="ck_domain_idempotency_keys_request_path"),
    CheckConstraint(_IDEMPOTENCY_KEY_SHAPE_CHECK, name="ck_domain_idempotency_keys_key_shape"),
    CheckConstraint(_IDEMPOTENCY_FINGERPRINT_CHECK, name="ck_domain_idempotency_keys_fingerprint"),
    CheckConstraint(_IDEMPOTENCY_KEY_VERSION_CHECK, name="ck_domain_idempotency_keys_key_version"),
    CheckConstraint(
        _IDEMPOTENCY_RESPONSE_STATUS_CHECK, name="ck_domain_idempotency_keys_response_status"
    ),
    CheckConstraint(
        _IDEMPOTENCY_RESPONSE_BODY_CHECK, name="ck_domain_idempotency_keys_response_body"
    ),
    CheckConstraint(_IDEMPOTENCY_EXPIRY_CHECK, name="ck_domain_idempotency_keys_expiry"),
    UniqueConstraint(
        "tenant_club_id",
        "actor_user_id",
        "idempotency_key",
        name="uq_domain_idempotency_keys_tenant_actor_key",
    ),
    # Indice para el barrido de caducidad de S3.3c-4 (aqui no hay purga todavia).
    Index("ix_domain_idempotency_keys_expires_at", "expires_at"),
)
