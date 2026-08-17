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
its transaction — supplies the owner explicitly. The reader passes the same one,
so a value still cannot be opened under the wrong owner.

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

Derived from the user's data key by `derive_blind_index_key`, never stored. Kept
apart from the encryption key so an index leak does not hand over the plaintext,
and derived rather than stored so no second secret has to be wrapped, rotated,
and backed up alongside the first.

It is a secret of the same standing as the encryption key.

## Where they are used

- **Merchant lookup** — the alias and rule matching in `CATEGORIES.md`, built from
  the normalized name so three spellings share one token.
- **Duplicate grouping** — `deterministic_key` covers an approval code, or the
  instrument, date, amount, currency, and direction. Every one of those is low
  entropy, which is exactly why it is keyed.

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
