"""Upload validation.

A PDF is a small virtual machine with a document attached. These tests build
files that exercise each of its dangerous capabilities and assert the gate
refuses them — a validator nobody attacks is a validator nobody has tested.
"""

from __future__ import annotations

import io

import pikepdf
import pytest

from app.security.pdf_validation import RejectionReason, validate_pdf


def _blank_pdf(pages: int = 1) -> bytes:
    pdf = pikepdf.Pdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(595, 842))
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def _pdf_with_catalog_entry(name: str, value) -> bytes:
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    pdf.Root[name] = value
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


class TestBasicShape:
    def test_a_plain_pdf_is_accepted(self) -> None:
        result = validate_pdf(_blank_pdf())
        assert result.ok
        assert result.page_count == 1

    def test_empty_input_is_rejected(self) -> None:
        assert validate_pdf(b"").reason is RejectionReason.EMPTY

    def test_a_renamed_executable_is_rejected(self) -> None:
        """The extension and the declared Content-Type are both caller-supplied.

        The magic number is the only claim about a file's type worth anything.
        """
        result = validate_pdf(b"MZ\x90\x00\x03" + b"\x00" * 500)
        assert result.reason is RejectionReason.NOT_A_PDF

    def test_a_pdf_shaped_but_corrupt_file_is_rejected(self) -> None:
        result = validate_pdf(b"%PDF-1.7\nthis is not actually a pdf body")
        assert result.reason is RejectionReason.CORRUPT

    def test_an_oversized_file_is_rejected_before_parsing(self) -> None:
        from app.core.config import settings

        oversized = b"%PDF-1.7" + b"\x00" * (settings.max_upload_bytes + 1)
        assert validate_pdf(oversized).reason is RejectionReason.TOO_LARGE

    def test_too_many_pages_is_rejected(self) -> None:
        from app.core.config import settings

        result = validate_pdf(_blank_pdf(pages=settings.MAX_PDF_PAGES + 1))
        assert result.reason is RejectionReason.TOO_MANY_PAGES


class TestActiveContent:
    """Each of these is a real PDF capability, not a hypothetical."""

    def test_embedded_javascript_is_rejected(self) -> None:
        data = _pdf_with_catalog_entry(
            "/Names",
            pikepdf.Dictionary(
                JavaScript=pikepdf.Dictionary(Names=pikepdf.Array([]))
            ),
        )
        result = validate_pdf(data)
        assert not result.ok
        assert result.reason is RejectionReason.ACTIVE_CONTENT

    def test_an_open_action_is_rejected(self) -> None:
        """Runs the moment the document is opened — no interaction required."""
        data = _pdf_with_catalog_entry(
            "/OpenAction", pikepdf.Dictionary(S=pikepdf.Name("/JavaScript"), JS="app.alert(1)")
        )
        result = validate_pdf(data)
        assert not result.ok
        assert result.reason is RejectionReason.ACTIVE_CONTENT

    def test_a_launch_action_is_rejected(self) -> None:
        """Asks the viewer to execute an external program."""
        data = _pdf_with_catalog_entry(
            "/OpenAction",
            pikepdf.Dictionary(S=pikepdf.Name("/Launch"), Launch="/bin/sh"),
        )
        assert not validate_pdf(data).ok

    def test_additional_actions_are_rejected(self) -> None:
        """/AA hides page-open and page-close hooks away from the catalog."""
        data = _pdf_with_catalog_entry(
            "/AA", pikepdf.Dictionary(O=pikepdf.Dictionary(S=pikepdf.Name("/JavaScript")))
        )
        assert not validate_pdf(data).ok

    def test_an_embedded_file_is_rejected(self) -> None:
        data = _pdf_with_catalog_entry(
            "/Names",
            pikepdf.Dictionary(EmbeddedFiles=pikepdf.Dictionary(Names=pikepdf.Array([]))),
        )
        result = validate_pdf(data)
        assert not result.ok
        assert result.reason is RejectionReason.EMBEDDED_FILE

    def test_an_xfa_form_is_rejected(self) -> None:
        """XFA is an entire second forms engine with its own scripting."""
        data = _pdf_with_catalog_entry(
            "/AcroForm", pikepdf.Dictionary(XFA=pikepdf.Array([]))
        )
        assert not validate_pdf(data).ok

    def test_findings_name_the_construct_not_its_payload(self) -> None:
        """The audit trail records *what kind* of thing was found.

        Storing the payload would mean archiving the attack in a table people
        read.
        """
        data = _pdf_with_catalog_entry(
            "/OpenAction",
            pikepdf.Dictionary(S=pikepdf.Name("/JavaScript"), JS="stealEverything()"),
        )
        result = validate_pdf(data)
        assert "/OpenAction" in result.findings
        assert not any("steal" in finding for finding in result.findings)


class TestEncryptedPdfs:
    def test_a_password_protected_pdf_asks_for_a_password(self) -> None:
        """Indian banks routinely password-protect statements, so this is the
        normal case rather than an error."""
        pdf = pikepdf.Pdf.new()
        pdf.add_blank_page(page_size=(595, 842))
        buffer = io.BytesIO()
        pdf.save(buffer, encryption=pikepdf.Encryption(user="secret", owner="secret"))

        result = validate_pdf(buffer.getvalue())
        assert result.reason is RejectionReason.PASSWORD_REQUIRED

    def test_the_correct_password_opens_it(self) -> None:
        pdf = pikepdf.Pdf.new()
        pdf.add_blank_page(page_size=(595, 842))
        buffer = io.BytesIO()
        pdf.save(buffer, encryption=pikepdf.Encryption(user="secret", owner="secret"))

        result = validate_pdf(buffer.getvalue(), password="secret")
        assert result.ok
        assert result.is_encrypted

    def test_a_wrong_password_is_reported_as_such(self) -> None:
        pdf = pikepdf.Pdf.new()
        pdf.add_blank_page(page_size=(595, 842))
        buffer = io.BytesIO()
        pdf.save(buffer, encryption=pikepdf.Encryption(user="secret", owner="secret"))

        result = validate_pdf(buffer.getvalue(), password="wrong")
        assert result.reason is RejectionReason.WRONG_PASSWORD


class TestErrorMessages:
    @pytest.mark.parametrize(
        "data",
        [b"", b"MZ\x90\x00", b"%PDF-1.7\nbroken"],
    )
    def test_rejection_messages_never_echo_file_content(self, data: bytes) -> None:
        """A parser error can quote raw bytes from the file.

        Putting that in a user-facing message is a reflected-content bug, and
        putting it in a log is a leak — so the message is always one of our own.
        """
        result = validate_pdf(data)
        assert not result.ok
        assert result.message
        for fragment in (b"MZ", b"broken"):
            assert fragment.decode() not in result.message
