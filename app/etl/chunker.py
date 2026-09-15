"""ETL – text chunking (table-aware).

Two entry points:

* ``chunk_structured(text)`` — preferred. Detects HTML / Markdown / CSV tables and
  yields *row-group chunks* (header + one or more complete rows) with an optional
  narrative prefix from the text immediately preceding the table.
* ``chunk_text(text)`` — kept as-is, plain overlapping window (no table awareness).
  Used internally as the fallback when no table is detected.

Rationale (E8/01 spec §2.1)
--------------------------
Carving documents by character offset alone tears table rows apart and loses the
header context, so the retriever returns isolated data cells without column
semantics. Table-aware chunking keeps each ``<tr>`` / MD ``|...|`` line intact
and prefixes it with the header row and a short narrative sentence.
"""
from __future__ import annotations

import re
from typing import Iterator

_CHUNK = 600
_OVERLAP = 100

# ── helpers (kept from original) ────────────────────────────────────────────

def chunk_text(text: str, chunk_size: int = _CHUNK, overlap: int = _OVERLAP) -> list[str]:
    """Plain overlapping window — sentence-aware at boundaries. Unchanged."""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= chunk_size:
        return [text] if text else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        window = text[start:end]
        if end < len(text):
            for sep in ["\n\n", "。", ". ", "\n", " "]:
                idx = window.rfind(sep)
                if idx > chunk_size * 0.5:
                    end = start + idx + len(sep)
                    window = text[start:end]
                    break
        chunks.append(window.strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


# ── table detection ───────────────────────────────────────────────────────

# Markdown: at least one header separator line |---|---|
_MD_SEP_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")
_MD_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_HTML_TABLE_RE = re.compile(r"<table[\s>].*?</table>", re.DOTALL | re.IGNORECASE)
_HTML_TR_RE = re.compile(r"<tr[\s>].*?</tr>", re.DOTALL | re.IGNORECASE)
_HTML_TD_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE)


def _is_markdown_table(block: list[str]) -> bool:
    """A block of consecutive MD rows qualifies as a table if it contains a separator."""
    rows = [ln for ln in block if _MD_ROW_RE.match(ln)]
    if len(rows) < 2:
        return False
    return any(_MD_SEP_RE.match(ln) for ln in block)


def _strip_html(html: str) -> str:
    """Crude but dependency-free text extraction from an HTML fragment."""
    return re.sub(r"<[^>]+>", " ", html)


def _narrative_prefix(text_before: str, max_chars: int = 120) -> str:
    """Pull the last sentence (or partial) before a table — used as semantic prefix."""
    text_before = text_before.strip()
    if not text_before:
        return ""
    # last paragraph, last 120 chars
    tail = text_before.split("\n\n")[-1].strip()
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    # try to end at a sentence boundary
    for sep in ["。", ". ", "\n"]:
        idx = tail.rfind(sep)
        if idx > max_chars * 0.3:
            return tail[idx + len(sep):].strip() or tail
    return tail


def _chunk_rows(rows: list[str], header: str, prefix: str,
               chunk_size: int = _CHUNK) -> list[str]:
    """Pack *rows* (all HTML <tr> or MD |..| lines) into chunk_size windows."""
    if not rows:
        return []
    chunks: list[str] = []
    buf: list[str] = []
    head = f"{prefix}\n{prefix and prefix + chr(10)}{header}".strip()
    header_len = len(head) + 1
    for r in rows:
        if (header_len + sum(len(x) + 1 for x in buf) + len(r) > chunk_size) and buf:
            chunks.append(head + "\n" + "\n".join(buf))
            buf = []
        buf.append(r.strip())
    if buf:
        chunks.append(head + "\n" + "\n".join(buf))
    return chunks


def _extract_html_table_chunks(html_table: str, text_before: str,
                               chunk_size: int = _CHUNK) -> list[str]:
    """HTML <table> → row-group chunks with header context."""
    trs = _HTML_TR_RE.findall(html_table)
    if not trs:
        return []
    # first row = header (assume <th> or first <tr>)
    header_cells = _HTML_TD_RE.findall(trs[0])
    if not header_cells:
        return []
    header_text = _strip_html(" | ".join(header_cells))
    prefix = _narrative_prefix(text_before)

    body_rows: list[str] = []
    for tr in trs[1:]:
        cells = _HTML_TD_RE.findall(tr)
        body_rows.append(_strip_html(" | ".join(cells)))

    return _chunk_rows(body_rows, header_text, prefix, chunk_size)


def _extract_md_table_chunks(md_lines: list[str], text_before: str,
                             chunk_size: int = _CHUNK) -> list[str]:
    """Markdown table lines → row-group chunks."""
    # locate header (first row) + separator (second row)
    header_line = md_lines[0]
    body_rows = [ln for ln in md_lines[2:] if _MD_ROW_RE.match(ln) and not _MD_SEP_RE.match(ln)]
    header_text = _strip_md_row(header_line)
    prefix = _narrative_prefix(text_before)
    return _chunk_rows(list(body_rows), header_text, prefix, chunk_size)


def _strip_md_row(line: str) -> str:
    """' | a | b | c ' → 'a | b | c' (strip outer pipes and whitespace)."""
    inner = line.strip().strip("|").strip()
    return " | ".join(p.strip() for p in inner.split("|") if p.strip())


