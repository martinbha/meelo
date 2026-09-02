from django.conf import settings
from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import (
    PasswordChangeDoneView,
    PasswordResetCompleteView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.http import HttpRequest, HttpResponseBase
from django.urls import path, re_path
from django.views.generic import RedirectView
from django.views.static import serve as serve_static

from apps.categorization.views import (
    CategoryCorrectionView,
    CategoryListView,
    CategoryRuleListView,
)
from apps.core.views import dashboard, health_check
from apps.financial_accounts.views import (
    FinancialAccountDetailView,
    FinancialAccountListView,
)
from apps.instruments.views import (
    PaymentInstrumentDetailView,
    PaymentInstrumentListView,
)
from apps.observations.views import (
    DocumentImageView,
    DocumentOverrideView,
    DocumentReprocessView,
    ObservationActionView,
    ObservationReviewView,
    ReviewQueueView,
)
from apps.processing.views import (
    UploadCreateView,
    UploadDeleteView,
    UploadDetailView,
    UploadListView,
)
from apps.reconciliation.views import (
    MatchActionView,
    MatchDetailView,
    MatchLinkView,
    MatchQueueView,
)
from apps.reports.views import (
    AccountReportView,
    CardReportView,
    ExportDeleteView,
    ExportDownloadView,
    ExportView,
    MerchantReportView,
    OverviewReportView,
    SpendingReportView,
    WorkloadReportView,
)
from apps.transactions.views import (
    ManualTransactionCreateView,
    ManualTransactionUpdateView,
    TransactionDeleteView,
    TransactionDetailView,
    TransactionListView,
)
from apps.users.views import (
    AccountSecurityView,
    OtherSessionsRevokeView,
    RecoveryCodeRegenerateView,
    SessionRevokeView,
    TOTPDisableView,
    TOTPEnrollView,
    TwoFactorVerifyView,
    UserLoginView,
    UserLogoutView,
    UserPasswordChangeView,
    UserPasswordResetConfirmView,
)


def _serve_collected_static(request: HttpRequest, path: str) -> HttpResponseBase:
    """Serve only the files produced by collectstatic, including in production."""

    return serve_static(request, path, document_root=str(settings.STATIC_ROOT))


urlpatterns = [
    path("health/", health_check, name="health-check"),
    path("", dashboard, name="dashboard"),
    path("admin/", admin.site.urls),
    path("login/", UserLoginView.as_view(), name="login"),
    path("logout/", UserLogoutView.as_view(), name="logout"),
    path("password/change/", UserPasswordChangeView.as_view(), name="password-change"),
    path(
        "password/change/done/",
        PasswordChangeDoneView.as_view(template_name="registration/password_change_done.html"),
        name="password-change-done",
    ),
    path(
        "password/reset/",
        PasswordResetView.as_view(
            template_name="registration/password_reset_form.html",
            email_template_name="registration/password_reset_email.txt",
            subject_template_name="registration/password_reset_subject.txt",
            success_url="/password/reset/done/",
        ),
        name="password-reset",
    ),
    path(
        "password/reset/done/",
        PasswordResetDoneView.as_view(template_name="registration/password_reset_done.html"),
        name="password-reset-done",
    ),
    path(
        "password/reset/<uidb64>/<token>/",
        UserPasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path(
        "password/reset/complete/",
        PasswordResetCompleteView.as_view(
            template_name="registration/password_reset_complete.html"
        ),
        name="password-reset-complete",
    ),
    path("transactions/", TransactionListView.as_view(), name="transaction-list"),
    path("transactions/new/", ManualTransactionCreateView.as_view(), name="transaction-new"),
    path("transactions/<uuid:pk>/", TransactionDetailView.as_view(), name="transaction-detail"),
    path(
        "transactions/<uuid:pk>/category/",
        CategoryCorrectionView.as_view(),
        name="transaction-category",
    ),
    path(
        "transactions/<uuid:pk>/edit/",
        ManualTransactionUpdateView.as_view(),
        name="transaction-edit",
    ),
    path(
        "transactions/<uuid:pk>/delete/",
        TransactionDeleteView.as_view(),
        name="transaction-delete",
    ),
    path("review/", ReviewQueueView.as_view(), name="review-queue"),
    path(
        "uploads/<uuid:pk>/review/",
        ObservationReviewView.as_view(),
        name="observation-review",
    ),
    path("uploads/<uuid:pk>/image/", DocumentImageView.as_view(), name="document-image"),
    path(
        "uploads/<uuid:pk>/reprocess/",
        DocumentReprocessView.as_view(),
        name="document-reprocess",
    ),
    path(
        "uploads/<uuid:pk>/source/",
        DocumentOverrideView.as_view(),
        name="document-override",
    ),
    path(
        "observations/<uuid:pk>/<str:action>/",
        ObservationActionView.as_view(),
        name="observation-action",
    ),
    path("reconciliation/", MatchQueueView.as_view(), name="match-queue"),
    path("reconciliation/link/", MatchLinkView.as_view(), name="match-link"),
    path("reconciliation/<uuid:pk>/", MatchDetailView.as_view(), name="match-detail"),
    # The specification calls confirming a candidate "accept"; the service has
    # always called it "confirm". Naming both here keeps the specification's
    # path without renaming a service that a dozen callers already use.
    path(
        "reconciliation/<uuid:pk>/accept/",
        MatchActionView.as_view(),
        {"action": "confirm"},
        name="match-accept",
    ),
    path(
        "reconciliation/<uuid:pk>/reject/",
        MatchActionView.as_view(),
        {"action": "reject"},
        name="match-reject",
    ),
    path(
        "reconciliation/<uuid:pk>/<str:action>/",
        MatchActionView.as_view(),
        name="match-action",
    ),
    path("reports/monthly/", OverviewReportView.as_view(), name="report-overview"),
    path("reports/categories/", SpendingReportView.as_view(), name="report-categories"),
    path("reports/merchants/", MerchantReportView.as_view(), name="report-merchants"),
    path("reports/accounts/", AccountReportView.as_view(), name="report-accounts"),
    path("reports/outstanding/", WorkloadReportView.as_view(), name="report-outstanding"),
    path("reports/export/", ExportView.as_view(), name="report-exports"),
    path(
        "reports/export/<uuid:pk>/download/",
        ExportDownloadView.as_view(),
        name="export-download",
    ),
    path(
        "reports/export/<uuid:pk>/delete/",
        ExportDeleteView.as_view(),
        name="export-delete",
    ),
    path("reports/cards/", CardReportView.as_view(), name="report-cards"),
    path("uploads/", UploadListView.as_view(), name="upload-list"),
    path("uploads/new/", UploadCreateView.as_view(), name="upload-new"),
    path("uploads/<uuid:pk>/", UploadDetailView.as_view(), name="upload-detail"),
    path("uploads/<uuid:pk>/delete/", UploadDeleteView.as_view(), name="upload-delete"),
    # Accounts, instruments, categories, and rules. Read-only for now: creating
    # and editing them is #183, #185, #186, and #187. The paths are registered
    # anyway, because a route table that is half true is one nobody can rely on.
    #
    # The redirects below are behind ``login_required`` for the same reason the
    # pages they point at are. A redirect is still an answer, and answering an
    # anonymous request with "go to /accounts/" tells them the path exists.
    path("accounts/", FinancialAccountListView.as_view(), name="financial-account-list"),
    path(
        "accounts/new/",
        login_required(
            RedirectView.as_view(pattern_name="financial-account-list", permanent=False)
        ),
        name="financial-account-new",
    ),
    path(
        "accounts/<uuid:pk>/",
        FinancialAccountDetailView.as_view(),
        name="financial-account-detail",
    ),
    path("instruments/", PaymentInstrumentListView.as_view(), name="instrument-list"),
    path(
        "instruments/new/",
        login_required(RedirectView.as_view(pattern_name="instrument-list", permanent=False)),
        name="instrument-new",
    ),
    path(
        "instruments/<uuid:pk>/",
        PaymentInstrumentDetailView.as_view(),
        name="instrument-detail",
    ),
    path("categories/", CategoryListView.as_view(), name="category-list"),
    path("rules/", CategoryRuleListView.as_view(), name="category-rule-list"),
    path("account/security/", AccountSecurityView.as_view(), name="account-security"),
    path(
        "account/security/sessions/<uuid:pk>/revoke/",
        SessionRevokeView.as_view(),
        name="session-revoke",
    ),
    path(
        "account/security/sessions/revoke-others/",
        OtherSessionsRevokeView.as_view(),
        name="sessions-revoke-others",
    ),
    path("account/security/totp/enroll/", TOTPEnrollView.as_view(), name="totp-enroll"),
    path("account/security/totp/disable/", TOTPDisableView.as_view(), name="totp-disable"),
    path("account/security/two-factor/", TwoFactorVerifyView.as_view(), name="two-factor-verify"),
    path(
        "account/security/recovery-codes/regenerate/",
        RecoveryCodeRegenerateView.as_view(),
        name="recovery-codes-regenerate",
    ),
    # The detail page already states the processing status. #189 replaces this
    # with the polling endpoint the progress UI needs.
    path(
        "uploads/<uuid:pk>/status/",
        login_required(RedirectView.as_view(pattern_name="upload-detail", permanent=False)),
        name="upload-status",
    ),
    # Paths that moved. Permanent, because a bookmark saved before the rename is
    # a link somebody kept, and answering it with a 404 loses whatever they were
    # looking at.
    path(
        "review/<uuid:pk>/",
        RedirectView.as_view(pattern_name="observation-review", permanent=True),
    ),
    path(
        "review/<uuid:pk>/image/",
        RedirectView.as_view(pattern_name="document-image", permanent=True),
    ),
    path(
        "review/<uuid:pk>/reprocess/",
        RedirectView.as_view(pattern_name="document-reprocess", permanent=True),
    ),
    path(
        "review/<uuid:pk>/source/",
        RedirectView.as_view(pattern_name="document-override", permanent=True),
    ),
    path("matches/", RedirectView.as_view(pattern_name="match-queue", permanent=True)),
    path(
        "matches/<uuid:pk>/",
        RedirectView.as_view(pattern_name="match-detail", permanent=True),
    ),
    path(
        "reports/exports/",
        RedirectView.as_view(pattern_name="report-exports", permanent=True),
    ),
    path("reports/", RedirectView.as_view(pattern_name="report-overview", permanent=True)),
]

# Only the collected STATIC_ROOT is exposed. Uploads, exports, and temporary
# processing files live outside it and have no static URL. Django's
# ``django.conf.urls.static.static`` helper intentionally disables itself when
# DEBUG is false, so use the same safe file-serving view explicitly for this
# small self-contained deployment.
urlpatterns += [re_path(r"^static/(?P<path>.*)$", _serve_collected_static)]

handler400 = "apps.core.error_views.bad_request"
handler403 = "apps.core.error_views.permission_denied"
handler404 = "apps.core.error_views.page_not_found"
handler500 = "apps.core.error_views.server_error"
