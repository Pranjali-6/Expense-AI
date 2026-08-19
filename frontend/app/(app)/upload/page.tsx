import type { Metadata } from "next";
import { Lock, ScanLine, ShieldCheck } from "lucide-react";

import { PageHeader } from "@/components/shared/page-header";
import { UploadDropzone } from "@/components/upload/upload-dropzone";
import { Card, CardContent } from "@/components/ui/card";

export const metadata: Metadata = { title: "Upload statements" };

const GUARANTEES = [
  {
    icon: ScanLine,
    title: "Read deterministically",
    body: "Text and tables are extracted with PyMuPDF, pdfplumber and Camelot. OCR runs only when a statement is scanned. An AI model is never the extraction engine.",
  },
  {
    icon: Lock,
    title: "Encrypted at rest",
    body: "Your PDF goes straight to encrypted object storage under a per-tenant key. It is never written to a worker's disk beyond the task that reads it.",
  },
  {
    icon: ShieldCheck,
    title: "Reconciled before it counts",
    body: "Opening balance plus credits minus debits must equal the closing balance exactly. A statement that does not reconcile is flagged, not quietly trusted.",
  },
];

export default function UploadPage() {
  return (
    <>
      <PageHeader
        title="Upload statements"
        description="Drop in bank and credit-card statement PDFs. You will never type a transaction."
      />

      <UploadDropzone />

      <section className="mt-6 grid gap-4 sm:grid-cols-3">
        {GUARANTEES.map((item) => (
          <Card key={item.title}>
            <CardContent className="p-5">
              <item.icon className="size-4 text-primary-text" aria-hidden="true" />
              <h3 className="mt-3 text-sm font-semibold">{item.title}</h3>
              <p className="mt-1 text-sm text-muted">{item.body}</p>
            </CardContent>
          </Card>
        ))}
      </section>

    </>
  );
}
