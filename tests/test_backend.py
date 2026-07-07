"""Backend tests.

Each test uses Flask's `test_client` against the in-memory store. The `client`
fixture resets the store before every test so ordering doesn't matter.

The suite mirrors the spec's rules one-to-one: field validation, conditional
required fields, server-side approver routing, permission checks, status
derivation from events, sanitizer behavior, and that the client cannot forge
protected fields.
"""

from __future__ import annotations

import pytest

import app as app_module


@pytest.fixture()
def client():
    app_module.reset_store()
    app_module.app.testing = True
    with app_module.app.test_client() as c:
        yield c


def as_(user_id: str) -> dict:
    return {"X-User-Id": user_id}


def create_draft(client, user: str, values: dict) -> dict:
    r = client.post("/api/requests", json={"values": values}, headers=as_(user))
    assert r.status_code == 201, r.get_json()
    return r.get_json()


# ---------------------------------------------------------------------------
# Meta and identity
# ---------------------------------------------------------------------------

def test_meta_lists_types_clients_threshold(client):
    body = client.get("/api/meta").get_json()
    assert body["expenseTypes"] == ["Travel", "Software", "Equipment", "Meal", "Other"]
    assert "Acme" in body["clients"]
    assert body["largeAmountCents"] == 100_000


def test_users_seeded(client):
    users = client.get("/api/users").get_json()
    ids = {u["id"] for u in users}
    assert ids == {"u_alice", "u_bob", "u_carol", "u_mallory", "u_peggy", "u_trent"}


def test_unknown_user_is_401(client):
    r = client.post("/api/requests", json={"values": {}}, headers=as_("u_bogus"))
    assert r.status_code == 401


def test_missing_user_header_is_401(client):
    r = client.post("/api/requests", json={"values": {}})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Listing + derived status from seed data
# ---------------------------------------------------------------------------

def test_list_returns_derived_status_for_seed_data(client):
    rs = {r["id"]: r for r in client.get("/api/requests").get_json()}
    assert rs["REQ-001"]["status"] == "Draft"
    assert rs["REQ-001"]["currentApproverId"] is None
    assert rs["REQ-002"]["status"] == "Submitted"
    assert rs["REQ-002"]["currentApproverId"] == "u_carol"
    assert rs["REQ-003"]["status"] == "Approved"
    assert rs["REQ-003"]["currentApproverId"] is None
    assert rs["REQ-004"]["status"] == "Draft"


def test_get_request_by_id(client):
    r = client.get("/api/requests/REQ-002").get_json()
    assert r["status"] == "Submitted"
    assert r["currentApproverId"] == "u_carol"


