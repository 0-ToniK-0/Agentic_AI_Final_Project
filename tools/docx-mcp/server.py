"""Word documents MCP server for the requirements-to-delivery assistant.

A thin layer over docx-mcp-server 0.7.4 (https://github.com/SecurityRonin/docx-mcp),
which edits the .docx XML directly, so no Microsoft Word is needed.

What this layer adds:
- A small tool set (26 instead of 219) where every edit is a tracked change
  signed by DOCX_AUTHOR. Nothing can be removed without leaving a red strikethrough.
- Works on documents saved by Word 2007 or by non-Word tools: paragraphs without a
  w14:paraId get one when the document is opened (Word does the same when it re-saves).
- Inserted text keeps the formatting of the text it replaces or of its paragraph.
  docx-mcp 0.7.4 writes it without run properties, so it falls back to the document
  default font.
- Short repeated values ("3", "Vendor CR Number") can be edited. docx-mcp refuses a
  text edit when the text also appears in another paragraph, even with a para_id.
- New table rows copy an existing row (widths, merged cells, shading, fonts).
- New paragraphs and paragraph deletions are tracked (docx-mcp does both untracked).
- Documents created from a .dotx get the .docx content type, so Word opens them.

Run:  python server.py            (stdio transport)
Env:  DOCX_AUTHOR  name on tracked changes and comments (default "AI Assistant")
"""

import copy
import logging
import os
import shutil
import zipfile
import zlib
from pathlib import Path

from lxml import etree
from mcp.server.fastmcp import FastMCP

import docx_mcp.document.tracks as _tracks
import docx_mcp.server as base
from docx_mcp.document import DocxDocument

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
W14 = "{%s}" % W14_NS
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

AUTHOR = os.getenv("DOCX_AUTHOR", "AI Assistant")
DOCUMENT = "word/document.xml"

# The para_id already names the paragraph, so the document-wide uniqueness check
# in docx-mcp only blocks valid edits of short values that repeat in forms.
_tracks._doc_norm_count = lambda *args, **kwargs: 1

# Importing docx_mcp.server sets mcp logging to INFO (one stderr line per request)
logging.getLogger("mcp").setLevel(logging.WARNING)

mcp = FastMCP(
    "word_documents",
    instructions=(
        "Edits Word (.docx) documents with tracked changes, so a reviewer can accept or reject "
        "every change in Word. Workflow: open_document (or create_from_template for a new "
        "document) -> get_paragraphs to find the para_id or table cell to change -> edit -> "
        "add_comment to explain non-obvious changes -> save_document -> generate_change_summary. "
        "Every edit is tracked. The one exception is fill_template, which only works on a new "
        "document from create_from_template."
    ),
)

_state = {"doc": None, "from_template": False, "old_ids": set()}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _doc() -> DocxDocument:
    if _state["doc"] is None:
        raise RuntimeError("No document is open. Call open_document or create_from_template first.")
    return _state["doc"]


def _is_paragraph_part(name: str) -> bool:
    return name in (DOCUMENT, "word/footnotes.xml", "word/endnotes.xml") or (
        name.startswith(("word/header", "word/footer")) and name.endswith(".xml")
    )


