# Expense Requests — Notes

A small internal expense-request tool: Flask backend + vanilla-JS single-page
frontend, in-memory store seeded from `data/*.json`.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py          # listens on 127.0.0.1:5050 (PORT env to override)
```

Then open <http://127.0.0.1:5050/> and pick a user from the "Acting as" dropdown
at the top right. Everything is in memory — restarting the server resets state
back to the seed data.

## Tests

Two suites, both under `tests/`:

- **`tests/test_backend.py`** — 64 tests using Flask's `test_client` against the
  in-memory store. Covers validation, permissions, single- and multi-step
  approval routing, status derivation, sanitizer behavior, fix-and-resubmit,
  approver comments, and full happy-path lifecycles.
- **`tests/test_frontend.py`** — 13 end-to-end Playwright tests. Spawns the
  Flask app as a subprocess on a free port with `EXPENSE_TEST_MODE=1` (which
  enables `POST /api/_test/reset`) and drives the SPA in headless Chromium.

One-time Playwright browser install:

```bash
.venv/bin/python -m playwright install chromium
```

Run everything:

```bash
.venv/bin/python -m pytest tests/
```

Run just one suite:

```bash
.venv/bin/python -m pytest tests/test_backend.py
.venv/bin/python -m pytest tests/test_frontend.py
```

The `/api/_test/reset` endpoint is gated by the `EXPENSE_TEST_MODE` env var so
it's off in normal use — the frontend fixture sets it when it spawns the
subprocess. `reset_store()` re-seeds `USERS` / `REQUESTS` via slice-assign so
already-imported list references stay valid across tests.

## Design choices & tradeoffs

**Stack.** Flask + vanilla JS with no build step. The whole app is ~700 lines
across `app.py`, `static/app.js`, `static/index.html`, `static/style.css`.
No framework, no ORM, no DB — for the size of the domain, more layers would
just get in the way.

**Auth is a trusted `X-User-Id` header.** The spec explicitly allows this. The
frontend sets it from a user picker; direct API callers can supply any user id.
Nothing about the app assumes the user id came from a real login — treating it
as an untrusted claim would be an easy swap. `X-User-Id` is also accepted via
an `?as=` query param for quick curl testing.

**Events are the source of truth.** Each request stores an ordered `events`
array (`created` / `submitted` / `approved` / `rejected`). Status and
current-approver are *derived* on every read (see `status_of`, `current_approver`
in `app.py`). Clients never send `status`, `requesterId`, or `approverId` — the
`values` object is whitelisted on write. This makes tampering hard and history
free.

**Server-side routing.** `approval_chain(requester, amountCents)` returns an
ordered list of approver ids:

- Under $1,000 → requester's `managerId` (falls back to finance if no manager).
- $1,000–$4,999 → finance only.
- $5,000+ → manager **then** finance (two-step).
- An approver that is also the requester is skipped; an empty chain fails with
  a spec-mandated clear error (e.g. finance can't approve their own request).

For multi-step routing, each `submitted` event carries the full `approverChain`
and its current `chainIndex`. When an intermediate approver approves, the server
appends a fresh `submitted` event pointing at the next approver, so status stays
"Submitted" and the derived current-approver picks up the next hop without any
mutable status field. Peggy (no manager) is a good test case: her small
requests fall back to Trent.

**Fix-and-resubmit.** Rejection is not terminal. The owner can PATCH `values`
on a `Rejected` request and re-submit; the rejection stays in history and the
approver is re-routed based on the current amount (so bumping over a threshold
correctly reroutes). The SPA relabels "Edit"/"Submit" to "Edit & Fix"/"Resubmit"
in that state.

**Approver comments.** `approve` and `reject` accept an optional `comment`
string body which is stored on the event and rendered inline in history.

**Type-specific fields.** `TYPE_FIELDS` (in `app.py`) declares extra fields per
expense type (Travel: destination + dates; Software: vendor + reason). The
schema is served via `/api/meta` and validated on the server, so client and
server never drift.

**Validation.** Rules mirror the spec exactly. `validate_values` returns a
`{field: message}` dict; the API returns `{"errors": {...}}` with HTTP 400 so
the UI can highlight fields inline. Server-side is authoritative — the same
rules run whether you use the SPA or `curl`.

**Conditional fields.** Drafts can be incomplete — validation only runs on
submit. The `_sanitize_values` helper drops conditional fields whose gate is
off (e.g., clears `client` if `billable` is false), so a stored draft doesn't
leak stale conditional data if the user flips a checkbox back and forth.

**Money.** Stored as `amountCents` (whole int cents). The UI shows/parses
dollars but round-trips through cents. Floats and negatives are rejected on
the server.

**Concurrency.** All writes go through a `threading.Lock`. In-memory + Flask
dev server means concurrency is unlikely to matter, but list/append/mutation
across `REQUESTS` is easy to protect and cheap.

**Frontend structure.** Three tabs (`list`, `new`/edit, `detail`) driven from
`state`. The form updates conditional-field visibility via direct DOM toggling
rather than re-rendering, so in-flight typing keeps focus. Server field errors
are surfaced next to their respective inputs; a "please fix highlighted fields"
line summarizes. The detail view shows a chain widget (done / current / pending
pills) when routing is multi-step.

## What the tests cover

The scenarios exercised by the backend and frontend suites (see the `Tests`
section above for how to run them):

- Empty submit returns `{errors: {expenseType, amountCents, description}}` with 400.
- Small-amount routes to manager (Alice → Carol).
- Large-amount ($1,000–$4,999) routes to finance and blocks submit without
  `additionalJustification`.
- Very-large-amount ($5,000+) routes to manager first, then finance, with the
  chain and index tracked on the `submitted` event.
- Intermediate rejection in a multi-step chain is terminal.
- Fix-and-resubmit after rejection recomputes the chain (e.g. bumping the
  amount over $5,000 re-routes to the two-step chain).
- `billable: true` without `client` is a 400.
- Expense type `Other` without `otherReason` is a 400.
- Travel requires destination + valid depart/return dates (return >= depart).
- Software requires vendor + reason.
- Negative and non-integer amounts are 400.
- Only the owner can edit a Draft or a Rejected request (403 for others).
- Only the currently-assigned approver can approve/reject (403 for owner, for
  random users, and — in multi-step — for the next-step approver until it's
  their turn).
- Edits to a Submitted/Approved request are 409.
- Finance-submitting-their-own request → 409 with the spec-mandated error.
- Peggy (no manager) submitting a small request falls back to Trent.
- Approval/rejection comments are stored and returned; empty/whitespace-only
  comments are dropped.
- Client-supplied `status` / `events` fields are ignored on create — the new
  request is always a Draft with a single `created` event.
- Unknown/missing user → 401.

## API surface

```
GET   /api/meta                    # expense types, clients, thresholds, typeFields
GET   /api/users                   # for the user picker
GET   /api/me                      # who the header resolves to
GET   /api/requests                # all requests + derived status/approver
GET   /api/requests/<id>
POST  /api/requests                # create Draft (owner = header user)
PATCH /api/requests/<id>           # owner-only edit of `values` (Draft or Rejected)
POST  /api/requests/<id>/submit    # validate + route + append 'submitted'
POST  /api/requests/<id>/approve   # currently-assigned approver only (optional {comment})
POST  /api/requests/<id>/reject    # currently-assigned approver only (optional {comment})
```

`submitted` events carry `approverId`, `approverChain` (list), and `chainIndex`
(int) so multi-step routing state is fully reconstructable from history.

Errors come back as `{"error": "..."}` or `{"errors": {"field": "msg", ...}}`
depending on whether it's a general or field-level problem.

## What I'd do next (given more time)

1. **Real persistence** — swap the two module-level lists for SQLite via
   `sqlite3` stdlib; the event-log shape maps cleanly to an `events` table with
   a `request_id` foreign key. Status derivation stays the same.
2. **Real auth** — the `X-User-Id` header is the seam; the rest of the app
   doesn't care where the identity came from.
3. **Role-scoped list view** — everyone can see every request today. A finance
   dashboard filter (all Submitted at me) is already achievable via the "For
   me to act on" checkbox, but requesters could use a "just my requests" home
   screen and observers a read-only feed.
4. **Delegate / re-route** — an approver on vacation should be able to reassign
   a pending request without rejecting it.
5. **Attachments** — receipts/screenshots stored alongside the event history.

## AI assistance

I used Claude Code as a pair. It wrote most of the plumbing — routes, the SPA
skeleton, the bulk of the tests. I drove the design and pushed back where its
defaults didn't fit.

A few concrete places I diverged from what it produced first:

- It reached for a `Store` class. For a single-process in-memory app with one
  lock, that was ceremony — two module-level lists work fine and the lock is
  right there next to the writes.
- Its first draft stored a `status` field on the request and updated it
  alongside pushing events. I asked to derive status from the event log
  instead. Later the same call paid off for multi-step routing — I could keep
  the chain state on the `submitted` event and avoid re-introducing a mutable
  field.
- The initial POST/PATCH handlers spread the request body's `values` verbatim.
  I asked for a `_ALLOWED_VALUE_KEYS` whitelist so clients can't inject fields
  that aren't part of the schema.
- Amount handling started as "accept dollars, convert on the server." I moved
  it to int cents end-to-end and rejected floats at the boundary — one unit,
  no rounding anywhere in the middle.
- The form initially re-rendered on every input change to update conditional
  fields. That stole focus mid-typing. Switched it to direct show/hide on the
  existing DOM.
- Comments in the first draft explained a lot of *what* the code does. On the
  polish pass I trimmed harder than it wanted to.

## Prompts I used

Reconstructed from the session, not a verbatim transcript — the real thing had
more back-and-forth, terse follow-ups ("that broke test X, fix"), and me
correcting course mid-answer. But these capture the shape of what I asked for
at each stage.

**Scaffold.**
> Read `README.md` and build the core spec. Flask + vanilla JS, no build step,
> in-memory store from `data/*.json`. `X-User-Id` header for auth. Server owns
> validation, permissions, status, routing — client can't set status,
> requesterId, approverId, or events. Core only, no stretches yet.

Follow-ups on this one were about trimming: drop the `Store` class, use cents
not dollars, whitelist the `values` keys.

**Backend tests.**
> Add pytest tests against Flask's `test_client`. Cover the validation rules,
> the permission checks, the routing (including finance-can't-approve-own),
> and one full lifecycle. Hit the in-memory store directly, no mocks. Add a
> `_test/reset` endpoint gated by an env var so tests don't need to import
> app internals.

**Playwright tests.**
> Add e2e tests that spawn the app as a subprocess and drive the SPA in
> headless Chromium. Only cover things the browser is the source of truth
> for — conditional fields showing/hiding, inline error rendering, the
> Approve/Reject buttons only appearing for the current approver. Skip
> anything the backend suite already covers.

**Type-specific fields (stretch).**
> Travel needs destination + depart/return dates (return >= depart). Software
> needs vendor + a reason. Put the schema in one place so client and server
> can't drift, expose it via `/api/meta`, and have `_sanitize_values` drop
> fields that don't belong to the current type so switching type on a draft
> doesn't leave stale values.

**Approver comments (stretch).**
> Optional comment on approve/reject, stored on the event. Trim and drop
> empty strings. Frontend can use `window.prompt`, this is a demo. — Then a
> follow-up when a Playwright test started hanging on the prompt dialog.

**Fix-and-resubmit (stretch).**
> Rejected shouldn't be terminal. Owner can PATCH and re-submit. Resubmit
> re-runs validation and recomputes the approver from the current amount, so
> bumping across a threshold reroutes. Keep the rejection in history. Relabel
> the buttons to "Edit & Fix" / "Resubmit" when the request is Rejected.

**Multi-step approval (stretch).**
> $5,000+ should go manager → finance. Don't add a mutable status field on
> the request. Try this: `submitted` event carries the full chain and an
> index; when an intermediate step approves, append a new `submitted` event
> pointing at the next approver. Status stays "Submitted" until the last one.
> Rejection anywhere is terminal.

Initial suggestion was a `chainIndex` field on the request itself — I asked
to keep the derived-from-events invariant.

**Polish + docs.**
> Read the codebase and strip comments that just restate the code. Keep the
> ones that explain *why* — non-obvious constraints, UX invariants like the
> focus preservation on the form. Then rewrite NOTES.md to match reality:
> test counts, three-tier routing, current API surface, which stretches
> actually shipped.

**Conformance check.**
> Go through the spec's core requirements list and check each one against
> the code. Cite file:line. Don't just tell me it passes — flag anything
> that's partial or missing.

### What I did *not* delegate

- The data model. Event-sourced, derived status, no mutable `status` or
  `currentApproverId` fields — that call was mine and I re-applied it when
  multi-step routing came along.
- The multi-step chain mechanic. First AI suggestion was a `chainIndex` field
  on the request. I preferred keeping the invariant that state lives in events.
- The error envelope split (`{error}` vs `{errors: {field: msg}}`) and the
  API shape more generally.
- Which stretches to build and in what order.
- Where the line was between a comment that earns its keep and one that just
  narrates the code. The AI erred toward keeping; I trimmed harder on the
  polish pass.
