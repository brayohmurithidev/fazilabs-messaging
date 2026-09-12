import re
from typing import Any


class InvalidTemplateSchemaError(ValueError):
    pass


class InvalidTemplateParametersError(ValueError):
    pass


SUPPORTED_COMPONENT = "body"
KNOWN_COMPONENTS = {"header", "body", "button"}


def validate_parameter_schema(schema: Any) -> dict[str, list[str]]:
    if schema is None:
        return {SUPPORTED_COMPONENT: []}
    if not isinstance(schema, dict) or not set(schema).issubset(KNOWN_COMPONENTS):
        raise InvalidTemplateSchemaError("schema may only contain header, body, and button")
    normalized: dict[str, list[str]] = {}
    seen: set[str] = set()
    total = 0
    for component in ("header", "body", "button"):
        names = schema.get(component, [])
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise InvalidTemplateSchemaError(f"{component} must be an array of parameter names")
        if component != SUPPORTED_COMPONENT and names:
            raise InvalidTemplateSchemaError(
                f"non-empty {component} parameters are not supported in this version"
            )
        for name in names:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
                raise InvalidTemplateSchemaError(f"invalid parameter name: {name!r}")
            if name in seen:
                raise InvalidTemplateSchemaError(f"duplicate parameter name: {name}")
            seen.add(name)
            total += 1
        normalized[component] = names
    if total > 20:
        raise InvalidTemplateSchemaError("templates may define at most 20 parameters")
    return normalized


def ordered_body_values(schema: dict[str, Any] | None, parameters: dict[str, str]) -> list[str]:
    normalized = validate_parameter_schema(schema)
    expected = set(normalized[SUPPORTED_COMPONENT])
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
    return [parameters[name] for name in normalized[SUPPORTED_COMPONENT]]