def _ensure_para_ids(doc: DocxDocument) -> int:
    """Give every paragraph a w14:paraId (all edit tools find paragraphs by it).

    The ids come from the part name and paragraph position, so the same file gets the
    same ids in every session even before it is saved.
    """
    used = {p.get(f"{W14}paraId") for root in doc._trees.values() for p in root.iter(f"{W}p")}
    added = 0
    for name in sorted(doc._trees):
        root = doc._trees[name]
        if not _is_paragraph_part(name) or all(p.get(f"{W14}paraId") for p in root.iter(f"{W}p")):
            continue
        if W14_NS not in root.nsmap.values():
            nsmap = {**root.nsmap, "w14": W14_NS, "mc": root.nsmap.get("mc", MC_NS)}
            new_root = etree.Element(root.tag, attrib=dict(root.attrib), nsmap=nsmap)
            new_root.extend(list(root))
            doc._trees[name] = root = new_root
        ignorable = root.get(f"{{{MC_NS}}}Ignorable", "").split()
        if "w14" not in ignorable:
            root.set(f"{{{MC_NS}}}Ignorable", " ".join(ignorable + ["w14"]))
        for i, p in enumerate(root.iter(f"{W}p")):
            if not p.get(f"{W14}paraId"):
                seed = zlib.crc32(f"{name}:{i}".encode())
                while (pid := f"{(seed & 0x7FFFFFFF) or 1:08X}") in used:
                    seed += 1
                used.add(pid)
                p.set(f"{W14}paraId", pid)
                p.set(f"{W14}textId", "77777777")
                added += 1
        doc._mark(name)
    return added


def _markup_ids(doc: DocxDocument) -> set[str]:
    root = doc._require(DOCUMENT)
    return {el.get(f"{W}id") for tag in ("ins", "del") for el in root.iter(f"{W}{tag}")}


def _clean_rpr(rpr):
    """Copy of a run-properties element without revision markers, or None."""
    if rpr is None:
        return None
    rpr = copy.deepcopy(rpr)
    for tag in ("ins", "del", "moveFrom", "moveTo", "rPrChange"):
        for el in rpr.findall(f"{W}{tag}"):
            rpr.remove(el)
    return rpr if len(rpr) else None


def _paragraph(el):
    while el is not None and el.tag != f"{W}p":
        el = el.getparent()
    return el


def _formatting_for(ins):
    """Run properties a new insertion should carry: the text it replaces, else its paragraph mark, else a nearby run."""
    prev = ins.getprevious()
    if prev is not None and prev.tag == f"{W}del":
        rpr = _clean_rpr(prev.find(f"{W}r/{W}rPr"))
        if rpr is not None:
            return rpr
    para = _paragraph(ins)
    if para is None:
        return None
    rpr = _clean_rpr(para.find(f"{W}pPr/{W}rPr"))
    if rpr is not None:
        return rpr
    for r in para.iter(f"{W}r"):
        rpr = _clean_rpr(r.find(f"{W}rPr"))
        if rpr is not None:
            return rpr
    return None


def _repair_formatting(doc: DocxDocument) -> None:
    """Give runs inserted in this session the formatting of the text around them."""
    root = doc._require(DOCUMENT)
    for ins in root.iter(f"{W}ins"):
        if ins.get(f"{W}id") in _state["old_ids"] or ins.getparent().tag in (f"{W}rPr", f"{W}trPr"):
            continue
        for r in ins.findall(f"{W}r"):
            if r.find(f"{W}rPr") is None:
                rpr = _formatting_for(ins)
                if rpr is not None:
                    r.insert(0, rpr)
                    doc._mark(DOCUMENT)


def _new_markup(tag: str, cid: int):
    el = etree.Element(f"{W}{tag}")
    el.set(f"{W}id", str(cid))
    el.set(f"{W}author", AUTHOR)
    el.set(f"{W}date", _tracks._now_iso())
    return el


def _mark_paragraph(ppr, marker) -> None:
    """Put an ins/del marker on the paragraph mark (pPr/rPr), keeping schema order."""
    rpr = ppr.find(f"{W}rPr")
    if rpr is None:
        rpr = etree.Element(f"{W}rPr")
        tail = ppr.find(f"{W}sectPr")
        if tail is None:
            tail = ppr.find(f"{W}pPrChange")
        tail.addprevious(rpr) if tail is not None else ppr.append(rpr)
    for tag in ("ins", "del"):
        for el in rpr.findall(f"{W}{tag}"):
            rpr.remove(el)
    rpr.insert(0, marker)


