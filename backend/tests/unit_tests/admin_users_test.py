"""Tests de la gestion administrativa de usuarios.

Cubren, sin necesidad de PostgreSQL:

A. signup publico cerrado cuando ``allow_user_registration`` es False
B. un REGULAR no accede a los endpoints de administracion (403) y un ADMIN si
C. todas las rutas /users/admin* exigen la dependencia ``user_is_admin``
D. un usuario inactivo no se autentica (ni /token ni JWT ya emitido)
E. no se puede dejar el sistema sin administradores activos
F. no se permite duplicar un email desde administracion
G. bcrypt: hash correcto y nunca texto plano
H. ninguna respuesta de la API expone ``password_hash``
I. el formulario de /create-account ya no es utilizable (frontend)
"""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from heliclockter import datetime_utc, timedelta

from bracket import config as config_module
from bracket.app import app
from bracket.models.db.account import UserAccountType
from bracket.models.db.user import (
    DemoUserToRegister,
    User,
    UserAccountTypeToUpdate,
    UserActiveToUpdate,
    UserPublic,
    UserToCreateByAdmin,
    UserToRegister,
)
from bracket.routes import auth as routes_auth
from bracket.routes import users as routes_users
from bracket.routes.models import UserPublicResponse, UsersPublicResponse
from bracket.utils.id_types import UserId
from bracket.utils.security import hash_password, verify_password

PASSWORD = "ContrasenaDePrueba123"
# El prefijo de la API viene de la configuracion (vacio por defecto, "/api" en
# produccion), asi que las rutas se construyen con el para no depender del entorno.
API = config_module.config.api_prefix
ADMIN_ROUTES = [
    (f"{API}/users/admin", "GET"),
    (f"{API}/users/admin", "POST"),
    (f"{API}/users/admin/{{user_id}}/password", "PUT"),
    (f"{API}/users/admin/{{user_id}}/active", "PUT"),
    (f"{API}/users/admin/{{user_id}}/account-type", "PUT"),
]


def make_user(
    user_id: int = 1,
    account_type: UserAccountType = UserAccountType.ADMIN,
    active: bool = True,
) -> UserPublic:
    return UserPublic(
        id=UserId(user_id),
        email=f"user{user_id}@example.org",
        name=f"User {user_id}",
        created=datetime_utc.now(),
        account_type=account_type,
        active=active,
    )


def make_user_in_db(
    user_id: int = 1,
    account_type: UserAccountType = UserAccountType.ADMIN,
    active: bool = True,
) -> Any:
    return User(
        id=UserId(user_id),
        email=f"user{user_id}@example.org",
        name=f"User {user_id}",
        created=datetime_utc.now(),
        account_type=account_type,
        active=active,
        password_hash=hash_password(PASSWORD),
    )


def route_dependency_names(path: str, method: str) -> set[str]:
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            dependant = getattr(route, "dependant", None)
            if dependant is None:
                continue
            return {
                dependency.call.__name__
                for dependency in dependant.dependencies
                if dependency.call is not None
            }
    raise AssertionError(f"ruta no encontrada: {method} {path}")


# A. signup publico cerrado
async def test_signup_publico_cerrado(monkeypatch: pytest.MonkeyPatch) -> None:
    closed_config = config_module.config.model_copy(update={"allow_user_registration": False})
    monkeypatch.setattr(routes_users, "config", closed_config)

    with pytest.raises(HTTPException) as exc:
        await routes_users.register_user(
            UserToRegister(
                email="nuevo@example.org", name="Nuevo", password=PASSWORD, captcha_token="x"
            )
        )

    assert exc.value.status_code == 401


async def test_signup_demo_cerrado(monkeypatch: pytest.MonkeyPatch) -> None:
    closed_config = config_module.config.model_copy(update={"allow_demo_user_registration": False})
    monkeypatch.setattr(routes_users, "config", closed_config)

    with pytest.raises(HTTPException) as exc:
        await routes_users.register_demo_user(DemoUserToRegister(captcha_token="x"))

    assert exc.value.status_code == 401


# B. REGULAR no, ADMIN si
async def test_regular_no_es_admin() -> None:
    regular = make_user(account_type=UserAccountType.REGULAR)
    with pytest.raises(HTTPException) as exc:
        await routes_auth.user_is_admin(regular)
    assert exc.value.status_code == 403


async def test_admin_si_es_admin() -> None:
    admin = make_user(account_type=UserAccountType.ADMIN)
    assert await routes_auth.user_is_admin(admin) == admin


