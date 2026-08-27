"""Create the first account from the environment, once.

Runs on every container start, so it has to be safe to run when the account
already exists — which is the ordinary case on every restart after the first.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from gpodsync.accounts import AccountError, create_account
from gpodsync.config import ConfigurationError, env_text


class Command(BaseCommand):
    help = "Create the account named by GPODSYNC_BOOTSTRAP_USER, if it does not exist."

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            username = env_text("GPODSYNC_BOOTSTRAP_USER")
            password = env_text("GPODSYNC_BOOTSTRAP_PASSWORD")
        except ConfigurationError as error:
            raise CommandError(str(error)) from error

        if not username:
            return
        if not password:
            raise CommandError(
                "GPODSYNC_BOOTSTRAP_USER is set but no password was given. Set "
                "GPODSYNC_BOOTSTRAP_PASSWORD or GPODSYNC_BOOTSTRAP_PASSWORD_FILE."
            )

        try:
            create_account(username, password)
        except AccountError as error:
            # Already existing is the normal case from the second start onwards,
            # and must not stop the container from coming up. A rejected password
            # must, or the operator would never learn their account was not made.
            if "already exists" in str(error):
                return
            raise CommandError(str(error)) from error

        self.stdout.write(f"Created the initial account {username}.")
