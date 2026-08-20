# 0.1.0 release handover

The version is declared in `pyproject.toml` and must be tagged as `v0.1.0`
from the reviewed `main` commit. The tag is the deployment input; do not deploy
an untagged working branch.

## Before tagging

- [ ] Run the full test suite and coverage gate.
- [ ] Run `docker compose config` with production secrets supplied out of band.
- [ ] Create and verify a PostgreSQL backup.
- [ ] Perform a restore drill into an isolated database.
- [ ] Execute a key-rotation drill and verify the pre-rotation restore path.
- [ ] Record proxy health, worker health, and restart behaviour.

## Deploying the tag

1. Check out `v0.1.0` on the deployment host.
2. Provide the required secret files and environment variables from the secret store.
3. Build the image and run migrations with the migration role.
4. Start the compose services and wait for every health check to pass.
5. Upload a fixture screenshot, complete review, and verify a report/export.
6. Record image digest, migration output, health output, and backup identifiers.

Any step that needs an undocumented exception is a release blocker and belongs
in a follow-up issue before the tag is considered operable.
