"""
offer_letters.py
Stores uploaded offer letters / agreements (PDF) and extracts their text so
the terms can be READ in the UI and confirmed by a human.

DELIBERATELY DOES NOT AUTO-PARSE SALARY NUMBERS.
It would be easy to regex a "Rs 10,000" out of a PDF and drop it straight into
payroll_config.json. We don't, because a misread clause becomes a wrong number
on a real person's payslip, and a confident-looking wrong salary is exactly the
failure this project has been hardened against all along. Instead: we store the
document as the audit-trail source, show its text, and a human sets the terms
explicitly (via the Payroll tab's config form -> payroll_config.json).

The extracted text is a READING AID, not an input to any calculation.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LETTERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "offer_letters")

MAX_PREVIEW_CHARS = 20000


def save_offer_letter(filename, file_bytes):
    """Store the uploaded document verbatim. Returns the stored path."""
    os.makedirs(LETTERS_DIR, exist_ok=True)
    safe_name = os.path.basename(filename or "offer_letter.pdf")
    path = os.path.join(LETTERS_DIR, safe_name)
    with open(path, "wb") as f:
        f.write(file_bytes)
    return path


def extract_text(path):
    """Best-effort text extraction. Returns (text, error_or_None).

    A PDF that is a scan (image-only) yields little or no text — we say so
    plainly rather than pretending the document was read.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, "pypdf is not installed — run: pip install pypdf"

    try:
        reader = PdfReader(path)
        chunks = []
        for i, page in enumerate(reader.pages):
            try:
                chunks.append(page.extract_text() or "")
            except Exception as e:
                chunks.append(f"[page {i+1}: could not extract — {e}]")
        text = "\n".join(chunks).strip()
    except Exception as e:
        return None, f"Could not read this PDF: {e}"

    if not text:
        return "", ("No selectable text found. This is most likely a scanned/image PDF — "
                    "the terms will need to be read by eye and entered manually below.")
    return text[:MAX_PREVIEW_CHARS], None


def list_offer_letters():
    """[{name, size_kb, uploaded_at}] newest first."""
    import time
    if not os.path.isdir(LETTERS_DIR):
        return []
    out = []
    for name in os.listdir(LETTERS_DIR):
        path = os.path.join(LETTERS_DIR, name)
        if not os.path.isfile(path):
            continue
        st = os.stat(path)
        out.append({
            "name": name,
            "size_kb": round(st.st_size / 1024, 1),
            "uploaded_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "mtime": st.st_mtime,
        })
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out
