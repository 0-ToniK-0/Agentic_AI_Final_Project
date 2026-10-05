"""Approval tool: every human decision is recorded so that MCP tools can verify approval before acting."""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

LEDGER = Path("approvals.json")


def _load() -> list:
    return json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else []


def record_decision(artefact_type: str, artefact_ref: str, approver: str, decision: str, comments: str = "") -> dict:
    record = {
        "approval_id": f"APR-{uuid.uuid4().hex[:8].upper()}",
        "artefact_type": artefact_type,
        "artefact_ref": artefact_ref,
        "approver": approver,
        "decision": decision,
        "comments": comments,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    records = _load()
    records.append(record)
    LEDGER.write_text(json.dumps(records, indent=2), encoding="utf-8")
    return record


def is_approved(approval_id: str, artefact_type: str, artefact_ref: str | None = None) -> bool:
    """True when a person approved this artefact type (and, if given, this exact artefact)."""
    return any(
        r["approval_id"] == approval_id and r["artefact_type"] == artefact_type and r["decision"] == "approved"
        and (artefact_ref is None or r["artefact_ref"] == artefact_ref)
        for r in _load()
    )
