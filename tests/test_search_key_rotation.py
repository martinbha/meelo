"""Rotating the search key without losing anything to it (#168, specification 22.4, 22.6).

The window is the whole problem. Between a new key becoming active and the last
token being rebuilt, a table holds tokens under two keys — and a lookup that
knows about only one finds half the rows and reports that as an answer. Nobody
files a bug against a search that quietly returned less than it should.

So the property under test is not "the rotation completes". It is that an exact
match returns the same rows before it starts, in the middle of it, and after it
finishes.
"""

from __future__ import annotations

import base64
import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError, call_command

from apps.categorization.normalization import merchant_blind_index
from apps.core.blind_index import SearchKey, blind_index, index_version
from apps.core.key_management import (
    active_search_key_version,
    get_user_data_key,
    get_user_search_keys,
    provision_next_search_key,
    provision_user_data_key,
)
from apps.core.management.commands.rotate_search_key import (
    index_candidates,
    reindex_user,
    stale_token_count,
)
from apps.core.models import RotationCheckpoint
from apps.transactions.models import CanonicalTransaction
from apps.transactions.services import create_manual_transaction
from apps.users.models import UserSearchKey
from tests.factories import make_account, make_user

pytestmark = pytest.mark.django_db

MERCHANT = "스타벅스 강남점"


@pytest.fixture
def master_key(tmp_path: Path, settings: Any) -> bytes:
    key = os.urandom(32)
    path = tmp_path / "master.key"
    path.write_text(base64.urlsafe_b64encode(key).decode(), encoding="ascii")
    path.chmod(0o600)
    settings.FIELD_ENCRYPTION_MASTER_KEY_FILE = str(path)
    return key


@pytest.fixture
def owner(master_key: bytes) -> Any:
    user = make_user(email="reindex-owner@example.com")
    provision_user_data_key(user=user, actor=user, master_key=master_key)
    return user


@pytest.fixture
def data_key(owner: Any, master_key: bytes) -> bytes:
    return get_user_data_key(user=owner, actor=owner, master_key=master_key)


def add_rows(owner: Any, data_key: bytes, search_key: SearchKey, count: int) -> None:
    account = make_account(owner, name_blind_index="reindex-account")
    for index in range(count):
        create_manual_transaction(
            user=owner,
            occurred_at=date(2026, 8, 1 + index % 28),
            amount_minor=1_000 + index,
            currency="KRW",
            transaction_type=CanonicalTransaction.TransactionType.PURCHASE,
            financial_account=account,
            merchant=MERCHANT,
            data_key=data_key,
            blind_index_key=search_key,
        )


def matching(owner: Any, keys: dict[int, SearchKey]) -> int:
    """How many rows an exact-match search finds across every live key version."""

    from apps.core.management.commands.rotate_search_key import any_version

    tokens = index_candidates(merchant_blind_index, MERCHANT, user_id=owner.pk, keys=keys)
    return (
        CanonicalTransaction.objects.filter(user=owner)
        .filter(any_version("merchant_blind_index", tokens))
        .count()
    )


# ----------------------------------------------------------------------
# The token carries the key version
# ----------------------------------------------------------------------


def test_the_prefix_names_the_search_key_not_the_scheme() -> None:
    """Rotating the key must change something a query can see without the key.

    A scheme version in the prefix would be identical before and after a
    rotation, so a half-reindexed table would look exactly like a finished one.
    """

    key = os.urandom(32)
    first = blind_index("merchant", "cafe", user_id=1, key=key, version=1)
    second = blind_index("merchant", "cafe", user_id=1, key=key, version=2)

    assert index_version(first) == 1
    assert index_version(second) == 2
    # And the digest differs too: the version is inside the HMAC as well, so a
    # token cannot be relabelled by editing its prefix.
    assert first.split(":")[1] != second.split(":")[1]


# ----------------------------------------------------------------------
# Search is identical before, during, and after
# ----------------------------------------------------------------------