def _text_run(text: str, rpr) -> etree._Element:
    r = etree.Element(f"{W}r")
    if rpr is not None:
        r.append(rpr)
    t = etree.SubElement(r, f"{W}t")
    t.text = text
    t.set(XML_SPACE, "preserve")
    return r


def _edit(result: str) -> str:
    _repair_formatting(_doc())
    return result


def _location(root, p) -> str:
    cell = row = tbl = None
    el = p.getparent()
    while el is not None:
        if el.tag == f"{W}tc" and cell is None:
            cell = el
        elif el.tag == f"{W}tr" and row is None:
            row = el
        elif el.tag == f"{W}tbl" and tbl is None:
            tbl = el
        elif el.tag == f"{W}txbxContent":
            return "text box"
        el = el.getparent()
    if tbl is None:
        return "body"
    t_idx = list(root.iter(f"{W}tbl")).index(tbl)
    return f"table {t_idx}, row {tbl.findall(f'{W}tr').index(row)}, cell {row.findall(f'{W}tc').index(cell)}"


# ── Opening and saving ───────────────────────────────────────────────────────


def _load(path: str, from_template: bool) -> str:
    if _state["doc"] is not None:
        _state["doc"].close()
    doc = DocxDocument(path)
    doc.open()
    ids_added = _ensure_para_ids(doc)
    _state.update(doc=doc, from_template=from_template, old_ids=_markup_ids(doc))
    base._docs[base._DEFAULT_HANDLE] = doc
    info = doc.get_info()
    info = info if isinstance(info, dict) else {"info": info}
    info["para_ids_added"] = ids_added
    return base._js(info)


@mcp.tool()
def open_document(path: str) -> str:
    """Open an existing .docx for reading and tracked editing.

    Args:
        path: Absolute path to the .docx file.
    """
    return _load(path, from_template=False)


@mcp.tool()
def create_from_template(template_path: str, output_path: str) -> str:
    """Create a new document as a copy of a template (.docx or .dotx) and open it.

    The template file itself is never changed.

    Args:
        template_path: Absolute path to the template (.docx or .dotx).
        output_path: Absolute path for the new .docx. Must not exist yet.
    """
    src, out = Path(template_path), Path(output_path)
    if not src.exists():
        raise FileNotFoundError(f"Template not found: {src}")
    if out.exists():
        raise FileExistsError(f"{out} already exists; choose a new output_path")
    if out.suffix.lower() != ".docx":
        raise ValueError("output_path must end with .docx")
    shutil.copy2(src, out)
    if src.suffix.lower() in (".dotx", ".dotm"):
        _template_to_document(out)
    return _load(str(out), from_template=True)


def _template_to_document(path: Path) -> None:
    """Switch the main part's content type from template to document so Word opens the .docx."""
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "[Content_Types].xml":
                data = data.replace(b"wordprocessingml.template.main+xml", b"wordprocessingml.document.main+xml")
                data = data.replace(b"ms-word.template.macroEnabledTemplate.main+xml", b"wordprocessingml.document.main+xml")
            zout.writestr(info, data)
    tmp.replace(path)


@mcp.tool()
def save_document(output_path: str = "") -> str:
    """Save the open document.

    Args:
        output_path: Absolute path to save to. Empty saves over the opened or created file.
    """
    doc = _doc()
    return base._js(doc.save(output_path or None, backup=False))


@mcp.tool()
def close_document() -> str:
    """Close the open document without saving."""
    if _state["doc"] is not None:
        _state["doc"].close()
        _state["doc"] = None
        base._docs.pop(base._DEFAULT_HANDLE, None)
    return "Closed."


# ── Reading ──────────────────────────────────────────────────────────────────


