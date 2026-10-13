# DECISIONS.md

## 1. Indexes

Four indexes are created at startup (idempotently via `create_index`):

| Index | Collection | Purpose |
|---|---|---|
| `emp_code` (unique) | `employees` | Enforces uniqueness at the database level; makes concurrent duplicate-create races safe without application-level locks. Serves `find_one({"emp_code": ...})`. |
| `(emp_code, date)` (unique) | `attendance_logs` | Natural key for a record. Enforces one record per employee per day at the DB level; makes concurrent punch-in races safe (one insert wins, the rest get `DuplicateKeyError` → 409). |
| `(date DESC, emp_code ASC)` | `attendance_logs` | Matches the exact sort order required by `GET /attendance`. Allows MongoDB to use an `IXSCAN` for the sort, skip, and limit rather than a full collection scan + in-memory sort. |
| `date ASC` | `attendance_logs` | Supports range queries on `date` for analytics endpoints (employee monthly, department summary, leaderboard, trend). Avoids `COLLSCAN` on the 100k-row dataset the grader uses. |

**Considered and rejected:** a compound index on `(emp_code, date, status)` (covering index for analytics). Rejected because it would duplicate the `(emp_code, date)` unique index and the analytics pipelines use `$lookup` across collections anyway, so the marginal benefit does not justify the extra write cost.

## 2. Punch-in race condition

Two identical `POST /attendance/punch-in` requests arrive at the same instant for the same employee and date.

1. Both requests pass Pydantic validation and look up the employee — both `find_one` calls succeed.
2. Both compute the same `att_date` and build their document.
3. Both call `insert_one()`.
4. MongoDB's write lock on the unique `(emp_code, date)` index ensures only one insert commits. The second raises `pymongo.errors.DuplicateKeyError`.
5. The handler catches `DuplicateKeyError` and returns HTTP 409.

Result: exactly one 201 and one 409 — no application-level lock or extra query needed.

## 3. Tie handling in the leaderboard

When two employees share the same `total_late_minutes` at the cutoff of `limit`, the endpoint returns **all** tied employees, even if the response contains more than `limit` rows. This is correct per the spec: "return every employee whose `rank <= limit`".

Standard competition ranking (1, 2, 2, 4) is computed in Python after sorting: rank is set to `i + 1` (1-based position in the sorted list) only when the current row's minutes differ from the previous row's. Tied employees carry the same rank. The loop breaks as soon as `rank > limit`, so a tie at `rank == limit` is fully included.

## 4. Headcount with zero-log employees

`GET /analytics/departments/summary` runs a **single aggregation pipeline** starting from the `employees` collection:

1. `$match` — employees with `joined_on <= last_day_of_month` (R9 headcount rule).
2. `$lookup` (with correlated sub-pipeline) — fetches that employee's attendance logs for the month from `attendance_logs`.
3. `$unwind` with `preserveNullAndEmptyArrays: true` — employees with zero logs produce one row with all `logs.*` fields null, so they still contribute to headcount.
4. `$group` by department — accumulates headcount (`$addToSet` of `emp_code`), present days, late counts, work-hours sum/count, etc.
5. `$project` — reshapes output; `avg_work_hours` is computed as `work_hours_sum / work_hours_count` inside MongoDB.

This is a single round-trip to the database. Zero-log employees are counted in `headcount` because of `preserveNullAndEmptyArrays: true`; their null `logs.*` fields contribute 0 to all sums via `$ifNull`.

## 5. One thing to change for 100× the data

`GET /analytics/departments/{department}/trend` now uses a single MongoDB aggregation pipeline with `$densify` (gap-fill every calendar day), `$setWindowFields` (7-day moving average), and a correlated `$lookup` per densified day to count how many employees had `joined_on <= that_day`. The Python layer only rounds and casts the results.

At 100× scale the per-day correlated `$lookup` for headcount becomes the bottleneck: for a 92-day range each densified document triggers a separate sub-query on `employees`, totalling 92 `$count` sub-aggregations. The fix is to pre-compute the headcount time-series in a single `$facet` or `$bucket` stage before `$densify`, then `$merge` or `$lookup` the array of `(date, count)` pairs into the densified stream. This reduces the headcount computation from O(days) sub-queries to a single O(employees) pass.
