"""Create an account.

This exists so the admin interface does not have to. With no public registration
and one command to add a user, the admin earns nothing and costs a second login
surface to attack.
"""

import getpass
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from gpodsync.accounts import AccountError, create_account


class Command(BaseCommand):
    help = "Create a gpodsync account."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("username")
        parser.add_argument(
            "--password",
            help=(
                "Prompted for when omitted, which is the better habit: an "
                "argument is visible in ps and in your shell history."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        password = options["password"] or getpass.getpass("Password: ")
        try:
            create_account(options["username"], password)
        except AccountError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(f"Created {options['username'].strip()}.")
