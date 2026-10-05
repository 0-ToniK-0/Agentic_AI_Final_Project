"""Smoke test for server.py against any .docx/.dotx template.

Starts the server over stdio (as the notebooks do), makes one edit of each kind in a
copy of the template, then checks the saved file directly:
- every edit is a tracked change by DOCX_AUTHOR, and the comments are there
- inserted text carries run formatting (font/size) instead of the document default
- formatting of the paragraphs and tables that were already there is unchanged
- styles, numbering, headers and footers are untouched apart from added paraIds
- the template file itself is unchanged

Usage:
    python smoke_test.py <template.docx|.dotx> <output_folder>
"""

import asyncio
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

from lxml import etree
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
W14 = "{http://schemas.microsoft.com/office/word/2010/wordml}"
SERVER = Path(__file__).with_name("server.py")


async def run_edits(template: Path, out_doc: Path, out_log: Path) -> list[str]:
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()

            async def call(tool, **args):
                res = await s.call_tool(tool, args)
                text = "\n".join(c.text for c in res.content if hasattr(c, "text"))
                if res.isError:
                    raise RuntimeError(f"{tool} failed: {text}")
                return json.loads(text) if text[:1] in "[{" else text

            tools = [t.name for t in (await s.list_tools()).tools]
            print(f"{len(tools)} tools exposed")
            await call("create_from_template", template_path=str(template), output_path=str(out_doc))
            paras = await call("get_paragraphs")
            texts = [p for p in paras if p["text"].strip()]
            done = []

            # 1. replace a word inside a paragraph that has text
            target = next(p for p in texts if re.search(r"[A-Za-z]{4,}", p["text"]))
            word = re.search(r"[A-Za-z]{4,}", target["text"]).group(0)
            await call("replace_text", para_id=target["para_id"], find=word, replace=word.upper())
            done.append(f"replace_text '{word}' in {target['location']}")

            # 2. append text to another paragraph
            target2 = texts[min(3, len(texts) - 1)]
            await call("insert_text", para_id=target2["para_id"], text=" [added by smoke test]")
            done.append(f"insert_text in {target2['location']}")

            # 3. new paragraph after a paragraph, then comment on it
            new = await call("insert_paragraph", after_para_id=target2["para_id"], text="New paragraph from smoke test.")
            await call("add_comment", para_id=new["para_id"], text="Smoke test: inserted paragraph.")
            done.append("insert_paragraph + add_comment")

            # 4. tracked paragraph deletion
            victim = texts[-1]
            await call("delete_paragraph", para_id=victim["para_id"])
            await call("add_comment", para_id=victim["para_id"], text="Smoke test: deleted paragraph.")
            done.append(f"delete_paragraph in {victim['location']}")

            # 5. tables: fill an empty cell (or overwrite one) and add a row to the last table
            tables = await call("get_tables")
            if tables:
                firsts = {}  # modify_cell replaces a cell's first paragraph, so look at those
                for p in paras:
                    if p["location"].startswith("table"):
                        firsts.setdefault(p["location"], p)
                empty = next((p for p in firsts.values() if not p["text"].strip()), list(firsts.values())[-1])
                t, r, c = (int(x) for x in re.findall(r"\d+", empty["location"]))
                await call("modify_cell", table_idx=t, row=r, col=c, text="Filled by smoke test")
                done.append(f"modify_cell {empty['location']}")
                last = len(tables) - 1
                await call("add_table_row", table_idx=last, cells=["Row added by smoke test"])
                done.append(f"add_table_row table {last}")

            await call("save_document")
            summary = await call("generate_change_summary", output_path=str(out_log))
            audit = await call("audit_document")
            entries = len(re.findall(r"^\d+\. ", summary, re.M))
            done.append(f"change summary: {entries} entries; audit valid: "
                        f"{all(v.get('valid', True) for v in audit.values() if isinstance(v, dict))}")
            return done