def _extract_csv_chunks(lines: list[str], text_before: str,
                        chunk_size: int = _CHUNK) -> list[str]:
    """CSV block (first line + N lines with the same comma count) → row-group chunks."""
    if len(lines) < 2:
        return []
    parts = [p.strip() for p in lines[0].split(",")]
    n_cols = len(parts)
    if n_cols < 2:
        return []
    header_text = " | ".join(parts)
    prefix = _narrative_prefix(text_before)
    body_rows = []
    for ln in lines[1:]:
        cells = ln.split(",")
        if len(cells) != n_cols:
            break  # misaligned → end of table
        body_rows.append(" | ".join(c.strip() for c in cells))
    return _chunk_rows(body_rows, header_text, prefix, chunk_size)


# ── public entry point ──────────────────────────────────────────────────────

def _classify_block(lines: list[str]) -> str:
    """Classify a contiguous block of non-empty lines: 'md_table' | 'csv' | 'plain'."""
    if len(lines) >= 2 and _is_markdown_table(lines):
        return "md_table"
    if len(lines) >= 2:
        first_commas = lines[0].count(",")
        if first_commas >= 1 and all(ln.count(",") == first_commas for ln in lines[1:]):
            return "csv"
    return "plain"


def _find_md_table_boundaries(text: str) -> list[tuple[int, int]]:
    """Return ``[(start_line, end_line_exclusive), ...]`` for every MD / CSV table
    found in the document. Uses a state machine over lines so adjacent tables don't
    merge and tables surrounded by blank lines are still detected.
    """
    lines = text.split("\n")
    boundaries: list[tuple[int, int]] = []
    i = 0
    n = len(lines)
    while i < n:
        # MD table: line starting with | followed by separator line
        if _MD_ROW_RE.match(lines[i]):
            j = i + 1
            found_sep = False
            while j < n and _MD_ROW_RE.match(lines[j]):
                if _MD_SEP_RE.match(lines[j]):
                    found_sep = True
                j += 1
            if found_sep:
                boundaries.append((i, j))
                i = j
                continue
        # CSV table: 3+ consecutive lines with same comma count >=1
        commas = lines[i].count(",")
        if commas >= 1:
            j = i + 1
            while j < n and lines[j].count(",") == commas:
                j += 1
            if j - i >= 3:
                boundaries.append((i, j))
                i = j
                continue
        i += 1
    return boundaries


def chunk_structured(text: str, chunk_size: int = _CHUNK, overlap: int = _OVERLAP) -> list[str]:
    """Table-aware chunking entry point.

    Strategy (E8/01 spec §2.1):
        1. Split text into *regions*: HTML tables are pulled out first, then MD/CSV
           tables are detected in the remaining regions.
        2. Table regions → row-group chunks (header + rows, with narrative prefix).
        3. Non-table regions → original ``chunk_text`` sliding window.
        4. Malformed tables (no body, single column, broken separators) silently
           degrade to plain-text sliding windows — no data loss.

    Returns ``list[str]`` in document order.
    """
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text.strip():
        return []
    if len(text) <= chunk_size and "|" not in text and "<table" not in text.lower():
        return [text] if text else []

    # ── 1. Pull out HTML tables (may span multiple lines) ─────────────────
    html_tables = list(_HTML_TABLE_RE.finditer(text))
    chunks: list[str] = []
    cursor = 0  # text offset already processed

    for html_match in html_tables:
        # plain text before this table
        pre_text = text[cursor:html_match.start()]
        if pre_text.strip():
            chunks.extend(_chunk_region_with_tables(pre_text, chunk_size, overlap))
        # process the HTML table
        html_blob = html_match.group(0)
        text_before = text[: html_match.start()]
        try:
            tbl_chunks = _extract_html_table_chunks(html_blob, text_before, chunk_size)
        except Exception:
            tbl_chunks = []
        if tbl_chunks:
            chunks.extend(tbl_chunks)
        else:
            chunks.extend(chunk_text(html_blob, chunk_size, overlap))
        cursor = html_match.end()

    # remaining text after last HTML table
    if cursor < len(text):
        remaining = text[cursor:]
        if remaining.strip():
            chunks.extend(_chunk_region_with_tables(remaining, chunk_size, overlap))

    return [c for c in chunks if c.strip()]


def _chunk_region_with_tables(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Handle a non-HTML region: split by MD/CSV tables and route each sub-region."""
    boundaries = _find_md_table_boundaries(text)
    lines = text.split("\n")
    chunks: list[str] = []
    consumed = 0  # line index

    for (start, end) in boundaries:
        # plain text before the table
        if start > consumed:
            pre_text = "\n".join(lines[consumed:start])
            if pre_text.strip():
                chunks.extend(chunk_text(pre_text, chunk_size, overlap))
        # the table
        tbl_text = "\n".join(lines[start:end])
        try:
            tbl_chunks = _extract_md_table_chunks(lines[start:end], "\n".join(lines[:start]),
                                                 chunk_size)
        except Exception:
            tbl_chunks = []
        if tbl_chunks:
            chunks.extend(tbl_chunks)
        else:
            chunks.extend(chunk_text(tbl_text, chunk_size, overlap))
        consumed = end

    # remaining text after last table
    if consumed < len(lines):
        tail = "\n".join(lines[consumed:])
        if tail.strip():
            chunks.extend(chunk_text(tail, chunk_size, overlap))
    return chunks
