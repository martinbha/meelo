from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    """Render the authenticated application landing page."""

    return render(request, "dashboard.html")


def health_check(request: HttpRequest) -> JsonResponse:
    """Report application and database readiness without exposing configuration."""

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return JsonResponse({"status": "ok"})
