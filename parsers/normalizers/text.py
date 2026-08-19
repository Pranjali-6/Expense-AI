"""Description cleanup shared by every parser.

Statement narrations are dense with machine noise: reference numbers, terminal
IDs, masked card numbers, IFSC codes, UPI handles, branch codes. The merchant is
in there somewhere, usually surrounded by four things nobody wants to read.

Everything here is *structural* cleanup — it removes tokens by shape. Deciding
that what is left means "Swiggy" is the merchant normalizer's job.
"""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"[\s   ]+")

# A description cell that wrapped across PDF lines arrives with newlines inside
# it. Joining on a space is right; joining on "" would weld words together.
_LINEBREAK = re.compile(r"[\r\n]+")

# ---------------------------------------------------------------- noise sets --

# Masked card numbers in every shape Indian banks print them.
_MASKED_CARD = re.compile(r"\b\d{0,6}[Xx*]{4,12}\d{2,4}\b")
# Bare long digit runs: reference, UTR, terminal, cheque and order numbers.
_LONG_DIGITS = re.compile(r"\b\d{8,}\b")
# IFSC: four letters, a literal zero, six alphanumerics.
_IFSC = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
# UPI virtual payment address. The handle half is the noise; the name half is
# often the merchant, so this is stripped only after the merchant extractor has
# had a look at it.
_UPI_HANDLE = re.compile(r"\b[\w.\-]{2,64}@[a-zA-Z]{2,20}\b")
# Trailing separator debris left behind once tokens are removed.
_SEPARATOR_RUN = re.compile(r"[\-/|:,]{2,}")
_EDGE_JUNK = re.compile(r"^[\s\-/|:,.]+|[\s\-/|:,.]+$")


def collapse(raw: str | None) -> str:
    """Whitespace-normalise a cell, joining wrapped lines."""
    if not raw:
        return ""
    return _WHITESPACE.sub(" ", _LINEBREAK.sub(" ", str(raw))).strip()


def strip_machine_noise(raw: str) -> str:
    """Remove reference numbers, masked cards, IFSC codes and digit runs."""
    text = collapse(raw)
    text = _MASKED_CARD.sub(" ", text)
    text = _IFSC.sub(" ", text)
    text = _LONG_DIGITS.sub(" ", text)
    text = _SEPARATOR_RUN.sub(" ", text)
    text = _WHITESPACE.sub(" ", text)
    return _EDGE_JUNK.sub("", text)


def split_tokens(raw: str) -> list[str]:
    """Split a narration on the separators Indian banks use between fields."""
    parts = re.split(r"[/|\\]+|\s+-\s+|(?<=[A-Za-z0-9])-(?=[A-Za-z])", collapse(raw))
    return [part.strip() for part in parts if part and part.strip()]


def upi_handle_name(raw: str) -> str | None:
    """Return the local part of a UPI VPA, which often names the payee.

    ``UPI-SWIGGY-swiggy@ybl-YESB0…`` → ``swiggy``.

    The address is found by **tokenising first**, not by matching a VPA pattern
    against the whole narration. A hyphen is legal inside a VPA local part *and*
    is the field separator Indian banks use, so a greedy pattern reading
    ``UPI-x-swiggy@ybl`` returns ``UPI-x-swiggy`` — dragging two rail tokens
    into what gets stored as a merchant name. Splitting on separators can
    truncate the rare hyphenated VPA, which is the safer failure: capturing too
    little loses a name, capturing too much invents one.

    A numeric local part (a phone-number VPA) is rejected outright — that
    identifies a person, and a person's phone number must never become a stored
    "merchant".
    """
    for token in re.split(r"[\s/|\\-]+", collapse(raw)):
        if "@" not in token:
            continue
        local, _, domain = token.partition("@")
        if not re.fullmatch(r"[A-Za-z]{2,20}", domain):
            continue
        cleaned = re.sub(r"[._]+", " ", local).strip()
        if not cleaned or cleaned.isdigit():
            continue
        if sum(character.isdigit() for character in cleaned) > len(cleaned) / 2:
            continue
        return cleaned
    return None
