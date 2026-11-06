# Review historical automatic duplicate merges

Automatic duplicate merging is an explicit opt-in. The current safety checks apply to new merges; they do not decide whether an older merge was correct. Do not bulk split historical pairs or edit observation links directly in the database.

## Build a review list

In a private operator session, list audit events where `event_type` is `duplicate_merged` and `metadata.automatic` is `true`. For each event, use its object ID to open the related reconciliation match and its two source observations. Use the owner-scoped reconciliation page so encrypted fields are decrypted only for that owner. Preserve the source documents while reviewing.

The audit event contains observation and match identifiers plus the score and feature names. It does not contain merchant or amount values. Keep any investigation notes in a restricted operations record and omit financial plaintext.

## Review one pair at a time

1. Compare both source images and the preserved observation values. Check dates, amount, currency, direction, mapped instrument/account, and approval code.
2. If both rows represent one transaction, leave the merge in place.
3. If the rows represent different events, use the reconciliation page's unlink action for that match. The application restores the merged observation to review and records an unlink event. Review the reopened row before accepting it as a separate transaction.
4. If the evidence is incomplete, leave the match unchanged and mark the review unresolved.
5. If either row already contributed to a canonical transaction, verify that transaction and its ledger effects through the normal transaction lifecycle before editing any further history. Do not split records with direct SQL.

Record the match ID, review result, and any unlink action in a restricted change log. Do not run a bulk reversal or unmerge operation; each correction must be checked against its source evidence and the resulting canonical history.
