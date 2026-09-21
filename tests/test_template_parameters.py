import pytest

from app.services.template_parameters import (
    InvalidTemplateParametersError,
    InvalidTemplateSchemaError,
    ordered_body_values,
    ordered_template_values,
    semantic_parameter_names,
    validate_parameter_schema,
)


def test_body_parameters_are_ordered_deterministically() -> None:
    schema = {"body": ["parent_name", "student_name", "term"]}
    values = {"term": "Term 2", "parent_name": "Jane", "student_name": "Brian"}
    assert ordered_body_values(schema, values) == ["Jane", "Brian", "Term 2"]


def test_body_and_url_button_parameters_are_ordered_from_semantic_mapping() -> None:
    schema = {
        "body": ["parent_name", "student_name", "term"],
        "buttons": [{"index": 0, "type": "url", "parameter": "results_path"}],
    }
    parameters = {
        "results_path": "access/opaque-token",
        "term": "Term 2",
        "parent_name": "Jane",
        "student_name": "Brian",
    }
    values = ordered_template_values(schema, parameters)
    assert values.body == ["Jane", "Brian", "Term 2"]
    assert [(button.index, button.value) for button in values.url_buttons] == [
        (0, "access/opaque-token")
    ]
    assert semantic_parameter_names(schema) == [
        "parent_name",
        "student_name",
        "term",
        "results_path",
    ]


def test_multiple_url_buttons_are_supported_in_mapping_order() -> None:
    schema = {
        "body": [],
        "buttons": [
            {"index": 1, "type": "url", "parameter": "receipt_path"},
            {"index": 0, "type": "url", "parameter": "invoice_path"},
        ],
    }
    values = ordered_template_values(
        schema, {"invoice_path": "invoice/1", "receipt_path": "receipt/1"}
    )
    assert [(button.index, button.value) for button in values.url_buttons] == [
        (1, "receipt/1"),
        (0, "invoice/1"),
    ]


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
        {"buttons": [{"index": -1, "type": "url", "parameter": "results_path"}]},
        {"buttons": [{"index": True, "type": "url", "parameter": "results_path"}]},
        {"buttons": [{"index": 0, "type": "quick_reply", "parameter": "choice"}]},
        {
            "body": ["results_path"],
            "buttons": [{"index": 0, "type": "url", "parameter": "results_path"}],
        },
        {
            "buttons": [
                {"index": 0, "type": "url", "parameter": "first_path"},
                {"index": 0, "type": "url", "parameter": "second_path"},
            ]
        },
    ],
)
def test_unsupported_or_invalid_schema_is_rejected(schema) -> None:
    with pytest.raises(InvalidTemplateSchemaError):
        validate_parameter_schema(schema)


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        (
            {"parent_name": "Jane"},
            "missing parameters: results_path",
        ),
        (
            {"parent_name": "Jane", "results_path": "path", "raw_components": "no"},
            "unexpected parameters: raw_components",
        ),
    ],
)
def test_button_parameters_are_required_and_raw_extras_are_rejected(parameters, message) -> None:
    schema = {
        "body": ["parent_name"],
        "buttons": [{"index": 0, "type": "url", "parameter": "results_path"}],
    }
    with pytest.raises(InvalidTemplateParametersError, match=message):
        ordered_template_values(schema, parameters)
