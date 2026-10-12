"""S4.5 — contrato de la pantalla publica (LAB ONLY).

Fija dos cosas que el organizador, la API publica y la pantalla TV/tablet tienen que contar igual:

* **quien es AHORA y quien es SIGUIENTE** en cada tatami (§12);
* **que estado** tiene cada combate (§16): ``NOW``, ``NEXT``, ``UPCOMING``, ``WAITING``,
  ``COMPLETED`` y ``DELAYED``.

El modulo es puro: recibe combates ya planificados (plan + estimacion + quien pelea) y devuelve la
vista por tatami y el cuerpo JSON publico. No toca base de datos, ni HTTP, ni nombres de columnas.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from heliclockter import datetime_utc

UTC = ZoneInfo("UTC")

#: Estados de un combate en la pantalla (§16). ``DELAYED`` sustituye a ``NOW``/``NEXT``/``UPCOMING``
#: cuando el tatami lleva retraso, porque es lo unico que el organizador necesita ver.
STATE_NOW = "NOW"
STATE_NEXT = "NEXT"
STATE_UPCOMING = "UPCOMING"
STATE_WAITING = "WAITING"
STATE_COMPLETED = "COMPLETED"
STATE_DELAYED = "DELAYED"

#: Claves permitidas en el cuerpo publico. Todo lo que no este aqui no sale de aqui (§11).
PUBLIC_MATCH_KEYS = frozenset(
    {
        "category",
        "estimated_start",
        "fighter_a",
        "fighter_b",
        "match_id",
        "position_in_schedule",
        "round_name",
        "scheduled_start",
        "state",
        "waiting_for",
    }
)


@dataclass(frozen=True)
class PublicMatch:
    """Un combate visto desde la grada: lo deportivo, sin registro de cuenta ni auditoria."""

    match_id: int
    court_id: int | None
    category: str
    round_name: str
    fighter_a: str | None
    fighter_b: str | None
    scheduled_start: datetime_utc | None
    estimated_start: datetime_utc | None
    duration_minutes: int
    margin_minutes: int
    #: Etiquetas legibles de lo que falta ("Ganador combate 10"). Vacio si ya se sabe quien pelea.
    waiting_for: tuple[str, ...] = ()
    finished_at: datetime_utc | None = None
    position_in_schedule: int | None = None
    playable: bool = True

    @property
    def has_fighters(self) -> bool:
        return self.fighter_a is not None and self.fighter_b is not None


@dataclass(frozen=True)
class DisplayEntry:
    match_id: int
    category: str
    round_name: str
    fighter_a: str | None
    fighter_b: str | None
    scheduled_start: datetime_utc | None
    estimated_start: datetime_utc | None
    state: str
    position_in_schedule: int | None
    waiting_for: tuple[str, ...]
    delay_minutes: int

    def to_public_dict(self) -> dict[str, object]:
        """Cuerpo que se sirve por HTTP: solo las claves de :data:`PUBLIC_MATCH_KEYS`."""
        return {
            "match_id": self.match_id,
            "category": self.category,
            "round_name": self.round_name,
            "fighter_a": self.fighter_a,
            "fighter_b": self.fighter_b,
            "scheduled_start": _iso(self.scheduled_start),
            "estimated_start": _iso(self.estimated_start),
            "state": self.state,
            "position_in_schedule": self.position_in_schedule,
            "waiting_for": list(self.waiting_for),
        }


@dataclass(frozen=True)
class CourtDisplay:
    court_id: int
    court_name: str
    #: Retraso observado del tatami en minutos (0 = en hora).
    lag_minutes: int
    current: DisplayEntry | None
    next: DisplayEntry | None
    upcoming: tuple[DisplayEntry, ...]
    #: Ultimos combates terminados, el mas reciente primero.
    completed: tuple[DisplayEntry, ...]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "court_id": self.court_id,
            "court_name": self.court_name,
            "lag_minutes": self.lag_minutes,
            "current": None if self.current is None else self.current.to_public_dict(),
            "next": None if self.next is None else self.next.to_public_dict(),
            "upcoming": [entry.to_public_dict() for entry in self.upcoming],
            "completed": [entry.to_public_dict() for entry in self.completed],
        }


def _iso(value: datetime_utc | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _state_for(entry: PublicMatch, role: str, *, delay_minutes: int) -> str:
    """Estado visible, en este orden: terminado > falta saber quien pelea > retraso > papel.

    Se prefiere ``WAITING`` a ``DELAYED`` porque al publico le importa mas saber que ese combate
    todavia no tiene rivales que saber que el tatami va tarde; el retraso va igualmente en
    ``delay_minutes``, que la pantalla puede mostrar aparte.
    """
    if entry.finished_at is not None:
        return STATE_COMPLETED
    if not entry.has_fighters:
        return STATE_WAITING
    if delay_minutes > 0:
        return STATE_DELAYED
    return role


def _entry(entry: PublicMatch, role: str, *, delay_minutes: int) -> DisplayEntry:
    return DisplayEntry(
        match_id=entry.match_id,
        category=entry.category,
        round_name=entry.round_name,
        fighter_a=entry.fighter_a,
        fighter_b=entry.fighter_b,
        scheduled_start=entry.scheduled_start,
        estimated_start=entry.estimated_start,
        state=_state_for(entry, role, delay_minutes=delay_minutes),
        position_in_schedule=entry.position_in_schedule,
        waiting_for=entry.waiting_for,
        delay_minutes=delay_minutes,
    )


def build_court_display(
    *,
    court_id: int,
    court_name: str,
    matches: list[PublicMatch] | tuple[PublicMatch, ...],
    lag_minutes: int = 0,
    upcoming_limit: int = 3,
    completed_limit: int = 3,
) -> CourtDisplay:
    """Vista de un tatami: AHORA, SIGUIENTE, los siguientes y lo ultimo terminado.

    El orden es el del plan (hora planificada, luego posicion, luego id): la pantalla no reordena
    nada por su cuenta. Los combates estructurales (bye/walkover) no son combates y no se muestran.
    """
    delay = max(0, int(lag_minutes))
    pending = sorted(
        (
            match
            for match in matches
            if match.playable and match.finished_at is None and match.court_id == court_id
        ),
        key=lambda match: (
            match.scheduled_start or datetime.datetime.min.replace(tzinfo=UTC),
            match.position_in_schedule if match.position_in_schedule is not None else 10**9,
            match.match_id,
        ),
    )
    done = sorted(
        (
            match
            for match in matches
            if match.playable and match.finished_at is not None and match.court_id == court_id
        ),
        key=lambda match: match.finished_at or datetime.datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )

    roles = [STATE_NOW, STATE_NEXT]
    entries = [
        _entry(match, roles[index] if index < len(roles) else STATE_UPCOMING, delay_minutes=delay)
        for index, match in enumerate(pending)
    ]

    return CourtDisplay(
        court_id=court_id,
        court_name=court_name,
        lag_minutes=delay,
        current=entries[0] if entries else None,
        next=entries[1] if len(entries) > 1 else None,
        upcoming=tuple(entries[2 : 2 + max(0, upcoming_limit)]),
        completed=tuple(
            _entry(match, STATE_COMPLETED, delay_minutes=0)
            for match in done[: max(0, completed_limit)]
        ),
    )


def to_public_payload(
    *,
    tournament_name: str,
    tournament_start_time: datetime_utc | None,
    courts: list[CourtDisplay],
    generated_at: datetime_utc,
) -> dict[str, object]:
    """Cuerpo de ``GET /public/tournaments/{tid}/schedule`` (§11).

    Solo datos deportivos del evento: nombre, hora de inicio, tatamis y sus combates. Ni correo, ni
    fecha de nacimiento, ni tokens, ni auditoria, ni identificadores internos de cuenta.
    """
    return {
        "tournament": {
            "name": tournament_name,
            "start_time": _iso(tournament_start_time),
        },
        "generated_at": _iso(generated_at),
        "courts": [court.to_public_dict() for court in courts],
    }


def assert_public_payload_is_safe(payload: dict[str, object]) -> None:
    """Comprueba que el cuerpo publico no lleva nada que no sea deportivo.

    Es la misma comprobacion que hace la prueba de privacidad, expuesta para que la usen tambien la
    capa HTTP y el E2E: una lista cerrada de claves, revisada de forma recursiva.
    """
    forbidden = {
        "email",
        "date_of_birth",
        "jwt",
        "token",
        "password",
        "audit",
        "audit_log",
        "user_id",
        "club_id",
        "created_by",
        "idempotency_key",
    }

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            leaf = path.rsplit(".", 1)[-1]
            is_match_entry = (
                leaf in ("current", "next") or ".upcoming" in path or ".completed" in path
            )
            if is_match_entry:
                assert set(node) <= PUBLIC_MATCH_KEYS, f"claves inesperadas en {path}: {set(node)}"
            for key, value in node.items():
                if str(key).lower() in forbidden:
                    raise AssertionError(f"el cuerpo publico expone {key!r} en {path}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    walk(payload, "$")
    json.dumps(payload)  # serializable: la ruta lo sirve tal cual
