import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0001_initial"),
        ("transactions", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="LedgerEntry",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("entry_type", models.CharField(choices=[("debit", "Debit"), ("credit", "Credit")], max_length=8)),
                ("amount_encrypted", models.TextField()),
                ("currency", models.CharField(max_length=3)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="ledger_entries", to="ledger.ledgeraccount")),
                ("transaction", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="ledger_entries", to="transactions.canonicaltransaction")),
            ],
            options={
                "ordering": ("created_at", "id"),
                "indexes": [models.Index(fields=["transaction", "entry_type"], name="ledger_entry_txn_type_idx"), models.Index(fields=["account", "created_at"], name="ledger_entry_account_date_idx")],
            },
        ),
    ]
