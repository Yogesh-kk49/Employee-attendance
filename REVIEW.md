# REVIEW.md

Defects found in the starter `app/main.py`. Line numbers refer to the starter.

| # | Where | What is wrong | How you'd notice it | Fix |
|---|---|---|---|---|
| 1 | `health` | Returns ok without pinging MongoDB. | Stop Mongo; `/health` still returns 200. | Ping; 503 on failure. |
| 2 | `create_employee` | Check-then-insert, no unique index. | Two parallel POSTs with the same `emp_code` both get 201. | Unique index at startup; `DuplicateKeyError` -> 409. |
| 3 | `create_employee` | `created_at = datetime.now()` is naive local time and is returned as an ISO string. | `created_at` is `"2026-07-06T10:30:00"`, not an integer. | UTC-aware time; response converted to epoch ms. |
| 4 | `EmployeeIn` | No validation of `emp_code`, email, lengths, `HH:MM`, `joined_on`, or `shift_start == shift_end`. | POST with `shift_start == shift_end` returns 201. | Pydantic patterns and a model validator -> 422. |
| 5 | `list_employees` L93 | `skip = page * page_size`. | `?page=1` skips the first page. | `(page - 1) * page_size`. |
| 6 | `list_employees` L94 | `total` ignores the department filter. | `?department=Sales` returns the whole-collection total. | `count_documents(q)`. |
| 7 | `list_employees` | No sort, and no bounds on `page` / `page_size` (R10). | `page=0` gives a negative skip; `page_size=100000` is accepted. | Sort by `emp_code`; `Query(ge=1, le=100)`. |
| 8 | `punch_in` L101 | Unknown employee gives `None["shift_start"]`, a 500. | Punch in as `EMP9999`. | 404. |
| 9 | `punch_in` L102 | `fromtimestamp(ms/1000)` is naive local time, so the date shifts on non-IST hosts, and sub-seconds are kept. | `punched_at` at 22:00 UTC gets the wrong date on a UTC host. | UTC-aware `ms_to_dt`, truncated to whole seconds (R1). |
| 10 | `punch_in` L103 | The date is not the IST date, and overnight shifts are ignored (R1). | 02:00 IST on a 22:00-06:00 shift lands on the wrong day. | `attendance_date_for`. |
| 11 | `punch_in` L104 | Check-then-insert; no unique `(emp_code, date)` index. | Two parallel punch-ins both return 201. | Unique index; `DuplicateKeyError` -> 409. |
| 12 | `PunchInIn` | `status: str` accepts anything, including ABSENT, LEAVE or `"foo"`. `punched_at` has no range check, and `if body.punched_at` treats 0 as "now". | Punch in with `status: "LEAVE"`; send seconds instead of ms. | Presence-only validator; epoch-ms range check -> 422. |
| 13 | `punch_in` L119, `list_attendance` L149 | Responses add `id = str(_id)`, but `_id` must never appear. Datetimes are returned raw, not epoch ms. | Look at any attendance response body. | Strip `_id`; `fmt_record` converts to epoch ms. |
| 14 | `compute_late_minutes` L36-37 | Floors to whole minutes **before** comparing `> 10`, so 09:40:01-09:40:59 gives 0 instead of 10 (R2). It also builds `start` from the punch's own date. | 09:40:01 on a 09:30 shift returns 0. | Compare in seconds (`> 600`), then floor. `start` comes from the attendance date in IST. |
| 15 | `compute_overtime` L44-47 | No 30-minute minimum (R3), and the shift end is always on the same day, so overnight shifts are wrong. | Punch-out 10 minutes after shift end gives overtime 10, not 0. | Minimum 30 min (`< 1800 s` gives 0); next-day end for overnight shifts. |
| 16 | `compute_work_hours` L41 | Built-in `round()` is banker's rounding (R4). | 32850 s gives 9.12, not 9.13. | `Decimal` with `ROUND_HALF_UP`. |
| 17 | `list_attendance` L144-147 | Loads the whole collection into Python, sorts only by `date`, paginates in memory. | 100k records: slow, high memory, unstable order within a date. | DB-side sort `(date desc, emp_code asc)`, skip, limit, plus the matching index. |
| 18 | `list_attendance` | `date_from > date_to` gives an empty page; a bad `status` or date string is accepted. | `?date_from=2026-07-10&date_to=2026-07-01`. | 422 for each. |
| 19 | module | `MongoClient` uses the default 30 s server-selection timeout, so `/health` hangs when Mongo is down. | `/health` with Mongo stopped. | `serverSelectionTimeoutMS=5000`. |

## Found in my own first version (not starter defects)
- `parse_date` used `fromisoformat`, which on Python 3.11+ accepts `20260706`, `2026-W28-1` and `2026-07-06T10:00`. Now a strict `YYYY-MM-DD` regex check.
- Punch-out only looked for open records. It now takes the latest record at or before `punched_at` and returns 409 if it is already closed.
- PATCH only guarded on history length, so it could overwrite a concurrent punch-out. The update filter now also pins status, `punch_in` and `punch_out`.
- Index creation swallowed every error. It now retries and fails loudly.
- Trend returned nothing when a department had no logs in the range; a `$unionWith` seed row now guarantees `$densify` has input.
- Leaderboard ranking ran in a Python loop. It now uses `$setWindowFields` / `$rank`.

## Looked at and fine
- `IST = UTC+05:30` is correct.
- Plain `def` handlers run in FastAPI's thread pool, and `MongoClient` is thread-safe.
- `sample_seed.py` loads BSON dates correctly.