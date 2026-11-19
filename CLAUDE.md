# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Atten** — location-aware attendance tracking for a lab or small team. Members check in/out
only when physically within a geofence radius of the lab; admins approve leave and manage the
roster. Flask + Flask-SQLAlchemy (SQLite) + Flask-Login, server-rendered Jinja templates, vanilla
JS/CSS (no build step, no frontend framework, no chart library).

## Running

```bash
pip install -r requirements.txt
python app.py                 # dev server, debug=True, http://127.0.0.1:5001
PORT=8000 python app.py       # different port
```

There is no test suite, linter, or build step in this repo — verify changes by running the app and
exercising the flow in a browser. The SQLite DB (`instance/atten.db`) and roster are created
automatically on first run; delete `instance/atten.db` to reset to a clean seeded state.

### Deployment notes

- `.env` (copied from `.env.example`, chmod 600, **never commit**) holds `SECRET_KEY`, `PORT` and
  `TRUST_PROXY`. Example nginx and systemd configs live in `deploy/`.
- Run gunicorn with **one worker and several threads on purpose**: `create_app()` executes at
  import time (`app.py` bottom), so every extra worker process would re-run `db.create_all()`,
  `run_migrations()` and `seed_from_csv()` on boot and then contend for the SQLite write lock.
- Set the service `TZ` to the lab's timezone because `window_state()` compares `datetime.now()`
  against the check-in cutoff — a different service timezone silently shifts when check-in closes.

Bootstrap admin login: Lab ID `labadmin`, password `admin@123` (from `Config.DEFAULT_ADMIN_ID`
/ `DEFAULT_ADMIN_PASSWORD`).

## Architecture

The entire backend is four files — there is no package structure, no blueprints, no service layer:

- **`app.py`** — app factory (`create_app`), all routes (registered via `register_routes(app)`
  inside the factory), and every piece of business logic: geofence math, attendance window,
  leave quota accrual, device fingerprinting/anti-proxy, per-member statistics. Routes are grouped
  by comment banners (Auth, Dashboard, Attendance API, Profile, Leaves, Admin, Security, Errors).
  Read the banner comments before adding a route — they explain *why*, not just what.
- **`models.py`** — `User`, `Attendance`, `LeaveRequest`, `Device`, `SecurityFlag`, `Setting`
  (a simple key/value store used for all runtime-editable config: geofence, leave policy, window
  cutoffs, network allowlist, Discord webhook).
- **`notify.py`** — Discord webhook payload builders + a fire-and-forget background sender.
  Notification failures are always swallowed; the DB is the source of truth, Discord is best-effort.
- **`mailer.py`** — the same contract over SMTP (stdlib only), for the leave flow: a new/withdrawn
  request mails the admins, an approve/reject mails the member. Builders return
  `(subject, text, html)` and are called from the routes via `send_email(...)` in `app.py`; the
  helpers there (`smtp_config`, `email_enabled`, `admin_recipients`) mirror the Discord ones.
  Anything member-supplied that lands in the HTML body is escaped in `_shell`.
- **`config.py`** — defaults for everything `Setting` can override at runtime (geofence location/
  radius, leave rate/accrual start, check-in/out cutoffs, device limits), plus upload limits and
  `TRUST_PROXY`.

There is no migration framework. `run_migrations()` in `app.py` runs on every boot and adds any
missing columns via raw `ALTER TABLE` (SQLite-safe, additive only). **When adding a model column,
add the corresponding `add_column(...)` call in `run_migrations()` in the same change** — an
existing `instance/atten.db` must upgrade in place with no manual step.

### Settings vs. Config

Runtime-tunable values (geofence, leave rate, window cutoffs, network allowlist/mode, Discord
webhook + toggles, SMTP credentials + email toggles) live in the `settings` key/value table via
`Setting.get/get_float/set`, seeded with defaults from `Config` in `ensure_defaults()`. Admins edit
these live from `Admin → Settings` (`admin_settings` route, sectioned by a hidden `section` form
field: `geofence` / `policy` / `network` / `discord` / `email`). `Config` class attributes are the fallback defaults only — never read
directly for these values in request-handling code, always go through `Setting`.

### Attendance flow (`POST /api/attendance`, action `in`/`out`)

Checks run in a specific, deliberate order — cheapest/hardest-to-fake first — and each layer is
documented inline in `app.py` with the reasoning:

1. **Time window** (`window_state`) — check-in closes at a configurable cutoff (default 17:00) so
   a day can't be back-filled in the evening; check-out closes later (default 23:59).
2. **Lab network** (`ip_on_lab_network`, modes `off`/`warn`/`enforce`) — source IP can't be forged
   the way GPS can. Only trusts `X-Forwarded-For` when `TRUST_PROXY=1` (only set that behind a
   real reverse proxy — otherwise the header is attacker-controlled).
3. **Geofence** (`haversine_m` vs. `lab_geofence()`) — GPS reported by the browser; a convenience
   check, not proof of location. Same three modes as the network check
   (`geofence_mode`, `off`/`warn`/`enforce`, editable in `Admin → Settings → geofence`): `off`
   skips it *and* tells `attendance.js` not to ask the browser for a position at all — a browser
   only gives one on a secure origin, so on a plain-http host the check can never pass. A
   position may therefore be `None`: `distance_m`, the API's `distance` field and the Discord
   line all have to tolerate that.
