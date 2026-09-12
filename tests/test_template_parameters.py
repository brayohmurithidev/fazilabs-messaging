import pytest

from app.services.template_parameters import (
    InvalidTemplateParametersError,
    InvalidTemplateSchemaError,
    ordered_body_values,
    validate_parameter_schema,
)


def test_body_parameters_are_ordered_deterministically() -> None:
    schema = {"body": ["parent_name", "student_name", "term"]}
    values = {"term": "Term 2", "parent_name": "Jane", "student_name": "Brian"}
    assert ordered_body_values(schema, values) == ["Jane", "Brian", "Term 2"]


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ({"parent_name": "Jane"}, "missing parameters: student_name"),
        (
            {"parent_name": "Jane", "student_name": "Brian", "extra": "value"},
            "unexpected parameters: extra",
        ),
    ],
)
def test_missing_and_extra_parameters_are_rejected(parameters, message) -> None:
    with pytest.raises(InvalidTemplateParametersError, match=message):
        ordered_body_values({"body": ["parent_name", "student_name"]}, parameters)


@pytest.mark.parametrize(
    "schema",
    [
        {"header": ["image"]},
        {"button": ["url"]},
        {"body": ["duplicate", "duplicate"]},
        {"unknown": []},
        {"body": "not-a-list"},
    ],
)
def test_unsupported_or_invalid_schema_is_rejected(schema) -> None:
    with pytest.raises(InvalidTemplateSchemaError):
        validate_parameter_schema(schema)
