"""SharePoint document filing through Microsoft Graph (live) or a local folder (dry-run).

Documents are filed as  <SHAREPOINT_ROOT_FOLDER>/<Customer>/<ID - Description>/<Document type>/<file>
in the library SHAREPOINT_LIBRARY of the site SHAREPOINT_SITE_URL.

Live mode needs SHAREPOINT_SITE_URL and Graph access (the app registration of notebook 03):
- Delegated (default): MailClient-style device-code sign-in with Sites.ReadWrite.All, cached in
  .graph_token_cache.json. Run SharePointClient().sign_in() once in the notebook.
- App-only: GRAPH_CLIENT_SECRET with the Sites.Selected permission, granted by an admin on this site only.
Only files generated in output/ or downloaded from a support email in attachments/ can be uploaded.
"""
import os
import re
import shutil
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

GRAPH_URL = os.getenv("GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0")
SCOPES = ["Sites.ReadWrite.All"]
TOKEN_CACHE = Path(".graph_token_cache.json")
DRY_RUN_DIR = Path("sharepoint_dry_run")
UPLOADABLE_DIRS = (Path("output"), Path("attachments"))
DOC_TYPES = ("Change Request", "Design", "Test", "Customer Files")
MAX_SIMPLE_UPLOAD_BYTES = 250 * 1024 * 1024


def safe_segment(text: str, max_length: int = 60) -> str:
    """One folder name SharePoint accepts: no " * : < > ? / \\ | # %, no leading/trailing dots or spaces."""
    text = re.sub(r'["*:<>?/\\|#%\x00-\x1f]', " ", str(text))
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) > max_length:  # cut at a word, and drop a dangling "and", "the", ... at the end
        text = text[:max_length].rsplit(" ", 1)[0]
        clause = text.lower().rfind(" and ")
        if clause > 20:  # end on a complete phrase: "Fix posting and add the customer's" -> "Fix posting"
            text = text[:clause]
        text = re.sub(r"(\s+(and|or|the|a|an|to|of|for|in|on|with|&|-))+$", "", text, flags=re.IGNORECASE)
    return text.rstrip(" .,-") or "Unnamed"


def ticket_folder(customer: str, item_id, description: str, doc_type: str | None = None) -> list:
    """['Customer', 'ID - Description', 'Document type'] below the root folder."""
    description = re.sub(rf"^{re.escape(customer)}\s*-\s*", "", description.strip(), flags=re.IGNORECASE)
    parts = [safe_segment(customer), safe_segment(f"{item_id} - {description}")]
    if doc_type:
        if doc_type not in DOC_TYPES:
            raise ValueError(f"Unknown document type {doc_type!r}: use one of {DOC_TYPES}")
        parts.append(doc_type)
    return parts


