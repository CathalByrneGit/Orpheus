"""Build the two-page PDF fixture the ingest tests run against.

    python3 data-raw/make_test_pdf.py tests/testthat/fixtures/services-agreement.pdf

Written by hand rather than exported from a word processor for three reasons:
the result is 2.4KB rather than 30KB, it is deterministic byte-for-byte so it
does not churn the diff, and the content stream is uncompressed, so anyone
wondering what the test is actually reading can open the file in a text editor.

The point of the fixture is that it is a *real* PDF -- pdftotext has to find the
text layer and the page break -- because ingest's pdftotext branch splits pages
on the form feed it emits, and nothing else in the suite exercises that.
"""
import sys

def esc(s):
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

def content(lines, leading=15, top=760, left=60, size=11):
    out = ["BT", "/F1 %d Tf" % size, "%d %d Td" % (left, top), "%d TL" % leading]
    for ln in lines:
        out.append("(%s) Tj T*" % esc(ln))
    out.append("ET")
    return "\n".join(out).encode("latin-1")

def build(pages, path):
    objs, body = [], b""
    n_pages = len(pages)
    # 1 catalog, 2 pages, 3 font, then per page: page obj + content obj
    page_ids = [4 + 2 * i for i in range(n_pages)]
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join("%d 0 R" % i for i in page_ids)
    objs.append(("<< /Type /Pages /Count %d /Kids [%s] >>" % (n_pages, kids)).encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                b"/Encoding /WinAnsiEncoding >>")
    for i, lines in enumerate(pages):
        stream = content(lines)
        objs.append(("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                     "/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>"
                     % (page_ids[i] + 1)).encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")

    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    open(path, "wb").write(out)
    return len(out)

PAGE1 = [
    "SERVICES AGREEMENT",
    "",
    "Reference: DOH-2026-0431",
    "",
    "This Agreement is made on 4 February 2026 between Ardmore Digital Limited",
    "(company registration number 612884), having its registered office in",
    'Dublin (the "Supplier"), and the Department of Health (the "Contracting',
    'Authority").',
    "",
    "1. Term",
    "",
    "This Agreement commences on 1 March 2026 and terminates on 28 February",
    "2029 unless extended in accordance with clause 7.",
    "",
    "2. Consideration",
    "",
    "The total contract value is EUR 1,480,000 exclusive of VAT. Payment shall",
    "be made within 30 days of receipt of a valid invoice.",
    "",
    "3. Award procedure",
    "",
    "This contract was awarded following an open procurement procedure",
    "advertised on eTenders on 12 November 2025.",
]
PAGE2 = [
    "4. Limitation of liability",
    "",
    "The Supplier's aggregate liability under this Agreement shall not exceed",
    "the total contract value, save that nothing in this clause limits",
    "liability for death or personal injury.",
    "",
    "5. Termination",
    "",
    "Either party may terminate this Agreement on 90 days written notice. The",
    "Contracting Authority may terminate immediately on a material breach.",
    "",
    "6. Governing law",
    "",
    "This Agreement is governed by the laws of Ireland.",
    "",
    "SIGNED for and on behalf of Ardmore Digital Limited",
    "",
    "SIGNED for and on behalf of the Department of Health",
]

if __name__ == "__main__":
    print(build([PAGE1, PAGE2], sys.argv[1]), "bytes")