@mcp.tool()
def get_paragraphs(contains: str = "", include_empty_cells: bool = True) -> str:
    """List paragraphs with their para_id, text and location (body, text box, or table/row/cell).

    Use this to find what to edit; most documents and forms have no headings.

    Args:
        contains: Only paragraphs whose text contains this (case-insensitive). Empty = all.
        include_empty_cells: Also list empty paragraphs inside table cells (fields to fill in).
    """
    root = _doc()._require(DOCUMENT)
    out = []
    for p in root.iter(f"{W}p"):
        if any(a.tag == f"{{{MC_NS}}}Fallback" for a in p.iterancestors()):
            continue  # older copy of a text box, same text as the drawing version
        text = "".join(t.text or "" for t in p.iter(f"{W}t"))
        where = _location(root, p)
        if not text.strip() and not (include_empty_cells and where.startswith("table")):
            continue
        if contains and contains.lower() not in text.lower():
            continue
        out.append({"para_id": p.get(f"{W14}paraId"), "location": where, "text": text})
    return base._js(out)


@mcp.tool()
def get_document_info() -> str:
    """Paragraph, heading, table and image counts and the document parts."""
    return base._js(_doc().get_info())


@mcp.tool()
def get_headings() -> str:
    """Heading structure (level, text, para_id). Empty if the document uses no heading styles."""
    return base.get_headings()


@mcp.tool()
def get_body_text() -> str:
    """Full text of the document as it reads with all tracked changes accepted."""
    return base.get_body_text()


@mcp.tool()
def get_tables() -> str:
    """All tables with their cell text, indexed for modify_cell / add_table_row."""
    return base.get_tables()


@mcp.tool()
def get_headers_footers() -> str:
    """Text of the headers and footers."""
    return base.get_headers_footers()


@mcp.tool()
def get_comments() -> str:
    """All comments with author, text and the paragraph they belong to."""
    return base.get_comments()


@mcp.tool()
def get_tracked_changes() -> str:
    """All tracked insertions and deletions in document order."""
    return base.get_tracked_changes()


@mcp.tool()
def list_template_fields() -> str:
    """Content-control fields (tag, label, type) of a template, for fill_template."""
    return base.list_template_fields()


# ── Tracked editing ──────────────────────────────────────────────────────────


@mcp.tool()
def insert_text(para_id: str, text: str, position: str = "end") -> str:
    """Insert text into a paragraph as a tracked insertion.

    Args:
        para_id: Paragraph to insert into.
        text: Text to insert. Include a leading space if it follows existing text.
        position: "start", "end", or existing text in the paragraph to insert after.
    """
    return _edit(base.insert_text(para_id=para_id, text=text, position=position, author=AUTHOR))


@mcp.tool()
def replace_text(para_id: str, find: str, replace: str) -> str:
    """Replace text within a paragraph (tracked: old text struck through, new text underlined).

    Args:
        para_id: Paragraph containing the text.
        find: Exact text to replace.
        replace: New text.
    """
    return _edit(base.replace_text(para_id=para_id, find=find, replace=replace, author=AUTHOR))


@mcp.tool()
def delete_text(para_id: str, text: str) -> str:
    """Delete text within a paragraph (tracked: stays visible as strikethrough until accepted).

    Args:
        para_id: Paragraph containing the text.
        text: Exact text to delete.
    """
    return _edit(base.delete_text(para_id=para_id, text=text, author=AUTHOR))


