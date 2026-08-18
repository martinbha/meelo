"""The section 6 audit, run as a test rather than believed as a document.

Each check here fails on a *difference*, not on a direction. A required field
that disappears fails; so does a field recorded as deliberately absent that
quietly comes back, and so does a new column nobody wrote a justification for.
That symmetry is the point: a deviation table only stays honest if drifting
away from it in either direction breaks the build.
"""

from __future__ import annotations

import pytest
from django.apps import apps
from django.db import models

from apps.core.model_specification import CORE_MODELS, SUPPORTING_MODELS, ModelSpecification


def _attribute_names(model: type[models.Model]) -> set[str]:
    return {field.attname for field in model._meta.concrete_fields}


@pytest.mark.parametrize("specification", CORE_MODELS, ids=lambda spec: spec.label)
def test_required_specification_fields_exist(specification: ModelSpecification) -> None:
    model = apps.get_model(specification.label)
    present = _attribute_names(model)
    for name in specification.fields:
        if name in specification.absent:
            continue
        implemented = specification.implemented_name(name)
        assert implemented in present, (
            f"{specification.label} is missing {name!r} "
            f"(specification {specification.section}); expected attribute {implemented!r}."
        )


@pytest.mark.parametrize("specification", CORE_MODELS, ids=lambda spec: spec.label)
def test_recorded_absences_are_still_absent(specification: ModelSpecification) -> None:
    """A justification for a missing field expires the moment the field lands."""

    model = apps.get_model(specification.label)
    present = _attribute_names(model)
    for name, justification in specification.absent.items():
        implemented = specification.implemented_name(name)
        assert implemented not in present, (
            f"{specification.label}.{implemented} now exists, but the deviation table still "
            f"says it does not: {justification!r}. Remove the entry and update DATAMODEL.md."
        )


@pytest.mark.parametrize("specification", CORE_MODELS, ids=lambda spec: spec.label)
def test_every_additive_field_is_justified(specification: ModelSpecification) -> None:
    model = apps.get_model(specification.label)
    undocumented = _attribute_names(model) - specification.expected_attributes()
    assert not undocumented, (
        f"{specification.label} carries fields the deviation table does not explain: "
        f"{sorted(undocumented)}. Add them to CORE_MODELS and DATAMODEL.md."
    )


@pytest.mark.parametrize("specification", CORE_MODELS, ids=lambda spec: spec.label)
def test_specification_enumeration_values_exist(specification: ModelSpecification) -> None:
    model = apps.get_model(specification.label)
    for attribute, values in specification.enumerations.items():
        choices = getattr(model, attribute, None)
        assert choices is not None, (
            f"{specification.label} has no {attribute} choices class "
            f"(specification {specification.section})."
        )
        implemented = set(choices.values)
        missing = [value for value in values if value not in implemented]
        assert not missing, (
            f"{specification.label}.{attribute} is missing specification values {missing}."
        )


@pytest.mark.parametrize("specification", CORE_MODELS, ids=lambda spec: spec.label)
def test_enumeration_fields_use_their_choices_class(specification: ModelSpecification) -> None:
    """A choices class the column does not reference constrains nothing."""

    model = apps.get_model(specification.label)
    for attribute in specification.enumerations:
        expected = set(getattr(model, attribute).values)
        using = [
            field
            for field in model._meta.concrete_fields
            if field.choices and {value for value, _ in field.choices} == expected
        ]
        assert using, (
            f"No field on {specification.label} uses {attribute}; the enumeration is "
            f"documentation rather than a constraint."
        )


def test_every_project_model_is_covered_by_the_audit() -> None:
    """The audit covers the schema, not only the part section 6 tabulates."""

    audited = {specification.label for specification in CORE_MODELS} | set(SUPPORTING_MODELS)
    project_models = {
        f"{model._meta.app_label}.{model.__name__}"
        for model in apps.get_models()
        if model.__module__.startswith("apps.")
    }
    unaudited = project_models - audited
    assert not unaudited, (
        f"These models are in no audit list: {sorted(unaudited)}. "
        "Add them to CORE_MODELS or SUPPORTING_MODELS with a justification."
    )
    stale = audited - project_models
    assert not stale, f"The audit names models that no longer exist: {sorted(stale)}."


def test_dropping_a_required_field_is_detected() -> None:
    """The audit's own failure mode, exercised rather than assumed."""

    specification = ModelSpecification(
        section="6.2",
        label="financial_accounts.FinancialAccount",
        fields=("currency", "interest_rate"),
    )
    model = apps.get_model(specification.label)
    present = _attribute_names(model)
    assert "currency" in present
    assert "interest_rate" not in present
    with pytest.raises(AssertionError):
        test_required_specification_fields_exist(specification)
