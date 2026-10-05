"""Azure DevOps Boards client.

Live mode (REST API 7.1) is used when ADO_ORG_URL and ADO_PAT are set. ADO_PROJECT is the default project for
the delivery plan; support work items go to the project of the customer's Feature.
Otherwise every call goes to a local JSON board (dry-run), so the notebook runs without credentials.
"""
import base64
import csv
import html
import json
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

import requests

API_VERSION = "7.1"
CLOSED_STATES = {"Closed", "Done", "Removed", "Resolved"}
DRY_RUN_BOARD = Path("ado_dry_run_board.json")
CUSTOMER_FEATURES = Path("customer_features.csv")  # remembered email domain -> support Feature
PRIORITY = {"High": 1, "Medium": 2, "Low": 3}


def email_domain(address: str) -> str:
    """'rana@cedarretail.example' -> 'cedarretail' (the part that names the company)."""
    return address.split("@")[-1].split(".")[0].lower() if "@" in address else address.lower()


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def name_similarity(domain: str, name: str) -> float:
    domain, name = _normalise(domain), _normalise(name)
    if not domain or not name:
        return 0.0
    if domain in name or name in domain:  # "cedarretail" in "Cedar Retail Group"
        return 1.0
    return SequenceMatcher(None, domain, name).ratio()