@mcp.tool()
def insert_paragraph(after_para_id: str, text: str, style: str = "") -> str:
    """Insert a new paragraph after another one, as a tracked insertion.

    The new paragraph copies the formatting of the one before it (style, indent, list
    numbering, font), so adding an item after a list item continues the list.

    Args:
        after_para_id: Paragraph to insert after.
        text: Text of the new paragraph.
        style: Optional paragraph style name to use instead (e.g. "Heading2").
    """
    doc = _doc()
    root = doc._require(DOCUMENT)
    target = doc._find_para(root, after_para_id)
    if target is None:
        raise ValueError(f"Paragraph '{after_para_id}' not found")
    cid = doc._next_markup_id(root)

    new_p = etree.Element(f"{W}p")
    new_pid = doc._new_para_id()
    new_p.set(f"{W14}paraId", new_pid)
    new_p.set(f"{W14}textId", "77777777")
    src_ppr = target.find(f"{W}pPr")
    ppr = copy.deepcopy(src_ppr) if src_ppr is not None else etree.Element(f"{W}pPr")
    for tag in ("sectPr", "pPrChange"):
        for el in ppr.findall(f"{W}{tag}"):
            ppr.remove(el)
    run_rpr = None
    if style:
        ps = ppr.find(f"{W}pStyle")
        if ps is None:
            ps = etree.Element(f"{W}pStyle")
            ppr.insert(0, ps)
        ps.set(f"{W}val", style)
        old = ppr.find(f"{W}rPr")
        if old is not None:
            ppr.remove(old)
    else:
        # Like pressing Enter at the end of the paragraph: continue its last text's formatting
        text_runs = [r for r in target.iter(f"{W}r") if r.find(f"{W}t") is not None]
        run_rpr = _clean_rpr(text_runs[-1].find(f"{W}rPr")) if text_runs else None
        if run_rpr is None:
            run_rpr = _clean_rpr(ppr.find(f"{W}rPr"))
    _mark_paragraph(ppr, _new_markup("ins", cid))
    new_p.append(ppr)
    ins = _new_markup("ins", cid + 1)
    ins.append(_text_run(text, run_rpr))
    new_p.append(ins)
    target.addnext(new_p)
    doc._mark(DOCUMENT)
    return base._js({"para_id": new_pid, "text": text, "tracked": True})


@mcp.tool()
def delete_paragraph(para_id: str) -> str:
    """Delete a whole paragraph as a tracked deletion (stays visible as strikethrough until accepted).

    Args:
        para_id: Paragraph to delete.
    """
    doc = _doc()
    root = doc._require(DOCUMENT)
    para = doc._find_para(root, para_id)
    if para is None:
        raise ValueError(f"Paragraph '{para_id}' not found")
    cid = doc._next_markup_id(root)
    deleted = 0
    for r in list(para.iter(f"{W}r")):
        parent = r.getparent()
        if parent.tag in (f"{W}del", f"{W}ins") or r.find(f"{W}t") is None and r.find(f"{W}instrText") is None:
            continue
        for t in r.findall(f"{W}t"):
            t.tag = f"{W}delText"
        for t in r.findall(f"{W}instrText"):
            t.tag = f"{W}delInstrText"
        wrapper = _new_markup("del", cid)
        cid += 1
        r.addprevious(wrapper)
        wrapper.append(r)
        deleted += 1
    ppr = para.find(f"{W}pPr")
    if ppr is None:
        ppr = etree.Element(f"{W}pPr")
        para.insert(0, ppr)
    _mark_paragraph(ppr, _new_markup("del", cid))
    doc._mark(DOCUMENT)
    return base._js({"para_id": para_id, "runs_deleted": deleted, "tracked": True})


@mcp.tool()
def modify_cell(table_idx: int, row: int, col: int, text: str) -> str:
    """Replace the text of a table cell (tracked). Best for short values and empty fields.

    Replaces the first paragraph of the cell; for cells with several paragraphs use
    get_paragraphs and the paragraph tools instead.

    Args:
        table_idx: Table index from get_tables / get_paragraphs.
        row: Row index (0-based).
        col: Cell index within the row (0-based).
        text: New cell text.
    """
    return _edit(base.modify_cell(table_idx=table_idx, row=row, col=col, text=text, author=AUTHOR))


