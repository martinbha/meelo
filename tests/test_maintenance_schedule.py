from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_maintenance_runner_uses_a_non_blocking_per_command_lock() -> None:
    runner = (PROJECT_ROOT / "deploy" / "maintenance" / "run_command.sh").read_text()

    assert "exec 9>\"$lock_file\"" in runner
    assert "flock -n 9" in runner
    assert "Skipped $command_name" in runner
    assert "exec docker compose exec --no-TTY web python manage.py" in runner


def test_maintenance_runner_allows_only_known_commands() -> None:
    runner = (PROJECT_ROOT / "deploy" / "maintenance" / "run_command.sh").read_text()

    assert "cleanup_document_files|expire_document_retention" in runner
    assert "generate_reconciliation_candidates" in runner
    assert "process_document_jobs" in runner
    assert "Unsupported scheduled management command" in runner
