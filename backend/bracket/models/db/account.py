from enum import auto

from bracket.utils.types import EnumAutoStr


class UserAccountType(EnumAutoStr):
    REGULAR = auto()
    DEMO = auto()
    # ``ADMIN`` no es un nivel de cuota como REGULAR/DEMO: es una autorizacion
    # administrativa explicita. Se comprueba con la dependencia
    # ``bracket.routes.auth.user_is_admin``.
    ADMIN = auto()