async def test_demo_no_es_admin() -> None:
    demo = make_user(account_type=UserAccountType.DEMO)
    with pytest.raises(HTTPException) as exc:
        await routes_auth.user_is_admin(demo)
    assert exc.value.status_code == 403


# C. las rutas de administracion exigen la dependencia
def test_rutas_admin_protegidas() -> None:
    for path, method in ADMIN_ROUTES:
        assert "user_is_admin" in route_dependency_names(path, method), f"{method} {path}"


def test_rutas_admin_declaradas_antes_del_comodin() -> None:
    """``/users/admin`` no debe casar con ``/users/{user_id}``.

    El comodin de path no lleva convertidor de tipo: si ``/users/{user_id}`` se
    declarase antes, ``admin`` casaria con el y FastAPI devolveria 422.
    """
    paths = [getattr(route, "path", "") for route in app.routes]
    assert paths.index(f"{API}/users/admin") < paths.index(f"{API}/users/{{user_id}}")


def test_signup_publico_sin_dependencia_admin() -> None:
    assert "user_is_admin" not in route_dependency_names(f"{API}/users/register", "POST")


# D. usuario inactivo
async def test_usuario_inactivo_no_obtiene_token(monkeypatch: pytest.MonkeyPatch) -> None:
    inactive = make_user_in_db(active=False)

    async def fake_get_user(email: str) -> Any:
        return inactive

    monkeypatch.setattr(routes_auth, "get_user", fake_get_user)
    assert await routes_auth.authenticate_user(inactive.email, PASSWORD) is None


async def test_usuario_inactivo_no_usa_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    inactive = make_user_in_db(active=False)
    token = routes_auth.create_access_token(
        data={"user": inactive.email}, expires_delta=timedelta(minutes=5)
    )

    async def fake_get_user(email: str) -> Any:
        return inactive

    monkeypatch.setattr(routes_auth, "get_user", fake_get_user)
    assert await routes_auth.check_jwt_and_get_user(token) is None

    with pytest.raises(HTTPException) as exc:
        await routes_auth.user_authenticated(token)
    assert exc.value.status_code == 401


async def test_usuario_activo_si_obtiene_token(monkeypatch: pytest.MonkeyPatch) -> None:
    user = make_user_in_db(account_type=UserAccountType.REGULAR, active=True)

    async def fake_get_user(email: str) -> Any:
        return user

    monkeypatch.setattr(routes_auth, "get_user", fake_get_user)
    assert await routes_auth.authenticate_user(user.email, PASSWORD) == user


# E. nunca cero administradores activos
async def test_no_se_puede_desactivar_al_ultimo_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = make_user(account_type=UserAccountType.ADMIN)

    async def fake_get_user_by_id(user_id: UserId) -> UserPublic:
        return admin

    async def fake_count() -> int:
        return 1

    monkeypatch.setattr(routes_users, "get_user_by_id", fake_get_user_by_id)
    monkeypatch.setattr(routes_users, "count_active_admins", fake_count)

    with pytest.raises(HTTPException) as exc:
        await routes_users.put_user_active_admin(admin.id, UserActiveToUpdate(active=False), admin)
    assert exc.value.status_code == 400
    assert "last active administrator" in str(exc.value.detail)


async def test_no_se_puede_degradar_al_ultimo_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = make_user(account_type=UserAccountType.ADMIN)

    async def fake_get_user_by_id(user_id: UserId) -> UserPublic:
        return admin

    async def fake_count() -> int:
        return 1

    monkeypatch.setattr(routes_users, "get_user_by_id", fake_get_user_by_id)
    monkeypatch.setattr(routes_users, "count_active_admins", fake_count)

    with pytest.raises(HTTPException) as exc:
        await routes_users.put_user_account_type_admin(
            admin.id, UserAccountTypeToUpdate(account_type=UserAccountType.REGULAR), admin
        )
    assert exc.value.status_code == 400


