# Recovering a transaction draft that cannot be opened for editing

The edit form decrypts a draft before displaying its merchant, counterparty,
notes, and amount. If any stored value is corrupt or has an extra encryption
layer, the form hides the value and refuses to save. Do not remove an envelope
prefix or submit a replacement value through the blocked form; ciphertext shape
alone is not proof of what the original text said.

## Safe recovery

1. Record the transaction identifier shown in the page address and stop editing
   that draft. The edit form has not changed the stored row.
2. Find a trusted source for the complete transaction details: the original
   bank/card record, a retained screenshot, a verified export, or a backup made
   before the affected edit. Do not infer merchant, counterparty, or notes from
   an encrypted envelope.
3. If the values are verified, create a replacement draft through the normal
   transaction form. Enter the date, amount, currency, account, type, merchant,
   counterparty, and notes from that source. Open the replacement edit page and
   confirm that the readable fields match.
4. Use the old draft's delete action with a reason that identifies the
   replacement. Confirm the action only after checking the draft identifier.
   The old transaction remains as voided history; any posted entries are
   reversed by the deletion service.
5. If no trusted source is available, leave the draft unchanged and restore a
   verified backup/key set or ask an operator to recover it from one. Keep the
   original row until the replacement is verified.

This procedure is manual by design. It avoids a bulk rewrite based on text
prefixes, which could mistake a legitimate user-entered string for an
encryption envelope.
