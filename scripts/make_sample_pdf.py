"""Write samples/basic-safety-training-2027.pdf, a one-page course notice for
demoing the PDF course update (/admin/upload).

Against the seed data it raises the Basic Safety Training fee from 18,500
to 19,500 and adds a January 2027 batch. The PDF is written by hand (one
page of Helvetica text) so no PDF library is needed.

Usage: python scripts/make_sample_pdf.py
"""

from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "samples" / "basic-safety-training-2027.pdf"

# (font size, text); an empty string is a blank line.
LINES = [
    (20, "Kochi Maritime Academy"),
    (11, "Course notice - fees and schedule for 2027"),
    (11, ""),
    (16, "Basic Safety Training (STCW)"),
    (11, "Course code: BST"),
    (11, "Duration: 10 days"),
    (11, "Course fee: INR 19,500 per candidate (revised from 1 January 2027)"),
    (11, ""),
    (11, "Covers personal survival techniques, fire prevention and fire fighting,"),
    (11, "elementary first aid, and personal safety and social responsibility."),
    (11, ""),
    (14, "New batch"),
    (11, "Start date: 11 January 2027"),
    (11, "End date: 20 January 2027"),
    (11, "Location: Kochi"),
    (11, "Capacity: 30 seats"),
    (11, ""),
    (9, "Enquiries: admissions office, Kochi Maritime Academy."),
]

def escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

def content_stream() -> bytes:
    ops = ["BT", "72 770 Td"]
    for size, text in LINES:
        ops.append(f"/F1 {size} Tf")
        ops.append(f"0 -{round(size * 1.6)} Td")
        if text:
            ops.append(f"({escape(text)}) Tj")
    ops.append("ET")
    return "\n".join(ops).encode("latin-1")

def build_pdf() -> bytes:
    stream = content_stream()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    pdf += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, xref
    )
    return bytes(pdf)

if __name__ == "__main__":
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_bytes(build_pdf())
    print(f"Wrote {OUT}")
