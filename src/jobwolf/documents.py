"""Bounded, local document-to-text preview. Originals are never persisted.

The short-lived worker limits parsing time and library resource use, but is not an
OS security sandbox. It receives private bytes through stdin, not filenames/argv.
"""

import base64
import binascii
import io
import json
import logging
import re
import subprocess
import sys
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 60000
MAX_LINES = 2000
MAX_EXPANDED_BYTES = 8 * 1024 * 1024
PARSE_TIMEOUT = 15


class DocumentError(Exception):
    """Safe to display; never includes document contents or parser diagnostics."""


def display_filename(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f:\ud800-\udfff]", "_", name).strip()[:200]
    return name or "resume.txt"


def checked_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise DocumentError("No readable text was found. Scanned PDFs need OCR elsewhere; paste the text instead.")
    if len(text) > MAX_TEXT_CHARS or len(text.split("\n")) > MAX_LINES:
        raise DocumentError("The extracted text exceeds 60,000 characters or 2,000 lines. Use a shorter document or paste an excerpt.")
    if any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise DocumentError("This file contains unsupported control characters. Use a text document or paste a readable excerpt.")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise DocumentError("The text contains unsupported characters. Correct or paste a readable excerpt.") from None
    return text


def _pdf(data: bytes) -> tuple[str, list[str]]:
    from pypdf import PdfReader, apply_configuration

    with apply_configuration(
        maximum_declared_stream_length=MAX_EXPANDED_BYTES,
        array_based_stream_maximum_output_length=MAX_EXPANDED_BYTES,
        zlib_maximum_output_length=MAX_EXPANDED_BYTES,
        lzw_maximum_output_length=MAX_EXPANDED_BYTES,
        run_length_maximum_output_length=MAX_EXPANDED_BYTES,
        brotli_maximum_output_length=MAX_EXPANDED_BYTES,
        jbig2_maximum_output_length=MAX_EXPANDED_BYTES,
        image_maximum_buffer_size=MAX_EXPANDED_BYTES,
        stream_decoding_work_maximum_length=MAX_EXPANDED_BYTES,
        stream_parsing_work_maximum_count=100000,
        array_maximum_element_count=20000,
        dictionary_maximum_element_count=20000,
        page_tree_maximum_entries=1000,
        page_tree_maximum_depth=30,
        xform_maximum_invocations_per_extraction=100,
        jbig2dec_binary=None,
    ):
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise DocumentError("Password-protected PDFs are not supported. Export an unencrypted text-based PDF or paste its text.")
        if not 1 <= len(reader.pages) <= 20:
            raise DocumentError("Use a PDF with 1–20 pages, or paste the relevant excerpt.")
        parts = []
        empty_pages = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            empty_pages += not bool(text.strip())
            parts.append(text)
            if sum(len(part) for part in parts) > MAX_TEXT_CHARS:
                raise DocumentError("The PDF has too much text. Use a shorter document or paste an excerpt.")
        warnings = ["PDF reading order can be imperfect, especially with columns. Review and correct the text before saving."]
        if empty_pages:
            warnings.append("Some pages have no readable text. Images and scanned content were not transcribed.")
        return "\n\n".join(parts), warnings


def _word_xml(data: bytes) -> list[str]:
    # DOCX parts normally use UTF-8. Refuse alternate encodings rather than allow
    # declarations/entities to bypass the check through a different byte encoding.
    xml = data.decode("utf-8-sig")
    if "\x00" in xml or "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise DocumentError("This DOCX contains unsupported XML declarations. Export a PDF or paste the text.")
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs = []
    depth = count = 0
    for event, element in ElementTree.iterparse(io.StringIO(xml), events=("start", "end")):
        if event == "start":
            count += 1
            depth += 1
            if count > 100000 or depth > 100:
                raise DocumentError("This DOCX is too complex. Export a simple PDF or paste its text.")
        else:
            depth -= 1
            if element.tag == namespace + "p":
                pieces = []
                for child in element.iter():
                    if child.tag == namespace + "t":
                        pieces.append(child.text or "")
                    elif child.tag == namespace + "tab":
                        pieces.append("\t")
                    elif child.tag in (namespace + "br", namespace + "cr"):
                        pieces.append("\n")
                paragraphs.append("".join(pieces))
                element.clear()
    return paragraphs