class SharePointClient:
    def __init__(self):
        self.site_url = os.getenv("SHAREPOINT_SITE_URL", "").rstrip("/")
        self.library = os.getenv("SHAREPOINT_LIBRARY", "Documents")
        self.root = [safe_segment(p) for p in os.getenv("SHAREPOINT_ROOT_FOLDER", "Support").split("/") if p.strip()]
        self.client_id = os.getenv("GRAPH_CLIENT_ID", "")
        self.tenant_id = os.getenv("GRAPH_TENANT_ID", "")
        self.secret = os.getenv("GRAPH_CLIENT_SECRET", "")
        self.static_token = os.getenv("GRAPH_ACCESS_TOKEN", "")
        self.live = bool(self.site_url and (self.static_token or (self.client_id and self.tenant_id)))
        self._drive_id = None

    @property
    def mode(self) -> str:
        return "live" if self.live else "dry-run"

    # ---------------- authentication (same app and token cache as the mail client) ----------------
    def _public_app(self):
        import msal

        cache = msal.SerializableTokenCache()
        if TOKEN_CACHE.exists():
            cache.deserialize(TOKEN_CACHE.read_text(encoding="utf-8"))
        app = msal.PublicClientApplication(
            self.client_id, authority=f"https://login.microsoftonline.com/{self.tenant_id}", token_cache=cache)
        return app, cache

    def sign_in(self) -> str:
        """Delegated sign-in with a device code for SharePoint access. Cached for later runs."""
        if not self.live or self.static_token or self.secret:
            return f"No interactive sign-in needed ({self.mode}{', app-only' if self.secret else ''})."
        app, cache = self._public_app()
        accounts = app.get_accounts()
        if accounts and app.acquire_token_silent(SCOPES, account=accounts[0]):
            return f"SharePoint access ready for {accounts[0]['username']}"
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Could not start the device sign-in: {flow.get('error_description', flow)}")
        print(flow["message"], flush=True)
        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(f"Sign-in failed: {result.get('error_description', result)}")
        if cache.has_state_changed:
            TOKEN_CACHE.write_text(cache.serialize(), encoding="utf-8")
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
                raise PermissionError("No SharePoint access yet: run SharePointClient().sign_in() in the notebook.")
            if cache.has_state_changed:
                TOKEN_CACHE.write_text(cache.serialize(), encoding="utf-8")
        if "access_token" not in result:
            raise PermissionError(f"Could not get a Graph token: {result.get('error_description', result)}")
        return result["access_token"]

    def _graph(self, method: str, path: str, ok=(), **kwargs) -> requests.Response:
        response = requests.request(method, f"{GRAPH_URL}{path}", timeout=120,
                                    headers={"Authorization": f"Bearer {self._token()}", **kwargs.pop("headers", {})},
                                    **kwargs)
        if not response.ok and response.status_code not in ok:
            raise requests.HTTPError(f"Graph {method} {path} -> {response.status_code}: {response.text[:400]}",
                                     response=response)
        return response

    # ---------------- site, library and folders ----------------
    def drive_id(self) -> str:
        """The document library of the site (matched by name, e.g. 'Documents' = 'Shared Documents')."""
        if self._drive_id:
            return self._drive_id
        url = urlparse(self.site_url)
        site = self._graph("GET", f"/sites/{url.hostname}:{quote(url.path)}").json()
        drives = self._graph("GET", f"/sites/{site['id']}/drives").json()["value"]
        wanted = self.library.lower()
        for drive in drives:
            names = {drive.get("name", "").lower(), drive.get("webUrl", "").rstrip("/").rsplit("/", 1)[-1].replace("%20", " ").lower()}
            if wanted in names or (wanted == "documents" and "shared documents" in names):
                self._drive_id = drive["id"]
                return self._drive_id
        raise ValueError(f"No library named {self.library!r} on {self.site_url}: {[d.get('name') for d in drives]}")

    @staticmethod
    def _path(parts: list) -> str:
        return "/".join(quote(part, safe="") for part in parts)

    def ensure_folder(self, parts: list) -> dict:
        """Create the folders below the root that do not exist yet; return the last folder."""
        full = self.root + [safe_segment(p, 120) for p in parts]
        if not self.live:
            folder = DRY_RUN_DIR.joinpath(*full)
            folder.mkdir(parents=True, exist_ok=True)
            return {"path": "/".join(full), "web_url": folder.resolve().as_uri()}

        drive = self.drive_id()
        item = None
        for depth in range(1, len(full) + 1):
            found = self._graph("GET", f"/drives/{drive}/root:/{self._path(full[:depth])}", ok=(404,))
            if found.status_code != 404:
                item = found.json()
                continue
            parent = f"/drives/{drive}/root" + (f":/{self._path(full[:depth - 1])}:" if depth > 1 else "")
            created = self._graph("POST", f"{parent}/children", ok=(409,), json={
                "name": full[depth - 1], "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
            item = created.json() if created.status_code != 409 else \
                self._graph("GET", f"/drives/{drive}/root:/{self._path(full[:depth])}").json()  # created meanwhile
        return {"path": "/".join(full), "web_url": item["webUrl"]}

    @staticmethod
    def _uploadable(path: str) -> Path:
        file = Path(path).resolve()
        if not any(file.is_relative_to(folder.resolve()) for folder in UPLOADABLE_DIRS):
            raise PermissionError(f"Only files from output/ or attachments/ can be uploaded: {path}")
        if not file.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        if file.stat().st_size > MAX_SIMPLE_UPLOAD_BYTES:
            raise ValueError(f"{file.name} is larger than 250 MB.")
        return file

    def upload(self, parts: list, path: str) -> dict:
        """Upload one file into the folder (a new version when the file already exists)."""
        file = self._uploadable(path)
        folder = self.ensure_folder(parts)
        if not self.live:
            target = DRY_RUN_DIR.joinpath(*folder["path"].split("/"), file.name)
            shutil.copy2(file, target)
            return {"name": file.name, "size": file.stat().st_size, "web_url": target.resolve().as_uri()}
        item = self._graph(
            "PUT", f"/drives/{self.drive_id()}/root:/{self._path(folder['path'].split('/') + [file.name])}:/content",
            data=file.read_bytes(), headers={"Content-Type": "application/octet-stream"}).json()
        return {"name": file.name, "size": item.get("size"), "web_url": item["webUrl"]}

    def file_documents(self, customer: str, item_id, description: str, doc_type: str, paths: list) -> dict:
        """File documents as <root>/<Customer>/<ID - Description>/<Document type>/ and report every file."""
        parts = ticket_folder(customer, item_id, description, doc_type)  # validates the type before creating folders
        ticket = self.ensure_folder(parts[:-1])
        files = []
        for path in paths:
            try:
                files.append({**self.upload(parts, path), "status": "uploaded"})
            except Exception as error:  # report every failed file instead of hiding it
                files.append({"name": Path(path).name, "status": f"failed: {error}"})
        return {"mode": self.mode, "ticket_folder": ticket, "folder": "/".join(self.root + parts), "files": files}
