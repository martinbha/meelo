from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("reconciliation", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="reconciliationmatch",
            name="resolution_revision",
            field=models.PositiveIntegerField(
                default=0,
                editable=False,
                help_text="User-reopened relationship revision for canonical event idempotency.",
            ),
        ),
    ]
