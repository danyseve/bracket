from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from heliclockter import datetime_utc, timedelta
from starlette import status

from bracket.config import config
from bracket.logic.subscriptions import setup_demo_account
from bracket.models.db.account import UserAccountType
from bracket.models.db.user import (
    DemoUserToRegister,
    User,
    UserAccountTypeToUpdate,
    UserActiveToUpdate,
    UserInsertable,
    UserPasswordToUpdate,
    UserPublic,
    UserToCreateByAdmin,
    UserToRegister,
    UserToUpdate,
)
from bracket.routes.auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    Token,
    create_access_token,
    user_authenticated,
    user_is_admin,
)
from bracket.routes.models import (
    SuccessResponse,
    TokenResponse,
    UserPublicResponse,
    UsersPublicResponse,
)
from bracket.sql.users import (
    check_whether_email_is_in_use,
    count_active_admins,
    create_user,
    get_all_users,
    get_user_by_id,
    update_user,
    update_user_account_type,
    update_user_active,
    update_user_password,
)
from bracket.utils.id_types import UserId
from bracket.utils.security import hash_password, verify_captcha_token
from bracket.utils.types import assert_some

router = APIRouter(prefix=config.api_prefix)


def _to_user_public(user: User | UserPublic) -> UserPublic:
    """Convierte a ``UserPublic`` campo a campo: ``password_hash`` nunca viaja."""
    return UserPublic(
        id=user.id,
        email=user.email,
        name=user.name,
        created=user.created,
        account_type=user.account_type,
        active=user.active,
    )


async def _ensure_active_admin_remains(target: UserPublic) -> None:
    """Bloquea una operacion que dejaria el sistema sin administradores activos."""
    if target.account_type != UserAccountType.ADMIN or not target.active:
        return

    if await count_active_admins() <= 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Cannot deactivate or demote the last active administrator",
        )


@router.get("/users/me", response_model=UserPublicResponse)
async def get_user(user_public: UserPublic = Depends(user_authenticated)) -> UserPublicResponse:
    return UserPublicResponse(data=user_public)


# Administracion de usuarios (solo ADMIN).
#
# IMPORTANTE: estas rutas se declaran ANTES de ``/users/{user_id}``. El comodin
# de path no lleva convertidor de tipo, asi que "admin" casaria con ``user_id``
# y FastAPI devolveria 422 sin llegar nunca a estas funciones.
@router.get("/users/admin", response_model=UsersPublicResponse)
async def get_users_admin(
    _admin: UserPublic = Depends(user_is_admin),
) -> UsersPublicResponse:
    """Lista todos los usuarios. Nunca incluye el hash de la contrasena."""
    return UsersPublicResponse(data=await get_all_users())


