import math
import re
from dataclasses import dataclass


class InvalidSmsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SmsAnalysis:
    character_count: int
    page_count: int


_EMOJI = re.compile(
    "["
    "\U0001f000-\U0001faff"
    "\U00002600-\U000027bf"
    "\U0000231a-\U0000231b"
    "\U000023e9-\U000023f3"
    "\U000023f8-\U000023fa"
    "\U00002b00-\U00002bff"
    "\U0000fe0f"
    "\U0000200d"
    "]"
)
_SMS_TOKEN = re.compile(r"{{([a-z][a-z0-9_]{0,63})}}")


def analyze_sms(text: str) -> SmsAnalysis:
    if not text:
        raise InvalidSmsError("rendered SMS must not be empty")
    if _EMOJI.search(text):
        raise InvalidSmsError("rendered SMS contains unsupported emoji")
    count = len(text)
    if count > 960:
        raise InvalidSmsError("rendered SMS must not exceed 960 characters")
    return SmsAnalysis(character_count=count, page_count=math.ceil(count / 160))


def sms_template_parameters(body: str) -> list[str]:
    names = _SMS_TOKEN.findall(body)
    residual = _SMS_TOKEN.sub("", body)
    if "{{" in residual or "}}" in residual:
        raise InvalidSmsError("SMS body contains invalid template syntax")
    if len(names) != len(set(names)):
        raise InvalidSmsError("SMS body contains duplicate parameter placeholders")
    return names


def render_sms(body: str, expected: list[str], parameters: dict[str, str]) -> str:
    placeholders = sms_template_parameters(body)
    if placeholders != expected:
        raise InvalidSmsError("SMS body placeholders must match declared body parameters in order")
    expected_set = set(expected)
    missing = [name for name in expected if name not in parameters]
    extra = sorted(set(parameters) - expected_set)
    if missing:
        raise InvalidSmsError(f"missing template parameters: {', '.join(missing)}")
    if extra:
        raise InvalidSmsError(f"unexpected template parameters: {', '.join(extra)}")
    rendered = _SMS_TOKEN.sub(lambda match: parameters[match.group(1)], body)
    analyze_sms(rendered)
    return rendered


def normalize_kenyan_sms_recipient(value: str) -> str:
    normalized = re.sub(r"[\s()-]", "", value)
    if normalized.startswith("+"):
        normalized = normalized[1:]
    if normalized.startswith("0"):
        normalized = "254" + normalized[1:]
    if not re.fullmatch(r"254(?:7\d{8}|1\d{8})", normalized):
        raise ValueError("SMS recipient must be a supported Kenyan mobile number")
    return normalized
