"""Password-protected statements, over real HTTP.

The behaviour these lock down, in one sentence: a locked statement is *stored*
and parked, unlocked per file rather than per batch, and the password itself is
never persisted, logged or audited.

The bug this replaced is worth remembering. The password was accepted at upload
and used for the structural check, then dropped — the still-encrypted bytes went
to storage and extraction later read zero pages from them. Supplying the correct
password produced a worse outcome than supplying none, which is why
`test_the_stored_object_opens_without_a_password` exists.
"""

from __future__ import annotations

import io
import logging

import httpx
import pikepdf
import pytest

from tests.conftest import auth_header, register_user

PASSWORD = "KAVE0104"


def _statement_pdf() -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setFont("Helvetica", 9)
    y = 780
    for line in [
        "HDFC BANK LIMITED",
        "Account Statement",
        "Opening Balance: 45,230.00   Closing Balance: 44,780.00",
        "02/03/2026  UPI-SWIGGY-8829172  450.00  44,780.00",
    ]:
        pdf.drawString(40, y, line)
        y -= 14
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _locked_pdf(password: str = PASSWORD) -> bytes:
    """The same statement, encrypted the way a bank sends it."""
    buffer = io.BytesIO()
    with pikepdf.open(io.BytesIO(_statement_pdf())) as pdf:
        pdf.save(
            buffer,
            encryption=pikepdf.Encryption(owner=password, user=password, R=6),
        )
    return buffer.getvalue()


def _files(*items: tuple[str, bytes]):
    return [("files", (name, data, "application/pdf")) for name, data in items]


@pytest.fixture
async def account(client: httpx.AsyncClient):
    return await register_user(client)


async def _upload_locked(client: httpx.AsyncClient, account, name="march.pdf"):
    response = await client.post(
        "/api/v1/statements/upload",
        headers=auth_header(account["access_token"]),
        files=_files((name, _locked_pdf())),
    )
    assert response.status_code == 202
    return response.json()["results"][0]


class TestALockedUploadIsParkedNotRejected:
    async def test_it_is_stored_and_reports_a_statement_id(
        self, client: httpx.AsyncClient, account
    ) -> None:
        result = await _upload_locked(client, account)

        assert result["locked"] is True
        assert result["accepted"] is False
        assert result["error_code"] == "password_required"
        # The whole point: the file is on the server, so the client can prompt
        # for a password instead of asking for the file again.
        assert result["statement_id"]

    async def test_no_job_is_queued_for_a_file_nothing_can_read(
        self, client: httpx.AsyncClient, account
    ) -> None:
        result = await _upload_locked(client, account)
        assert result["job_id"] is None

    async def test_it_is_listed_as_locked_rather_than_failed(
        self, client: httpx.AsyncClient, account
    ) -> None:
        await _upload_locked(client, account)

        listing = await client.get(
            "/api/v1/statements", headers=auth_header(account["access_token"])
        )
        row = listing.json()[0]
        # `failed` would tell a user their statement is unreadable when it is
        # one password away from importing.
        assert row["status"] == "password_required"

    async def test_a_locked_file_does_not_block_the_rest_of_the_batch(
        self, client: httpx.AsyncClient, account
    ) -> None:
        response = await client.post(
            "/api/v1/statements/upload",
            headers=auth_header(account["access_token"]),
            files=_files(("locked.pdf", _locked_pdf()), ("open.pdf", _statement_pdf())),
        )

        body = response.json()
        assert body["accepted"] == 1
        assert sum(1 for r in body["results"] if r["locked"]) == 1


