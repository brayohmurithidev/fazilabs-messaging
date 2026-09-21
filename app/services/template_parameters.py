import re
from dataclasses import dataclass
from typing import Any


class InvalidTemplateSchemaError(ValueError):
    pass


class InvalidTemplateParametersError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UrlButtonValue:
    index: int
    value: str


@dataclass(frozen=True, slots=True)
class OrderedTemplateValues:
    body: list[str]
    url_buttons: list[UrlButtonValue]


PARAMETER_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
KNOWN_COMPONENTS = {"header", "body", "button", "buttons"}


def _validate_name(name: Any) -> str:
    if not isinstance(name, str) or not PARAMETER_NAME_PATTERN.fullmatch(name):
        raise InvalidTemplateSchemaError(f"invalid parameter name: {name!r}")
    return name


def validate_parameter_schema(schema: Any) -> dict[str, Any]:
    if schema is None:
        return {"body": [], "buttons": []}
    if not isinstance(schema, dict) or not set(schema).issubset(KNOWN_COMPONENTS):
        raise InvalidTemplateSchemaError(
            "schema may only contain header, body, button, and buttons"
        )
    for legacy_component in ("header", "button"):
        legacy_value = schema.get(legacy_component, [])
        if not isinstance(legacy_value, list):
            raise InvalidTemplateSchemaError(
                f"{legacy_component} must be an array of parameter names"
            )
        if legacy_value:
            raise InvalidTemplateSchemaError(
                f"non-empty {legacy_component} parameters are not supported"
            )

    body = schema.get("body", [])
    if not isinstance(body, list):
        raise InvalidTemplateSchemaError("body must be an array of parameter names")
    seen_names: set[str] = set()
    normalized_body: list[str] = []
    for raw_name in body:
        name = _validate_name(raw_name)
        if name in seen_names:
            raise InvalidTemplateSchemaError(f"duplicate parameter name: {name}")
        seen_names.add(name)
        normalized_body.append(name)

    buttons = schema.get("buttons", [])
    if not isinstance(buttons, list):
        raise InvalidTemplateSchemaError("buttons must be an array of button definitions")
    normalized_buttons: list[dict[str, Any]] = []
    seen_indexes: set[int] = set()
    for button in buttons:
        if not isinstance(button, dict):
            raise InvalidTemplateSchemaError("each button must be an object")
        if set(button) != {"index", "type", "parameter"}:
            raise InvalidTemplateSchemaError(
                "each button must contain only index, type, and parameter"
            )
        index = button["index"]
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise InvalidTemplateSchemaError("button index must be a non-negative integer")
        if index in seen_indexes:
            raise InvalidTemplateSchemaError(f"duplicate button index: {index}")
        seen_indexes.add(index)
        button_type = button["type"]
        if button_type != "url":
            raise InvalidTemplateSchemaError(
                f"unsupported button type: {button_type!r}; only 'url' is supported"
            )
        name = _validate_name(button["parameter"])
        if name in seen_names:
            raise InvalidTemplateSchemaError(f"duplicate parameter name: {name}")
        seen_names.add(name)
        normalized_buttons.append({"index": index, "type": "url", "parameter": name})

    if len(seen_names) > 20:
        raise InvalidTemplateSchemaError("templates may define at most 20 parameters")
    return {"body": normalized_body, "buttons": normalized_buttons}


def semantic_parameter_names(schema: dict[str, Any] | None) -> list[str]:
    normalized = validate_parameter_schema(schema)
    return [
        *normalized["body"],
        *(button["parameter"] for button in normalized["buttons"]),
    ]


def ordered_template_values(
    schema: dict[str, Any] | None, parameters: dict[str, str]
) -> OrderedTemplateValues:
    normalized = validate_parameter_schema(schema)
    expected = set(semantic_parameter_names(normalized))
    supplied = set(parameters)
    missing = sorted(expected - supplied)
    extra = sorted(supplied - expected)
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing parameters: {', '.join(missing)}")
        if extra:
            parts.append(f"unexpected parameters: {', '.join(extra)}")
        raise InvalidTemplateParametersError("; ".join(parts))
    return OrderedTemplateValues(
        body=[parameters[name] for name in normalized["body"]],
        url_buttons=[
            UrlButtonValue(index=button["index"], value=parameters[button["parameter"]])
            for button in normalized["buttons"]
        ],
    )


def ordered_body_values(schema: dict[str, Any] | None, parameters: dict[str, str]) -> list[str]:
    """Return ordered BODY values for compatibility with existing callers."""
    return ordered_template_values(schema, parameters).body