async def test_con_dos_admins_si_se_puede_desactivar(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = make_user(user_id=7, account_type=UserAccountType.ADMIN)
    updated: dict[str, Any] = {"active": True}

    async def fake_get_user_by_id(user_id: UserId) -> UserPublic:
        return admin.model_copy(update={"active": updated["active"]})

    async def fake_count() -> int:
        return 2

    async def fake_update(user_id: UserId, active: bool) -> None:
        updated["active"] = active

    monkeypatch.setattr(routes_users, "get_user_by_id", fake_get_user_by_id)
    monkeypatch.setattr(routes_users, "count_active_admins", fake_count)
    monkeypatch.setattr(routes_users, "update_user_active", fake_update)

    response = await routes_users.put_user_active_admin(
        admin.id, UserActiveToUpdate(active=False), admin
    )
    assert response.data.active is False


async def test_a_un_regular_no_se_le_aplica_el_guardia(monkeypatch: pytest.MonkeyPatch) -> None:
    """El guardia solo protege a administradores activos, no a cuentas normales."""
    regular = make_user(user_id=9, account_type=UserAccountType.REGULAR)
    updated: dict[str, Any] = {"active": True}

    async def fake_get_user_by_id(user_id: UserId) -> UserPublic:
        return regular.model_copy(update={"active": updated["active"]})

    async def fake_count() -> int:
        raise AssertionError("no deberia consultar administradores para un REGULAR")

    async def fake_update(user_id: UserId, active: bool) -> None:
        updated["active"] = active

    monkeypatch.setattr(routes_users, "get_user_by_id", fake_get_user_by_id)
    monkeypatch.setattr(routes_users, "count_active_admins", fake_count)
    monkeypatch.setattr(routes_users, "update_user_active", fake_update)

    response = await routes_users.put_user_active_admin(
        regular.id, UserActiveToUpdate(active=False), regular
    )
    assert response.data.active is False


# F. email duplicado
async def test_no_se_puede_duplicar_email(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = make_user()

    async def fake_in_use(email: str) -> bool:
        return True

    async def fake_create(user: Any) -> Any:
        raise AssertionError("no deberia intentar crear un usuario con email duplicado")

    monkeypatch.setattr(routes_users, "check_whether_email_is_in_use", fake_in_use)
    monkeypatch.setattr(routes_users, "create_user", fake_create)

    with pytest.raises(HTTPException) as exc:
        await routes_users.create_user_admin(
            UserToCreateByAdmin(email="repe@example.org", name="Repe", password=PASSWORD), admin
        )
    assert exc.value.status_code == 400


def test_no_se_puede_crear_un_demo_desde_administracion() -> None:
    with pytest.raises(ValueError):
        UserToCreateByAdmin(
            email="demo@example.org",
            name="Demo",
            password=PASSWORD,
            account_type=UserAccountType.DEMO,
        )


def test_password_demasiado_corta_rechazada() -> None:
    with pytest.raises(ValueError):
        UserToCreateByAdmin(email="corta@example.org", name="Corta", password="1234567")


# G. bcrypt
def test_bcrypt_hash_correcto() -> None:
    hashed = hash_password(PASSWORD)
    assert hashed != PASSWORD
    assert hashed.startswith("$2")
    assert verify_password(PASSWORD, hashed)
    assert not verify_password("otra-contrasena", hashed)


def test_bcrypt_salt_aleatorio() -> None:
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


# H. sin password_hash en la API
def test_ningun_modelo_publico_expone_password_hash() -> None:
    for model in (UserPublic, UserPublicResponse, UsersPublicResponse):
        assert "password_hash" not in json.dumps(model.model_json_schema())


def test_conversion_a_publico_descarta_el_hash() -> None:
    user = make_user_in_db()
    public = routes_users._to_user_public(user)
    assert "password_hash" not in public.model_dump()
    assert "password_hash" not in json.dumps(public.model_dump(), default=str)


def test_ninguna_ruta_devuelve_password_hash() -> None:
    """Ningun esquema de respuesta declara un campo ``password_hash``.

    Se comprueban las PROPIEDADES de cada modelo del OpenAPI (no el texto
    completo: las descripciones pueden nombrar el campo sin exponerlo).
    """
    schemas = app.openapi()["components"]["schemas"]
    offenders = [
        name
        for name, definition in schemas.items()
        if "password_hash" in definition.get("properties", {})
    ]
    assert offenders == []


# I. frontend: /create-account sin formulario
def create_account_source() -> str:
    path = Path(__file__).resolve().parents[3] / "frontend" / "src" / "pages" / "create_account.tsx"
    return path.read_text(encoding="utf8")


def test_create_account_sin_formulario() -> None:
    source = create_account_source()
    assert "<form" not in source
    assert "HCaptchaInput" not in source
    assert 'type="submit"' not in source
    assert "captcha" not in source.lower()


def test_create_account_menciona_al_administrador() -> None:
    source = create_account_source()
    assert "create_account_alert_description" in source
    assert "/login" in source

    locale = (
        Path(__file__).resolve().parents[3]
        / "frontend"
        / "public"
        / "locales"
        / "es"
        / "common.json"
    )
    assert (
        json.loads(locale.read_text(encoding="utf8"))["create_account_alert_description"]
        == "Las cuentas son creadas por un administrador."
    )
