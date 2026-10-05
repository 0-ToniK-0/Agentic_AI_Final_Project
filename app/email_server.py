"""Email MCP server: reads support emails from Outlook (Microsoft Graph) or the local mailbox, downloads their
attachments, saves reply drafts and sends them. Drafting and sending each need their own human approval."""
import json
import sys

from mcp.server.fastmcp import FastMCP

from approval_ledger import is_approved
from mail_client import MailClient

mcp = FastMCP("email")
mail = MailClient()


def log(message: str) -> None:
    print(f"[email tool:{mail.mode}] {message}", file=sys.stderr, flush=True)  # stdout is reserved for MCP


@mcp.tool()
def list_support_emails(max_emails: int = 10) -> str:
    """List the newest support emails the assistant may read (ID, subject, sender, preview, attachment names).
    Emails from the team, internal senders, excluded domains or confidential topics are filtered out."""
    log(f"list_support_emails(max_emails={max_emails})")
    return json.dumps(mail.list_support_emails(max_emails))


@mcp.tool()
def get_email(message_id: str) -> str:
    """Return one support email with its full text, recipients and attachment names."""
    log(f"get_email({message_id[:24]!r})")
    return json.dumps(mail.get_email(message_id))


@mcp.tool()
def save_attachments(message_id: str) -> str:
    """Download the file attachments of a support email (inline images are skipped) and return local paths."""
    log(f"save_attachments({message_id[:24]!r})")
    return json.dumps(mail.save_attachments(message_id))


@mcp.tool()
def save_reply_draft(message_id: str, subject: str, body: str, approval_id: str,
                     attachment_paths: list[str] | None = None) -> str:
    """Save a reply-all to a support email as a DRAFT in the mailbox. Nothing is sent.
    body: plain text, **bold** is allowed.
    attachment_paths: documents generated in output/ (for example a Change Request); other files are refused.
    approval_id: an approved 'customer_email' decision for this message_id from the approval ledger."""
    log(f"save_reply_draft(message_id={message_id[:24]!r}, approval_id={approval_id!r}, "
        f"attachments={len(attachment_paths or [])})")
    if not is_approved(approval_id, "customer_email", message_id):
        raise PermissionError("No human approval found for this email text. A person must approve it first.")
    return json.dumps(mail.create_reply_draft(message_id, subject, body, attachment_paths=attachment_paths))


@mcp.tool()
def send_draft(draft_id: str, approval_id: str) -> str:
    """Send a saved draft to the customer.
    approval_id: an approved 'send_email' decision for this exact draft_id from the approval ledger."""
    log(f"send_draft(draft_id={draft_id[:24]!r}, approval_id={approval_id!r})")
    if not is_approved(approval_id, "send_email", draft_id):
        raise PermissionError("Sending needs a person's approval for this exact draft.")
    return json.dumps(mail.send_draft(draft_id))


if __name__ == "__main__":
    mcp.run(transport="stdio")