def test_search_finds_the_same_rows_throughout_a_rotation(
    owner: Any, data_key: bytes, master_key: bytes
) -> None:
    keys = get_user_search_keys(user=owner, master_key=master_key)
    add_rows(owner, data_key, keys[1], 6)
    assert matching(owner, keys) == 6

    # The new key goes active; nothing has been reindexed yet.
    record = provision_next_search_key(user=owner, actor=owner, master_key=master_key)
    keys = get_user_search_keys(user=owner, master_key=master_key)
    assert set(keys) == {1, 2}
    assert matching(owner, keys) == 6

    # Half reindexed: the worst moment, and the one that has to work.
    reindex_user(
        user=owner,
        data_key=data_key,
        search_key=keys[record.version],
        key_version=record.version,
        batch_size=3,
    )
    assert matching(owner, keys) == 6

    versions = {
        index_version(token)
        for token in CanonicalTransaction.objects.filter(user=owner).values_list(
            "merchant_blind_index", flat=True
        )
    }
    assert versions == {2}
    # And with only the new key, which is what remains after retirement.
    assert matching(owner, {2: keys[2]}) == 6


def test_a_lookup_with_only_the_new_key_misses_rows_mid_rotation(
    owner: Any, data_key: bytes, master_key: bytes
) -> None:
    """The failure this design exists to prevent, demonstrated.

    Half the rows, silently, with no error anywhere — which is why the lookup
    has to be handed both keys rather than trusted to notice.
    """

    keys = get_user_search_keys(user=owner, master_key=master_key)
    add_rows(owner, data_key, keys[1], 4)
    record = provision_next_search_key(user=owner, actor=owner, master_key=master_key)
    keys = get_user_search_keys(user=owner, master_key=master_key)

    only_new = {record.version: keys[record.version]}

    assert matching(owner, only_new) == 0
    assert matching(owner, keys) == 4


def test_an_alias_lookup_matches_across_both_versions(
    owner: Any, data_key: bytes, master_key: bytes
) -> None:
    from apps.categorization.models import Category
    from apps.categorization.services import create_merchant_alias, find_merchant_alias

    keys = get_user_search_keys(user=owner, master_key=master_key)
    category = Category.objects.create(
        user=owner,
        name_encrypted="coffee",
        name_blind_index="reindex-coffee",
        category_type=Category.CategoryType.EXPENSE,
    )
    create_merchant_alias(
        user=owner,
        alias=MERCHANT,
        normalized_merchant=MERCHANT,
        default_category=category,
        encryption_key=data_key,
        blind_index_key=keys[1],
        key_version=1,
    )
    provision_next_search_key(user=owner, actor=owner, master_key=master_key)
    keys = get_user_search_keys(user=owner, master_key=master_key)

    # With the new key alone the alias is invisible; with both it is found.
    assert find_merchant_alias(user=owner, merchant=MERCHANT, blind_index_key=keys[2]) is None
    found = find_merchant_alias(
        user=owner,
        merchant=MERCHANT,
        blind_index_key=keys[2],
        additional_keys=[keys[1]],
    )
    assert found is not None


# ----------------------------------------------------------------------
# Resumable, and reports progress
# ----------------------------------------------------------------------


def test_the_reindex_is_resumable_and_records_progress(
    owner: Any, data_key: bytes, master_key: bytes
) -> None:
    keys = get_user_search_keys(user=owner, master_key=master_key)
    add_rows(owner, data_key, keys[1], 5)
    record = provision_next_search_key(user=owner, actor=owner, master_key=master_key)
    keys = get_user_search_keys(user=owner, master_key=master_key)

    report = reindex_user(
        user=owner,
        data_key=data_key,
        search_key=keys[record.version],
        key_version=record.version,
        batch_size=2,
    )

    assert report.is_clean
    assert report.tokens_rebuilt == 5
    checkpoint = RotationCheckpoint.objects.get(
        user=owner,
        key_kind="search",
        key_version=record.version,
        model_label="transactions.CanonicalTransaction",
    )
    assert checkpoint.is_complete
    assert checkpoint.rows_rotated == 5


