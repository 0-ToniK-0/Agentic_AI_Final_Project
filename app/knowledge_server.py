"""Knowledge MCP server: semantic search over approved F&O knowledge."""
import json
import os
import sys

from langchain_core.vectorstores import InMemoryVectorStore
from langchain_ollama import OllamaEmbeddings
from mcp.server.fastmcp import FastMCP

store = InMemoryVectorStore.load(
    "fo_knowledge_store.json", OllamaEmbeddings(model=os.getenv("EMBED_MODEL", "nomic-embed-text"))
)
mcp = FastMCP("fo_knowledge")


def log(message: str) -> None:
    print(f"[knowledge tool] {message}", file=sys.stderr, flush=True)  # stdout is reserved for MCP


@mcp.tool()
def search_fo_knowledge(query: str, module: str = "", source_type: str = "", k: int = 4) -> str:
    """Search approved Dynamics 365 F&O knowledge: Microsoft guidance, company standards,
    previously approved designs and email templates.
    module: '' (all), 'Finance' or 'Supply Chain'.
    source_type: '' (all), 'microsoft_guidance', 'company_standard', 'approved_design' or 'email_template'."""
    log(f"search_fo_knowledge(query={query!r}, module={module!r}, source_type={source_type!r})")

    def keep(doc) -> bool:
        m = doc.metadata
        module_ok = module not in ("Finance", "Supply Chain") or m["module"] in (module, "General")
        return bool(m["approved"]) and module_ok and (not source_type or m["source_type"] == source_type)

    results = store.similarity_search(query, k=k, filter=keep)
    return json.dumps([
        {"doc_id": d.metadata["doc_id"], "title": d.metadata["title"], "source_type": d.metadata["source_type"],
         "module": d.metadata["module"], "snippet": d.page_content}
        for d in results
    ])


@mcp.tool()
def get_document(doc_id: str) -> str:
    """Return the full text of one approved knowledge document (for example an email template) by its ID."""
    log(f"get_document({doc_id!r})")
    parts = sorted(
        (r for r in store.store.values() if r["metadata"]["doc_id"] == doc_id and r["metadata"]["approved"]),
        key=lambda r: r["metadata"].get("chunk", 0),
    )
    if not parts:
        raise ValueError(f"Unknown or unapproved document: {doc_id}")
    return json.dumps({"doc_id": doc_id, "title": parts[0]["metadata"]["title"],
                       "text": "\n".join(r["text"] for r in parts)})


if __name__ == "__main__":
    mcp.run(transport="stdio")
