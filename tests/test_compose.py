from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_compose_has_required_redis_free_services() -> None:
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text()

    assert "web:" in compose
    assert "worker:" in compose
    assert "postgres:" in compose
    assert "proxy:" in compose
    assert "redis" not in compose.lower()
    assert "celery" not in compose.lower()
    assert "DOCUMENT_TMP_ROOT: /run/finance-ocr" in compose
    assert compose.count("healthcheck:") == 4
    assert "http://127.0.0.1:8000/health/" in compose
    assert "connection.ensure_connection()" in compose
    assert "finance_ocr_tmp:/run/finance-ocr" in compose
