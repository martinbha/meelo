from django import template

from apps.core.value_objects import Money

register = template.Library()


@register.filter(name="money")
def format_money(amount_minor: int, currency: str) -> str:
    """Render stored minor units using the shared currency exponent and symbol."""

    return Money(amount_minor, currency).format()
