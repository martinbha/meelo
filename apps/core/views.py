from django.db import connection
from django.http import HttpRequest, JsonResponse


def health_check(request: HttpRequest) -> JsonResponse:
    """Report application and database readiness without exposing configuration."""

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return JsonResponse({"status": "ok"})