def _docx(data: bytes) -> tuple[str, list[str]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(entries) > 300 or len(set(names)) != len(names) or sum(e.file_size for e in entries) > MAX_EXPANDED_BYTES:
            raise DocumentError("This DOCX exceeds the archive limits. Export a simpler document or paste its text.")
        if any(e.flag_bits & 1 for e in entries) or any("vbaproject" in name.lower() for name in names):
            raise DocumentError("Encrypted or macro-containing documents are not supported. Use PDF, DOCX without macros, or pasted text.")
        if "word/document.xml" not in names or "[Content_Types].xml" not in names:
            raise DocumentError("This is not a supported DOCX file. Use a document saved in DOCX format.")
        selected = sorted(n for n in names if re.fullmatch(r"word/header\d+\.xml", n))
        selected += ["word/document.xml"]
        selected += sorted(n for n in names if re.fullmatch(r"word/footer\d+\.xml", n))
        paragraphs = []
        for name in selected:
            with archive.open(name) as part:
                xml = part.read(MAX_EXPANDED_BYTES + 1)
            if len(xml) > MAX_EXPANDED_BYTES:
                raise DocumentError("This DOCX expands beyond the parsing limit. Paste a text excerpt instead.")
            paragraphs.extend(_word_xml(xml))
        return "\n".join(paragraphs), ["Layout, images, and tracked changes may not be represented faithfully. Check columns, dates, and table text before saving."]


def extract_text(data: bytes, format: str) -> dict:
    if not 0 < len(data) <= MAX_FILE_BYTES:
        raise DocumentError("Choose a nonempty file no larger than 2 MiB.")
    if format == "pdf":
        text, warnings = _pdf(data)
    elif format == "docx":
        text, warnings = _docx(data)
    elif format in ("txt", "md"):
        encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        text, warnings = data.decode(encoding), []
    else:
        raise DocumentError("Use a text-based PDF, DOCX, TXT, or Markdown file. Legacy DOC and images are not supported; paste text instead.")
    return {"format": format, "text": checked_text(text), "warnings": warnings}


def preview_document(filename: str, content_base64: str) -> dict:
    name = display_filename(filename)
    format = PurePosixPath(name).suffix.lower().lstrip(".")
    if format not in ("pdf", "docx", "txt", "md"):
        raise DocumentError("Use a text-based PDF, DOCX, TXT, or Markdown file. Legacy DOC and images are not supported; paste text instead.")
    if len(content_base64) > 4 * ((MAX_FILE_BYTES + 2) // 3):
        raise DocumentError("Choose a file no larger than 2 MiB.")
    try:
        data = base64.b64decode(content_base64, validate=True)
    except (ValueError, binascii.Error):
        raise DocumentError("The file transfer was invalid. Choose the file again; nothing was saved.") from None
    if not 0 < len(data) <= MAX_FILE_BYTES:
        raise DocumentError("Choose a nonempty file no larger than 2 MiB.")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "jobwolf.documents", "--worker", format],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=PARSE_TIMEOUT, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise DocumentError("Document parsing took too long. Try a simpler file or paste the text. Nothing was saved.") from None
    except OSError:
        raise DocumentError("The local document parser could not start. Check the installation or paste the text.") from None
    try:
        output = json.loads(result.stdout) if result.returncode == 0 and len(result.stdout) <= 1024 * 1024 else None
        if not isinstance(output, dict):
            raise TypeError
        if "error" in output:
            raise DocumentError(output["error"])
        output["text"] = checked_text(output["text"])
        if output["format"] != format or not isinstance(output["warnings"], list):
            raise ValueError
    except (ValueError, KeyError, UnicodeError, TypeError):
        raise DocumentError("This document could not be read safely. Try a text-based PDF, DOCX, or pasted text.") from None
    return {"filename": name, **output}


def _worker() -> None:
    logging.disable(logging.CRITICAL)
    try:
        if len(sys.argv) != 3 or sys.argv[1] != "--worker":
            raise DocumentError("Unsupported parser invocation.")
        result = extract_text(sys.stdin.buffer.read(MAX_FILE_BYTES + 1), sys.argv[2])
    except DocumentError as error:
        result = {"error": str(error)}
    except Exception:  # noqa: BLE001 -- Never expose private parser diagnostics.
        result = {"error": "This document could not be read safely. Try a simpler text-based PDF, DOCX, or pasted text."}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    _worker()
