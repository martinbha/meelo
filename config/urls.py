from django.contrib import admin
from django.urls import path

from apps.processing.views import UploadCreateView, UploadDetailView, UploadListView
from apps.transactions.views import (
    ManualTransactionCreateView,
    ManualTransactionUpdateView,
    TransactionListView,
)
from apps.users.views import UserLoginView, UserLogoutView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", UserLoginView.as_view(), name="login"),
    path("logout/", UserLogoutView.as_view(), name="logout"),
    path("transactions/", TransactionListView.as_view(), name="transaction-list"),
    path("transactions/new/", ManualTransactionCreateView.as_view(), name="transaction-new"),
    path(
        "transactions/<uuid:pk>/edit/",
        ManualTransactionUpdateView.as_view(),
        name="transaction-edit",
    ),
    path("uploads/", UploadListView.as_view(), name="upload-list"),
    path("uploads/new/", UploadCreateView.as_view(), name="upload-new"),
    path("uploads/<uuid:pk>/", UploadDetailView.as_view(), name="upload-detail"),
]
