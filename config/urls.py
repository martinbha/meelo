from django.contrib import admin
from django.contrib.auth.views import (
    PasswordChangeDoneView,
    PasswordResetCompleteView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.urls import path

from apps.categorization.views import CategoryCorrectionView
from apps.core.views import dashboard, health_check
from apps.observations.views import (
    DocumentImageView,
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
    TransactionListView,
)
from apps.users.views import (
    UserLoginView,
    UserLogoutView,
    UserPasswordChangeView,
    UserPasswordResetConfirmView,
)

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
    path("review/", ReviewQueueView.as_view(), name="review-queue"),
    path("review/<uuid:pk>/", ObservationReviewView.as_view(), name="observation-review"),
    path("review/<uuid:pk>/image/", DocumentImageView.as_view(), name="document-image"),
    path(
        "review/<uuid:pk>/reprocess/",
        DocumentReprocessView.as_view(),
        name="document-reprocess",
    ),
    path(
        "observations/<uuid:pk>/<str:action>/",
        ObservationActionView.as_view(),
        name="observation-action",
    ),
    path("matches/", MatchQueueView.as_view(), name="match-queue"),
    path("matches/link/", MatchLinkView.as_view(), name="match-link"),
    path("matches/<uuid:pk>/", MatchDetailView.as_view(), name="match-detail"),
    path(
        "matches/<uuid:pk>/<str:action>/",
        MatchActionView.as_view(),
        name="match-action",
    ),
    path("reports/", OverviewReportView.as_view(), name="report-overview"),
    path("reports/categories/", SpendingReportView.as_view(), name="report-categories"),
    path("reports/merchants/", MerchantReportView.as_view(), name="report-merchants"),
    path("reports/accounts/", AccountReportView.as_view(), name="report-accounts"),
    path("reports/outstanding/", WorkloadReportView.as_view(), name="report-outstanding"),
    path("reports/exports/", ExportView.as_view(), name="report-exports"),
    path(
        "reports/exports/<uuid:pk>/download/",
        ExportDownloadView.as_view(),
        name="export-download",
    ),
    path(
        "reports/exports/<uuid:pk>/delete/",
        ExportDeleteView.as_view(),
        name="export-delete",
    ),
    path("reports/cards/", CardReportView.as_view(), name="report-cards"),
    path("uploads/", UploadListView.as_view(), name="upload-list"),
    path("uploads/new/", UploadCreateView.as_view(), name="upload-new"),
    path("uploads/<uuid:pk>/", UploadDetailView.as_view(), name="upload-detail"),
    path("uploads/<uuid:pk>/delete/", UploadDeleteView.as_view(), name="upload-delete"),
]