def test_a_second_pass_rebuilds_nothing(owner: Any, data_key: bytes, master_key: bytes) -> None:
    keys = get_user_search_keys(user=owner, master_key=master_key)
    add_rows(owner, data_key, keys[1], 3)
    record = provision_next_search_key(user=owner, actor=owner, master_key=master_key)
    keys = get_user_search_keys(user=owner, master_key=master_key)
    reindex_user(
        user=owner,
        data_key=data_key,
        search_key=keys[record.version],
        key_version=record.version,
    )

    again = reindex_user(
        user=owner,
        data_key=data_key,
        search_key=keys[record.version],
        key_version=record.version,
    )

    assert again.tokens_rebuilt == 0


def test_the_data_key_checkpoint_and_the_search_key_checkpoint_are_separate(
    owner: Any, data_key: bytes, master_key: bytes
) -> None:
    """They rotate for different reasons; one must not be read as the other."""

    from apps.core.rotation import resume_point

    keys = get_user_search_keys(user=owner, master_key=master_key)
    add_rows(owner, data_key, keys[1], 2)
    record = provision_next_search_key(user=owner, actor=owner, master_key=master_key)
    keys = get_user_search_keys(user=owner, master_key=master_key)
    reindex_user(
        user=owner,
        data_key=data_key,
        search_key=keys[record.version],
        key_version=record.version,
        batch_size=1,
    )

    label = "transactions.CanonicalTransaction"
    assert resume_point(user=owner, new_version=2, label=label, kind="data") == ""
    assert RotationCheckpoint.objects.filter(user=owner, key_kind="search").exists()


# ----------------------------------------------------------------------
# Retirement
# ----------------------------------------------------------------------


def test_the_old_key_is_removed_only_when_no_token_uses_it(
    owner: Any, data_key: bytes, master_key: bytes, capsys: Any
) -> None:
    keys = get_user_search_keys(user=owner, master_key=master_key)
    add_rows(owner, data_key, keys[1], 3)

    call_command("rotate_search_key", email=owner.email)

    assert UserSearchKey.objects.filter(user=owner).count() == 2
    assert stale_token_count(user=owner, key_version=2) == 0

    call_command("rotate_search_key", email=owner.email, retire=True)

    remaining = UserSearchKey.objects.filter(user=owner)
    assert remaining.count() == 1
    assert remaining.get().version == 2
    assert "no token remains on an older key" in capsys.readouterr().out


def test_retirement_is_refused_while_a_token_is_still_on_the_old_key(
    owner: Any, data_key: bytes, master_key: bytes
) -> None:
    """Retiring early makes those rows unsearchable forever, with no error."""

    keys = get_user_search_keys(user=owner, master_key=master_key)
    add_rows(owner, data_key, keys[1], 3)
    provision_next_search_key(user=owner, actor=owner, master_key=master_key)

    with pytest.raises(CommandError, match="unsearchable forever"):
        call_command("rotate_search_key", email=owner.email, retire=True)

    assert UserSearchKey.objects.filter(user=owner).count() == 2


def test_a_dry_run_creates_no_key_and_writes_nothing(
    owner: Any, data_key: bytes, master_key: bytes, capsys: Any
) -> None:
    keys = get_user_search_keys(user=owner, master_key=master_key)
    add_rows(owner, data_key, keys[1], 3)
    before = dict(CanonicalTransaction.objects.values_list("pk", "merchant_blind_index"))

    call_command("rotate_search_key", email=owner.email, dry_run=True)

    assert UserSearchKey.objects.filter(user=owner).count() == 1
    assert active_search_key_version(user=owner) == 1
    assert not RotationCheckpoint.objects.exists()
    assert dict(CanonicalTransaction.objects.values_list("pk", "merchant_blind_index")) == before
    assert "Nothing was written" in capsys.readouterr().out