@router.post("/users/admin", response_model=UserPublicResponse)
async def create_user_admin(
    user_to_create: UserToCreateByAdmin,
    _admin: UserPublic = Depends(user_is_admin),
) -> UserPublicResponse:
    """Crea una cuenta. La contrasena se guarda unicamente como hash bcrypt."""
    if await check_whether_email_is_in_use(user_to_create.email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email address already in use")

    user = await create_user(
        UserInsertable(
            email=user_to_create.email,
            name=user_to_create.name,
            password_hash=hash_password(user_to_create.password),
            created=datetime_utc.now(),
            account_type=user_to_create.account_type,
        )
    )
    return UserPublicResponse(data=_to_user_public(user))


@router.put("/users/admin/{user_id}/password", response_model=SuccessResponse)
async def put_user_password_admin(
    user_id: UserId,
    user_to_update: UserPasswordToUpdate,
    _admin: UserPublic = Depends(user_is_admin),
) -> SuccessResponse:
    """Restablece la contrasena de otro usuario (no se devuelve ni se registra)."""
    if await get_user_by_id(user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    await update_user_password(user_id, hash_password(user_to_update.password))
    return SuccessResponse()


@router.put("/users/admin/{user_id}/active", response_model=UserPublicResponse)
async def put_user_active_admin(
    user_id: UserId,
    user_to_update: UserActiveToUpdate,
    _admin: UserPublic = Depends(user_is_admin),
) -> UserPublicResponse:
    target = await get_user_by_id(user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    if not user_to_update.active:
        await _ensure_active_admin_remains(target)

    await update_user_active(user_id, user_to_update.active)
    return UserPublicResponse(data=_to_user_public(assert_some(await get_user_by_id(user_id))))


@router.put("/users/admin/{user_id}/account-type", response_model=UserPublicResponse)
async def put_user_account_type_admin(
    user_id: UserId,
    user_to_update: UserAccountTypeToUpdate,
    _admin: UserPublic = Depends(user_is_admin),
) -> UserPublicResponse:
    target = await get_user_by_id(user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    if user_to_update.account_type != UserAccountType.ADMIN:
        await _ensure_active_admin_remains(target)

    await update_user_account_type(user_id, user_to_update.account_type)
    return UserPublicResponse(data=_to_user_public(assert_some(await get_user_by_id(user_id))))


@router.get("/users/{user_id}", response_model=UserPublicResponse)
async def get_me(
    user_id: UserId, user_public: UserPublic = Depends(user_authenticated)
) -> UserPublicResponse:
    if user_public.id != user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Can't view details of this user")

    return UserPublicResponse(data=user_public)


@router.put("/users/{user_id}", response_model=UserPublicResponse)
async def update_user_details(
    user_id: UserId,
    user_to_update: UserToUpdate,
    user_public: UserPublic = Depends(user_authenticated),
) -> UserPublicResponse:
    if user_public.id != user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Can't change details of this user")

    await update_user(user_public.id, user_to_update)
    user_updated = await get_user_by_id(user_id)
    return UserPublicResponse(data=assert_some(user_updated))


@router.put("/users/{user_id}/password", response_model=SuccessResponse)
async def put_user_password(
    user_id: UserId,
    user_to_update: UserPasswordToUpdate,
    user_public: UserPublic = Depends(user_authenticated),
) -> SuccessResponse:
    if user_public.id != user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Can't change details of this user")
    await update_user_password(user_public.id, hash_password(user_to_update.password))
    return SuccessResponse()


@router.post("/users/register", response_model=TokenResponse)
async def register_user(user_to_register: UserToRegister) -> TokenResponse:
    if not config.allow_user_registration:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account creation is unavailable for now")

    if not await verify_captcha_token(user_to_register.captcha_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Failed to validate captcha")

    user = UserInsertable(
        email=user_to_register.email,
        password_hash=hash_password(user_to_register.password),
        name=user_to_register.name,
        created=datetime_utc.now(),
        account_type=UserAccountType.REGULAR,
    )
    if await check_whether_email_is_in_use(user.email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email address already in use")

    user_created = await create_user(user)
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"user": user_created.email}, expires_delta=access_token_expires
    )
    return TokenResponse(
        data=Token(access_token=access_token, token_type="bearer", user_id=user_created.id)
    )


@router.post("/users/register_demo", response_model=TokenResponse)
async def register_demo_user(user_to_register: DemoUserToRegister) -> TokenResponse:
    if not config.allow_demo_user_registration:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Demo account creation is unavailable for now"
        )

    if not await verify_captcha_token(user_to_register.captcha_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Failed to validate captcha")

    username = f"demo-{uuid4()}"
    user = UserInsertable(
        email=f"{username}@example.org",
        password_hash=hash_password(str(uuid4())),
        name=username,
        created=datetime_utc.now(),
        account_type=UserAccountType.DEMO,
    )
    if await check_whether_email_is_in_use(user.email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email address already in use")

    user_created = await create_user(user)
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"user": user_created.email}, expires_delta=access_token_expires
    )
    await setup_demo_account(user_created.id)
    return TokenResponse(
        data=Token(access_token=access_token, token_type="bearer", user_id=user_created.id)
    )
