"""Parser dispatch.

Every parser scores the document; the highest score wins. Detection is a score
rather than a first-match-wins loop because Indian statements genuinely overlap
— an ICICI credit-card statement mentions ICICI Bank, and so does an ICICI
savings statement — and the parser that recognises the *document* has to beat
the parser that merely recognises the *issuer*.

When nothing scores above the floor the generic parsers take over rather than
the pipeline failing. A statement read by the generic parser and reconciled to
₹0.00 is a good outcome; refusing to read an unrecognised bank is not.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import DocumentType

from parsers.base import BankParser
from parsers.document import ExtractedDocument

#: Below this, a bank-specific parser is not considered a match.
DETECTION_FLOOR = 0.35


@dataclass(frozen=True, slots=True)
class Dispatch:
    parser: BankParser
    confidence: float
    #: True when no bank-specific parser matched and a generic one is standing in.
    is_fallback: bool
    #: Every candidate's score, for the Statement Health report and for
    #: debugging a misdetection without re-running extraction.
    scores: tuple[tuple[str, float], ...]


class ParserRegistry:
    def __init__(self) -> None:
        self._parsers: list[BankParser] = []

    def register(self, parser: BankParser) -> BankParser:
        self._parsers.append(parser)
        self._parsers.sort(key=lambda item: -item.priority)
        return parser

    def all(self) -> list[BankParser]:
        return list(self._parsers)

    def by_code(self, bank_code: str, document_type: DocumentType) -> BankParser | None:
        for parser in self._parsers:
            if parser.bank_code == bank_code and document_type in parser.document_types:
                return parser
        return None

    def resolve(
        self,
        document: ExtractedDocument,
        *,
        document_type: DocumentType = DocumentType.UNKNOWN,
    ) -> Dispatch:
        """Pick a parser for this document.

        ``document_type`` comes from the classifier and is a hard filter, not a
        hint: a bank-statement parser pointed at a credit-card statement reads
        the "Amount Due" summary block as transactions and produces confident
        nonsense, so it is not allowed to try.
        """
        candidates = [
            parser
            for parser in self._parsers
            if document_type == DocumentType.UNKNOWN
            or document_type in parser.document_types
        ]
        if not candidates:
            candidates = self._parsers

        scored = sorted(
            ((parser, parser.detect(document)) for parser in candidates),
            key=lambda pair: (-pair[1], -pair[0].priority),
        )
        scores = tuple((parser.parser_name, score) for parser, score in scored)

        specific = [
            (parser, score)
            for parser, score in scored
            if score >= DETECTION_FLOOR and parser.priority > 0
        ]
        if specific:
            parser, score = specific[0]
            return Dispatch(parser=parser, confidence=score, is_fallback=False, scores=scores)

        fallback = self._fallback(document_type)
        return Dispatch(parser=fallback, confidence=0.0, is_fallback=True, scores=scores)

    def _fallback(self, document_type: DocumentType) -> BankParser:
        generics = [parser for parser in self._parsers if parser.priority == 0]
        for parser in generics:
            if document_type in parser.document_types:
                return parser
        if not generics:
            raise LookupError("no generic parser is registered")
        return generics[0]


registry = ParserRegistry()


def load_parsers() -> ParserRegistry:
    """Import every parser module so registration side effects run.

    Explicit imports rather than a directory scan: a parser that fails to import
    should break the build loudly, not silently vanish from the registry and
    take a bank's accuracy to zero with nobody noticing.
    """
    from parsers.banks import (  # noqa: F401
        axis,
        generic,
        generic_card,
        hdfc,
        icici,
        idfc,
        indusind,
        kotak,
        sbi,
        yesbank,
    )

    return registry
