"""End-to-end frontend tests using Playwright.

We spawn the Flask app as a subprocess on a free port (with
`EXPENSE_TEST_MODE=1` so the reset endpoint is enabled), then drive the SPA in
Chromium. Each test resets the in-memory store first so ordering doesn't matter.

These are UI-level checks — the point is that the *frontend* correctly wires
the form's conditional fields, submits requests, shows server errors inline,
and lets the assigned approver act. Rule-level enforcement is covered by the
backend suite; here we just verify the browser experience.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.1)
    raise RuntimeError(f"Server did not come up at {url} within {timeout}s")


@pytest.fixture(scope="session")
def server_url():
    """Boot the Flask app for the whole test session."""
    port = _free_port()
    env = {
        **os.environ,
        "PORT": str(port),
        "EXPENSE_TEST_MODE": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "app.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT),
    )
    base = f"http://127.0.0.1:{port}"
    try:
        try:
            _wait_for(f"{base}/api/meta", timeout=15.0)
        except RuntimeError as e:
            # Surface subprocess output when boot fails
            proc.terminate()
            try:
                out = proc.communicate(timeout=2)[0] or b""
            except subprocess.TimeoutExpired:
                proc.kill()
                out = b""
            raise RuntimeError(
                f"{e}\nsubprocess output:\n{out.decode(errors='replace')}"
            )
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(autouse=True)
def reset_store_before_each(server_url):
    urllib.request.urlopen(
        urllib.request.Request(f"{server_url}/api/_test/reset", method="POST"), timeout=2
    ).read()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def act_as(page, name: str) -> None:
    page.locator("#user-select").select_option(value=_user_id_for(name))


def _user_id_for(name: str) -> str:
    return {
        "Alice": "u_alice", "Bob": "u_bob", "Carol": "u_carol",
        "Mallory": "u_mallory", "Peggy": "u_peggy", "Trent": "u_trent",
    }[name]


def go_home(page, base: str) -> None:
    page.goto(base)
    # Options aren't visible in a closed select; wait for them to attach.
    page.wait_for_selector("#user-select option", state="attached")
    # Wait for the initial request load so tests can query rows immediately
    page.wait_for_selector("#requests-table tbody tr")


def wait_for_detail(page, request_id: str) -> None:
    """Wait for the detail view to show the given request id in its title."""
    page.wait_for_selector("#tab-detail.active")
    page.wait_for_function(
        f"() => document.querySelector('#detail-title')?.textContent?.includes('{request_id}')"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_list_shows_seed_requests_with_status(page, server_url):
    go_home(page, server_url)
    # 4 seed rows visible
    rows = page.locator("#requests-table tbody tr")
    assert rows.count() == 4
    # REQ-002 is Submitted, awaiting Carol
    req002 = page.locator("#requests-table tbody tr", has_text="REQ-002")
    assert "Submitted" in req002.inner_text()
    assert "Carol" in req002.inner_text()


def test_filter_awaiting_my_approval_shows_only_my_pending(page, server_url):
    """Carol should see REQ-002 waiting on her when 'awaiting' is checked."""
    go_home(page, server_url)
    act_as(page, "Carol")
    page.locator("#filter-todo").check()
    rows = page.locator("#requests-table tbody tr")
    # Wait for the filter to apply
    page.wait_for_function(
        "() => document.querySelectorAll('#requests-table tbody tr').length === 1"
    )
    assert rows.count() == 1
    assert "REQ-002" in rows.first.inner_text()


def test_mine_filter_shows_only_my_requests(page, server_url):
    go_home(page, server_url)
    act_as(page, "Alice")
    page.locator("#filter-mine").check()
    page.wait_for_function(
        "() => document.querySelectorAll('#requests-table tbody tr').length === 2"
    )
    text = page.locator("#requests-table tbody").inner_text()
    assert "REQ-001" in text and "REQ-002" in text
    assert "REQ-003" not in text  # Bob's
    assert "REQ-004" not in text  # Mallory's


def test_client_field_appears_when_billable_checked(page, server_url):
    go_home(page, server_url)
    act_as(page, "Alice")
    page.locator("nav.tabs button[data-tab=new]").click()
    # Client field starts hidden
    assert not page.locator('[data-field=client]').is_visible()
    page.locator("input[name=billable]").check()
    # …and appears once billable is checked
    assert page.locator('[data-field=client]').is_visible()


def test_other_reason_appears_when_type_is_other(page, server_url):
    go_home(page, server_url)
    act_as(page, "Alice")
    page.locator("nav.tabs button[data-tab=new]").click()
    assert not page.locator('[data-field=otherReason]').is_visible()
    page.locator("select[name=expenseType]").select_option("Other")
    assert page.locator('[data-field=otherReason]').is_visible()


def test_extra_justification_appears_at_1000(page, server_url):
    go_home(page, server_url)
    act_as(page, "Alice")
    page.locator("nav.tabs button[data-tab=new]").click()
    # Below threshold
    page.locator("input[name=amountCents]").fill("999.99")
    page.locator("input[name=amountCents]").press("Tab")
    assert not page.locator('[data-field=additionalJustification]').is_visible()
    # At threshold
    page.locator("input[name=amountCents]").fill("1000")
    page.locator("input[name=amountCents]").press("Tab")
    assert page.locator('[data-field=additionalJustification]').is_visible()


def test_submit_shows_inline_field_errors(page, server_url):
    """Submitting an empty draft should mark the missing fields inline."""
    go_home(page, server_url)
    act_as(page, "Alice")
    page.locator("nav.tabs button[data-tab=new]").click()
    page.locator("#btn-submit").click()
    # The three required fields should be flagged
    page.wait_for_selector('[data-field=expenseType] .field-error')
    for field in ["expenseType", "amountCents", "description"]:
        assert page.locator(f'[data-field={field}] .field-error').is_visible(), field


def test_create_and_submit_routes_to_manager(page, server_url):
    """Alice creates a $25 meal, submits, and it shows Submitted awaiting Carol."""
    go_home(page, server_url)
    act_as(page, "Alice")
    page.locator("nav.tabs button[data-tab=new]").click()
    page.locator("select[name=expenseType]").select_option("Meal")
    page.locator("input[name=amountCents]").fill("25")
    page.locator("textarea[name=description]").fill("Team lunch")
    page.locator("#btn-submit").click()
    # Detail view appears with Submitted status and Carol as approver
    try:
        page.wait_for_selector("#tab-detail.active", timeout=5000)
    except Exception:
        form_error = page.locator("#form-error").inner_text()
        field_errors = page.locator(".field-error").all_inner_texts()
        raise AssertionError(
            f"Detail never opened. form-error={form_error!r} field-errors={field_errors}"
        )
    detail = page.locator("#detail-body").inner_text()
    assert "Submitted" in detail
    assert "Carol" in detail


def test_large_amount_requires_justification_inline(page, server_url):
    go_home(page, server_url)
    act_as(page, "Bob")
    page.locator("nav.tabs button[data-tab=new]").click()
    page.locator("select[name=expenseType]").select_option("Equipment")
    page.locator("input[name=amountCents]").fill("2000")
    page.locator("textarea[name=description]").fill("Monitors")
    page.locator("#btn-submit").click()
    page.wait_for_selector('[data-field=additionalJustification] .field-error')
    assert page.locator('[data-field=additionalJustification] .field-error').is_visible()


def test_approver_can_approve_from_detail(page, server_url):
    """Carol opens REQ-002, clicks Approve, sees Approved."""
    go_home(page, server_url)
    act_as(page, "Carol")
    page.locator("#requests-table tbody tr", has_text="REQ-002").click()
    wait_for_detail(page, "REQ-002")
    page.wait_for_selector("#detail-actions button")
    page.on("dialog", lambda d: d.accept(""))
    page.locator("#detail-actions button", has_text="Approve").click()
    page.wait_for_function(
        "() => document.querySelector('#detail-body').innerText.includes('Approved')"
    )
    assert "Approved" in page.locator("#detail-body").inner_text()


def test_non_approver_sees_no_approve_button(page, server_url):
    """Bob has no business approving REQ-002 — Approve/Reject must not be shown."""
    go_home(page, server_url)
    act_as(page, "Bob")
    page.locator("#requests-table tbody tr", has_text="REQ-002").click()
    wait_for_detail(page, "REQ-002")
    # Wait for renderDetail to finish populating the actions container
    page.wait_for_function(
        "() => document.querySelector('#detail-body')?.innerText?.includes('Submitted')"
    )
    assert page.locator("#detail-actions button", has_text="Approve").count() == 0
    assert page.locator("#detail-actions button", has_text="Reject").count() == 0


def test_owner_sees_edit_and_submit_on_draft(page, server_url):
    """Alice opens her Draft REQ-001 and sees Edit + Submit."""
    go_home(page, server_url)
    act_as(page, "Alice")
    page.locator("#requests-table tbody tr", has_text="REQ-001").click()
    wait_for_detail(page, "REQ-001")
    # Wait for the async renderDetail() to populate the action buttons
    page.wait_for_selector("#detail-actions button")
    assert page.locator("#detail-actions button", has_text="Edit").count() == 1
    assert page.locator("#detail-actions button", has_text="Submit").count() == 1


def test_non_owner_cannot_edit_draft(page, server_url):
    """Bob opens Alice's Draft REQ-001 — no Edit or Submit buttons."""
    go_home(page, server_url)
    act_as(page, "Bob")
    page.locator("#requests-table tbody tr", has_text="REQ-001").click()
    wait_for_detail(page, "REQ-001")
    # Wait for detail to be fully rendered (status appears in body)
    page.wait_for_function(
        "() => document.querySelector('#detail-body')?.innerText?.includes('Draft')"
    )
    assert page.locator("#detail-actions button", has_text="Edit").count() == 0
    assert page.locator("#detail-actions button", has_text="Submit").count() == 0
