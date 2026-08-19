# Encryption and Search

# Field Encryption

How a financial value is stored, and what that buys.

## The envelope

Every value-bearing field holds one AES-256-GCM envelope, dot-separated:

```text
v1 . <key version> . <nonce> . <ciphertext> . <tag>
```

The format version and the key version are in the envelope rather than beside it,
so a row carries enough to be read without a lookup table — and both are covered
by the authentication, so neither can be edited to change how the value is
interpreted.

## Associated data binds a value to its place

The AAD is `version | key version | model | record id | field | user id`. That
means a ciphertext cannot be:

- **moved to another row** — the record id differs,
- **moved to another column** — the field name differs,
- **moved to another user** — the owner differs,
- or **downgraded to an older key version** — the version differs.

Each of those fails authentication rather than decrypting into a plausible wrong
value, which is the whole point: a wrong amount that opens cleanly is worse than
a refusal, because only the refusal gets noticed.

A record with no owner column of its own — a ledger entry belongs to whoever owns
its transaction — answers `encryption_owner_id` from its parent. The reader gets
the same answer without being told, so a value still cannot be opened under the
wrong owner, and neither side has to remember to pass it.

## One unwrap per scope

Unwrapping a data key is an AES-GCM operation against the master key and an
audit write. A page rendering forty encrypted merchant names used to do both
forty times — forty rows in the audit log saying the same thing, which is noise
that hides the one access that mattered.

`apps.core.key_scope` unwraps once and holds the key in a `ContextVar` for the
life of one request or one worker job. The choice of container is the whole
design:

| Where a cached key could live | Why not |
| --- | --- |
| `django.core.cache` | A key in Redis |
| The session | A key in the database and in a signed cookie |
| A module global | A key that outlives the request that earned it |
| A `ContextVar` | Exactly as wide as the thing that needs it |

The scope records **whose** key it holds. Asked for a different user it refuses
rather than answering, because answering would hand one person's key to code
acting for another. Nesting a scope for the same user reuses the outer one, so a
service that opens one defensively does not double the audit trail.

`DataKeyScopeMiddleware` clears the scope in a `finally`, so an exception does
not leave a key in a worker thread that will serve the next request; the worker
does the same at the end of every job. The reference is dropped rather than
overwritten — CPython does not let you overwrite an immutable `bytes` in place,
and code that appeared to do so would be reassuring rather than true.

The scope is opened lazily, by the first view that asks. Most requests decrypt
nothing, and unwrapping a key for a page that lists dates is work done for
nobody.

## The worker's door

A queued screenshot is parsed minutes after the person who uploaded it closed
the tab, and the OCR output has to be sealed under their key or it is not
theirs. So the worker needs a data key with nobody signed in.

`get_user_data_key` requires an authenticated actor who *is* the owner. Passing
the owner in as their own actor would satisfy that while meaning nothing — the
rule becomes "the worker says this is fine". `get_worker_data_key` is a separate
door with a rule of its own, and the rule is the **document**:

- the key belongs to whoever owns the document being processed, and the caller
  does not choose the user — the document does, so there is no argument to point
  at the wrong person;
- a deactivated owner's key is not unwrapped, because a suspended account should
  stop being processed rather than quietly continue.

Access is audited as `worker_key_accessed`, with the document identifier
attached. "The owner opened their key" and "a background job opened the owner's
key while nobody was signed in" are different events, and only one of them can
be correlated with a person at a keyboard.

`worker_data_key_scope` opens once per job and closes with it. Worker code calls
`require_data_key`, which **refuses** rather than falling back to an unwrap:
a fallback there would restore the owner-as-their-own-actor rule that this door
exists to replace.

## One door in and out

`EncryptedFieldsMixin` is the only way an encrypted column is written or read.
Each model declares its `encrypted_fields`, and the mixin works the associated
data out from the instance rather than from its caller — so binding a value to
the wrong record is no longer something a service can express.

Three checks hold this shut, all in `tests/test_encrypted_field_mixin.py`:

- Every `*_encrypted` column in the schema is declared by its model, and every
  declaration names a column that exists. Adding a column and forgetting the
  encryption is the mistake worth catching, because that column holds real
  financial data from its first write.
- Rotation's field list and the models' declarations must agree. A field the
  mixin encrypts but rotation skips would keep an old key alive indefinitely.
