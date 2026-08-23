import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_maintenance_runner_uses_a_non_blocking_per_command_lock() -> None:
    runner = (PROJECT_ROOT / "deploy" / "maintenance" / "run_command.sh").read_text()

    assert 'exec 9>"$lock_file"' in runner
    assert "flock -n 9" in runner
    assert "Skipped $command_name" in runner
    assert "exec docker compose exec --no-TTY web python manage.py" in runner


def test_maintenance_runner_allows_only_known_commands() -> None:
    runner = (PROJECT_ROOT / "deploy" / "maintenance" / "run_command.sh").read_text()

    assert "cleanup_document_files|expire_document_retention" in runner
    assert "generate_reconciliation_candidates" in runner
    assert "process_document_jobs" in runner
    assert "Unsupported scheduled management command" in runner


def test_cron_schedule_covers_worker_recovery_cleanup_retention_and_reconciliation() -> None:
    schedule = (PROJECT_ROOT / "deploy" / "maintenance" / "finance-ocr.cron").read_text()

    for command in (
        "process_document_jobs --once",
        "recover_processing_jobs",
        "cleanup_document_files",
        "expire_document_retention",
        "generate_reconciliation_candidates",
        "rotate_encryption_keys --verify-only",
    ):
        assert command in schedule

    scheduled_lines = [line for line in schedule.splitlines() if line and not line.startswith("#")]
    assert scheduled_lines
    assert all(">>/var/log/finance-ocr/maintenance.log 2>&1" in line for line in scheduled_lines)


def test_systemd_maintenance_service_is_oneshot_and_uses_the_locked_runner() -> None:
    service = (PROJECT_ROOT / "deploy" / "systemd" / "finance-ocr-maintenance@.service").read_text()

    assert "Type=oneshot" in service
    assert "After=docker.service" in service
    assert "ExecStart=/opt/finance-ocr/deploy/maintenance/run_command.sh %i" in service


def test_systemd_timers_are_persistent_and_cover_the_scheduled_commands() -> None:
    timers = {
        "finance-ocr-process.timer": "process_document_jobs",
        "finance-ocr-recover.timer": "recover_processing_jobs",
        "finance-ocr-cleanup.timer": "cleanup_document_files",
        "finance-ocr-retention.timer": "expire_document_retention",
        "finance-ocr-reconciliation.timer": "generate_reconciliation_candidates",
        "finance-ocr-exports.timer": "purge_expired_exports",
        "finance-ocr-audit.timer": "prune_audit_events",
    }

    for filename, command in timers.items():
        timer = (PROJECT_ROOT / "deploy" / "systemd" / filename).read_text()
        assert "Persistent=true" in timer
        assert f"finance-ocr-maintenance@{command}.service" in timer

    key_timer = (
        PROJECT_ROOT / "deploy" / "systemd" / "finance-ocr-key-verification.timer"
    ).read_text()
    key_service = (
        PROJECT_ROOT / "deploy" / "systemd" / "finance-ocr-key-verification.service"
    ).read_text()
    assert "Persistent=true" in key_timer
    assert "rotate_encryption_keys --verify-only" in key_service


def test_maintenance_runner_propagates_management_command_exit_code(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\nexit 17\n")
    docker.chmod(0o755)
    flock = fake_bin / "flock"
    flock.write_text("#!/bin/sh\nexit 0\n")
    flock.chmod(0o755)

    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "MEELO_PROJECT_DIR": str(tmp_path),
        "MEELO_MAINTENANCE_LOCK_DIR": str(tmp_path / "locks"),
    }
    result = subprocess.run(
        [str(PROJECT_ROOT / "deploy" / "maintenance" / "run_command.sh"), "operational_status"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 17
