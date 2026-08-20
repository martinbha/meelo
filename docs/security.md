# Security model

Meelo is a private, self-hosted application. Its security boundary is the
deployment and the account that owns it; application-level controls reduce
exposure inside that boundary but do not replace host, database, or backup
hardening. The implementation details and operational key-source guidance are
in [SECURITY.md](../SECURITY.md).

## Threat assumptions and limits

The design protects one user's financial data from another application user,
accidental logging, ordinary database reads, and a compromised web session that
does not have the owner's decryption scope. It does not protect data from a
host administrator, a process with the master key, an attacker who can read the
running process memory, or an operator who restores an unencrypted backup.
Screenshots are processed locally; OCR output is never sent to a third-party
OCR service.

## Identity and authentication

- Passwords are hashed with Argon2id. The application never stores or logs a
  password in readable form.
- Django sessions are signed and expire. The security page shows only an
  eight-character session-key prefix, never a bearer credential.
- `django-axes` throttles failed login attempts by username and IP address.
  Successful authentication resets the failure counter, and a cool-off period
  prevents an indefinite lockout.
- Two-factor device and recovery-code tables are installed and device secrets
  are excluded from administration and logs. The current release records and
  reports enrollment state; enforcing a second factor at login remains a
  separate deployment decision until the enrollment and recovery flows are
  complete.

## Encryption and searchable fields

Readable financial values use AES-256-GCM envelopes. Every envelope carries a
format version and data-key version, and authenticated associated data binds it
to the model, row, field, and owning user. Copying ciphertext to another row,
field, user, or key version therefore fails authentication.

Searchable values have a separate HMAC-SHA-256 blind index scoped to the user
and search-key version. The index supports exact lookup without putting the
merchant or identifier in a queryable column. Financial amounts are encrypted,
not hashed, because reports must later total and display them.

Data keys are wrapped by the deployment master key. A request or worker job
opens one owner-scoped key in a `ContextVar`, and the scope is cleared in a
`finally` block. Rotation writes a new version before moving old rows, resumes
by key version, verifies every field, and retires an old key only after all
values can be read.

## Logging and audit

Audit events record who performed a state-changing action, when it happened,
the object identity, and safe field names. They do not contain financial
plaintext, OCR text, passwords, device seeds, recovery codes, or encryption
keys. Structured logging applies the same redaction rules to request and worker
context. Audit records are append-only and chained so deletion or modification
is detectable.

## Operational controls still required

Operators must provide controls the application cannot enforce:

1. Keep the master key in a Docker secret, systemd credential, or root-owned
   file with owner-only permissions. Never put it in an environment variable or
   image layer.
2. Restrict the database network and use separate application and migration
   roles. Apply TLS at the reverse proxy and keep security headers enabled.
3. Encrypt database and object-storage backups separately from the master-key
   backup. Test restore and key-rotation procedures against a tagged build.
4. Limit shell and filesystem access to the host, protect `.env` files, rotate
   credentials, and monitor audit and service logs without exporting sensitive
   fields.
5. Remove expired screenshots and temporary files, and verify that retention
   jobs and backup expiry run on schedule.

Security reports should describe a field, model, event code, or count—not the
value itself. When in doubt, treat a value-bearing field as sensitive and add a
test that proves it cannot enter a log, audit record, or plaintext database
column.
