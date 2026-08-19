"""Fill the demo tenant by uploading statements through the real API.

    python -m tools.demo_seed                 six months, default demo login
    python -m tools.demo_seed --months 12

Deliberately the long way round. Inserting a few hundred transactions straight
into PostgreSQL would take a second and would demonstrate nothing: the demo
would show a ledger that never went through validation, reconciliation,
deduplication, movement classification or confidence scoring — the parts that
make the numbers mean anything. So the seed generates PDFs and posts them to
``/statements/upload`` like a person dragging files onto the page, and waits for
the pipeline to finish.

That makes it slow, and it makes it a genuine end-to-end test of the whole
stack. A demo that will not seed is a product that does not work.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

DEFAULT_BASE = "http://localhost/api/v1"
DEFAULT_EMAIL = "demo@expense-ai.dev"
DEFAULT_PASSWORD = "DemoPassword123!"

#: Generous: OCR-free statements parse in seconds, but a cold worker and a
#: first-run import of twelve files should not be called a failure.
POLL_TIMEOUT_SECONDS = 300


def _login(client: httpx.Client, email: str, password: str) -> str:
    response = client.post("/auth/login", json={"email": email, "password": password})
    if response.status_code != 200:
        raise SystemExit(
            f"Could not sign in as {email}. Run `make seed-demo` first.\n"
            f"  {response.status_code}: {response.text[:200]}"
        )
    return response.json()["access_token"]


def _wait_for_quiet(client: httpx.Client, headers: dict[str, str]) -> tuple[int, int]:
    """Block until nothing is processing. Returns (statements, transactions)."""
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        statements = client.get("/statements", headers=headers).json()
        busy = [
            item for item in statements
            if item["status"] in {"uploaded", "processing"}
        ]
        if not busy:
            total = sum(item["transaction_count"] for item in statements)
            return len(statements), total
        time.sleep(3)
    raise SystemExit("Timed out waiting for the pipeline. Check `make logs-worker`.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path("tests/fixtures/demo"),
        help="where the demo statements were generated",
    )
    args = parser.parse_args()

    pdfs = sorted(args.directory.glob("demo-*.pdf"))
    if not pdfs:
        raise SystemExit(
            f"No demo statements in {args.directory}. Run:\n"
            f"  python -m tools.statement_generator --demo "
            f"--months {args.months} --output {args.directory}"
        )

    with httpx.Client(base_url=args.base_url, timeout=120.0) as client:
        token = _login(client, args.email, args.password)
        headers = {"Authorization": f"Bearer {token}"}

        print(f"  uploading {len(pdfs)} statements as {args.email}")
        for index in range(0, len(pdfs), 4):
            batch = pdfs[index : index + 4]
            files = [
                ("files", (path.name, path.read_bytes(), "application/pdf"))
                for path in batch
            ]
            response = client.post("/statements/upload", headers=headers, files=files)
            if response.status_code not in {200, 202}:
                raise SystemExit(
                    f"Upload refused: {response.status_code} {response.text[:300]}"
                )
            body = response.json()
            for item in body["results"]:
                mark = "ok" if item["accepted"] else f"refused ({item['error_code']})"
                print(f"    {item['filename']:26} {mark}")

        statements, transactions = _wait_for_quiet(client, headers)

    print(f"\n  {statements} statements · {transactions} transactions in the ledger")
    print("  http://localhost/dashboard\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
