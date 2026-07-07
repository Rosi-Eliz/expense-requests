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

I used Claude Code (this session) as a pair to draft the initial scaffolding —
routes, the validation function, the SPA structure. Places where I pushed back
or diverged from the AI's first take:

- **Kept the store as two module-level lists** rather than a `Store` class the
  AI initially suggested. The class was pure ceremony for a single-process
  in-memory app — I only need a lock, not encapsulation.
- **Derived status from events rather than storing a `status` field.** The AI's
  first draft stored `status` explicitly and updated it alongside pushing
  events. Deriving it eliminates the class of bug where the two get out of sync
  and matches the spec's "status always matches the latest action" wording.
  Multi-step routing extended this: instead of a mutable `chainIndex` on the
  request, the current step is derived from the latest `submitted` event.
- **Whitelisted `values` fields on write.** The AI's draft accepted the body's
  `values` verbatim. Filtering to `_ALLOWED_VALUE_KEYS` prevents junk fields
  from being persisted and prevents a client from setting anything meaningful
  outside the schema.
- **Amount stored as int cents throughout, with float rejection at the API
  boundary.** The AI suggested "accept dollars, convert on the server." I
  preferred a single unit end-to-end so nothing in the server has to think
  about rounding.
- **Toggle conditional-field visibility with direct DOM show/hide, not a
  re-render.** The AI's first draft re-rendered the whole form on every gate
  change, which stole focus mid-typing. Small UX detail but noticeable.

Relevant prompts and this session's transcript accompany the submission.
