"""Creating an account, in one place.

Both the interactive command and the container's first-start bootstrap need the
same rules, and password validation is the kind of thing that gets applied in one
of two paths and quietly skipped in the other.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError


class AccountError(Exception):
    """The account cannot be created as asked."""


def create_account(username: str, password: str) -> AbstractBaseUser:
    """Create an account, refusing a password the configured validators reject.

    Management commands do not run AUTH_PASSWORD_VALIDATORS on their own, so a
    twelve-character minimum would otherwise be a setting nobody ever applied.
    With Basic auth reachable from the internet, this password is the only real
    barrier in front of the account.
    """
    model = get_user_model()
    name = username.strip()
    if not name:
        raise AccountError("a username is required")
    if model.objects.filter(username=name).exists():
        raise AccountError(f"the account {name!r} already exists")

    try:
        validate_password(password, model(username=name))
    except ValidationError as error:
        raise AccountError("\n".join(error.messages)) from error

    return model.objects.create_user(username=name, password=password)