@mcp.tool()
def add_table_row(table_idx: int, cells: list[str], after_row: int = -1) -> str:
    """Add a table row as a tracked insertion, copying the formatting of an existing row.

    Args:
        table_idx: Table index.
        cells: Text for each cell of the new row, left to right.
        after_row: Row to copy and insert after (0-based). -1 = last row.
    """
    doc = _doc()
    root = doc._require(DOCUMENT)
    tbl = doc._get_table(table_idx)
    rows = tbl.findall(f"{W}tr")
    ref = rows[after_row]
    new_tr = copy.deepcopy(ref)
    cid = doc._next_markup_id(root)

    # Keep structure and formatting only: drop text, revisions, comments and bookmarks
    for tag in ("del", "moveFrom", "commentRangeStart", "commentRangeEnd", "bookmarkStart", "bookmarkEnd", "rPrChange", "pPrChange", "tcPrChange", "trPrChange"):
        for el in list(new_tr.iter(f"{W}{tag}")):
            el.getparent().remove(el)
    for ins in list(new_tr.iter(f"{W}ins")):
        parent = ins.getparent()
        if parent.tag in (f"{W}rPr", f"{W}trPr"):
            parent.remove(ins)
        else:
            for child in list(ins):
                ins.addprevious(child)
            parent.remove(ins)
    for el in new_tr.iter():
        if el.get(f"{W14}paraId"):
            el.set(f"{W14}paraId", doc._new_para_id())

    tcs = new_tr.findall(f"{W}tc")
    if len(cells) > len(tcs):
        raise ValueError(f"Row has {len(tcs)} cells but {len(cells)} values were given")
    for i, tc in enumerate(tcs):
        paras = tc.findall(f"{W}p")
        first = paras[0]
        for extra in paras[1:]:
            tc.remove(extra)
        runs = [r for r in first if r.tag in (f"{W}r", f"{W}hyperlink")]
        rpr = None
        text_run = next((r for r in first.iter(f"{W}r") if r.find(f"{W}t") is not None), None)
        if text_run is not None:
            rpr = _clean_rpr(text_run.find(f"{W}rPr"))
        if rpr is None:
            rpr = _clean_rpr(first.find(f"{W}pPr/{W}rPr"))
        for r in runs:
            first.remove(r)
        if i < len(cells) and cells[i]:
            ins = _new_markup("ins", cid)
            cid += 1
            ins.append(_text_run(cells[i], rpr))
            first.append(ins)

    tr_pr = new_tr.find(f"{W}trPr")
    if tr_pr is None:
        tr_pr = etree.Element(f"{W}trPr")
        tbl_pr_ex = new_tr.find(f"{W}tblPrEx")
        (tbl_pr_ex.addnext(tr_pr) if tbl_pr_ex is not None else new_tr.insert(0, tr_pr))
    tr_pr.append(_new_markup("ins", cid))
    ref.addnext(new_tr)
    doc._mark(DOCUMENT)
    return base._js({"table_index": table_idx, "row_index": tbl.findall(f"{W}tr").index(new_tr), "tracked": True})


@mcp.tool()
def delete_table_row(table_idx: int, row: int) -> str:
    """Delete a table row as a tracked deletion.

    Args:
        table_idx: Table index.
        row: Row index (0-based).
    """
    return _edit(base.delete_table_row(table_idx=table_idx, row_idx=row, author=AUTHOR))


@mcp.tool()
def fill_template(data: dict[str, str | list[str]]) -> str:
    """Fill content-control fields of a NEW document made with create_from_template (not tracked).

    Only for templates that use content controls (see list_template_fields).

    Args:
        data: Field tag -> value. Use a list of strings for repeating sections.
    """
    if not _state["from_template"]:
        raise PermissionError("fill_template writes without tracked changes; use it only on documents from create_from_template")
    return _edit(base.fill_template(data=data))


# ── Comments and review output ───────────────────────────────────────────────


@mcp.tool()
def add_comment(para_id: str, text: str) -> str:
    """Add a comment to a paragraph, e.g. to explain why something was changed or removed.

    Args:
        para_id: Paragraph to comment on.
        text: Comment text.
    """
    return base.add_comment(para_id=para_id, text=text, author=AUTHOR)


