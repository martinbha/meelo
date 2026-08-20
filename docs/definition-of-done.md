# MVP definition-of-done evidence

Evidence review for the current `main` build (2026-08-20). This record separates
automated evidence from deployment evidence; a unit or integration test alone is
never treated as proof of an operational statement.

| # | Statement | Status | Evidence or gap |
|---:|---|---|---|
| 1 | Uploads are authenticated and owner-scoped | Met | `tests/test_uploads.py`; route and queryset checks |
| 2 | Upload validation rejects unsupported or oversized files | Met | `tests/test_uploads.py`; streaming-limit tests |
| 3 | Temporary files use private paths and restrictive permissions | Met | `tests/test_uploads.py`; `apps/processing/storage.py` |
| 4 | Temporary files are removed after processing | Met | `tests/test_processing.py`; `tests/test_pipeline_failure_modes.py` |
| 5 | OCR execution is bounded and classified | Met | `tests/test_ocr_execution.py`; timeout and configuration cases |
| 6 | OCR runs and tokens are recorded | Met | `tests/test_ocr_runs.py`; `tests/test_ocr_tokens.py` |
| 7 | Parser selection is deterministic and reviewable | Met | parser contract and regression suites |
| 8 | Imported observations are owner-scoped | Met | `tests/test_observation_import.py`; ownership checks |
| 9 | Review corrections are audited | Met | `tests/test_review_actions.py`; audit event assertions |
| 10 | Confirmed rows reconcile to balanced ledger entries | Met | `tests/test_reconciliation_services.py`; ledger posting tests |
| 11 | Reports exclude rejected and opening-balance rows | Met | `tests/test_monthly_spending.py`; opening-balance suite |
| 12 | Exports are owner-scoped, short-lived, and deleted explicitly | Met | `tests/test_end_to_end_pipeline.py`; export service tests |
| 13 | Sensitive fields are encrypted at rest | Met by inspection and tests | `apps/core/encrypted_fields.py`; `tests/test_encrypted_field_mixin.py` |
| 14 | Plaintext encrypted-column audit is available | Met | `manage.py audit_plaintext_fields`; `tests/test_audit_plaintext_fields.py` |
| 15 | Database roles follow least privilege | Met by inspection | `deploy/postgres/init/10-roles.sh`; PostgreSQL CI checks |
| 16 | PostgreSQL migrations are drift-checked | Met in CI | `.github/workflows/checks.yml`; PostgreSQL job |
| 17 | Backups can be created and verified | Unmet | Run a tagged deployment backup and restore drill; attach logs and checksum output |
| 18 | Key rotation and recovery are proven operationally | Unmet | Execute rotation against a deployment and verify a restore from the pre-rotation backup |
| 19 | Resource limits, health checks, and proxy hardening are active | Unmet | Compose and proxy checks exist; deploy and capture health/restart evidence |
| 20 | Release can be deployed from the runbook without undocumented steps | Unmet | Perform a clean-host deployment from the tagged build and record commands and versions |

The four unmet statements are deliberate follow-up work, not claims that local
tests prove a production deployment.
