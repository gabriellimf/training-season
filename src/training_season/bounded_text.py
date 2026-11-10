"""Text rules for untrusted model JSON: trimmed, sized, no control characters."""

import unicodedata

from pydantic import AfterValidator, StringConstraints


def reject_control_characters(text: str) -> str:
    """
    Refuse any Unicode control or format character.

    Used as `AfterValidator(reject_control_characters)`
    """
    for character in text:
        if unicodedata.category(character).startswith("C"):
            raise ValueError(
                f"control or format character U+{ord(character):04X} is not allowed"
            )
    return text


def reject_control_characters_but_newline(text: str) -> str:
    """
    Refuse control or format characters, except the line feed.

    Used as `AfterValidator(reject_control_characters_but_newline)`
    """
    reject_control_characters(text.replace("\n", ""))
    return text


def drop_control_characters_but_newline(text: str) -> str:
    """
    Remove control or format characters, except the line feed, from free text.

    Used as `drop_control_characters_but_newline(f"Oi{chr(27)}[2J")`, giving `Oi[2J`
    """
    return "".join(
        character
        for character in text
        if character == "\n" or not unicodedata.category(character).startswith("C")
    )


SINGLE_LINE = AfterValidator(reject_control_characters)
MULTI_LINE = AfterValidator(reject_control_characters_but_newline)


def trimmed(max_length: int) -> StringConstraints:
    """
    Strip outer whitespace, then require 1 to `max_length` characters.

    Used as `Annotated[str, trimmed(60), SINGLE_LINE]`
    """
    return StringConstraints(strip_whitespace=True, min_length=1, max_length=max_length)


def text_length_errors(label: str, text: str, max_length: int) -> tuple[str, ...]:
    """
    One error line when the raw text is longer than `max_length`, else none.

    Used as `text_length_errors("macro plan text", text, 100_000)`
    """
    if len(text) <= max_length:
        return ()
    return (f"{label} has {len(text)} characters, the maximum is {max_length}",)