def test_get_missing_request_is_404(client):
    r = client.get("/api/requests/REQ-999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Create + edit permissions
# ---------------------------------------------------------------------------

def test_create_draft_owner_is_header_user(client):
    r = create_draft(client, "u_alice", {"expenseType": "Meal", "amountCents": 100})
    assert r["requesterId"] == "u_alice"
    assert r["status"] == "Draft"
    assert [e["type"] for e in r["events"]] == ["created"]


def test_create_ignores_client_supplied_status_and_events(client):
    """Client cannot forge status, requester, or event history on create."""
    r = client.post(
        "/api/requests",
        json={
            "values": {"expenseType": "Meal", "amountCents": 100, "description": "x"},
            "requesterId": "u_bob",
            "status": "Approved",
            "events": [{"type": "approved", "actorId": "u_carol"}],
        },
        headers=as_("u_alice"),
    ).get_json()
    assert r["requesterId"] == "u_alice"
    assert r["status"] == "Draft"
    assert [e["type"] for e in r["events"]] == ["created"]


def test_only_owner_can_edit_draft(client):
    draft = create_draft(client, "u_alice", {"description": "mine"})
    r = client.patch(
        f"/api/requests/{draft['id']}",
        json={"values": {"description": "hax"}},
        headers=as_("u_bob"),
    )
    assert r.status_code == 403


def test_edit_returns_updated_values(client):
    draft = create_draft(client, "u_alice", {"description": "v1"})
    r = client.patch(
        f"/api/requests/{draft['id']}",
        json={"values": {"expenseType": "Meal", "amountCents": 500, "description": "v2"}},
        headers=as_("u_alice"),
    ).get_json()
    assert r["values"]["description"] == "v2"
    assert r["values"]["amountCents"] == 500


def test_cannot_edit_submitted_request(client):
    """Editing a submitted request returns 409, not silent success."""
    r = client.patch(
        "/api/requests/REQ-002",
        json={"values": {"description": "hax"}},
        headers=as_("u_alice"),
    )
    assert r.status_code == 409


def test_cannot_edit_approved_request(client):
    r = client.patch(
        "/api/requests/REQ-003",
        json={"values": {"description": "hax"}},
        headers=as_("u_bob"),
    )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_submit_empty_draft_returns_all_missing_field_errors(client):
    draft = create_draft(client, "u_alice", {})
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_alice"))
    assert r.status_code == 400
    errors = r.get_json()["errors"]
    assert set(errors) == {"expenseType", "amountCents", "description"}


def test_submit_negative_amount_is_400(client):
    draft = create_draft(client, "u_alice", {
        "expenseType": "Meal", "amountCents": -1, "description": "x",
    })
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_alice"))
    assert r.status_code == 400
    assert "amountCents" in r.get_json()["errors"]


def test_submit_float_amount_is_400(client):
    """Amounts must be whole cents — a dollar float is rejected server-side."""
    draft = create_draft(client, "u_alice", {
        "expenseType": "Meal", "amountCents": 12.5, "description": "x",
    })
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_alice"))
    assert r.status_code == 400
    assert "amountCents" in r.get_json()["errors"]


def test_billable_requires_client(client):
    draft = create_draft(client, "u_alice", {
        "expenseType": "Travel", "amountCents": 500, "description": "trip",
        "billable": True,
        "destination": "NYC", "departDate": "2026-08-01", "returnDate": "2026-08-03",
    })
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_alice"))
    assert r.status_code == 400
    assert "client" in r.get_json()["errors"]


def test_billable_with_unknown_client_is_400(client):
    draft = create_draft(client, "u_alice", {
        "expenseType": "Travel", "amountCents": 500, "description": "trip",
        "billable": True, "client": "NotAClient",
        "destination": "NYC", "departDate": "2026-08-01", "returnDate": "2026-08-03",
    })
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_alice"))
    assert r.status_code == 400
    assert "client" in r.get_json()["errors"]


def test_other_requires_other_reason(client):
    draft = create_draft(client, "u_alice", {
        "expenseType": "Other", "amountCents": 500, "description": "misc",
    })
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_alice"))
    assert r.status_code == 400
    assert "otherReason" in r.get_json()["errors"]


def test_large_amount_requires_extra_justification(client):
    draft = create_draft(client, "u_bob", {
        "expenseType": "Equipment", "amountCents": 100_000,
        "description": "gear", "billable": False,
    })
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_bob"))
    assert r.status_code == 400
    assert "additionalJustification" in r.get_json()["errors"]


def test_amount_999_dollars_is_not_large(client):
    """The threshold is $1,000; $999.99 (99_999 cents) does not need extra justification."""
    draft = create_draft(client, "u_alice", {
        "expenseType": "Software", "amountCents": 99_999,
        "description": "just under the line",
        "vendor": "Acme Inc.", "softwareReason": "Team collaboration",
    })
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_alice"))
    assert r.status_code == 200


def test_whitespace_only_description_is_rejected(client):
    """A string of spaces shouldn't count as a description."""
    draft = create_draft(client, "u_alice", {
        "expenseType": "Meal", "amountCents": 100, "description": "   ",
    })
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_alice"))
    assert r.status_code == 400
    assert "description" in r.get_json()["errors"]


# ---------------------------------------------------------------------------
# Approver routing
# ---------------------------------------------------------------------------

def test_small_amount_routes_to_manager(client):
    draft = create_draft(client, "u_alice", {
        "expenseType": "Meal", "amountCents": 2500, "description": "lunch",
    })
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_alice")).get_json()
    assert r["status"] == "Submitted"
    assert r["currentApproverId"] == "u_carol"  # Alice's manager


def test_large_amount_routes_to_finance(client):
    draft = create_draft(client, "u_alice", {
        "expenseType": "Software", "amountCents": 500_000,
        "description": "big buy",
        "additionalJustification": "annual license",
        "vendor": "Acme Inc.", "softwareReason": "team-wide productivity tool",
    })
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_alice")).get_json()
    assert r["currentApproverId"] == "u_trent"


def test_missing_manager_falls_back_to_finance(client):
    """Peggy has no manager — a small request should still route (to finance)."""
    draft = create_draft(client, "u_peggy", {
        "expenseType": "Meal", "amountCents": 100, "description": "coffee",
    })
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_peggy")).get_json()
    assert r["currentApproverId"] == "u_trent"


def test_finance_cannot_approve_own_large_request(client):
    """Spec: if finance would also be the requester, submitting is refused."""
    draft = create_draft(client, "u_trent", {
        "expenseType": "Software", "amountCents": 500_000,
        "description": "tool", "additionalJustification": "needed",
        "vendor": "Acme Inc.", "softwareReason": "internal tooling",
    })
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_trent"))
    assert r.status_code == 409
    assert "finance" in r.get_json()["error"].lower()


def test_finance_small_request_routes_to_own_manager(client):
    """Trent (finance) has a manager (Peggy) — small requests should route there."""
    draft = create_draft(client, "u_trent", {
        "expenseType": "Meal", "amountCents": 100, "description": "coffee",
    })
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_trent")).get_json()
    assert r["currentApproverId"] == "u_peggy"


# ---------------------------------------------------------------------------
# Approve / reject
# ---------------------------------------------------------------------------

def test_only_assigned_approver_can_approve(client):
    """REQ-002 (from seed) is Submitted awaiting u_carol."""
    r = client.post("/api/requests/REQ-002/approve", headers=as_("u_bob"))
    assert r.status_code == 403


def test_owner_cannot_approve_own_request(client):
    r = client.post("/api/requests/REQ-002/approve", headers=as_("u_alice"))
    assert r.status_code == 403


def test_assigned_approver_can_approve(client):
    r = client.post("/api/requests/REQ-002/approve", headers=as_("u_carol")).get_json()
    assert r["status"] == "Approved"
    assert [e["type"] for e in r["events"]] == ["created", "submitted", "approved"]


def test_assigned_approver_can_reject(client):
    r = client.post("/api/requests/REQ-002/reject", headers=as_("u_carol")).get_json()
    assert r["status"] == "Rejected"


def test_approve_stores_optional_comment(client):
    r = client.post(
        "/api/requests/REQ-002/approve",
        headers=as_("u_carol"),
        json={"comment": "Looks fine, approving."},
    ).get_json()
    approved_event = r["events"][-1]
    assert approved_event["type"] == "approved"
    assert approved_event["comment"] == "Looks fine, approving."


def test_reject_stores_optional_comment(client):
    r = client.post(
        "/api/requests/REQ-002/reject",
        headers=as_("u_carol"),
        json={"comment": "Please provide more detail and resubmit."},
    ).get_json()
    rejected_event = r["events"][-1]
    assert rejected_event["type"] == "rejected"
    assert rejected_event["comment"] == "Please provide more detail and resubmit."


def test_approve_without_comment_omits_field(client):
    """Whitespace-only / missing comment should not add a `comment` key to the event."""
    r = client.post(
        "/api/requests/REQ-002/approve",
        headers=as_("u_carol"),
        json={"comment": "   "},
    ).get_json()
    approved_event = r["events"][-1]
    assert "comment" not in approved_event


def test_cannot_approve_a_draft(client):
    r = client.post("/api/requests/REQ-001/approve", headers=as_("u_carol"))
    assert r.status_code == 409


def test_cannot_approve_an_already_approved_request(client):
    r = client.post("/api/requests/REQ-003/approve", headers=as_("u_trent"))
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Sanitizer
# ---------------------------------------------------------------------------

def test_sanitizer_drops_client_when_not_billable(client):
    """If billable is false, `client` should not survive persistence."""
    draft = create_draft(client, "u_alice", {
        "expenseType": "Travel", "amountCents": 500,
        "description": "trip", "billable": False, "client": "Acme",
    })
    assert "client" not in draft["values"]


def test_sanitizer_drops_other_reason_when_type_isnt_other(client):
    draft = create_draft(client, "u_alice", {
        "expenseType": "Meal", "amountCents": 500,
        "description": "lunch", "otherReason": "shouldn't stick",
    })
    assert "otherReason" not in draft["values"]


def test_sanitizer_drops_extra_justification_below_threshold(client):
    draft = create_draft(client, "u_alice", {
        "expenseType": "Meal", "amountCents": 500,
        "description": "lunch",
        "additionalJustification": "shouldn't stick either",
    })
    assert "additionalJustification" not in draft["values"]


def test_sanitizer_rejects_unknown_keys(client):
    """Junk fields in `values` should not be persisted."""
    draft = create_draft(client, "u_alice", {
        "expenseType": "Meal", "amountCents": 500,
        "description": "lunch", "secretFlag": True,
    })
    assert "secretFlag" not in draft["values"]


# ---------------------------------------------------------------------------
# Type-specific fields (Travel, Software)
# ---------------------------------------------------------------------------

def test_meta_exposes_type_fields(client):
    body = client.get("/api/meta").get_json()
    assert "typeFields" in body
    assert "Travel" in body["typeFields"]
    keys = {f["key"] for f in body["typeFields"]["Travel"]}
    assert keys == {"destination", "departDate", "returnDate"}
    sw_keys = {f["key"] for f in body["typeFields"]["Software"]}
    assert sw_keys == {"vendor", "softwareReason"}


def test_travel_requires_type_specific_fields(client):
    draft = create_draft(client, "u_alice", {
        "expenseType": "Travel", "amountCents": 500, "description": "trip",
    })
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_alice"))
    assert r.status_code == 400
    errors = r.get_json()["errors"]
    assert "destination" in errors and "departDate" in errors and "returnDate" in errors


def test_travel_rejects_return_before_depart(client):
    draft = create_draft(client, "u_alice", {
        "expenseType": "Travel", "amountCents": 500, "description": "trip",
        "destination": "NYC", "departDate": "2026-08-05", "returnDate": "2026-08-01",
    })
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_alice"))
    assert r.status_code == 400
    assert "returnDate" in r.get_json()["errors"]


def test_travel_rejects_bad_date_format(client):
    draft = create_draft(client, "u_alice", {
        "expenseType": "Travel", "amountCents": 500, "description": "trip",
        "destination": "NYC", "departDate": "08/01/2026", "returnDate": "2026-08-05",
    })
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_alice"))
    assert r.status_code == 400
    assert "departDate" in r.get_json()["errors"]


def test_software_requires_type_specific_fields(client):
    draft = create_draft(client, "u_alice", {
        "expenseType": "Software", "amountCents": 500, "description": "tool",
    })
    r = client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_alice"))
    assert r.status_code == 400
    errors = r.get_json()["errors"]
    assert "vendor" in errors and "softwareReason" in errors


def test_sanitizer_drops_type_fields_for_other_types(client):
    """Software fields should not persist on a Meal draft."""
    draft = create_draft(client, "u_alice", {
        "expenseType": "Meal", "amountCents": 500, "description": "lunch",
        "vendor": "Nope", "softwareReason": "Nope", "destination": "Nope",
    })
    assert "vendor" not in draft["values"]
    assert "softwareReason" not in draft["values"]
    assert "destination" not in draft["values"]


def test_sanitizer_drops_travel_fields_when_switched_to_software(client):
    """Switching expense type should drop stale type fields."""
    draft = create_draft(client, "u_alice", {
        "expenseType": "Software", "amountCents": 500, "description": "tool",
        "destination": "NYC", "departDate": "2026-08-01", "returnDate": "2026-08-03",
        "vendor": "Acme", "softwareReason": "productivity",
    })
    assert "destination" not in draft["values"]
    assert "vendor" in draft["values"]


# ---------------------------------------------------------------------------
# End-to-end happy paths
# ---------------------------------------------------------------------------

def test_full_lifecycle_approve(client):
    draft = create_draft(client, "u_alice", {
        "expenseType": "Meal", "amountCents": 2500,
        "description": "team lunch",
    })
    submitted = client.post(
        f"/api/requests/{draft['id']}/submit", headers=as_("u_alice")
    ).get_json()
    assert submitted["status"] == "Submitted"
    assert submitted["currentApproverId"] == "u_carol"

    approved = client.post(
        f"/api/requests/{draft['id']}/approve", headers=as_("u_carol")
    ).get_json()
    assert approved["status"] == "Approved"
    assert [e["type"] for e in approved["events"]] == ["created", "submitted", "approved"]

    reread = client.get(f"/api/requests/{draft['id']}").get_json()
    assert reread["status"] == "Approved"


def test_full_lifecycle_reject(client):
    draft = create_draft(client, "u_bob", {
        "expenseType": "Equipment", "amountCents": 200_000,
        "description": "monitors", "additionalJustification": "old ones broken",
    })
    client.post(f"/api/requests/{draft['id']}/submit", headers=as_("u_bob"))
    rejected = client.post(
        f"/api/requests/{draft['id']}/reject", headers=as_("u_trent")
    ).get_json()
    assert rejected["status"] == "Rejected"
