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


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

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
    # REQ-005, REQ-006, ...; skips non-numeric IDs and probes for collisions.
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


# ---------------------------------------------------------------------------
# Derived state
# ---------------------------------------------------------------------------

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


def with_derived(req: dict) -> dict:
    r = dict(req)
    r["status"] = status_of(req)
    r["currentApproverId"] = current_approver(req)
    return r


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

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

    # $1,000+ requires extra justification
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


# ---------------------------------------------------------------------------
# Approver routing
# ---------------------------------------------------------------------------

def finance_user() -> dict | None:
    return next((u for u in USERS if u["role"] == "finance"), None)


def resolve_approver(requester: dict, amount_cents: int) -> tuple[str | None, str | None]:
    """Return (approverId, error). Falls back to finance when needed."""
    finance = finance_user()

    if amount_cents < LARGE_AMOUNT_CENTS:
        manager_id = requester.get("managerId")
        # Fall back to finance if no manager or the manager IS the requester
        if manager_id and manager_id != requester["id"]:
            return manager_id, None
        # fall through to finance
    # Large amount, or fallback case
    if finance and finance["id"] != requester["id"]:
        return finance["id"], None
    return None, "No eligible approver: the finance approver cannot approve their own request."


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def current_user() -> dict | None:
    return user_by_id(request.headers.get("X-User-Id") or request.args.get("as"))


def require_user():
    u = current_user()
    if not u:
        return None, (jsonify({"error": "Unknown or missing user (X-User-Id header)."}), 401)
    return u, None


# ---------------------------------------------------------------------------
# Routes — meta
# ---------------------------------------------------------------------------

@app.get("/api/meta")
def meta():
    return jsonify({
        "expenseTypes": EXPENSE_TYPES,
        "clients": CLIENTS,
        "largeAmountCents": LARGE_AMOUNT_CENTS,
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


# ---------------------------------------------------------------------------
# Routes — requests
# ---------------------------------------------------------------------------

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
    """Owner-only, Draft-only edit of `values`."""
    user, err = require_user()
    if err:
        return err
    with _lock:
        req = request_by_id(rid)
        if not req:
            return jsonify({"error": "Not found"}), 404
        if req["requesterId"] != user["id"]:
            return jsonify({"error": "Only the requester can edit this request."}), 403
        if status_of(req) != "Draft":
            return jsonify({"error": f"Cannot edit a {status_of(req)} request."}), 409

        body = request.get_json(silent=True) or {}
        if "values" in body:
            req["values"] = _sanitize_values(body["values"])
    return jsonify(with_derived(req))


@app.post("/api/requests/<rid>/submit")
def submit_request(rid: str):
    """Validate, compute approver server-side, append 'submitted' event."""
    user, err = require_user()
    if err:
        return err
    with _lock:
        req = request_by_id(rid)
        if not req:
            return jsonify({"error": "Not found"}), 404
        if req["requesterId"] != user["id"]:
            return jsonify({"error": "Only the requester can submit."}), 403
        if status_of(req) != "Draft":
            return jsonify({"error": f"Cannot submit a {status_of(req)} request."}), 409

        errors = validate_values(req["values"])
        if errors:
            return jsonify({"errors": errors}), 400

        approver_id, routing_err = resolve_approver(user, req["values"]["amountCents"])
        if routing_err:
            return jsonify({"error": routing_err}), 409

        req["events"].append({
            "type": "submitted",
            "at": now_iso(),
            "actorId": user["id"],
            "approverId": approver_id,
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
    with _lock:
        req = request_by_id(rid)
        if not req:
            return jsonify({"error": "Not found"}), 404
        if status_of(req) != "Submitted":
            return jsonify({"error": f"Cannot {verb} a {status_of(req)} request."}), 409
        approver_id = current_approver(req)
        if approver_id != user["id"]:
            return jsonify({"error": "Only the assigned approver can act on this request."}), 403

        req["events"].append({
            "type": decision,
            "at": now_iso(),
            "actorId": user["id"],
        })
    return jsonify(with_derived(req))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    """Whitelist known fields; coerce billable to bool."""
    out = {}
    for k, v in (values or {}).items():
        if k in _ALLOWED_VALUE_KEYS:
            if k == "billable":
                out[k] = bool(v)
            else:
                out[k] = v
    # Drop conditional fields that don't apply — keeps the record clean.
    if not out.get("billable"):
        out.pop("client", None)
    if out.get("expenseType") != "Other":
        out.pop("otherReason", None)
    amt = out.get("amountCents")
    if not (isinstance(amt, int) and not isinstance(amt, bool) and amt >= LARGE_AMOUNT_CENTS):
        out.pop("additionalJustification", None)
    # Drop type-specific fields that don't apply to the chosen expense type.
    etype = out.get("expenseType")
    keep_type_keys = {f["key"] for f in TYPE_FIELDS.get(etype, [])}
    for k in _TYPE_SPECIFIC_KEYS - keep_type_keys:
        out.pop(k, None)
    return out


# ---------------------------------------------------------------------------
# Static SPA
# ---------------------------------------------------------------------------

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