4. **Device binding / anti-proxy** (`resolve_device`) — each account may mark attendance from at
   most `MAX_DEVICES_PER_USER` (2) browsers, identified by a long-lived cookie token backed by a
   fingerprint hash of stable browser traits (`device_fingerprint`, `FP_KEYS` in `app.py` — must
   match the fields sent by `static/js/attendance.js`). A device belonging to another account
   blocks the check-in, voids that day (`void_attendance`), and raises a `SecurityFlag`.

Voided attendance rows are kept for audit but must never be counted — `Attendance.counts_present`
is the single source of truth for "did this day count," and any new statistics/aggregation code
must filter through it (or replicate its `check_in is not None and not is_voided` logic) rather
than checking `check_in` alone.


### Leave quota

Accrual is cycle-based, not a flat annual grant: `leave_summary()` / `leave_cycle()` /
`_months_between()` in `app.py` compute balance as `rate × months elapsed in cycle − approved days
used`. The first cycle runs from `leave_accrual_start` to Dec 31 of that year; every cycle after
is a full Jan–Dec year. Within a cycle, accrual starts at the later of the cycle start and
the member's joining month (`User.joined_on`, the 1st of the month; backfilled on boot from the
`joined` column of `static/people.csv` only where still NULL, so admin edits win). Half-day leave (`is_half_day`) always collapses to a single date and costs
0.5 days — it has its own attendance status (`half_leave`, distinct from `on_leave`) so a morning
check-in still counts. Going over quota never blocks a request, only warns (`quota_check`) — the
admin decides whether to approve anyway.

A quota is really **two buckets**, and `leave_summary()` returns both: the general one above under
`general`, and under `menstrual` an extra `menstrual_leave_rate` (default 1) day per **calendar
month** for members with `User.gender == "female"` (`User.gets_menstrual_leave`; everyone defaults
to `"male"` and the admin flips it on the member form). The menstrual grant is deliberately *not*
cycle accrual — it expires with its month, so `_menstrual_by_month()` splits usage per month and
`menstrual["balance"]` always describes the month being asked about, never a running total. Days
beyond a month's grant spill into the general bucket rather than being refused; general leave never
draws on the grant. The top-level `rate`/`earned`/`used`/`balance` keys are the two buckets added
together — that combined number is what the member is shown (3.5/month for a female member on
default settings) — so **anything enforcing a limit must use `bucket_balance(summary, leave_type)`
rather than `summary["balance"]`**, or menstrual days become general leave. Requests of type
`MENSTRUAL_LEAVE` are rejected server-side for members who don't get the grant; the form hides the
option.

### Statuses that must not be clobbered

`LEAVE_STATUSES = ("on_leave", "half_leave")` — voiding/restoring an `Attendance` row (`void_attendance`,
`admin_void_attendance`, `admin_resolve_flag`) must check against this before resetting `status` to
`"present"`/`"voided"`, or an approved leave day gets silently overwritten.

### Members who left the lab (`User.left_on`)

`left_on` is the member's **last day** in the lab (NULL = still here), set by the member from
Profile (`profile_left_lab`; they can't undo it once the day has passed) or by the admin on the
member form. It is separate from `is_active_member`: leavers can still log in to see their history,
but `User.has_left(day)` blocks check-in and leave past that date. `User.in_lab_on(day)` / `still_in_lab(day)` also treat days before `joined_on` as "not in the lab".
**Any code that builds a roster
(absentees, member counts, per-group averages) must filter with `still_in_lab(day)`**, or people
who left keep showing up as absent.

### Raw DB editor (`/dbmod`)

A generic browse/edit/create/delete page over every model in `DBMOD_MODELS`, deliberately *not*
linked from the Admin UI. It has its own login (any active admin's Lab ID + password), stored under
its own session key (`dbmod_uid`), not Flask-Login. Its POSTs carry a per-session CSRF token. It
skips all business rules, so it's a repair tool. **A new model must be added to `DBMOD_MODELS`**
or it won't show up there.

### Statistics (`admin_statistics`, `member_stats`)

Weekends are optional: in `member_stats` Sat/Sun check-ins count towards days present and total
hours, but every rate and average (weekday attendance %, avg check-in, avg hours) is over Mon–Fri
only, and only on days the member was in the lab (`in_lab_on`).

Dependency-free: charts are built server-side as lists of `{label, value, sub}` dicts and rendered
as SVG by `static/js/charts.js`, no charting library. If you add a new chart, follow this same
shape rather than introducing a new data contract.

## Conventions worth preserving

- No abstraction layer between routes and the DB — routes call `db.session` / model classes
  directly. Keep new routes consistent with this rather than introducing a service/repository layer.
- Business-logic helper functions live at module scope in `app.py` above `register_routes`, not
  inside the closure — only the route handlers themselves are nested inside `register_routes`.
- Admin routes always stack `@login_required` then `@admin_required`, in that order.
- Flash messages use categories `success` / `danger` / `warning` / `info` consistently — match this
  when adding new ones since templates style by category.
- `fmt_days` Jinja filter renders day counts without a trailing `.0` (`2.5` → `"2.5"`, `5.0` → `"5"`)
  — use it (or its logic) anywhere a leave-day count is shown to keep formatting consistent.
