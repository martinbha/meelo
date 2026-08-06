import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_migrations_are_current() -> None:
    call_command("makemigrations", check=True, dry_run=True, verbosity=0)
