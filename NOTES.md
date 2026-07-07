# Expense Requests — Notes

A small internal expense-request tool: Flask backend + vanilla-JS single-page
frontend, in-memory store seeded from `data/*.json`.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install flask
.venv/bin/python app.py          # listens on 127.0.0.1:5050 (PORT env to override)
```

Then open <http://127.0.0.1:5050/> and pick a user from the "Acting as" dropdown
at the top right. Everything is in memory — restarting the server resets state
back to the seed data.

## Design choices & tradeoffs

**Stack.** Flask + vanilla JS with no build step. The whole app is ~600 lines
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

**Server-side routing.** `resolve_approver(requester, amountCents)`:
- Under $1,000 → requester's `managerId`.
- $1,000+ → finance (`role == "finance"`).
- Missing manager, or manager is the requester → falls back to finance.
- Finance would also be the requester → submission refused with a clear error
  (matches the spec's "refuse with a clear error" case). Peggy, who has no
  manager, is a good test: her small requests fall back to Trent.

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
`state`. The form re-renders on gate-field changes so conditional fields show/
hide, and only re-renders on amount when the $1,000 threshold is crossed (to
avoid stealing focus mid-typing). Server field errors are surfaced next to
their respective inputs; a "please fix highlighted fields" line summarizes.

**What I did *not* build** — the stretch items. I stuck to the core list.
Where I could see a stretch path clearly, it's noted below.

## What I tested (via curl, before demoing the UI)

- Empty submit returns `{errors: {expenseType, amountCents, description}}` with 400.
- Small-amount routes to manager (Alice → Carol).
- Large-amount routes to finance (Bob → Trent), and blocks submit without
  `additionalJustification`.
- `billable: true` without `client` is a 400.
- Expense type `Other` without `otherReason` is a 400.
- Negative and non-integer amounts are 400.
- Only the owner can edit a Draft (403 for others).
- Only the assigned approver can approve/reject (403 for owner, for random
  users, and for finance-when-manager-owns).
- Edits to a Submitted/Approved/Rejected request are 409.
- Finance-submitting-their-own request → 409 with the spec-mandated error.
- Peggy (no manager) submitting a small request falls back to Trent.
- Client-supplied `status` / `events` fields are ignored on create — the new
  request is always a Draft with a single `created` event.
- Unknown/missing user → 401.

## API surface

```
GET  /api/meta                    # expense types, clients, threshold
GET  /api/users                   # for the user picker
GET  /api/me                      # who the header resolves to
GET  /api/requests                # all requests + derived status/approver
GET  /api/requests/<id>
POST /api/requests                # create Draft (owner = header user)
PATCH /api/requests/<id>          # owner-only, Draft-only edit of `values`
POST /api/requests/<id>/submit    # validate + route + append 'submitted'
POST /api/requests/<id>/approve   # assigned-approver only
POST /api/requests/<id>/reject    # assigned-approver only
```

Errors come back as `{"error": "..."}` or `{"errors": {"field": "msg", ...}}`
depending on whether it's a general or field-level problem.

## What I'd do next (given more time)

1. **Fix-and-resubmit for Rejected requests** — allow the owner to edit again
   after rejection, recompute the approver on resubmit, keep a full history
   trail, and let them attach a note. Also keep the rejection comment visible
   during the fix.
2. **Approver comments** — attach an optional note to `approved`/`rejected`
   events; show them in history and (for rejections) inline on the form during
   the resubmit flow.
3. **Type-specific fields** — Travel gets `destination`/`departDate`/
   `returnDate`; Software gets `vendor` + reason. Would refactor `validate_values`
   into a per-type dispatch.
4. **Read-only view for observers** — anyone can see any request today; a
   role-scoped view could hide unrelated ones or mark them read-only.
5. **Real persistence** — swap the two module-level lists for SQLite via
   `sqlite3` stdlib; the event-log shape maps cleanly to a table.
6. **Tests** — the curl script in the "What I tested" section is the seed of a
   `pytest` file. Would use Flask's `test_client` and hit each rule as a table.
7. **Real auth** — the `X-User-Id` header is the seam; the rest of the app
   doesn't care where the identity came from.

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
- **Whitelisted `values` fields on write.** The AI's draft accepted the body's
  `values` verbatim. Filtering to `_ALLOWED_VALUE_KEYS` prevents junk fields
  from being persisted and prevents a client from setting anything meaningful
  outside the schema.
- **Amount stored as int cents throughout, with float rejection at the API
  boundary.** The AI suggested "accept dollars, convert on the server." I
  preferred a single unit end-to-end so nothing in the server has to think
  about rounding.
- **Only re-render the form on threshold crossings while typing the amount.**
  The AI's first draft re-rendered on every keystroke, which stole focus. Small
  UX detail but noticeable.

Relevant prompts and this session's transcript accompany the submission.
