"""Statement upload and ingestion, over real HTTP."""

from __future__ import annotations

import io

import httpx
import pikepdf
import pytest

from tests.conftest import auth_header, register_user


def _statement_pdf(text_lines: list[str] | None = None) -> bytes:
    """A minimal but genuine PDF with a real text layer."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setFont("Helvetica", 9)
    y = 780
    for line in text_lines or [
        "HDFC BANK LIMITED",
        "Account Statement",
        "Account Number: XXXXXXXX4821    IFSC: HDFC0001234",
        "Opening Balance: 45,230.00   Closing Balance: 38,915.50",
        "02/03/2026  UPI-SWIGGY-8829172  450.00  44,780.00",
        "03/03/2026  NEFT-SALARY MAR     85,000.00  129,780.00",
    ]:
        pdf.drawString(40, y, line)
        y -= 14
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _malicious_pdf() -> bytes:
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    pdf.Root["/OpenAction"] = pikepdf.Dictionary(
        S=pikepdf.Name("/JavaScript"), JS="app.alert('x')"
    )
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def _files(*items: tuple[str, bytes]):
    return [("files", (name, data, "application/pdf")) for name, data in items]


@pytest.fixture
async def account(client: httpx.AsyncClient):
    return await register_user(client)


class TestUpload:
    async def test_a_valid_statement_is_accepted_and_queued(
        self, client: httpx.AsyncClient, account
    ) -> None:
        response = await client.post(
            "/api/v1/statements/upload",
            headers=auth_header(account["access_token"]),
            files=_files(("march.pdf", _statement_pdf())),
        )

        assert response.status_code == 202
        body = response.json()
        assert body["accepted"] == 1
        result = body["results"][0]
        assert result["statement_id"] and result["job_id"]
        assert result["page_count"] == 1

    async def test_a_pdf_with_active_content_is_refused(
        self, client: httpx.AsyncClient, account
    ) -> None:
        response = await client.post(
            "/api/v1/statements/upload",
            headers=auth_header(account["access_token"]),
            files=_files(("evil.pdf", _malicious_pdf())),
        )

        body = response.json()
        assert body["accepted"] == 0
        assert body["results"][0]["error_code"] == "active_content"
        # Refused before storage: nothing malicious is written down.
        assert body["results"][0]["statement_id"] is None

    async def test_a_renamed_non_pdf_is_refused(
        self, client: httpx.AsyncClient, account
    ) -> None:
        response = await client.post(
            "/api/v1/statements/upload",
            headers=auth_header(account["access_token"]),
            files=_files(("payload.pdf", b"MZ\x90\x00\x03not a pdf at all")),
        )
        assert response.json()["results"][0]["error_code"] == "not_a_pdf"

    async def test_one_bad_file_does_not_discard_the_good_ones(
        self, client: httpx.AsyncClient, account
    ) -> None:
        """A drop of twelve statements with one dud should import eleven."""
        response = await client.post(
            "/api/v1/statements/upload",
            headers=auth_header(account["access_token"]),
            files=_files(
                ("good-a.pdf", _statement_pdf(["HDFC BANK", "Opening Balance: 1.00"])),
                ("evil.pdf", _malicious_pdf()),
                ("good-b.pdf", _statement_pdf(["ICICI BANK", "Closing Balance: 2.00"])),
            ),
        )

        body = response.json()
        assert body["accepted"] == 2
        assert body["rejected"] == 1
        assert [r["accepted"] for r in body["results"]] == [True, False, True]

    async def test_the_same_file_twice_is_caught_by_content_hash(
        self, client: httpx.AsyncClient, account
    ) -> None:
        """Re-uploading the identical PDF is the commonest source of duplicate
        transactions. Catching it here costs one index lookup instead of a full
        extraction that then has to be discarded."""
        data = _statement_pdf(["AXIS BANK", "Opening Balance: 100.00"])

        first = await client.post(
            "/api/v1/statements/upload",
            headers=auth_header(account["access_token"]),
            files=_files(("stmt.pdf", data)),
        )
        assert first.json()["accepted"] == 1

        second = await client.post(
            "/api/v1/statements/upload",
            headers=auth_header(account["access_token"]),
            files=_files(("stmt-again.pdf", data)),
        )
        assert second.json()["accepted"] == 0
        assert second.json()["results"][0]["error_code"] == "duplicate_file"

    async def test_upload_requires_authentication(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/api/v1/statements/upload", files=_files(("x.pdf", _statement_pdf()))
        )
        assert response.status_code == 401


class TestStatementAccess:
    async def test_a_statement_is_listed_after_upload(
        self, client: httpx.AsyncClient, account
    ) -> None:
        await client.post(
            "/api/v1/statements/upload",
            headers=auth_header(account["access_token"]),
            files=_files(("kotak.pdf", _statement_pdf(["KOTAK BANK", "Balance 5.00"]))),
        )

        response = await client.get(
            "/api/v1/statements", headers=auth_header(account["access_token"])
        )
        assert response.status_code == 200
        assert len(response.json()) == 1

    async def test_a_new_statement_is_never_trusted_on_arrival(
        self, client: httpx.AsyncClient, account
    ) -> None:
        """Trust is earned by reconciliation, which has not run at upload time.

        Defaulting to trusted and downgrading later would mean every statement
        is briefly believed for reasons nobody checked.
        """
        await client.post(
            "/api/v1/statements/upload",
            headers=auth_header(account["access_token"]),
            files=_files(("sbi.pdf", _statement_pdf(["SBI", "Balance 9.00"]))),
        )
        listing = await client.get(
            "/api/v1/statements", headers=auth_header(account["access_token"])
        )
        assert listing.json()[0]["trust_status"] == "pending"
        assert listing.json()[0]["transaction_count"] == 0

    async def test_another_tenant_cannot_read_your_statement(
        self, client: httpx.AsyncClient, account
    ) -> None:
        """The id is real and the attacker's token is valid; only the tenant
        scope stops it."""
        from app.main import app

        upload = await client.post(
            "/api/v1/statements/upload",
            headers=auth_header(account["access_token"]),
            files=_files(("mine.pdf", _statement_pdf(["IDFC", "Balance 3.00"]))),
        )
        statement_id = upload.json()["results"][0]["statement_id"]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as other_client:
            intruder = await register_user(other_client)

        for path in (
            f"/api/v1/statements/{statement_id}",
            f"/api/v1/statements/{statement_id}/health",
            f"/api/v1/statements/{statement_id}/download-url",
        ):
            response = await client.get(path, headers=auth_header(intruder["access_token"]))
            assert response.status_code == 404, f"{path} leaked across tenants"

    async def test_another_tenant_cannot_delete_your_statement(
        self, client: httpx.AsyncClient, account
    ) -> None:
        from app.main import app

        upload = await client.post(
            "/api/v1/statements/upload",
            headers=auth_header(account["access_token"]),
            files=_files(("mine2.pdf", _statement_pdf(["YES BANK", "Balance 4.00"]))),
        )
        statement_id = upload.json()["results"][0]["statement_id"]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as other_client:
            intruder = await register_user(other_client)

        response = await client.delete(
            f"/api/v1/statements/{statement_id}",
            headers=auth_header(intruder["access_token"]),
        )
        assert response.status_code == 404

        # Still there for its owner.
        still = await client.get(
            f"/api/v1/statements/{statement_id}",
            headers=auth_header(account["access_token"]),
        )
        assert still.status_code == 200


class TestDownloadLinks:
    async def test_a_download_link_is_scoped_to_one_statement(
        self, client: httpx.AsyncClient, account
    ) -> None:
        """A leaked link should expose that document and nothing else."""
        uploads = await client.post(
            "/api/v1/statements/upload",
            headers=auth_header(account["access_token"]),
            files=_files(
                ("one.pdf", _statement_pdf(["BANK ONE", "Balance 1.00"])),
                ("two.pdf", _statement_pdf(["BANK TWO", "Balance 2.00"])),
            ),
        )
        first, second = (r["statement_id"] for r in uploads.json()["results"])

        link = await client.get(
            f"/api/v1/statements/{first}/download-url",
            headers=auth_header(account["access_token"]),
        )
        token = link.json()["url"].split("token=")[1]

        # The token for the first statement must not open the second.
        response = await client.get(
            f"/api/v1/statements/{second}/download",
            params={"token": token},
            headers=auth_header(account["access_token"]),
        )
        assert response.status_code == 401

    async def test_a_valid_link_returns_the_decrypted_pdf(
        self, client: httpx.AsyncClient, account
    ) -> None:
        """Round-trips through AES-GCM: what comes back is a readable PDF."""
        original = _statement_pdf(["ROUNDTRIP BANK", "Balance 7.00"])
        upload = await client.post(
            "/api/v1/statements/upload",
            headers=auth_header(account["access_token"]),
            files=_files(("rt.pdf", original)),
        )
        statement_id = upload.json()["results"][0]["statement_id"]

        link = await client.get(
            f"/api/v1/statements/{statement_id}/download-url",
            headers=auth_header(account["access_token"]),
        )
        token = link.json()["url"].split("token=")[1]

        response = await client.get(
            f"/api/v1/statements/{statement_id}/download",
            params={"token": token},
            headers=auth_header(account["access_token"]),
        )
        assert response.status_code == 200
        assert response.content.startswith(b"%PDF-")
        assert response.content == original

    async def test_a_forged_download_token_is_rejected(
        self, client: httpx.AsyncClient, account
    ) -> None:
        upload = await client.post(
            "/api/v1/statements/upload",
            headers=auth_header(account["access_token"]),
            files=_files(("f.pdf", _statement_pdf(["X BANK", "Balance 8.00"]))),
        )
        statement_id = upload.json()["results"][0]["statement_id"]

        response = await client.get(
            f"/api/v1/statements/{statement_id}/download",
            params={"token": "not.a.real.token"},
            headers=auth_header(account["access_token"]),
        )
        assert response.status_code == 401