- No module outside `apps/core` calls the crypto primitives. The shared path is
  only shared if it is the only path, so that is asserted by walking the syntax
  tree rather than trusted.

Writing an undeclared field raises rather than storing a ciphertext nobody will
read. An empty value is stored empty rather than encrypted: a ciphertext where
the absence of a value is the value would make "no note" and "a note nobody can
read" indistinguishable without decrypting first.

## Nonces

Twelve random bytes from `os.urandom` for **every** encryption. Never a counter:
a counter has to be persisted, and a counter restored from a backup repeats — and
a repeated nonce under one GCM key is not a weakened cipher, it leaks the XOR of
the two messages and the authentication key with it.

Random 96-bit nonces carry a birthday bound instead: collision probability stays
below 2⁻³² up to roughly 2³² encryptions **per key**. Keys are per user and per
version, so that is four billion field writes for one person before rotation.
Rotation (#94) happens long before it is relevant.

## What is encrypted

On the confirmed side: the amount, merchant, counterparty, and notes of every
`CanonicalTransaction`, and the amount of every `LedgerEntry` — an entry amount is
a second copy of money already encrypted, so leaving it in clear would undo the
row it came from.

Amounts are encrypted as the whole `minor_units:CURRENCY` string. Keeping the
currency inside the ciphertext rather than beside it means it cannot be edited in
the database to make an amount mean something else.

What stays readable is what has to be queried or sorted: dates, currency codes,
directions, statuses, confidences, and blind indexes.

## Reading

`read_model_field` recognises an envelope by its version prefix rather than by
attempting a decrypt and catching the failure, so a genuine authentication
failure stays loud. It **raises** when a field is encrypted and no key was
supplied, rather than returning the envelope: a caller handed ciphertext would go
on to display it, index it, or add it up.

Rows written before encryption reached a model are still readable in clear (#163
re-encrypts them). One module owns that distinction so no caller has to know
about it.

# Blind Indexes

An encrypted column cannot be queried. A blind index is what makes exact matching
possible anyway: a keyed digest of the normalized value, stored beside the
ciphertext, that the database compares without ever holding the value.

The word doing the work is **keyed**. A plain digest of a low-entropy value is
not an index, it is a lookup table waiting to be built — there are only so many
amounts a coffee costs, only so many dates in a year, and a six-digit approval
code has a million possibilities. An attacker holding the database can hash all
of them in seconds. HMAC with a key they do not have removes that entirely.

## Three properties

- **Domain separation.** A merchant named `4200` and an approval code of `4200`
  produce different tokens, so a match in one column cannot imply a match in
  another. Domains are a named set: a typo would otherwise create a second,
  incompatible index that matches nothing and reports no error.
- **Per-user scoping.** Two people who shop at the same café get different
  tokens, so the database cannot reveal that they have anything in common.
- **A visible version.** Every token is `<version>:<digest>`. The version sits
  outside the digest deliberately — a reindex has to find old tokens *without*
  the key, and the version is not a secret. A token with no version reports
  version 0, which means "rebuild me" rather than raising mid-migration.

## The search key

Derived from the **master** key with its own label — `derive_search_key` — and
stored wrapped in `UserSearchKey`, beside but not inside the wrapped data key.

It used to be derived from the data key, and that was the flaw. Two keys derived
from one are one secret wearing two hats: anyone who reaches the plaintext can
also build search tokens, and then confirm guesses against every index in the
database, including rows they could not otherwise read. Deriving from the master
key with a distinct label breaks that link — compromise of one no longer grants
the other (specification 22.4).

Three consequences:

- **Separate versions.** `UserSearchKey` has its own version, `is_active`, and
  retirement, so the search key rotates without re-encrypting anything and the
  data key rotates without rebuilding every index. Sharing a row would force one
  whenever the other was wanted, and rotation that is expensive is rotation that
  does not happen. The reindex itself is #168.
- **Rotating encryption leaves indexes alone.** Previously an encryption
  rotation silently rebuilt every blind index as a side effect — an index
  rebuild nobody asked for, hidden inside an operation about something else.
- **Stored, though derivable.** Deriving on demand would work and would leave
  the search key with no version to rotate and no row to retire. A stored row
  makes it a thing with a lifecycle.

The wrapped search key is bound to `users.usersearchkey` in its associated data,
so a wrapped *data* key pasted into that column fails to open rather than quietly
becoming a search key and reuniting the two secrets.

Both keys are provisioned together, because a value encrypted with no way to
index it is a value nothing can look up. Provisioned together is not the same as
derived from each other.

It is reached only through the scope — `request_search_key` for a request,
`require_search_key` for a worker job, which takes the *document* for the same
reason the data-key door does. A caller that wants to search asks for the search
key by name, so a page that merely renders values is not also holding the key
that turns a guess into a confirmed hit.

It is a secret of the same standing as the encryption key.

## What each domain normalizes to

A blind index only matches when both sides produce the same token, so the
normalization is as much part of the index as the key is. Getting it wrong does
not raise — it produces a token that matches nothing, and a lookup that quietly
returns no rows. That is indistinguishable from "there is nothing there", which
is why `apps.core.searchable` holds the rules once and both the write path and
the lookup path read them.

| Domain | Normalized to | Because |
| --- | --- | --- |
| `merchant` | `apps.categorization.normalization` | Three spellings of one shop share a rule |
| `counterparty` | Case-folded, whitespace collapsed | A person's name is not a merchant; merchant normalization strips branch and processor noise a bank name legitimately contains |
| `institution` | As counterparty | As counterparty |
| `approval_code` | Case, spacing, and separators removed | A card app prints `12-3456` where the bank prints `123456`; one authorisation, two spellings |
| `identifier` | Digits only | Masking is the bank's presentation, not part of the number |

A value that normalizes to nothing gets **no token**, not a token of the empty
string. An index over `""` matches every other empty row, which reads as a hit
and is not one.

The domain follows the *meaning*, not the column name. `CategoryRule` stores its
pattern in `merchant_pattern_blind_index` whatever the rule is about, so
`rule_pattern_index` picks the domain from the rule type — a counterparty rule
indexed in the merchant domain is compared against a counterparty token and can
never fire, for any input, silently.

## Backfilling

`manage.py backfill_blind_indexes` rebuilds tokens that are missing or stale. It
is idempotent because it compares the stored token against what it should be and
writes only on a difference, and resumable because the remaining work is a query
rather than a saved position — an interruption leaves finished rows finished.
Pages are walked with a keyset cursor on the primary key, not `OFFSET`, since
the rows are being written as they are read.

## Nothing readable stays in an encrypted column

Two halves, and the second is the one that keeps being true.

**Existing rows.** Encryption reached several models after their first rows
existed, so both forms sit in the same column: an envelope, and a readable
merchant name that looks exactly like one. `manage.py encrypt_plaintext_fields`
seals the readable ones, discovering the columns from the models' own
declarations so a newly encrypted column cannot be missed — a hand-kept list
would omit exactly the newest one, which is the likeliest to still hold
plaintext. Migration `core.0017` runs the same code on deploy, and skips
silently when no master key is configured, because a fresh install and a test
database have nothing to seal and refusing to migrate there would break the one
case that is already correct.

It is **one-way**. The way back is a restore from the backup taken before the
run, which is what the command says before it starts and why `--dry-run` exists.
A decrypt-the-whole-database routine is not something this application should
have sitting in it waiting to be called, so the migration's `reverse_code` is a
no-op that explains itself.

**New rows.** Several write paths accepted `data_key=None` and stored the value
in clear — a convenience for fixtures, and a hole in production.
`FIELD_ENCRYPTION_REQUIRED` turns a missing key into a refusal at the write,
before the column can hold something readable that adds up perfectly. It is on
everywhere except the test settings, and `tests/test_plaintext_encryption.py`
turns it back on and drives the real services through it: an off switch nothing
tests is an off switch that turns out to have been on.

Operational columns stay readable on purpose. A queue cannot select on a
ciphertext, an index cannot order one, and a status nobody can read is a status
the worker cannot act on.

## Where they are used

- **Merchant lookup** — the alias and rule matching in `CATEGORIES.md`, built from
  the normalized name so three spellings share one token.
- **Duplicate grouping** — `deterministic_key` covers an approval code, or the
  instrument, date, amount, currency, and direction. Every one of those is low
  entropy, which is exactly why the search key is **required** rather than
  optional. An unkeyed path left available is a path one caller reaches, and the
  resulting value is indistinguishable from a keyed one at the point where the
  difference matters.

## Testing

```bash
uv run pytest tests/test_blind_indexes.py
```

## Field encryption testing

```bash
uv run pytest tests/test_field_encryption.py tests/test_crypto.py
```

`tests/test_field_encryption.py` asserts the claims above rather than restating
them — including that the manual-entry page, the acceptance path, and ledger
posting all leave nothing readable in the database.

# Key Rotation

Rotation is a long operation over data a person cannot afford to lose, so the
design is shaped entirely by what happens when it stops halfway.

```bash
uv run python manage.py rotate_encryption_keys --email you@example.com
uv run python manage.py rotate_encryption_keys --email you@example.com --retire
uv run python manage.py rotate_encryption_keys --verify-only
```

## The order is the design

1. **Provision the new key and make it active.** Everything written from that
   moment is already correct, so rotation only has to catch up with history
   rather than chase writes still arriving under the old key.
2. **Move the values, in bounded batches.** One transaction per batch, so a
   rotation over a large history never holds one lock for its whole duration.
3. **Verify.** Every value is read back under the new key.
4. **Retire the old key only if step 3 was clean** — and only when asked.

## Resumability without bookkeeping

Every envelope carries its key version. That single fact is what makes an
interrupted rotation resumable: a row already at the target version is skipped,
so re-running processes exactly what is left. There is no cursor to corrupt and
no progress table that can disagree with the data.

It also means a write that lands *during* a rotation is already correct and gets
skipped, rather than being re-encrypted from a key it was never sealed with.

## Nothing is retired until it has been read

`verify_user` separates two failures because they mean different things:

- **Unreadable** — the value cannot be opened at all. Data loss in progress.
- **Stale** — rotation has not reached this row yet. Still readable under the
  old key, so retiring that key *now* would create the first kind.

`--retire` refuses on either. The safe state after a partial rotation is "both
keys exist"; the dangerous one is "the only key that could read this row is gone".

## The field registry

`ENCRYPTED_MODELS` lists every model holding encrypted values and which columns
those are. A test walks Django's model registry and fails if any `*_encrypted`
column is missing from it — because a field rotation walks past is a field left
readable only by a key that is about to be deleted, and nothing else would say so.

`LedgerEntry` is the one model without an owner column: its rows are selected
through `transaction__user_id` and its associated data borrows the transaction's
owner, the same way `entry_amount` reads them.

## Blind indexes move with the key

The search key is derived from the data key, so indexes are rebuilt in the same
pass. A rotated merchant whose index still came from the old key would be
unfindable, and nothing would report it.

## Testing

```bash
uv run pytest tests/test_key_rotation.py
```

## The regression sweep

`tests/test_security_regression.py` holds the claims this document makes to
being true, and it does it by enumeration rather than by sampling.

Every named route in the project's own URL tree is walked without a session, and
any route that answers with something other than a redirect to the login page is
a failure. Nothing has to be added to a list when a route is added — the list is
`get_resolver()`. The same applies to the cross-user sweep: one of every
addressable object is created for a second user, and each object route is
requested by the first. The expected answer is 404 rather than 403, because a
403 confirms the row exists.

That test found a real defect the first time it ran. `ManualTransactionUpdateView`
overrode `dispatch` to look up the row it was editing, and an override on the
subclass runs *before* `LoginRequiredMixin.dispatch` in the MRO. An anonymous
request therefore reached a database query and died on an assertion — a 500
where a redirect belonged. The fix is three lines; finding it without a sweep
would have taken somebody reading every view.

The rest of the file covers what the sweeps cannot: that a POST without a CSRF
token is refused, that a deletion route refuses a GET (a destructive action
behind a link is one a crawler can trigger), that nothing readable is stored for
an amount or a merchant, that the log formatter redacts what reaches it, and
that `config.settings.production` refuses to import at all when a secret is
missing. A settings module that quietly substitutes a working default is the one
that ships signing everybody's sessions with a key that is also in the
repository.

Upload-specific defences — path traversal, executable content, oversized images,
MIME sniffing — stay in `tests/test_uploads.py`, and login throttling and
session expiry stay in `tests/test_authentication.py`, next to the code they
constrain.
