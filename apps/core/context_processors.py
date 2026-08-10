from django.http import HttpRequest


def htmx_template(request: HttpRequest) -> dict[str, str]:
    """Select a content-only template base for boosted HTMX navigation."""

    is_htmx = request.headers.get("HX-Request", "").lower() == "true"
    return {"base_template": "partial.html" if is_htmx else "base.html"}
