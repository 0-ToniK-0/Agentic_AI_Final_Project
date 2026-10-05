"""Mailbox client: Outlook through Microsoft Graph (live) or a local JSON mailbox (dry-run).

Live mode is used when GRAPH_CLIENT_ID and GRAPH_TENANT_ID are set (or GRAPH_ACCESS_TOKEN, for a quick test
with a token from Graph Explorer).
- Delegated (default): run MailClient().sign_in() once in the notebook. The token is cached in
  .graph_token_cache.json and refreshed silently, so the MCP server can use it without asking again.
- App-only: also set GRAPH_CLIENT_SECRET and GRAPH_MAILBOX (the shared support mailbox).

Only support emails can be read: get_email(), save_attachments() and create_reply_draft() refuse any message
that list_support_emails() would filter out (see support_filter).
list_support_emails() also flags emails with possible prompt injection (prompt_guard.py).
"""
import base64
import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from prompt_guard import screen

GRAPH_URL = os.getenv("GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0")
SCOPES = ["Mail.ReadWrite", "Mail.Send"]
TOKEN_CACHE = Path(".graph_token_cache.json")
LOCAL_MAILBOX = Path("mailbox/messages.json")
OUTBOX = Path("mailbox/outbox.json")  # every draft and sent reply (both modes), for the audit trail
ATTACHMENTS_DIR = Path("attachments")
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
OUTPUT_DIR = Path("output")  # generated documents: the only files that can be attached to a reply
MAX_SEND_ATTACHMENT_BYTES = 3 * 1024 * 1024
TICKET_SUBJECT = re.compile(r"\|\s*.*?\d+")  # replies to existing tickets, e.g. "Contoso - Fix posting | 1234"
MESSAGE_FIELDS = "id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,hasAttachments,body"


def _env_list(name: str, default: str = "") -> list:
    return [item.strip().lower() for item in os.getenv(name, default).split(",") if item.strip()]


def support_filter(message: dict) -> str:
    """Return why a message is not a support email for the assistant, or "" when it is."""
    subject, body = message["subject"].lower(), message["body"].lower()
    sender_name, sender = message["from_name"].lower(), message["from_address"].lower()
    staff = _env_list("TECHNICAL_PEOPLE") + _env_list("FUNCTIONAL_PEOPLE")

    if any(word in subject or word in sender_name for word in _env_list("EXCLUDE_EMAILS")):
        return "excluded keyword"
    if TICKET_SUBJECT.search(subject):
        return "reply to an existing ticket"
    if any(person in sender_name for person in staff):
        return "sent by a team member"
    if any(domain in sender for domain in _env_list("DISINCLUDE_DOMAINS")):
        return "excluded domain"
    include_internal = os.getenv("EMAIL_INCLUDE_INTERNAL", "false").strip().lower() in ("true", "1", "yes")
    if not include_internal and any(domain in sender for domain in _env_list("EMAIL_INTERNAL_DOMAINS")):
        return "internal sender"
    if not all(word in f"{subject} {body}" for word in _env_list("EMAIL_REQUIRED_KEYWORDS", "f&o,support")):
        return "not a support email"
    return ""


def to_html(text: str) -> str:
    """Plain text with **bold** markers -> HTML for the email body."""
    escaped = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped).replace("\n", "<br>\n")