class TestUnlocking:
    async def test_the_right_password_resumes_the_pipeline(
        self, client: httpx.AsyncClient, account
    ) -> None:
        result = await _upload_locked(client, account)

        response = await client.post(
            f"/api/v1/statements/{result['statement_id']}/unlock",
            headers=auth_header(account["access_token"]),
            json={"password": PASSWORD},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["unlocked"] is True
        assert body["job_id"]
        assert body["page_count"] == 1

    async def test_the_stored_object_opens_without_a_password(
        self, client: httpx.AsyncClient, account
    ) -> None:
        """The regression that motivated all of this.

        Storing the encrypted bytes made the pipeline fail silently downstream.
        What is stored after an unlock must be readable with no password at
        all, because no password is kept for the worker to use.
        """
        result = await _upload_locked(client, account)
        statement_id = result["statement_id"]

        await client.post(
            f"/api/v1/statements/{statement_id}/unlock",
            headers=auth_header(account["access_token"]),
            json={"password": PASSWORD},
        )

        link = await client.get(
            f"/api/v1/statements/{statement_id}/download-url",
            headers=auth_header(account["access_token"]),
        )
        token = link.json()["url"].split("token=")[1]

        stored = await client.get(
            f"/api/v1/statements/{statement_id}/download",
            params={"token": token},
            headers=auth_header(account["access_token"]),
        )
        assert stored.status_code == 200

        with pikepdf.open(io.BytesIO(stored.content)) as pdf:
            assert not pdf.is_encrypted
            assert len(pdf.pages) == 1

    async def test_the_wrong_password_is_refused_without_saying_why(
        self, client: httpx.AsyncClient, account
    ) -> None:
        result = await _upload_locked(client, account)

        response = await client.post(
            f"/api/v1/statements/{result['statement_id']}/unlock",
            headers=auth_header(account["access_token"]),
            json={"password": "WRONG123"},
        )

        body = response.json()
        assert body["unlocked"] is False
        assert body["error_code"] == "wrong_password"
        assert body["attempts_remaining"] == 4
        # Nothing about the document, its length, or how close the guess was.
        assert body["message"] == "That password did not open the statement."

    async def test_a_wrong_password_leaves_the_statement_unlockable(
        self, client: httpx.AsyncClient, account
    ) -> None:
        result = await _upload_locked(client, account)
        headers = auth_header(account["access_token"])

        await client.post(
            f"/api/v1/statements/{result['statement_id']}/unlock",
            headers=headers,
            json={"password": "WRONG123"},
        )
        response = await client.post(
            f"/api/v1/statements/{result['statement_id']}/unlock",
            headers=headers,
            json={"password": PASSWORD},
        )

        assert response.json()["unlocked"] is True

    async def test_an_already_open_statement_cannot_be_unlocked(
        self, client: httpx.AsyncClient, account
    ) -> None:
        upload = await client.post(
            "/api/v1/statements/upload",
            headers=auth_header(account["access_token"]),
            files=_files(("open.pdf", _statement_pdf())),
        )
        statement_id = upload.json()["results"][0]["statement_id"]

        response = await client.post(
            f"/api/v1/statements/{statement_id}/unlock",
            headers=auth_header(account["access_token"]),
            json={"password": PASSWORD},
        )

        assert response.status_code == 409


class TestTheAttemptCapIsRealAndDurable:
    """Indian banks publish their statement password formulas, and they are
    built from PAN, date of birth and account digits. An unlock endpoint that
    answers indefinitely is a working oracle over exactly those identifiers."""

    async def test_the_statement_stops_answering_after_five_guesses(
        self, client: httpx.AsyncClient, account
    ) -> None:
        result = await _upload_locked(client, account)
        headers = auth_header(account["access_token"])
        url = f"/api/v1/statements/{result['statement_id']}/unlock"

        for _ in range(5):
            await client.post(url, headers=headers, json={"password": "WRONG123"})

        # The sixth is refused outright — and so is the correct password, which
        # is the point of a cap rather than a delay.
        response = await client.post(url, headers=headers, json={"password": PASSWORD})
        assert response.status_code == 422
        assert "unlock_attempts_exhausted" in response.text

    async def test_failed_attempts_persist_across_requests(
        self, client: httpx.AsyncClient, account
    ) -> None:
        result = await _upload_locked(client, account)
        headers = auth_header(account["access_token"])
        url = f"/api/v1/statements/{result['statement_id']}/unlock"

        seen = []
        for _ in range(3):
            response = await client.post(
                url, headers=headers, json={"password": "WRONG123"}
            )
            seen.append(response.json()["attempts_remaining"])

        # A counter that resets between requests is not a limit.
        assert seen == [4, 3, 2]

    async def test_a_successful_unlock_clears_the_count(
        self, client: httpx.AsyncClient, account
    ) -> None:
        result = await _upload_locked(client, account)
        headers = auth_header(account["access_token"])
        url = f"/api/v1/statements/{result['statement_id']}/unlock"

        await client.post(url, headers=headers, json={"password": "WRONG123"})
        response = await client.post(url, headers=headers, json={"password": PASSWORD})

        assert response.json()["attempts_remaining"] == 5


class TestTheSameLockedFileIsStillADuplicate:
    async def test_uploading_it_twice_is_caught_by_content_hash(
        self, client: httpx.AsyncClient, account
    ) -> None:
        data = _locked_pdf()
        headers = auth_header(account["access_token"])

        for _ in range(2):
            response = await client.post(
                "/api/v1/statements/upload", headers=headers, files=_files(("m.pdf", data))
            )
        assert response.json()["results"][0]["error_code"] == "duplicate_file"

    async def test_unlocking_does_not_reopen_the_duplicate_door(
        self, client: httpx.AsyncClient, account
    ) -> None:
        """The stored bytes change on unlock; the hash must not.

        `file_sha256` is the fingerprint of what the user uploaded. Rewriting it
        to the hash of the decrypted file would let the same locked statement be
        uploaded, unlocked and uploaded again indefinitely, each pass looking
        new — which is precisely the duplicate-transaction failure this system
        must not have.
        """
        data = _locked_pdf()
        headers = auth_header(account["access_token"])

        first = await client.post(
            "/api/v1/statements/upload", headers=headers, files=_files(("m.pdf", data))
        )
        statement_id = first.json()["results"][0]["statement_id"]
        await client.post(
            f"/api/v1/statements/{statement_id}/unlock",
            headers=headers,
            json={"password": PASSWORD},
        )

        again = await client.post(
            "/api/v1/statements/upload", headers=headers, files=_files(("m.pdf", data))
        )
        assert again.json()["results"][0]["error_code"] == "duplicate_file"


class TestIsolation:
    async def test_another_tenant_cannot_unlock_your_statement(
        self, client: httpx.AsyncClient, account
    ) -> None:
        result = await _upload_locked(client, account)
        intruder = await register_user(client)

        response = await client.post(
            f"/api/v1/statements/{result['statement_id']}/unlock",
            headers=auth_header(intruder["access_token"]),
            json={"password": PASSWORD},
        )

        # 404, not 403: RLS scopes the read, so the row is genuinely absent for
        # this session and the response leaks nothing about what exists.
        assert response.status_code == 404

    async def test_a_failed_cross_tenant_attempt_does_not_burn_the_owners_budget(
        self, client: httpx.AsyncClient, account
    ) -> None:
        result = await _upload_locked(client, account)
        intruder = await register_user(client)

        for _ in range(5):
            await client.post(
                f"/api/v1/statements/{result['statement_id']}/unlock",
                headers=auth_header(intruder["access_token"]),
                json={"password": "WRONG123"},
            )

        # Otherwise anyone who learns a statement id can lock its owner out.
        response = await client.post(
            f"/api/v1/statements/{result['statement_id']}/unlock",
            headers=auth_header(account["access_token"]),
            json={"password": PASSWORD},
        )
        assert response.json()["unlocked"] is True

    async def test_unlock_requires_authentication(
        self, client: httpx.AsyncClient, account
    ) -> None:
        result = await _upload_locked(client, account)
        response = await client.post(
            f"/api/v1/statements/{result['statement_id']}/unlock",
            json={"password": PASSWORD},
        )
        assert response.status_code == 401


class TestThePasswordIsNeverKept:
    async def test_it_does_not_reach_the_logs(
        self, client: httpx.AsyncClient, account, monkeypatch
    ) -> None:
        records: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(self.format(record))

        capture = _Capture(level=logging.DEBUG)
        capture.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        root = logging.getLogger()
        root.addHandler(capture)
        previous = root.level
        root.setLevel(logging.DEBUG)

        try:
            result = await _upload_locked(client, account)
            headers = auth_header(account["access_token"])
            url = f"/api/v1/statements/{result['statement_id']}/unlock"
            await client.post(url, headers=headers, json={"password": "WRONG123"})
            await client.post(url, headers=headers, json={"password": PASSWORD})
        finally:
            root.removeHandler(capture)
            root.setLevel(previous)

        stream = "\n".join(records)
        # Both the failure and the success path, since a wrong password is the
        # one most likely to be echoed back in an error.
        assert PASSWORD not in stream
        assert "WRONG123" not in stream

    async def test_it_does_not_reach_the_audit_trail(
        self, client: httpx.AsyncClient, account
    ) -> None:
        result = await _upload_locked(client, account)
        headers = auth_header(account["access_token"])
        url = f"/api/v1/statements/{result['statement_id']}/unlock"

        await client.post(url, headers=headers, json={"password": "WRONG123"})
        await client.post(url, headers=headers, json={"password": PASSWORD})

        logs = await client.get("/api/v1/audit/logs", headers=headers)
        body = logs.text
        assert PASSWORD not in body
        assert "WRONG123" not in body
        # The event is recorded even though the secret is not.
        assert "wrong_password" in body
