from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.generic import FormView, View

from apps.core.errors import ApplicationError, definition_for
from apps.core.ownership import get_owned_object_or_404, owned_queryset

from .forms import ScreenshotUploadForm
from .models import SourceDocument
from .retention import delete_document
from .upload_services import DuplicateUploadError, create_uploaded_document


class UploadListView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest) -> HttpResponse:
        return render(
            request,
            "processing/upload_list.html",
            {"documents": owned_queryset(SourceDocument, request.user)},
        )


class UploadCreateView(LoginRequiredMixin, FormView):  # type: ignore[type-arg]
    template_name = "processing/upload_form.html"
    form_class = ScreenshotUploadForm
    success_url = reverse_lazy("upload-list")

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["files"] = self.request.FILES
        return kwargs

    def form_valid(self, form: ScreenshotUploadForm) -> HttpResponse:
        try:
            create_uploaded_document(
                user=self.request.user,
                uploaded_file=form.cleaned_data["screenshot"],
                retention_policy=form.cleaned_data["retention_policy"],
            )
        except DuplicateUploadError as exc:
            form.add_error("screenshot", exc.public_message)
            return self.form_invalid(form)
        except ApplicationError as exc:
            form.add_error("screenshot", exc.public_message)
            return self.form_invalid(form)
        messages.success(self.request, "Screenshot queued for processing.")
        return super().form_valid(form)


class UploadDetailView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest, pk: object) -> HttpResponse:
        document = get_owned_object_or_404(SourceDocument, request.user, pk=pk)
        return render(
            request,
            "processing/upload_detail.html",
            {"document": document, "error_definition": definition_for(document.error_code)},
        )


class UploadDeleteView(LoginRequiredMixin, View):
    def post(self, request: HttpRequest, pk: object) -> HttpResponse:
        document = get_owned_object_or_404(SourceDocument, request.user, pk=pk)
        delete_document(document.pk, user=request.user)
        messages.success(request, "The screenshot was deleted.")
        return redirect("upload-list")