def _safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(name).name).strip() or "attachment"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MailClient:
    def __init__(self):
        self.client_id = os.getenv("GRAPH_CLIENT_ID", "")
        self.tenant_id = os.getenv("GRAPH_TENANT_ID", "")
        self.secret = os.getenv("GRAPH_CLIENT_SECRET", "")
        self.mailbox = os.getenv("GRAPH_MAILBOX", "")
        self.folder = os.getenv("GRAPH_MAIL_FOLDER", "inbox")
        self.static_token = os.getenv("GRAPH_ACCESS_TOKEN", "")
        self.live = bool(self.static_token or (self.client_id and self.tenant_id))
        if self.live and self.secret and not self.mailbox:
            raise ValueError("App-only Graph access needs GRAPH_MAILBOX (the mailbox to read and send from).")

    @property
    def mode(self) -> str:
        return "live" if self.live else "dry-run"

    # ---------------- Microsoft Graph authentication ----------------
    def _public_app(self):
        import msal

        cache = msal.SerializableTokenCache()
        if TOKEN_CACHE.exists():
            cache.deserialize(TOKEN_CACHE.read_text(encoding="utf-8"))
        app = msal.PublicClientApplication(
            self.client_id, authority=f"https://login.microsoftonline.com/{self.tenant_id}", token_cache=cache)
        return app, cache

    @staticmethod
    def _save_cache(cache) -> None:
        if cache.has_state_changed:
            TOKEN_CACHE.write_text(cache.serialize(), encoding="utf-8")

    def sign_in(self) -> str:
        """Delegated sign-in with a device code (open the link, type the code). Cached for later runs."""
        if not self.live or self.static_token or self.secret:
            return f"No interactive sign-in needed ({self.mode}{', app-only' if self.secret else ''})."
        app, cache = self._public_app()
        accounts = app.get_accounts()
        if accounts and app.acquire_token_silent(SCOPES, account=accounts[0]):
            self._save_cache(cache)
            return f"Already signed in as {accounts[0]['username']}"
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Could not start the device sign-in: {flow.get('error_description', flow)}")
        print(flow["message"], flush=True)
        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(f"Sign-in failed: {result.get('error_description', result)}")
        self._save_cache(cache)
        return f"Signed in as {result.get('id_token_claims', {}).get('preferred_username', 'unknown user')}"

    def _token(self) -> str:
        if self.static_token:
            return self.static_token
        import msal

        if self.secret:
            app = msal.ConfidentialClientApplication(
                self.client_id, client_credential=self.secret,
                authority=f"https://login.microsoftonline.com/{self.tenant_id}")
            result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        else:
            app, cache = self._public_app()
            accounts = app.get_accounts()
            result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
            if not result:
                raise PermissionError("Not signed in to Microsoft Graph: run MailClient().sign_in() in the notebook.")
            self._save_cache(cache)
        if "access_token" not in result:
            raise PermissionError(f"Could not get a Graph token: {result.get('error_description', result)}")
        return result["access_token"]

    def _graph(self, method: str, path: str, **kwargs) -> requests.Response:
        user = f"/users/{quote(self.mailbox)}" if self.secret else "/me"
        url = path if "://" in path else f"{GRAPH_URL}{user}{path}"  # @odata.nextLink is already absolute
        headers = {
            "Authorization": f"Bearer {self._token()}",
            # Immutable IDs stay valid when a message is moved to another folder.
            "Prefer": 'IdType="ImmutableId", outlook.body-content-type="text"',
            **kwargs.pop("headers", {}),
        }
        response = requests.request(method, url, headers=headers, timeout=60, **kwargs)
        if not response.ok:
            raise requests.HTTPError(f"Graph {method} {path} -> {response.status_code}: {response.text[:500]}",
                                     response=response)
        return response

    @staticmethod
    def _mid(message_id: str) -> str:
        return quote(message_id, safe="")

    # ---------------- reading ----------------
    @staticmethod
    def _from_graph(m: dict) -> dict:
        sender = (m.get("from") or {}).get("emailAddress", {})
        return {
            "message_id": m["id"],
            "conversation_id": m.get("conversationId", ""),
            "subject": m.get("subject") or "",
            "from_name": sender.get("name", ""),
            "from_address": sender.get("address", ""),
            "to": [r["emailAddress"]["address"] for r in m.get("toRecipients", [])],
            "cc": [r["emailAddress"]["address"] for r in m.get("ccRecipients", [])],
            "received": m.get("receivedDateTime", ""),
            "body": (m.get("body") or {}).get("content", ""),
            "has_attachments": bool(m.get("hasAttachments")),
        }

    def _local_messages(self) -> list:
        return json.loads(LOCAL_MAILBOX.read_text(encoding="utf-8")) if LOCAL_MAILBOX.exists() else []

    def _file_attachments(self, message: dict) -> list:
        """Real file attachments (inline images such as signature logos are skipped)."""
        if not self.live:
            items = [{**a, "id": a["name"]} for a in message.get("attachments", [])]
        elif not message.get("has_attachments"):
            return []
        else:
            items = self._graph(
                "GET", f"/messages/{self._mid(message['message_id'])}/attachments",
                params={"$select": "id,name,contentType,size,isInline"}).json()["value"]
            items = [{**a, "is_inline": a.get("isInline"), "content_type": a.get("contentType")} for a in items
                     if a.get("@odata.type") != "#microsoft.graph.referenceAttachment"]  # links have no file
        return [{"id": a["id"], "name": a["name"], "content_type": a.get("content_type", ""), "size": a.get("size", 0)}
                for a in items if not a.get("is_inline") and a.get("size", 0) <= MAX_ATTACHMENT_BYTES]

    def list_support_emails(self, max_emails: int = 10) -> list:
        """Newest support emails in the mailbox folder, after the support filter."""
        results = []
        if not self.live:
            pages = [sorted(self._local_messages(), key=lambda m: m["received"], reverse=True)]
        else:
            pages = self._graph_pages(max_emails)
        for page in pages:
            for message in page:
                if support_filter(message):
                    continue
                results.append({
                    "message_id": message["message_id"],
                    "subject": message["subject"],
                    "from": f"{message['from_name']} <{message['from_address']}>",
                    "received": message["received"],
                    "preview": " ".join(message["body"].split())[:300],
                    "attachments": [a["name"] for a in self._file_attachments(message)],
                    "injection_flags": [f["rule"] for f in screen(f"{message['subject']}\n{message['body']}")[1]],
                })
                if len(results) >= max_emails:
                    return results
        return results

    def _graph_pages(self, max_emails: int):
        url = f"/mailFolders/{quote(self.folder)}/messages"
        params = {"$top": 50, "$orderby": "receivedDateTime desc", "$select": MESSAGE_FIELDS}
        scanned, limit = 0, max(200, max_emails * 50)  # guard against scanning a huge mailbox
        while url and scanned < limit:
            data = self._graph("GET", url, params=params).json()
            page = [self._from_graph(m) for m in data.get("value", [])]
            scanned += len(page)
            yield page
            url, params = data.get("@odata.nextLink"), None

    def _message(self, message_id: str) -> dict:
        if not self.live:
            for message in self._local_messages():
                if message["message_id"] == message_id:
                    return message
            raise ValueError(f"Unknown message: {message_id}")
        data = self._graph("GET", f"/messages/{self._mid(message_id)}", params={"$select": MESSAGE_FIELDS}).json()
        return self._from_graph(data)

    def _support_message(self, message_id: str) -> dict:
        message = self._message(message_id)
        reason = support_filter(message)
        if reason:
            raise PermissionError(f"Message {message_id} is not available to the assistant ({reason}).")
        return message

    def get_email(self, message_id: str) -> dict:
        """One support email with its full text and the names of its file attachments."""
        message = self._support_message(message_id)
        return {**{k: v for k, v in message.items() if k != "attachments"},
                "attachments": [{k: a[k] for k in ("name", "content_type", "size")}
                                for a in self._file_attachments(message)]}

    def save_attachments(self, message_id: str) -> list:
        """Download the file attachments of a support email to attachments/<message>/ and return their paths."""
        message = self._support_message(message_id)
        folder = ATTACHMENTS_DIR / re.sub(r"[^A-Za-z0-9]", "", message_id)[-16:]
        folder.mkdir(parents=True, exist_ok=True)
        saved = []
        for attachment in self._file_attachments(message):
            if self.live:
                content = self._graph("GET", f"/messages/{self._mid(message_id)}/attachments/"
                                             f"{self._mid(attachment['id'])}/$value").content
            else:
                stored = next(a for a in message["attachments"] if a["name"] == attachment["name"])
                content = base64.b64decode(stored["content_base64"])
            path = folder / _safe_name(attachment["name"])
            path.write_bytes(content)
            saved.append({"name": attachment["name"], "path": str(path.resolve()), "size": len(content),
                          "content_type": attachment["content_type"]})
        return saved

    # ---------------- drafting and sending ----------------
    def _outbox(self) -> list:
        return json.loads(OUTBOX.read_text(encoding="utf-8")) if OUTBOX.exists() else []

    def _save_outbox(self, records: list) -> None:
        OUTBOX.parent.mkdir(parents=True, exist_ok=True)
        OUTBOX.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _sendable(path: str) -> Path:
        """Only documents the notebooks generated in output/ may leave in a customer email."""
        file = Path(path).resolve()
        if not file.is_relative_to(OUTPUT_DIR.resolve()):
            raise PermissionError(f"Only documents generated in {OUTPUT_DIR}/ can be attached: {path}")
        if not file.is_file():
            raise FileNotFoundError(f"Attachment not found: {path}")
        if file.stat().st_size > MAX_SEND_ATTACHMENT_BYTES:
            raise ValueError(f"{file.name} is larger than 3 MB, the limit for a simple Graph attachment.")
        return file

    def create_reply_draft(self, message_id: str, subject: str, body: str, reply_all: bool = True,
                           attachment_paths: list | None = None) -> dict:
        """Create a reply (all) draft to a support email. Nothing is sent. Body: plain text, **bold** allowed.
        attachment_paths: generated documents from output/ (for example a Change Request) to attach."""
        message = self._support_message(message_id)
        files = [self._sendable(path) for path in attachment_paths or []]
        if self.live:
            action = "createReplyAll" if reply_all else "createReply"
            html_body = {"Prefer": 'IdType="ImmutableId"'}  # keep the quoted original as HTML
            draft = self._graph("POST", f"/messages/{self._mid(message_id)}/{action}", json={},
                                headers=html_body).json()
            quoted = (draft.get("body") or {}).get("content", "")
            new_body = to_html(body)
            match = re.search(r"<body[^>]*>", quoted, flags=re.IGNORECASE)
            content = (quoted[:match.end()] + new_body + "<br>" + quoted[match.end():]) if match \
                else new_body + "<br><hr>" + quoted
            draft = self._graph("PATCH", f"/messages/{self._mid(draft['id'])}", headers=html_body,
                                json={"subject": subject, "body": {"contentType": "HTML", "content": content}}).json()
            draft_id = draft["id"]
            for file in files:
                self._graph("POST", f"/messages/{self._mid(draft_id)}/attachments", json={
                    "@odata.type": "#microsoft.graph.fileAttachment", "name": file.name,
                    "contentBytes": base64.b64encode(file.read_bytes()).decode()})
            to = [r["emailAddress"]["address"] for r in draft.get("toRecipients", [])]
            cc = [r["emailAddress"]["address"] for r in draft.get("ccRecipients", [])]
        else:
            draft_id = f"DRAFT-{len(self._outbox()) + 1:03d}"
            to = [message["from_address"]]
            cc = list(message["cc"]) if reply_all else []  # like Graph: our own mailbox address is not copied

        record = {"draft_id": draft_id, "message_id": message_id, "mode": self.mode, "to": to, "cc": cc,
                  "subject": subject, "body": body, "attachments": [file.name for file in files],
                  "status": "draft - not sent", "created_at": _now()}
        self._save_outbox(self._outbox() + [record])
        return record

    def send_draft(self, draft_id: str) -> dict:
        """Send a draft created by create_reply_draft."""
        records = self._outbox()
        record = next((r for r in records if r["draft_id"] == draft_id), None)
        if record is None:
            raise ValueError(f"Unknown draft: {draft_id}")
        if record["status"].startswith("sent"):
            raise ValueError(f"Draft {draft_id} was already sent.")
        if self.live:
            self._graph("POST", f"/messages/{self._mid(draft_id)}/send")
            record["status"] = "sent"
        else:
            record["status"] = "sent (dry-run: nothing left this computer)"
        record["sent_at"] = _now()
        self._save_outbox(records)
        return record
