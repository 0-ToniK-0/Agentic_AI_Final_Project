"""SharePoint MCP server: files generated documents and customer attachments in the support library as
<root>/<Customer>/<ID - Description>/<Document type>/<file>."""
import json
import sys

from mcp.server.fastmcp import FastMCP

from sharepoint_client import DOC_TYPES, SharePointClient

mcp = FastMCP("sharepoint")
sharepoint = SharePointClient()


def log(message: str) -> None:
    print(f"[sharepoint tool:{sharepoint.mode}] {message}", file=sys.stderr, flush=True)


@mcp.tool()
def file_documents(customer: str, item_id: str, description: str, doc_type: str, file_paths: list[str]) -> str:
    """Upload documents to <root>/<customer>/<item_id - description>/<doc_type>/, creating missing folders.
    item_id: the User Story ID (support) or the request ID (delivery). description: short ticket description.
    doc_type: 'Change Request', 'Design', 'Test' or 'Customer Files'.
    file_paths: files generated in output/ or downloaded from the email in attachments/ (others are refused).
    An existing file with the same name gets a new version."""
    log(f"file_documents(customer={customer!r}, item_id={item_id!r}, doc_type={doc_type!r}, files={len(file_paths)})")
    if doc_type not in DOC_TYPES:
        raise ValueError(f"doc_type must be one of {DOC_TYPES}")
    return json.dumps(sharepoint.file_documents(customer, item_id, description, doc_type, file_paths))


@mcp.tool()
def ticket_folder_url(customer: str, item_id: str, description: str) -> str:
    """Create (if needed) and return the ticket folder <root>/<customer>/<item_id - description>/."""
    log(f"ticket_folder_url(customer={customer!r}, item_id={item_id!r})")
    from sharepoint_client import ticket_folder

    return json.dumps(sharepoint.ensure_folder(ticket_folder(customer, item_id, description)))


if __name__ == "__main__":
    mcp.run(transport="stdio")
