import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError


@pytest.mark.django_db
def test_user_manager_normalizes_email_and_hashes_password() -> None:
    user_model = get_user_model()
    user = user_model.objects.create_user(
        "Owner@Example.com", password="correct horse battery staple"
    )

    assert user.email == "owner@example.com"
    assert user.check_password("correct horse battery staple")
    assert user.has_usable_password()
    assert user.encryption_key_version == 1


@pytest.mark.django_db
def test_superuser_manager_sets_required_flags() -> None:
    user = get_user_model().objects.create_superuser(
        "admin@example.com", "correct horse battery staple"
    )

    assert user.is_staff is True
    assert user.is_superuser is True


@pytest.mark.django_db
def test_email_identity_is_case_insensitive() -> None:
    user_model = get_user_model()
    user_model.objects.create_user("Owner@Example.com", password="password")

    with pytest.raises(IntegrityError):
        user_model.objects.create_user("owner@example.com", password="password")
