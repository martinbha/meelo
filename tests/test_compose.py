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


def test_the_database_is_not_reachable_from_the_host() -> None:
    """A published database port is one firewall mistake away from the internet."""

    import yaml

    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())

    assert "ports" not in compose["services"]["postgres"]
    assert compose["services"]["postgres"]["networks"] == ["internal"]
    assert compose["networks"]["internal"]["internal"] is True
    # Only the proxy faces outward.
    assert compose["services"]["proxy"]["networks"] == ["edge"]
    assert "ports" not in compose["services"]["worker"]


def test_the_web_process_does_not_connect_as_the_superuser() -> None:
    """The role that is exposed to the network is not the one that owns the schema."""

    import yaml

    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())
    postgres = compose["services"]["postgres"]["environment"]

    for service in ("web", "worker"):
        user = compose["services"][service]["environment"]["POSTGRES_USER"]
        assert "POSTGRES_APP_USER" in user
        assert "OWNER" not in user

    assert "POSTGRES_OWNER_USER" in postgres["POSTGRES_USER"]


def test_the_role_bootstrap_grants_least_privilege() -> None:
    script = (PROJECT_ROOT / "deploy" / "postgres" / "init" / "10-roles.sh").read_text()

    for role in ("finance_migrate", "finance_app", "finance_backup"):
        assert f"CREATE ROLE {role} LOGIN" in script
    # The application gets rows, not structure.
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO finance_app"
        in script
    )
    assert "GRANT ALL ON SCHEMA public TO finance_migrate" in script
    # Backup reads and never writes.
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA public TO finance_backup" in script
    assert "INSERT" not in script.split("finance_backup;")[-1]
    # Nothing is granted to everybody.
    assert "REVOKE ALL ON SCHEMA public FROM PUBLIC" in script
    assert "REVOKE ALL ON DATABASE" in script
    # And a table created by the next migration is covered too.
    assert script.count("ALTER DEFAULT PRIVILEGES") == 3


def test_the_bootstrap_is_idempotent() -> None:
    """A container restart over an existing volume must not fail."""

    script = (PROJECT_ROOT / "deploy" / "postgres" / "init" / "10-roles.sh").read_text()

    assert script.count("IF NOT EXISTS (SELECT FROM pg_roles") == 3


def test_every_role_password_is_required_rather_than_defaulted() -> None:
    """A default password is a password somebody keeps."""

    compose = (PROJECT_ROOT / "docker-compose.yml").read_text()

    for name in (
        "POSTGRES_OWNER_PASSWORD",
        "POSTGRES_APP_PASSWORD",
        "POSTGRES_MIGRATION_PASSWORD",
        "POSTGRES_BACKUP_PASSWORD",
    ):
        assert f"${{{name}:?" in compose


def test_the_migration_alias_uses_its_own_role() -> None:
    """Production reaches one database through two roles."""

    from config.settings import base

    source = (PROJECT_ROOT / "config" / "settings" / "base.py").read_text()

    assert set(base.DATABASES) >= {"default", "migration"}
    # A deploy is short; the privileged role must not stay connected after it.
    assert base.DATABASES["migration"]["CONN_MAX_AGE"] == 0
    # One database, so Django must not try to build a second for tests.
    assert base.DATABASES["migration"]["TEST"] == {"MIRROR": "default"}
    # The alias reads its own credentials, falling back only so a single-role
    # development database still works.
    assert 'os.getenv("POSTGRES_MIGRATION_USER"' in source
    assert "POSTGRES_MIGRATION_PASSWORD" in source
