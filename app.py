"""Expense-requests server.

Single-file Flask app: in-memory store seeded from data/*.json, JSON API,
plus a static SPA at /. Auth is a trusted `X-User-Id` header (spec allows this).

Status is derived from the latest event; the client never sets status/requester/approver.
All validation and permission rules are enforced here — the SPA is a convenience.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"

# Hardcoded client list — spec allows this; Acme must be present for seed data.
CLIENTS = ["Acme", "Globex", "Initech", "Umbrella", "Wayne Enterprises"]
EXPENSE_TYPES = ["Travel", "Software", "Equipment", "Meal", "Other"]
LARGE_AMOUNT_CENTS = 100_000  # $1,000 threshold for finance routing / extra justification
VERY_LARGE_AMOUNT_CENTS = 500_000  # $5,000 threshold for multi-step approval (manager + finance)

# Per-expense-type extra fields. Rendered by the SPA, validated on the server.
# Keeping the schema in one place so the two sides can't drift.
TYPE_FIELDS: dict[str, list[dict]] = {
    "Travel": [
        {"key": "destination", "label": "Destination", "type": "text"},
        {"key": "departDate", "label": "Departure date", "type": "date"},
        {"key": "returnDate", "label": "Return date", "type": "date"},
    ],
    "Software": [
        {"key": "vendor", "label": "Vendor", "type": "text"},
        {"key": "softwareReason", "label": "Reason for this software", "type": "textarea"},
    ],
}

app = Flask(__name__, static_folder=None)
_lock = threading.Lock()


def _load_json(name: str):
    with open(DATA_DIR / name) as f:
        return json.load(f)


USERS: list[dict] = _load_json("users.json")
REQUESTS: list[dict] = _load_json("requests.json")


def reset_store() -> None:
    """Re-seed USERS and REQUESTS from data/*.json. Used by tests."""
    global USERS, REQUESTS
    USERS[:] = _load_json("users.json")
    REQUESTS[:] = _load_json("requests.json")


def user_by_id(uid: str | None) -> dict | None:
    if not uid:
        return None
    return next((u for u in USERS if u["id"] == uid), None)


def request_by_id(rid: str) -> dict | None:
    return next((r for r in REQUESTS if r["id"] == rid), None)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def next_request_id() -> str:
    existing = {r["id"] for r in REQUESTS}
    nums = []
    for rid in existing:
        try:
            nums.append(int(rid.split("-")[1]))
        except (IndexError, ValueError):
            pass
    n = (max(nums) + 1) if nums else 1
    candidate = f"REQ-{n:03d}"
    while candidate in existing:
        n += 1
        candidate = f"REQ-{n:03d}"
    return candidate


def status_of(req: dict) -> str:
    """Status is the latest event's type, mapped to a status label."""
    if not req["events"]:
        return "Draft"
    last = req["events"][-1]["type"]
    return {
        "created": "Draft",
        "submitted": "Submitted",
        "approved": "Approved",
        "rejected": "Rejected",
    }.get(last, "Draft")


def current_approver(req: dict) -> str | None:
    """Approver from the most recent 'submitted' event, if still pending."""
    if status_of(req) != "Submitted":
        return None
    for ev in reversed(req["events"]):
        if ev["type"] == "submitted":
            return ev.get("approverId")
    return None


def last_submitted_event(req: dict) -> dict | None:
    for ev in reversed(req["events"]):
        if ev["type"] == "submitted":
            return ev
    return None


def with_derived(req: dict) -> dict:
    r = dict(req)
    r["status"] = status_of(req)
    r["currentApproverId"] = current_approver(req)
    return r


def validate_values(values: dict) -> dict[str, str]:
    """Return {field: message} for each rule violation. Empty dict = valid."""
    errors: dict[str, str] = {}
    v = values or {}

    etype = v.get("expenseType")
    if not etype:
        errors["expenseType"] = "Expense type is required."
    elif etype not in EXPENSE_TYPES:
        errors["expenseType"] = "Not a valid expense type."

    amount = v.get("amountCents")
    if amount is None or amount == "":
        errors["amountCents"] = "Amount is required."
    elif not isinstance(amount, int) or isinstance(amount, bool):
        errors["amountCents"] = "Amount must be a whole number of cents."
    elif amount < 0:
        errors["amountCents"] = "Amount cannot be negative."

    desc = v.get("description")
    if not desc or not str(desc).strip():
        errors["description"] = "Description is required."

    if v.get("billable"):
        client = v.get("client")
        if not client:
            errors["client"] = "Client is required when billable."
        elif client not in CLIENTS:
            errors["client"] = "Not a valid client."

    if isinstance(amount, int) and not isinstance(amount, bool) and amount >= LARGE_AMOUNT_CENTS:
        if not v.get("additionalJustification") or not str(v["additionalJustification"]).strip():
            errors["additionalJustification"] = "Extra justification is required for $1,000 or more."

    if etype == "Other":
        if not v.get("otherReason") or not str(v["otherReason"]).strip():
            errors["otherReason"] = "Please describe why this is 'Other'."

    if etype == "Travel":
        if not v.get("destination") or not str(v["destination"]).strip():
            errors["destination"] = "Destination is required for travel."
        depart = v.get("departDate")
        ret = v.get("returnDate")
        if not depart:
            errors["departDate"] = "Departure date is required for travel."
        elif not _iso_date(depart):
            errors["departDate"] = "Departure date must be YYYY-MM-DD."
        if not ret:
            errors["returnDate"] = "Return date is required for travel."
        elif not _iso_date(ret):
            errors["returnDate"] = "Return date must be YYYY-MM-DD."
        if _iso_date(depart) and _iso_date(ret) and ret < depart:
            errors["returnDate"] = "Return date cannot be before departure date."

    if etype == "Software":
        if not v.get("vendor") or not str(v["vendor"]).strip():
            errors["vendor"] = "Vendor is required for software."
        if not v.get("softwareReason") or not str(v["softwareReason"]).strip():
            errors["softwareReason"] = "Please explain why this software is needed."

    return errors


def finance_user() -> dict | None:
    return next((u for u in USERS if u["role"] == "finance"), None)


def approval_chain(requester: dict, amount_cents: int) -> tuple[list[str], str | None]:
    """Return (chain, error). Chain is the ordered list of approver IDs.

    Routing rules:
      < $1,000  → manager (or finance fallback)
      ≥ $1,000  → finance only
      ≥ $5,000  → manager THEN finance (two-step)

    An approver who is the requester is skipped; if the chain would end up
    empty, an error is returned instead.
    """
    finance = finance_user()
    manager_id = requester.get("managerId")
    manager_valid = bool(manager_id) and manager_id != requester["id"]
    finance_valid = bool(finance) and finance["id"] != requester["id"]

    if amount_cents >= VERY_LARGE_AMOUNT_CENTS:
        chain: list[str] = []
        if manager_valid:
            chain.append(manager_id)
        if finance_valid and (not chain or chain[-1] != finance["id"]):
            chain.append(finance["id"])
        if not chain:
            return [], "No eligible approver for a very large amount."
        return chain, None

    if amount_cents < LARGE_AMOUNT_CENTS and manager_valid:
        return [manager_id], None

    if finance_valid:
        return [finance["id"]], None
    return [], "No eligible approver: the finance approver cannot approve their own request."


def current_user() -> dict | None:
    return user_by_id(request.headers.get("X-User-Id") or request.args.get("as"))


def require_user():
    u = current_user()
    if not u:
        return None, (jsonify({"error": "Unknown or missing user (X-User-Id header)."}), 401)
    return u, None


@app.get("/api/meta")
def meta():
    return jsonify({
        "expenseTypes": EXPENSE_TYPES,
        "clients": CLIENTS,
        "largeAmountCents": LARGE_AMOUNT_CENTS,
        "veryLargeAmountCents": VERY_LARGE_AMOUNT_CENTS,
        "typeFields": TYPE_FIELDS,
    })


@app.get("/api/users")
def list_users():
    return jsonify(USERS)


@app.get("/api/me")
def me():
    u = current_user()
    if not u:
        return jsonify({"error": "Unknown user"}), 401
    return jsonify(u)


@app.post("/api/_test/reset")
def _test_reset():
    """Reset the in-memory store to seed data. Only enabled when EXPENSE_TEST_MODE=1."""
    if os.environ.get("EXPENSE_TEST_MODE") != "1":
        return jsonify({"error": "Not found"}), 404
    reset_store()
    return jsonify({"ok": True})


@app.get("/api/requests")
def list_requests():
    """List all requests with derived fields. Client can filter in the UI."""
    return jsonify([with_derived(r) for r in REQUESTS])


@app.get("/api/requests/<rid>")
def get_request(rid: str):
    r = request_by_id(rid)
    if not r:
        return jsonify({"error": "Not found"}), 404
    return jsonify(with_derived(r))


@app.post("/api/requests")
def create_request():
    """Create a Draft. Values may be incomplete — validation only runs on submit."""
    user, err = require_user()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    values = _sanitize_values(body.get("values") or {})

    with _lock:
        new_req = {
            "id": next_request_id(),
            "requesterId": user["id"],
            "values": values,
            "events": [
                {"type": "created", "at": now_iso(), "actorId": user["id"]}
            ],
        }
        REQUESTS.append(new_req)
    return jsonify(with_derived(new_req)), 201


@app.patch("/api/requests/<rid>")
def update_request(rid: str):
    """Owner-only edit of `values`. Allowed while Draft or after Rejection (fix-and-resubmit)."""
    user, err = require_user()
    if err:
        return err
    with _lock:
        req = request_by_id(rid)
        if not req:
            return jsonify({"error": "Not found"}), 404
        if req["requesterId"] != user["id"]:
            return jsonify({"error": "Only the requester can edit this request."}), 403
        if status_of(req) not in ("Draft", "Rejected"):
            return jsonify({"error": f"Cannot edit a {status_of(req)} request."}), 409

        body = request.get_json(silent=True) or {}
        if "values" in body:
            req["values"] = _sanitize_values(body["values"])
    return jsonify(with_derived(req))


@app.post("/api/requests/<rid>/submit")
def submit_request(rid: str):
    """Validate, compute approver server-side, append 'submitted' event.

    Allowed on Draft (first submit) and on Rejected (fix-and-resubmit). The
    resubmit path keeps the full event history — including the rejection —
    so the audit trail is intact and the approver is re-routed by amount.
    """
    user, err = require_user()
    if err:
        return err
    with _lock:
        req = request_by_id(rid)
        if not req:
            return jsonify({"error": "Not found"}), 404
        if req["requesterId"] != user["id"]:
            return jsonify({"error": "Only the requester can submit."}), 403
        if status_of(req) not in ("Draft", "Rejected"):
            return jsonify({"error": f"Cannot submit a {status_of(req)} request."}), 409

        errors = validate_values(req["values"])
        if errors:
            return jsonify({"errors": errors}), 400

        chain, routing_err = approval_chain(user, req["values"]["amountCents"])
        if routing_err:
            return jsonify({"error": routing_err}), 409

        req["events"].append({
            "type": "submitted",
            "at": now_iso(),
            "actorId": user["id"],
            "approverId": chain[0],
            "approverChain": chain,
            "chainIndex": 0,
        })
    return jsonify(with_derived(req))


# Event type ↔ verb, kept together so error messages stay grammatical.
_DECISION_VERBS = {"approved": "approve", "rejected": "reject"}


@app.post("/api/requests/<rid>/approve")
def approve_request(rid: str):
    return _decide(rid, "approved")


@app.post("/api/requests/<rid>/reject")
def reject_request(rid: str):
    return _decide(rid, "rejected")


def _decide(rid: str, decision: str):
    user, err = require_user()
    if err:
        return err
    verb = _DECISION_VERBS[decision]
    body = request.get_json(silent=True) or {}
    raw_comment = body.get("comment")
    comment = str(raw_comment).strip() if isinstance(raw_comment, str) else None
    with _lock:
        req = request_by_id(rid)
        if not req:
            return jsonify({"error": "Not found"}), 404
        if status_of(req) != "Submitted":
            return jsonify({"error": f"Cannot {verb} a {status_of(req)} request."}), 409
        approver_id = current_approver(req)
        if approver_id != user["id"]:
            return jsonify({"error": "Only the assigned approver can act on this request."}), 403

        event = {
            "type": decision,
            "at": now_iso(),
            "actorId": user["id"],
        }
        if comment:
            event["comment"] = comment
        req["events"].append(event)

        # Multi-step chains: if this approval leaves more steps in the chain,
        # append a fresh 'submitted' event routing to the next approver so
        # status stays "Submitted" and current_approver() picks up the next hop.
        if decision == "approved":
            last_submit = last_submitted_event(req)
            chain = (last_submit or {}).get("approverChain") or []
            idx = (last_submit or {}).get("chainIndex", 0)
            if chain and idx + 1 < len(chain):
                req["events"].append({
                    "type": "submitted",
                    "at": now_iso(),
                    "actorId": user["id"],
                    "approverId": chain[idx + 1],
                    "approverChain": chain,
                    "chainIndex": idx + 1,
                })
    return jsonify(with_derived(req))


_ALLOWED_VALUE_KEYS = {
    "expenseType", "amountCents", "description",
    "billable", "client", "additionalJustification", "otherReason",
    # Type-specific fields (see TYPE_FIELDS)
    "destination", "departDate", "returnDate",  # Travel
    "vendor", "softwareReason",                 # Software
}


_TYPE_SPECIFIC_KEYS = {k for fields in TYPE_FIELDS.values() for k in (f["key"] for f in fields)}


def _iso_date(s) -> bool:
    if not isinstance(s, str):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _sanitize_values(values: dict) -> dict:
    """Whitelist known fields; coerce billable to bool. Drops conditional fields whose gates are off."""
    out = {}
    for k, v in (values or {}).items():
        if k in _ALLOWED_VALUE_KEYS:
            if k == "billable":
                out[k] = bool(v)
            else:
                out[k] = v
    if not out.get("billable"):
        out.pop("client", None)
    if out.get("expenseType") != "Other":
        out.pop("otherReason", None)
    amt = out.get("amountCents")
    if not (isinstance(amt, int) and not isinstance(amt, bool) and amt >= LARGE_AMOUNT_CENTS):
        out.pop("additionalJustification", None)
    etype = out.get("expenseType")
    keep_type_keys = {f["key"] for f in TYPE_FIELDS.get(etype, [])}
    for k in _TYPE_SPECIFIC_KEYS - keep_type_keys:
        out.pop(k, None)
    return out


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/<path:_unused>")
def api_not_found(_unused: str):
    # Prevent the SPA catch-all below from serving HTML for unknown API paths.
    return jsonify({"error": "Not found"}), 404


@app.get("/<path:path>")
def static_files(path: str):
    return send_from_directory(STATIC_DIR, path)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="127.0.0.1", port=port, debug=debug)
