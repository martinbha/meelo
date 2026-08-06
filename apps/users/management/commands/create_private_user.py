import getpass

from django.core.management.base import BaseCommand, CommandError

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

        is_superuser = options["superuser"]
        user = User.objects.create_user(
            email=email,
            password=password,
            is_staff=options["staff"] or is_superuser,
            is_superuser=is_superuser,
        )
        self.stdout.write(self.style.SUCCESS(f"Created private user {user.email}"))
