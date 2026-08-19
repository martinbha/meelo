"""The specification's route table, resolved rather than believed (#157).

Section 24 names these paths precisely, and paths are what bookmarks, templates,
and documentation all point at. A path that quietly moves breaks all three at
once, so the table is checked instead of remembered.

Two directions are tested. Every specification path must resolve, and every path
that *used* to work must still answer — a bookmark saved before a rename is a
link somebody kept, not an error to correct.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.urls import Resolver404, resolve, reverse

from apps.core.key_management import provision_user_data_key
from config.routes import MOVED_ROUTES, SAMPLE_UUID, SPECIFICATION_ROUTES
from tests.factories import make_user

PASSWORD = "route-password"


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    return key


@pytest.fixture
def client_for(db: Any, master_key: bytes) -> Client:
    user = make_user(email="routes@example.com", password=PASSWORD)
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    client = Client()
    client.force_login(user)
    return client


# ----------------------------------------------------------------------
# Every specification path resolves
# ----------------------------------------------------------------------


@pytest.mark.parametrize("route", SPECIFICATION_ROUTES, ids=lambda route: route.path)
def test_every_specification_path_resolves(route: Any) -> None:
    try:
        match = resolve(route.concrete)
    except Resolver404:  # pragma: no cover - the assertion below reports it
        pytest.fail(f"Specification path {route.path} resolves to nothing.")
    assert match.url_name == route.name, (
        f"{route.path} resolves to {match.url_name!r}, not {route.name!r}."
    )


@pytest.mark.parametrize("route", SPECIFICATION_ROUTES, ids=lambda route: route.path)
def test_every_specification_path_is_what_its_name_reverses_to(route: Any) -> None:
    """Resolution alone would pass if two paths pointed at one name."""

    kwargs = {"pk": SAMPLE_UUID} if route.takes_identifier else {}
    assert reverse(route.name, kwargs=kwargs) == route.concrete


def test_a_path_the_specification_does_not_have_is_not_silently_accepted() -> None:
    """The audit's own failure mode, exercised rather than assumed."""

    with pytest.raises(Resolver404):
        resolve("/accounts/imaginary/page/")


def test_every_redirecting_route_records_why() -> None:
    """A route that redirects must say what will replace it, or it is just broken."""

    for route in SPECIFICATION_ROUTES:
        if route.redirects_to:
            assert route.note, f"{route.path} redirects with no reason recorded."


# ----------------------------------------------------------------------
# The redirects behave
# ----------------------------------------------------------------------


@pytest.mark.parametrize("route", MOVED_ROUTES, ids=lambda route: route.old_path)
def test_a_bookmarked_path_still_reaches_its_page(client_for: Client, route: Any) -> None:
    response = client_for.get(route.concrete)
    kwargs = {"pk": SAMPLE_UUID} if "<uuid>" in route.old_path else {}

    assert response.status_code == 301, f"{route.old_path} answered {response.status_code}."
    assert response.headers["Location"] == reverse(route.target_name, kwargs=kwargs)


@pytest.mark.parametrize(
    "route",
    [route for route in SPECIFICATION_ROUTES if route.redirects_to],
    ids=lambda route: route.path,
)
def test_a_pending_page_redirects_to_the_nearest_real_one(client_for: Client, route: Any) -> None:
    response = client_for.get(route.concrete)
    kwargs = {"pk": SAMPLE_UUID} if route.takes_identifier else {}

    assert response.status_code == 302
    assert response.headers["Location"] == reverse(route.redirects_to, kwargs=kwargs)


def test_a_redirect_is_not_a_way_around_the_login_wall(db: Any) -> None:
    """A redirect is still an answer, and answering says the path exists."""

    anonymous = Client()
    for route in SPECIFICATION_ROUTES:
        if not route.redirects_to:
            continue
        response = anonymous.get(route.concrete)
        assert reverse("login") in response.headers.get("Location", ""), route.path


# ----------------------------------------------------------------------
# The renamed pages still work
# ----------------------------------------------------------------------


def test_the_reconciliation_paths_carry_the_specifications_wording(client_for: Client) -> None:
    """The specification says "accept"; the service has always said "confirm"."""

    accept = reverse("match-accept", kwargs={"pk": SAMPLE_UUID})
    reject = reverse("match-reject", kwargs={"pk": SAMPLE_UUID})

    assert accept.endswith("/accept/")
    assert reject.endswith("/reject/")
    assert resolve(accept).kwargs["action"] == "confirm"
    assert resolve(reject).kwargs["action"] == "reject"


def test_the_read_only_pages_answer_for_their_owner(client_for: Client) -> None:
    for name in (
        "financial-account-list",
        "instrument-list",
        "category-list",
        "category-rule-list",
    ):
        response = client_for.get(reverse(name))
        assert response.status_code == 200, name