class AzureDevOpsClient:
    def __init__(self):
        self.org_url = os.getenv("ADO_ORG_URL", "").rstrip("/")  # e.g. https://dev.azure.com/my-org
        self.project = os.getenv("ADO_PROJECT", "")
        self.story_type = os.getenv("ADO_STORY_TYPE", "User Story")  # "Product Backlog Item" for Scrum
        self.due_date_field = os.getenv("ADO_DUE_DATE_FIELD", "Custom.DueDate1")  # empty = do not set
        pat = os.getenv("ADO_PAT", "")
        self.live = bool(self.org_url and pat)
        if self.live:
            token = base64.b64encode(f":{pat}".encode()).decode()
            self.headers = {"Authorization": f"Basic {token}"}

    @property
    def mode(self) -> str:
        return "live" if self.live else "dry-run"

    # ---------------- dry-run board ----------------
    def _load_board(self) -> dict:
        if DRY_RUN_BOARD.exists():
            return json.loads(DRY_RUN_BOARD.read_text(encoding="utf-8"))
        return {"next_id": 1000, "projects": [], "work_items": []}

    def _save_board(self, board: dict) -> None:
        DRY_RUN_BOARD.write_text(json.dumps(board, indent=2), encoding="utf-8")

    def _project(self, project: str | None) -> str:
        project = project or self.project
        if not project:
            raise ValueError("No Azure DevOps project: set ADO_PROJECT or pass a project.")
        return project

    def _api(self, path: str, project: str | None = None) -> str:
        return f"{self.org_url}/{quote(self._project(project))}/_apis/wit/{path}"

    def _org_api(self, path: str) -> str:
        return f"{self.org_url}/_apis/{path}"

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        response = requests.request(method, url, headers={**self.headers, **kwargs.pop("headers", {})},
                                    timeout=60, **kwargs)
        if not response.ok:
            raise requests.HTTPError(f"Azure DevOps {method} {url.split('?')[0]} -> {response.status_code}: "
                                     f"{response.text[:500]}", response=response)
        return response

    # ---------------- operations ----------------
    def create_work_item(self, work_item_type: str, fields: dict, parent_id: int | None = None,
                         project: str | None = None) -> dict:
        if not self.live:
            board = self._load_board()
            board["next_id"] += 1
            item = {"id": board["next_id"], "type": work_item_type, "parent_id": parent_id,
                    "fields": {"System.TeamProject": project or self.project or "dry-run", **fields}, "relations": []}
            board["work_items"].append(item)
            self._save_board(board)
            return {"id": item["id"], "type": work_item_type, "url": f"dry-run://workitems/{item['id']}"}

        patch = [{"op": "add", "path": f"/fields/{name}", "value": value} for name, value in fields.items()]
        if parent_id:
            patch.append({"op": "add", "path": "/relations/-", "value": {
                "rel": "System.LinkTypes.Hierarchy-Reverse",  # child -> parent link
                "url": self._org_api(f"wit/workItems/{parent_id}"),
            }})
        response = self._request(
            "POST", self._api(f"workitems/${quote(work_item_type)}?api-version={API_VERSION}", project),
            json=patch, headers={"Content-Type": "application/json-patch+json"},
        )
        data = response.json()
        return {"id": data["id"], "type": work_item_type, "url": data["_links"]["html"]["href"]}

    def get_work_item(self, work_item_id: int) -> dict:
        if not self.live:
            for item in self._load_board()["work_items"]:
                if item["id"] == work_item_id:
                    return item
            raise ValueError(f"Work item {work_item_id} not found")
        return self._request("GET", self._org_api(
            f"wit/workitems/{work_item_id}?$expand=relations&api-version={API_VERSION}")).json()

    def get_open_workload(self, project: str | None = None) -> dict:
        """Remaining hours of open work per assignee."""
        items = self._load_board()["work_items"] if not self.live else self._query_open_items(project)
        workload = {}
        for item in items:
            fields = item["fields"]
            person = fields.get("System.AssignedTo")
            if isinstance(person, dict):
                person = person.get("displayName")
            if person and fields.get("System.State", "New") not in CLOSED_STATES:
                hours = float(fields.get("Microsoft.VSTS.Scheduling.RemainingWork") or 0)
                workload[person] = workload.get(person, 0) + hours
        return workload

    def _query_open_items(self, project: str | None = None) -> list:
        if not (project or self.project):
            return []  # no project to look in (ADO_PROJECT not set)
        wiql = {"query": "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
                         "AND [System.State] NOT IN ('Closed', 'Done', 'Removed', 'Resolved') "
                         "AND [System.AssignedTo] <> ''"}
        ids = [w["id"] for w in self._request(
            "POST", self._api(f"wiql?api-version={API_VERSION}", project), json=wiql).json()["workItems"]]
        return [{"fields": f} for f in self._batch_fields(
            ids, ["System.AssignedTo", "System.State", "Microsoft.VSTS.Scheduling.RemainingWork"])]

    def _batch_fields(self, ids: list, fields: list) -> list:
        items = []
        for start in range(0, len(ids), 200):  # the batch API accepts 200 IDs per call
            response = self._request("POST", self._org_api(f"wit/workitemsbatch?api-version={API_VERSION}"),
                                     json={"ids": ids[start:start + 200], "fields": fields})
            items += [{**w["fields"], "System.Id": w["id"]} for w in response.json()["value"]]
        return items

    # ---------------- customers and their support Features ----------------
    def list_projects(self) -> list:
        if not self.live:
            return [{"name": name} for name in self._load_board().get("projects", [])]
        projects, url = [], self._org_api(f"projects?$top=500&api-version={API_VERSION}")
        response = self._request("GET", url)
        projects += [{"name": p["name"]} for p in response.json()["value"]]
        return projects

    def search_features(self, keyword: str = "support", project: str | None = None, top: int = 200) -> list:
        """Features whose title contains the keyword, newest first, in one project or the whole organization."""
        if not self.live:
            return [
                {"id": str(i["id"]), "title": i["fields"]["System.Title"], "project": i["fields"]["System.TeamProject"]}
                for i in reversed(self._load_board()["work_items"])
                if i["type"] == "Feature" and keyword.lower() in i["fields"]["System.Title"].lower()
                and (not project or i["fields"]["System.TeamProject"] == project)
            ][:top]
        keyword = keyword.replace("'", "''")  # WIQL string escaping
        where = "AND [System.TeamProject] = @project" if project else ""
        wiql = {"query": f"SELECT [System.Id] FROM WorkItems WHERE [System.WorkItemType] = 'Feature' "
                         f"AND [System.Title] CONTAINS '{keyword}' {where} ORDER BY [System.ChangedDate] DESC"}
        url = self._api(f"wiql?$top={top}&api-version={API_VERSION}", project) if project \
            else self._org_api(f"wit/wiql?$top={top}&api-version={API_VERSION}")
        ids = [w["id"] for w in self._request("POST", url, json=wiql).json()["workItems"]][:top]
        return [{"id": str(f["System.Id"]), "title": f.get("System.Title", ""), "project": f.get("System.TeamProject", "")}
                for f in self._batch_fields(ids, ["System.Title", "System.TeamProject"])]

    def match_customer(self, sender_email: str, threshold: float = 0.5) -> dict:
        """Find the customer's project from the sender's email domain: project names first, then Feature titles."""
        domain = email_domain(sender_email)
        best = max(((name_similarity(domain, p["name"]), p["name"]) for p in self.list_projects()), default=(0, None))
        if best[0] >= threshold:
            return {"domain": domain, "project": best[1], "score": round(best[0], 2), "matched_by": "project name"}
        best_feature = max(((name_similarity(domain, f["title"].split(" - ")[0]), f) for f in self.search_features()),
                           key=lambda pair: pair[0], default=(0, None))
        if best_feature[1] and best_feature[0] >= threshold:
            return {"domain": domain, "project": best_feature[1]["project"], "score": round(best_feature[0], 2),
                    "matched_by": f"feature '{best_feature[1]['title']}'"}
        return {"domain": domain, "project": None, "score": round(max(best[0], best_feature[0]), 2), "matched_by": "none"}

    def remembered_feature(self, domain: str) -> dict | None:
        if not CUSTOMER_FEATURES.exists():
            return None
        with CUSTOMER_FEATURES.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row["domain"].lower() == domain.lower():
                    return {"id": row["feature_id"], "title": row["title"], "project": row["project"]}
        return None

    def remember_feature(self, domain: str, feature: dict) -> None:
        rows = []
        if CUSTOMER_FEATURES.exists():
            with CUSTOMER_FEATURES.open(newline="", encoding="utf-8") as handle:
                rows = [r for r in csv.DictReader(handle) if r["domain"].lower() != domain.lower()]
        rows.append({"domain": domain, "feature_id": feature["id"], "title": feature["title"],
                     "project": feature.get("project", "")})
        with CUSTOMER_FEATURES.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["domain", "feature_id", "title", "project"])
            writer.writeheader()
            writer.writerows(rows)

    def suggest_features(self, sender_email: str) -> dict:
        """Support Features for a customer email: the remembered one first, else the matched project's features."""
        domain = email_domain(sender_email)
        remembered = self.remembered_feature(domain)
        if remembered:
            return {"domain": domain, "from_memory": True, "match": None, "default": remembered, "features": [remembered]}
        match = self.match_customer(sender_email)
        features = self.search_features(project=match["project"]) if match["project"] else []
        seen = {f["id"] for f in features}
        features += [f for f in self.search_features() if f["id"] not in seen]  # the rest of the organization
        return {"domain": domain, "from_memory": False, "match": match,
                "default": features[0] if match["project"] and features else None, "features": features}

    # ---------------- attachments ----------------
    def attach_file(self, work_item_id: int, path: str, project: str, comment: str = "") -> dict:
        """Upload a file to Azure DevOps and link it to a work item."""
        file = Path(path)
        if not file.is_file():
            raise FileNotFoundError(f"Attachment not found: {path}")
        if not self.live:
            board = self._load_board()
            item = next(i for i in board["work_items"] if i["id"] == work_item_id)
            item.setdefault("relations", []).append(
                {"rel": "AttachedFile", "url": f"dry-run://attachments/{file.name}", "attributes": {"comment": comment}})
            self._save_board(board)
            return {"name": file.name, "url": f"dry-run://attachments/{file.name}"}

        upload = self._request(
            "POST", self._api(f"attachments?fileName={quote(file.name)}&api-version={API_VERSION}", project),
            data=file.read_bytes(), headers={"Content-Type": "application/octet-stream"}).json()
        self._request(
            "PATCH", self._org_api(f"wit/workitems/{work_item_id}?api-version={API_VERSION}"),
            json=[{"op": "add", "path": "/relations/-",
                   "value": {"rel": "AttachedFile", "url": upload["url"], "attributes": {"comment": comment}}}],
            headers={"Content-Type": "application/json-patch+json"})
        return {"name": file.name, "url": upload["url"]}

    def add_link(self, work_item_id: int, url: str, comment: str) -> dict:
        """Add a hyperlink (for example the SharePoint folder of the ticket) to a work item."""
        relation = {"rel": "Hyperlink", "url": url, "attributes": {"comment": comment}}
        if not self.live:
            board = self._load_board()
            item = next(i for i in board["work_items"] if i["id"] == work_item_id)
            if all(r.get("url") != url for r in item.setdefault("relations", [])):
                item["relations"].append(relation)
            self._save_board(board)
            return {"work_item_id": work_item_id, "url": url, "status": "linked"}
        existing = self.get_work_item(work_item_id).get("relations") or []
        if any(r.get("url") == url for r in existing):
            return {"work_item_id": work_item_id, "url": url, "status": "already linked"}
        self._request("PATCH", self._org_api(f"wit/workitems/{work_item_id}?api-version={API_VERSION}"),
                      json=[{"op": "add", "path": "/relations/-", "value": relation}],
                      headers={"Content-Type": "application/json-patch+json"})
        return {"work_item_id": work_item_id, "url": url, "status": "linked"}

    def attach_documents(self, work_item_id: int, paths: list, comment: str) -> list:
        """Attach generated documents (for example a Change Request) to an existing work item."""
        fields = self.get_work_item(work_item_id).get("fields", {})
        project = fields.get("System.TeamProject") or self.project or "dry-run"
        results = []
        for path in paths:
            try:
                results.append({**self.attach_file(work_item_id, path, project, comment), "status": "attached"})
            except Exception as error:
                results.append({"name": Path(path).name, "status": f"failed: {error}"})
        return results

    # ---------------- support email -> story + small tasks ----------------
    def create_support_items(self, package: dict) -> dict:
        """One User Story under the customer's support Feature, its small tasks as children (Activity, estimate,
        due date), and the email's attachments on the story. Recommended owners go in the task descriptions."""
        esc = html.escape
        feature, ticket, tasks = package["feature"], package["ticket"], package["tasks"]
        project = feature.get("project") or self.project or "dry-run"
        total_hours = sum(t["estimate_hours"] for t in tasks)
        steps = "".join(f"<li>{esc(s)}</li>" for s in ticket["steps_to_reproduce"])
        story = self.create_work_item(self.story_type, {
            "System.Title": ticket["title"],
            "System.Description": (
                f"<p>{esc(ticket['description']).replace(chr(10), '<br>')}</p>"
                + (f"<p><b>Steps to reproduce:</b></p><ol>{steps}</ol>" if steps else "")
                + f"<p><b>Customer:</b> {esc(package['customer'])} | <b>Email:</b> {esc(package['email_subject'])}</p>"
            ),
            "Microsoft.VSTS.Common.AcceptanceCriteria":
                "<ul>" + "".join(f"<li>{esc(c)}</li>" for c in ticket["acceptance_criteria"]) + "</ul>",
            "Microsoft.VSTS.Common.Priority": PRIORITY.get(ticket["priority"], 2),
            "Microsoft.VSTS.Common.Activity": package.get("story_activity", "Design"),
            "Microsoft.VSTS.Scheduling.StoryPoints": total_hours,
            "Microsoft.VSTS.Scheduling.OriginalEstimate": total_hours,
            "System.Tags": "; ".join(["Support", ticket["category"], ticket["priority"], ticket["module"]]
                                     + (["Change Request"] if package.get("cr_required") else [])),
        }, parent_id=int(feature["id"]), project=project)

        created_tasks = []
        for task in tasks:
            fields = {
                "System.Title": task["title"],
                "System.Description": (
                    f"<p>{esc(task['description'])}</p>"
                    f"<p><b>Recommended owner (to be confirmed):</b> {esc(str(task.get('owner')))} - "
                    f"{esc(task.get('owner_reason', ''))}</p>"
                ),
                "Microsoft.VSTS.Common.Activity": task["activity"],
                "Microsoft.VSTS.Scheduling.OriginalEstimate": task["estimate_hours"],
                "Microsoft.VSTS.Scheduling.RemainingWork": task["estimate_hours"],
                "System.Tags": "; ".join(["Support", task["kind"]]),
            }
            if self.due_date_field and task.get("due_date"):
                fields[self.due_date_field] = f"{task['due_date']}T12:00:00Z"
            item = self.create_work_item("Task", fields, parent_id=story["id"], project=project)
            created_tasks.append({**item, "title": task["title"], "activity": task["activity"],
                                  "estimate_hours": task["estimate_hours"], "due_date": task.get("due_date")})

        attachments = []
        for path in package.get("attachment_paths", []):
            try:
                attachments.append({**self.attach_file(story["id"], path, project,
                                                       f"From the customer email: {package['email_subject']}"),
                                    "status": "attached"})
            except Exception as error:  # report every failed file instead of hiding it
                attachments.append({"name": Path(path).name, "status": f"failed: {error}"})

        self.remember_feature(package["domain"], feature)
        return {"mode": self.mode, "project": project, "story": {**story, "title": ticket["title"]},
                "tasks": created_tasks, "attachments": attachments}

    # ---------------- delivery plan -> linked work items ----------------
    def create_delivery_items(self, request_id: str, module: str, work_type: str,
                              plan: dict, owner_recommendations: dict) -> list:
        """Create one User Story per planned story and link its FUNC/TECH tasks as children.
        Recommended owners are written into the description; System.AssignedTo is left empty."""
        esc = html.escape
        created = []
        for story in plan["user_stories"]:
            criteria = "".join(
                f"<li><b>{esc(ac['ac_id'])}</b> ({esc(', '.join(ac['req_ids']))}): Given {esc(ac['given'])}, "
                f"when {esc(ac['when'])}, then {esc(ac['then'])}</li>"
                for ac in story["acceptance_criteria"]
            )
            story_item = self.create_work_item(self.story_type, {
                "System.Title": story["title"],
                "System.Description": (
                    f"<p>As a {esc(story['as_a'])}, I want {esc(story['i_want'])} so that {esc(story['so_that'])}.</p>"
                    f"<p>Request: {esc(request_id)} | Requirements: {esc(', '.join(story['req_ids']))}</p>"
                ),
                "Microsoft.VSTS.Common.AcceptanceCriteria": f"<ul>{criteria}</ul>",
                "System.Tags": "; ".join([module, work_type, request_id, *story["req_ids"]]),
            })
            created.append({**story_item, "ref": story["story_id"], "title": story["title"]})

            for task in story["tasks"]:
                owner = owner_recommendations.get(task["task_id"], {})
                task_item = self.create_work_item("Task", {
                    "System.Title": task["title"],
                    "System.Description": (
                        f"<p>{esc(task['description'])}</p>"
                        f"<p>Requirements: {esc(', '.join(task['req_ids']))}</p>"
                        f"<p><b>Recommended owner (to be confirmed):</b> {esc(str(owner.get('owner')))} - "
                        f"{esc(owner.get('reason', ''))}</p>"
                    ),
                    "Microsoft.VSTS.Scheduling.RemainingWork": task["estimate_hours"],
                    "System.Tags": "; ".join([module, task["kind"], request_id, *task["req_ids"]]),
                }, parent_id=story_item["id"])
                created.append({**task_item, "ref": task["task_id"], "title": task["title"],
                                "parent_id": story_item["id"]})
        return created
