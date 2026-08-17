import getpass

from django.core.management.base import BaseCommand, CommandError

from apps.core.key_management import load_master_key, provision_user_data_key
from apps.users.models import User


class Command(BaseCommand):
    help = "Create a private application user without enabling public registration."

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument("--email", required=True)
        parser.add_argument("--staff", action="store_true", help="Grant Django admin access.")
        parser.add_argument(
            "--superuser",
            action="store_true",
            help="Grant all permissions and Django admin access.",
        )

    def handle(self, *args, **options) -> None:  # type: ignore[no-untyped-def]
        email = options["email"]
        if User.objects.filter(email__iexact=email).exists():
            raise CommandError("A user with this email already exists")

        password = getpass.getpass("Password: ")
        confirmation = getpass.getpass("Password (again): ")
        if not password:
            raise CommandError("Password cannot be empty")
        if password != confirmation:
            raise CommandError("Passwords do not match")

        # Read the master key before creating anything. A user without a data
        # key cannot store an amount, a merchant, or an account name, so an
        # account created against a misconfigured key file is an account that
        # fails on its owner's first upload rather than here.
        try:
            master_key = load_master_key()
        except Exception as exc:  # noqa: BLE001 - reported as a command error
            raise CommandError(f"Cannot read the encryption master key: {exc}") from exc

        is_superuser = options["superuser"]
        user = User.objects.create_user(
            email=email,
            password=password,
            is_staff=options["staff"] or is_superuser,
            is_superuser=is_superuser,
        )
        key_record = provision_user_data_key(user=user, actor=user, master_key=master_key)
        self.stdout.write(
            self.style.SUCCESS(
                f"Created private user {user.email} with data key version {key_record.version}"
            )
        )