def check(template: Path, out_doc: Path, template_hash: str) -> list[str]:
    problems = []
    if hashlib.sha256(template.read_bytes()).hexdigest() != template_hash:
        problems.append("template file was modified")
    a, b = zipfile.ZipFile(template), zipfile.ZipFile(out_doc)
    for name in b.namelist():
        if name.endswith((".xml", ".rels")):
            etree.fromstring(b.read(name))  # raises if not well-formed

    def content(zf, name):  # XML compared as content: quote style, trailing newline and added paraIds ignored
        data = zf.read(name)
        if not name.endswith(".xml"):
            return data
        data = re.sub(rb'\s+w14:(paraId|textId)="[0-9A-Fa-f]+"', b"", data)
        return etree.tostring(etree.fromstring(data), method="c14n")

    for name in a.namelist():
        if name.startswith(("word/styles", "word/numbering", "word/theme", "word/media")) or re.match(r"word/(header|footer)\d*\.xml", name):
            if content(a, name) != content(b, name):
                problems.append(f"{name} changed")

    orig = etree.fromstring(a.read("word/document.xml"))
    doc = etree.fromstring(b.read("word/document.xml"))
    ins = list(doc.iter(f"{W}ins"))
    dels = list(doc.iter(f"{W}del"))
    if not ins or not dels:
        problems.append(f"expected tracked insertions and deletions, got {len(ins)} ins / {len(dels)} del")
    authors = {el.get(f"{W}author") for el in ins + dels}
    print(f"tracked changes: {len(ins)} insertions, {len(dels)} deletions, authors {authors}")

    bare = [el for el in ins if el.getparent().tag not in (f"{W}rPr", f"{W}trPr")
            for r in el.findall(f"{W}r") if r.find(f"{W}rPr") is None]
    if bare:
        problems.append(f"{len(bare)} inserted runs have no formatting (would show in the default font)")

    def ppr(p):
        el = p.find(f"{W}pPr")
        if el is None:
            return b""
        el = etree.fromstring(etree.tostring(el))
        for mark in el.findall(f"{W}rPr/{W}del"):  # paragraph-mark deletion marker is expected
            mark.getparent().remove(mark)
        return etree.tostring(el)

    def added(p):  # paragraphs and table rows inserted by the test
        if p.find(f"{W}pPr/{W}rPr/{W}ins") is not None:
            return True
        return any(a.tag == f"{W}tr" and a.find(f"{W}trPr/{W}ins") is not None for a in p.iterancestors())

    before = list(orig.iter(f"{W}p"))
    after = [p for p in doc.iter(f"{W}p") if not added(p)]
    if len(before) != len(after):
        problems.append(f"paragraph count {len(before)} -> {len(after)} (excluding added ones)")
    changed = sum(ppr(x) != ppr(y) for x, y in zip(before, after))
    if changed:
        problems.append(f"{changed} existing paragraphs changed formatting")
    if [etree.tostring(t.find(f"{W}tblPr")) for t in orig.iter(f"{W}tbl")] != [etree.tostring(t.find(f"{W}tblPr")) for t in doc.iter(f"{W}tbl")]:
        problems.append("table properties changed")
    if "word/comments.xml" not in b.namelist():
        problems.append("comments part missing")
    return problems


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    template, out_dir = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_doc, out_log = out_dir / f"{template.stem}_smoke.docx", out_dir / f"{template.stem}_smoke_changes.txt"
    out_doc.unlink(missing_ok=True)
    template_hash = hashlib.sha256(template.read_bytes()).hexdigest()

    for line in asyncio.run(run_edits(template, out_doc, out_log)):
        print(" -", line)
    problems = check(template, out_doc, template_hash) if template.suffix.lower() == ".docx" else []
    print(f"\noutput: {out_doc}\nchange log: {out_log}")
    if problems:
        print("FAILED:\n  " + "\n  ".join(problems))
        sys.exit(1)
    print("PASSED")


if __name__ == "__main__":
    main()
