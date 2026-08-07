import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("categorization", "0001_initial"),
        ("financial_accounts", "0001_initial"),
        ("instruments", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="MerchantAlias",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("alias_encrypted", models.TextField()),
                ("alias_blind_index", models.CharField(max_length=128)),
                ("normalized_merchant_encrypted", models.TextField()),
                ("normalized_merchant_blind_index", models.CharField(max_length=128)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("default_category", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="merchant_aliases", to="categorization.category")),
                ("payment_instrument", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="merchant_aliases", to="instruments.paymentinstrument")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="merchant_aliases", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ("alias_blind_index", "created_at"),
                "constraints": [models.UniqueConstraint(fields=("user", "alias_blind_index"), name="merchant_alias_user_alias_blind_unique")],
            },
        ),
        migrations.CreateModel(
            name="CategoryRule",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("merchant_pattern_encrypted", models.TextField()),
                ("merchant_pattern_blind_index", models.CharField(max_length=128)),
                ("amount_min_encrypted", models.TextField(blank=True)),
                ("amount_max_encrypted", models.TextField(blank=True)),
                ("priority", models.PositiveIntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("category", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="rules", to="categorization.category")),
                ("financial_account", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="category_rules", to="financial_accounts.financialaccount")),
                ("payment_instrument", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="category_rules", to="instruments.paymentinstrument")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="category_rules", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ("-priority", "created_at"),
                "indexes": [models.Index(fields=["user", "merchant_pattern_blind_index", "is_active"], name="category_rule_lookup_idx")],
            },
        ),
    ]
