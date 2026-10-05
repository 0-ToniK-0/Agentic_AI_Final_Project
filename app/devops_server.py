"""Azure DevOps MCP server: reads team workload, finds a customer's support Feature, and creates linked work
items (delivery plans and support tickets) after approval."""
import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ado_client import AzureDevOpsClient
from approval_ledger import is_approved

mcp = FastMCP("azure_devops")
client = AzureDevOpsClient()


def log(message: str) -> None:
    print(f"[devops tool:{client.mode}] {message}", file=sys.stderr, flush=True)


@mcp.tool()
def get_team_workload(project: str = "") -> str:
    """Return the remaining hours of open work per assignee in an Azure DevOps project (default project if empty)."""
    log(f"get_team_workload({project!r})")
    return json.dumps(client.get_open_workload(project or None))


@mcp.tool()
def get_work_item(work_item_id: int) -> str:
    """Return one work item with its fields and links."""
    log(f"get_work_item({work_item_id})")
    return json.dumps(client.get_work_item(work_item_id))


@mcp.tool()
def suggest_features_for_sender(sender_email: str) -> str:
    """Find the customer's project and support Features from the sender's email domain.
    A Feature chosen before for the same domain is returned first (from_memory = true)."""
    log(f"suggest_features_for_sender({sender_email!r})")
    return json.dumps(client.suggest_features(sender_email))


@mcp.tool()
def create_linked_work_items(package_json: str, approval_id: str) -> str:
    """Create User Stories with acceptance criteria and linked FUNC/TECH child tasks.
    package_json: JSON with request_id, module, work_type, plan and owner_recommendations.
    approval_id: an approved 'final_delivery_package' decision from the approval ledger."""
    log(f"create_linked_work_items(approval_id={approval_id!r})")
    if not is_approved(approval_id, "final_delivery_package"):
        raise PermissionError("The delivery package has not been approved by a person.")

    package = json.loads(package_json)
    created = client.create_delivery_items(
        package["request_id"], package["module"], package["work_type"],
        package["plan"], package.get("owner_recommendations", {}),
    )
    return json.dumps({"mode": client.mode, "created": created})


@mcp.tool()
def create_support_work_items(package_json: str, approval_id: str) -> str:
    """Create a support ticket: one User Story under the customer's Feature, its small tasks as children
    (Activity, estimate, due date, recommended owner) and the email's attachments on the story.
    The Feature is remembered for the sender's domain.
    package_json: JSON with message_id, domain, customer, email_subject, feature, ticket, tasks, attachment_paths.
    approval_id: an approved 'support_ticket' decision for this message_id from the approval ledger."""
    package = json.loads(package_json)
    log(f"create_support_work_items(feature={package['feature']['id']}, approval_id={approval_id!r})")
    if not is_approved(approval_id, "support_ticket", package["message_id"]):
        raise PermissionError("The support ticket has not been approved by a person.")
    return json.dumps(client.create_support_items(package))


@mcp.tool()
def attach_documents(work_item_id: int, file_paths: list[str], message_id: str, approval_id: str) -> str:
    """Attach documents generated in output/ (for example the Change Request for customer sign-off) to a
    support work item. approval_id: the approved 'support_ticket' decision for message_id."""
    log(f"attach_documents(work_item_id={work_item_id}, files={len(file_paths)}, approval_id={approval_id!r})")
    if not is_approved(approval_id, "support_ticket", message_id):
        raise PermissionError("The support ticket has not been approved by a person.")
    outside = [p for p in file_paths if not Path(p).resolve().is_relative_to(Path("output").resolve())]
    if outside:
        raise PermissionError(f"Only documents generated in output/ can be attached: {outside}")
    return json.dumps(client.attach_documents(work_item_id, file_paths, "Change Request for customer sign-off"))


@mcp.tool()
def add_document_link(work_item_id: int, url: str, approval_id: str, message_id: str = "") -> str:
    """Link the SharePoint folder of a ticket or request to a work item.
    url: must be inside SHAREPOINT_SITE_URL (or the local dry-run folder).
    approval_id: the approved 'support_ticket' decision for message_id, or the approved 'final_delivery_package'."""
    log(f"add_document_link(work_item_id={work_item_id}, approval_id={approval_id!r})")
    if not (is_approved(approval_id, "support_ticket", message_id or None) or is_approved(approval_id, "final_delivery_package")):
        raise PermissionError("Linking needs an approved support ticket or delivery package.")
    site = os.getenv("SHAREPOINT_SITE_URL", "").rstrip("/")
    allowed = (site and url.startswith(site + "/")) or url.startswith(Path("sharepoint_dry_run").resolve().as_uri())
    if not allowed:
        raise PermissionError(f"Only links to the SharePoint site {site or '(not configured)'} can be added.")
    return json.dumps(client.add_link(work_item_id, url, "Documents in SharePoint"))


if __name__ == "__main__":
    mcp.run(transport="stdio")