@mcp.tool()
def reply_to_comment(comment_id: int, text: str) -> str:
    """Reply to an existing comment.

    Args:
        comment_id: Id of the comment (from get_comments).
        text: Reply text.
    """
    return base.reply_to_comment(parent_id=comment_id, text=text, author=AUTHOR)


def _comments_by_paragraph(doc: DocxDocument, root) -> dict[str, list[str]]:
    comments_root = doc._tree("word/comments.xml")
    if comments_root is None:
        return {}
    texts = {
        c.get(f"{W}id"): f"{c.get(f'{W}author')}: " + " ".join(
            "".join(t.text or "" for t in p.iter(f"{W}t")) for p in c.iter(f"{W}p")
        ).strip()
        for c in comments_root.iter(f"{W}comment")
    }
    out: dict[str, list[str]] = {}
    for start in root.iter(f"{W}commentRangeStart"):
        para = _paragraph(start)
        if para is not None and start.get(f"{W}id") in texts:
            out.setdefault(para.get(f"{W14}paraId"), []).append(texts[start.get(f"{W}id")])
    return out


@mcp.tool()
def generate_change_summary(output_path: str = "") -> str:
    """Change log of all tracked changes and comments, one entry per changed paragraph.

    Each entry gives the location, the paragraph before and after, who made the change
    and the comments on it. Returns the log; also writes it to output_path if given.

    Args:
        output_path: Optional absolute path for a .txt copy of the log.
    """
    doc = _doc()
    root = doc._require(DOCUMENT)
    comments = _comments_by_paragraph(doc, root)
    entries = []
    for p in root.iter(f"{W}p"):
        if any(a.tag == f"{{{MC_NS}}}Fallback" for a in p.iterancestors()):
            continue
        marks = list(p.iter(f"{W}ins")) + list(p.iter(f"{W}del"))
        row_mark = next((a.find(f"{W}trPr/{W}ins") for a in p.iterancestors(f"{W}tr")), None)
        pid = p.get(f"{W14}paraId")
        if not marks and row_mark is None and pid not in comments:
            continue
        old = "".join(
            el.text or "" for el in p.iter(f"{W}t", f"{W}delText")
            if el.tag == f"{W}delText" or not any(a.tag == f"{W}ins" for a in el.iterancestors())
        )
        new = "".join(
            t.text or "" for t in p.iter(f"{W}t") if not any(a.tag == f"{W}del" for a in t.iterancestors())
        )
        mark_ins = p.find(f"{W}pPr/{W}rPr/{W}ins") is not None or row_mark is not None
        mark_del = p.find(f"{W}pPr/{W}rPr/{W}del") is not None
        if mark_ins and not new:
            continue  # empty cell of an added row
        if mark_ins:
            change = f'Added: "{new}"'
        elif mark_del:
            change = f'Removed paragraph: "{old}"'
        elif old != new:
            change = f'Before: "{old}"\n   After:  "{new}"'
        else:
            change = f'Text: "{new}"'
        who = sorted({f"{m.get(f'{W}author')}, {m.get(f'{W}date', '')[:10]}" for m in marks + ([row_mark] if row_mark is not None else [])})
        entry = f"{len(entries) + 1}. {_location(root, p)}" + (f"  ({'; '.join(who)})" if who else "") + f"\n   {change}"
        for c in comments.get(pid, []):
            entry += f"\n   Comment - {c}"
        entries.append(entry)

    log = "Document Change Summary\n" + "=" * 40 + f"\n{_doc().source_path.name}\n\n" + ("\n\n".join(entries) or "No tracked changes.")
    if output_path:
        Path(output_path).write_text(log + "\n", encoding="utf-8")
    return log


@mcp.tool()
def audit_document() -> str:
    """Structural check (footnotes, headings, bookmarks, ids) to run before handing a document over."""
    return base.audit_document()


if __name__ == "__main__":
    mcp.run(transport="stdio")
