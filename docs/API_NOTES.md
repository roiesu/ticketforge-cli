# TicketForge API notes

TicketForge has no public API documentation, so this is what I found by watching
the web app's network requests (browser dev tools) and then trying the endpoints
directly with `curl` / `requests`.

Base URL: `https://integrations-assignment-ticketforge.vercel.app/api/tforge`

## Authentication

- Every endpoint except registration uses **HTTP Basic auth** (`Authorization: Basic base64(user:pass)`).
- There is no login/token endpoint. The web app "logs in" by calling
  `GET /workitems/mine` and checking for a 200, and the CLI does the same.
- Registration: `POST /user/register` `{"username", "password"}`. Username needs
  at least 3 characters and password at least 6.

## Endpoints

| Method | Path | Body / params | Notes |
|---|---|---|---|
| POST | `/user/register` | `{username, password}` | no auth; `user_exists` if the name is taken |
| GET | `/workitems/mine` | `?cursor=` `?limit=` | your tickets, newest first |
| POST | `/workitem/publish` | `{title, description?, dependsOn?, customFields?}` | create; new tickets start as `open` |
| GET | `/workitem/{ref}` | `?view=deep` | `deep` adds `owner` |
| PUT | `/workitem/{ref}` | `{title, description, stage, dependsOn, customFields}` | **full replace**, see below |
| DELETE | `/workitem/{ref}` | | not used by the web UI, but it works |
| GET | `/custom-fields` | | field definitions for your account |
| POST | `/custom-fields` | `{name, label}` | `name` must match `[A-Za-z0-9_]+` |
| PUT | `/custom-fields/{id}` | `{label}` | only the label can be changed |
| DELETE | `/custom-fields/{id}` | | |

The method lists come from each route's `OPTIONS` response (`Allow` header), so
this table should be complete: `DELETE /workitem/{ref}` is the only operation the
web UI never uses.

Stages: `open`, `in_progress`, `review`, `closed`. There is no workflow: a ticket
can move from any stage to any other (e.g. `open` → `closed` directly), and the
web app's stage dropdown doesn't restrict it either.

## Behaviour worth knowing

**Pagination.** The list is cursor-based: the response has
`pagination: {limit, hasMore, nextCursor}` and you pass `?cursor=<nextCursor>` to
get the next page. Page size is capped at **5**. Asking for `limit=50` silently
gives 5, and `limit=0` gives 1. A malformed cursor returns `invalid_cursor`.

**Updates are a full replace.** `PUT /workitem/{ref}` doesn't merge:
- `title` and `stage` are required (sending only `{"stage": "closed"}` fails with
  "title is required").
- If you leave out `description` or `customFields`, they are **cleared**.
- If you leave out `dependsOn`, the old value is kept. `null` or `[]` clears it.

So the client always fetches the ticket first and sends the full object with the
changes applied (`build_update_payload` in `tforge/api.py`).

**Errors.** Errors come back as JSON:
`{"err": "<code>", "message": "<human text>", "retryable": <bool>}`.
Almost everything is HTTP **400**, including `not_found` and `forbidden`, so the
`err` code tells you more than the status. 401 means bad credentials. Unknown
routes return the Next.js HTML 404 page rather than JSON.

Codes I saw: `unauthorized`, `invalid_input`, `not_found`, `forbidden`,
`user_exists`, `duplicate`, `invalid_cursor`, `rate_limit_exceeded`.

**Rate limiting.** 50 requests per 60 seconds, **per user**. Successful responses
include `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset`.
Going over returns **429** with `Retry-After` (seconds) and `retryable: true`.

**Ownership.** You can only see, edit or depend on your own tickets. Using
someone else's ref returns `forbidden`. A ticket can't depend on itself.
Circular dependencies (A→B, B→A) are accepted.

**Refs.** Refs look like `TF-<n>` and are global across users. After a delete,
the same ref can be handed out again.

**No filtering or sorting.** The list is ordered by creation time (newest first;
updating a ticket doesn't move it). Parameters like `stage`, `status`, `q`,
`search`, `sort`, `order` and `fields` are silently ignored. On
`GET /workitem/{ref}`, the only `view` value that changes the response is `deep`.

**Custom fields.** These are per user and text-only (`type: "text"`). Setting a
value for a field that doesn't exist fails with `custom field "x" does not exist`.
In ticket responses, `customFields` and `dependsOn` are left out entirely when empty.

## How I checked for hidden endpoints

1. **Every JavaScript file the app ships** (11 bundles, not only the page bundles).
   I searched them for `/api` and `fetch(`. The only API paths are the ones in
   the table above.
2. **The Next.js build manifest's route filters** (Bloom filters of every page
   route). Decoding them confirmed the app has just `/`, `/login`, `/register`,
   `/dashboard` and `/ticket/[ref]`. None of about 80 likely hidden pages
   (`/admin`, `/settings`, `/api-keys`, ...) match. `robots.txt` and
   `sitemap.xml` don't exist.
3. **Probing the API.** Unknown routes return the HTML 404 page, while real routes
   answer with JSON or a 405, so they're easy to tell apart. About 65 likely
   paths (`/user/me`, `/workitems/search`, `/workitem/{ref}/comments`, `/stats`,
   `/webhooks`, `/openapi.json`, ...) all returned the 404 page.
4. **Asking each route which methods it supports.** `OPTIONS` returns an
   `Allow` header, and that's where the method column in the table comes from.
   Other methods (e.g. `PATCH` for partial updates) return 405.
