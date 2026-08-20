"""Ground-truth builder for the real corpus (P4.5, piece 2).

    python -m tools.corpus.groundtruth review tests/fixtures/real/axis-2025.pdf
    python -m tools.corpus.groundtruth adopt  axis-2025.review.json

`review` parses the statement, renders each page, and serves a review page on
**127.0.0.1 only**: the statement image on the left, the parser's proposed rows
on the right. You confirm or correct each row; Save writes
``<fixture>.expected.json`` beside the PDF.

The parser's output is a proposal, never an answer — every row starts
unconfirmed, and nothing is written until you have been through all of them.
That is the slow part, and it is the whole point: a fixture built by asking the
parser whether the parser was right measures nothing.

`adopt` is the offline path. If you opened the page from a file:// URL there is
no server to POST to, so the page downloads the raw review instead and this
turns it into expected.json.

Nothing here touches the network. The corpus is gitignored and stays local.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from tools.corpus.fixtures import FixtureLocked
from tools.corpus.groundtruth import emit, proposal

TEMPLATE = Path(__file__).with_name("ui.html")

#: Bound inside the container, so it has to be 0.0.0.0 to be reachable through
#: a published port at all — binding loopback here would only accept
#: connections from inside the container itself. Confinement is done where it
#: actually works, at the publish: `-p 127.0.0.1:8901:8901` puts it on the
#: host's loopback and nowhere else. This page embeds images of a real bank
#: statement, so that mapping is not optional. Override with --host when
#: running outside a container, where "127.0.0.1" is the right bind.
DEFAULT_HOST = "0.0.0.0"  # noqa: S104
DEFAULT_PORT = 8901


def render_page(prop: proposal.Proposal) -> bytes:
    html = TEMPLATE.read_text()
    # json.dumps twice would escape it; substituted as a literal so the page
    # needs no second fetch and works just as well from file://.
    return html.replace("__PROPOSAL__", json.dumps(prop.to_json())).encode("utf-8")


def finish(review: dict[str, Any], pdf_path: Path) -> tuple[Path, list[str]]:
    """Validate a review and write expected.json beside the PDF."""
    rows = review["rows"]
    metadata = review["metadata"]
    warnings = emit.validate(rows, metadata)
    payload = emit.build(
        rows,
        metadata,
        fixture=review["fixture"],
        page_count=review["page_count"],
    )
    destination = pdf_path.with_name(f"{review['fixture']}.expected.json")
    return emit.write(payload, destination), warnings


def serve(pdf_path: Path, host: str, port: int) -> int:
    print(f"  Parsing {pdf_path.name} …")
    try:
        prop = proposal.build(pdf_path)
    except FixtureLocked as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 1

    document = render_page(prop)
    print(f"  {prop.parser_note}")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/", "/index.html"):
                self._send(200, document, "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/save":
                self._send(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                review = json.loads(self.rfile.read(length))
                written, warnings = finish(review, pdf_path)
            except emit.ReviewIncomplete as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                return
            except Exception as exc:  # noqa: BLE001
                self._send(
                    500,
                    json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(),
                    "application/json",
                )
                return
            print(f"  wrote {written}")
            for warning in warnings:
                print(f"    warning: {warning}")
            self._send(
                200,
                json.dumps({"path": str(written), "warnings": warnings}).encode(),
                "application/json",
            )

        def log_message(self, *args: Any) -> None:
            """Silence per-request logging; the statement path is not log material."""

    server = HTTPServer((host, port), Handler)
    print(f"\n  Review {prop.fixture} at  http://127.0.0.1:{port}/")
    print("  Confirm every row, then Save. Ctrl-C when done.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped\n")
    finally:
        server.server_close()
    return 0


def adopt(review_path: Path, corpus: Path) -> int:
    review = json.loads(review_path.read_text())
    pdf_candidates = sorted(corpus.glob(f"{review['fixture']}.pdf"))
    if not pdf_candidates:
        print(f"\n  No {review['fixture']}.pdf in {corpus}\n", file=sys.stderr)
        return 1
    try:
        written, warnings = finish(review, pdf_candidates[0])
    except emit.ReviewIncomplete as exc:
        print(f"\n  Review is not complete: {exc}\n", file=sys.stderr)
        return 1
    print(f"\n  wrote {written}")
    for warning in warnings:
        print(f"    warning: {warning}")
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="tools.corpus.groundtruth", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="open the review UI for one statement")
    review.add_argument("pdf", type=Path)
    review.add_argument("--port", type=int, default=DEFAULT_PORT)
    review.add_argument("--host", default=DEFAULT_HOST)

    take = sub.add_parser("adopt", help="turn a downloaded review into expected.json")
    take.add_argument("review", type=Path)
    take.add_argument("--corpus", type=Path, default=Path("/app/tests/fixtures/real"))

    args = parser.parse_args()

    if args.command == "review":
        if not args.pdf.exists():
            print(f"\n  No such file: {args.pdf}\n", file=sys.stderr)
            return 1
        return serve(args.pdf, args.host, args.port)
    return adopt(args.review, args.corpus)


if __name__ == "__main__":
    raise SystemExit(main())
