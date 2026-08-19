"""The route table of specification section 24, written down so a test can check it.

Bookmarks, documentation, and the links in every template all point at paths.
A path that quietly moves breaks all three at once, and the specification names
these ones precisely so they do not have to be rediscovered later.

So the section 24 list lives here as data. :mod:`tests.test_routes` resolves
every entry and fails when one stops resolving, which is the difference between
"the routes match the specification" being a claim and being a check.

Paths that moved keep their old address as a permanent redirect. A bookmark from
before the rename is not an error to correct — it is a link somebody saved, and
answering it with a 404 loses whatever they were looking at.
"""

from __future__ import annotations

from dataclasses import dataclass

#: A concrete UUID, so a route with an identifier in it can be resolved without
#: needing a database row. It never has to exist: resolution is a URL-pattern
#: question, not a data one.
SAMPLE_UUID = "00000000-0000-4000-8000-000000000000"


@dataclass(frozen=True, slots=True)
class SpecificationRoute:
    """One path from section 24, and what answers it."""

    #: Exactly as the specification writes it, with ``<uuid>`` left in place.
    path: str
    #: The URL name it resolves to, for a reverse-and-compare check.
    name: str
    #: Set when the path answers with a redirect rather than a page of its own.
    #: The reason is recorded so "it redirects" cannot quietly become "it is
    #: broken and nobody looked".
    redirects_to: str = ""
    note: str = ""

    @property
    def concrete(self) -> str:
        return self.path.replace("<uuid>", SAMPLE_UUID)

    @property
    def takes_identifier(self) -> bool:
        return "<uuid>" in self.path


SPECIFICATION_ROUTES: tuple[SpecificationRoute, ...] = (
    SpecificationRoute("/", "dashboard"),
    SpecificationRoute("/login/", "login"),
    SpecificationRoute("/logout/", "logout"),
    SpecificationRoute("/account/security/", "account-security"),
    SpecificationRoute("/accounts/", "financial-account-list"),
    SpecificationRoute(
        "/accounts/new/",
        "financial-account-new",
        redirects_to="financial-account-list",
        note="Creation is #183. Redirecting to the list is better than a 404: "
        "somebody following the specification's own route table lands somewhere "
        "that shows them their accounts and says what is missing.",
    ),
    SpecificationRoute("/accounts/<uuid>/", "financial-account-detail"),
    SpecificationRoute("/instruments/", "instrument-list"),
    SpecificationRoute(
        "/instruments/new/",
        "instrument-new",
        redirects_to="instrument-list",
        note="Creation is #185.",
    ),
    SpecificationRoute("/instruments/<uuid>/", "instrument-detail"),
    SpecificationRoute("/uploads/", "upload-list"),
    SpecificationRoute("/uploads/new/", "upload-new"),
    SpecificationRoute("/uploads/<uuid>/", "upload-detail"),
    SpecificationRoute(
        "/uploads/<uuid>/status/",
        "upload-status",
        redirects_to="upload-detail",
        note="The detail page already states the processing status. #189 replaces "
        "this with the polling endpoint the progress UI needs.",
    ),
    SpecificationRoute("/uploads/<uuid>/review/", "observation-review"),
    SpecificationRoute("/uploads/<uuid>/reprocess/", "document-reprocess"),
    SpecificationRoute("/uploads/<uuid>/delete/", "upload-delete"),
    SpecificationRoute("/transactions/", "transaction-list"),
    SpecificationRoute("/transactions/<uuid>/", "transaction-detail"),
    SpecificationRoute("/transactions/<uuid>/edit/", "transaction-edit"),
    SpecificationRoute("/transactions/<uuid>/delete/", "transaction-delete"),
    SpecificationRoute("/reconciliation/", "match-queue"),
    SpecificationRoute("/reconciliation/<uuid>/", "match-detail"),
    SpecificationRoute("/reconciliation/<uuid>/accept/", "match-accept"),
    SpecificationRoute("/reconciliation/<uuid>/reject/", "match-reject"),
    SpecificationRoute("/categories/", "category-list"),
    SpecificationRoute("/rules/", "category-rule-list"),
    SpecificationRoute("/reports/monthly/", "report-overview"),
    SpecificationRoute("/reports/categories/", "report-categories"),
    SpecificationRoute("/reports/accounts/", "report-accounts"),
    SpecificationRoute("/reports/cards/", "report-cards"),
    SpecificationRoute("/reports/export/", "report-exports"),
)


@dataclass(frozen=True, slots=True)
class MovedRoute:
    """A path that used to work, and the specification path it now points at."""

    old_path: str
    target_name: str
    note: str = ""

    @property
    def concrete(self) -> str:
        return self.old_path.replace("<uuid>", SAMPLE_UUID)


#: Everything that changed address. Each one keeps answering, permanently, so a
#: bookmark saved before the rename still opens the page it was saved for.
MOVED_ROUTES: tuple[MovedRoute, ...] = (
    MovedRoute("/review/<uuid>/", "observation-review", "Review moved under its upload."),
    MovedRoute("/review/<uuid>/reprocess/", "document-reprocess"),
    MovedRoute("/review/<uuid>/image/", "document-image"),
    MovedRoute("/review/<uuid>/source/", "document-override"),
    MovedRoute("/matches/", "match-queue", "Matching is 'reconciliation' in the specification."),
    MovedRoute("/matches/<uuid>/", "match-detail"),
    MovedRoute("/reports/exports/", "report-exports", "Singular, per the specification."),
    MovedRoute("/reports/", "report-overview", "The monthly report is what /reports/ showed."),
)
