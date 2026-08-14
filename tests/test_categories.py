from typing import Any

import pytest
from django.core.exceptions import ValidationError

from apps.categorization.models import Category


@pytest.fixture
def user(db: Any) -> Any:
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user("owner@example.com", password="password")


def make_category(user: Any, name: str, parent: Category | None = None) -> Category:
    return Category.objects.create(
        user=user,
        name_encrypted=name,
        name_blind_index=f"{name}-index",
        parent=parent,
        category_type=Category.CategoryType.EXPENSE,
    )


@pytest.mark.django_db
def test_categories_support_nested_parent_paths(user: Any) -> None:
    food = make_category(user, "food")
    dining = make_category(user, "dining", food)
    restaurant = make_category(user, "restaurant", dining)

    assert restaurant.ancestors() == [dining, food]
    assert list(food.children.all()) == [dining]


@pytest.mark.django_db
def test_category_parent_must_belong_to_same_user(user: Any) -> None:
    other = type(user).objects.create_user("other@example.com", password="password")
    parent = make_category(other, "other-parent")
    category = Category(
        user=user,
        name_encrypted="child",
        name_blind_index="child-index",
        parent=parent,
        category_type=Category.CategoryType.EXPENSE,
    )

    with pytest.raises(ValidationError, match="same user"):
        category.full_clean()


@pytest.mark.django_db
def test_category_cycle_is_rejected(user: Any) -> None:
    parent = make_category(user, "parent")
    child = make_category(user, "child", parent)
    parent.parent = child

    with pytest.raises(ValidationError, match="cycle"):
        parent.full_clean()


@pytest.mark.django_db
def test_system_category_cannot_be_deleted(user: Any) -> None:
    category = make_category(user, "system")
    category.is_system = True
    category.save(update_fields=["is_system"])

    with pytest.raises(ValidationError, match="cannot be deleted"):
        category.delete()

    assert Category.objects.filter(pk=category.pk).exists()


@pytest.mark.django_db
def test_same_name_is_allowed_under_different_parents(user: Any) -> None:
    first_parent = make_category(user, "first")
    second_parent = make_category(user, "second")
    first = make_category(user, "shared", first_parent)
    second = make_category(user, "shared", second_parent)

    first.full_clean()
    second.full_clean()
    assert first.parent_id != second.parent_id
