# Word Documents MCP Server

Lets the agents document a change (what is customized, changed or removed) directly in the customer's or company's Word documents. Every edit is a **tracked change** with a comment where needed, so a reviewer accepts or rejects it in Word. Microsoft Word is not needed to run it.

It is a thin layer (`server.py`) over [docx-mcp-server](https://github.com/SecurityRonin/docx-mcp) 0.7.4, which edits the `.docx` XML directly. The layer fixes what broke when it was used on a change request form template:

| Problem in docx-mcp 0.7.4 | Fixed in `server.py` |
|---|---|
| Won't start with `mcp` 2.x | `mcp` pinned to 1.30.0 |
| Can't edit documents saved by Word 2007 or non-Word tools (no paragraph ids) | Ids added on open, the same in every session |
| Inserted text falls back to the default font (e.g. Calibri 11 instead of Arial 10) | Inserted text takes the formatting of what it replaces or continues |
| Refuses to edit short values that appear elsewhere ("3", "Vendor CR Number") | Allowed, since the paragraph is already named |
| New table rows lose widths, merged cells and fonts | New rows copy an existing row |
| `insert_paragraph` / `delete_paragraph` are not tracked | Both tracked |
| A document created from a `.dotx` won't open in Word | Content type switched to `.docx` |
| 219 tools, some untracked | 26 tools, all tracked (except `fill_template`, which is limited to new documents) |
| Change log is fragmented and has no locations | Change log per paragraph: location, before/after, author, comments |

## Setup (Windows)

From the repository root:

```bash
python -m venv tools/docx-mcp/.venv
tools/docx-mcp/.venv/Scripts/python -m pip install -r tools/docx-mcp/requirements.txt
```

About 370 MB, mostly spaCy (docx-mcp requires it for a feature we don't expose). The `.venv` folder is git-ignored.

Check it works on any template (writes into the output folder, never touches the template):

```bash
tools/docx-mcp/.venv/Scripts/python tools/docx-mcp/smoke_test.py "C:/path/to/template.docx" C:/temp/docx-smoke
```

## Using it

Start it like the notebook MCP servers, but with the venv's Python:

```python
params = StdioServerParameters(
    command="tools/docx-mcp/.venv/Scripts/python.exe",
    args=["tools/docx-mcp/server.py"],
    env={**os.environ, "DOCX_AUTHOR": "AI Assistant"},  # name on tracked changes and comments
)
```

Typical flow for an agent:

1. `create_from_template(template_path, output_path)` for a new document, or `open_document(path)` for an existing one
2. `get_paragraphs(contains=...)` to find the `para_id` (or the table/row/cell) to change. Forms usually have no headings.
3. Edit: `modify_cell`, `replace_text`, `insert_text`, `delete_text`, `insert_paragraph`, `delete_paragraph`, `add_table_row`, `delete_table_row`
4. `add_comment(para_id, text)` to explain why, especially for removals
5. `save_document()`, then `generate_change_summary(output_path)` for a change log to paste into an email or work item

Read tools: `get_document_info`, `get_headings`, `get_body_text`, `get_tables`, `get_headers_footers`, `get_comments`, `get_tracked_changes`, `list_template_fields`, `audit_document`.

## Limits

- `.docx` and `.dotx` only. Convert old `.doc` files to `.docx` first.
- `modify_cell` replaces the cell's first paragraph; for cells with several paragraphs use the paragraph tools.
- New text in an empty cell takes that cell's own formatting, which in some templates differs from the rest of the form.
- `server.py` patches docx-mcp internals. Before upgrading `docx-mcp-server`, re-run `smoke_test.py`.
