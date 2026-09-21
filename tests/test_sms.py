import pytest

from app.services.sms import (
    InvalidSmsError,
    analyze_sms,
    normalize_kenyan_sms_recipient,
    render_sms,
)


@pytest.mark.parametrize(
    ("length", "pages"),
    [(1, 1), (160, 1), (161, 2), (320, 2), (321, 3), (800, 5), (801, 6), (960, 6)],
)
def test_sms_page_boundaries(length: int, pages: int) -> None:
    analysis = analyze_sms("a" * length)
    assert analysis.character_count == length
    assert analysis.page_count == pages


def test_sms_rejects_empty_oversized_and_emoji() -> None:
    for text, message in (("", "empty"), ("a" * 961, "960"), ("Hello 😊", "emoji")):
        with pytest.raises(InvalidSmsError, match=message):
            analyze_sms(text)


def test_spaces_and_common_punctuation_count() -> None:
    text = "Hello, parent! Results are ready."
    assert analyze_sms(text).character_count == len(text)


def test_safe_deterministic_rendering() -> None:
    assert (
        render_sms(
            "Hello {{name}}, {{term}}.", ["name", "term"], {"name": "Amina", "term": "Term 2"}
        )
        == "Hello Amina, Term 2."
    )
    with pytest.raises(InvalidSmsError, match="missing"):
        render_sms("Hello {{name}}", ["name"], {})
    with pytest.raises(InvalidSmsError, match="unexpected"):
        render_sms("Hello {{name}}", ["name"], {"name": "Amina", "extra": "x"})


@pytest.mark.parametrize(
    ("supplied", "canonical"),
    [
        ("0712345678", "254712345678"),
        ("+254 712 345 678", "254712345678"),
        ("0112345678", "254112345678"),
        ("254112345678", "254112345678"),
    ],
)
def test_kenyan_recipient_normalization(supplied: str, canonical: str) -> None:
    assert normalize_kenyan_sms_recipient(supplied) == canonical


@pytest.mark.parametrize("number", ["", "254612345678", "07123", "255712345678", "not-phone"])
def test_invalid_kenyan_recipient_rejected(number: str) -> None:
    with pytest.raises(ValueError):
        normalize_kenyan_sms_recipient(number)
