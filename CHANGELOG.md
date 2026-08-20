# Changelog

## 0.1.0 — 2026-08-20

Initial MVP release candidate.

### Delivered

- private screenshot upload, bounded local OCR, parser selection, and review workflow;
- encrypted financial fields with key provisioning, rotation, and plaintext auditing;
- owner-scoped reconciliation, ledger posting, reporting, and expiring exports;
- PostgreSQL least-privilege roles, migration drift checks, backup tooling, and CI quality/security gates;
- container resource limits, health checks, TLS/header hardening, and static asset collection.

### Deferred by design

- managed OCR or cloud storage integrations;
- multi-tenant administration and organisation-wide reporting;
- asynchronous broker infrastructure beyond the database-backed worker;
- production deployment evidence for backup/restore and key-rotation drills.

The deferred items are intentionally out of scope for 0.1.0 and must not be
described as supported features.
