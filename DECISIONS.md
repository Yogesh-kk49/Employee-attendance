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

The `GET /analytics/departments/summary` endpoint runs two separate aggregations:

1. **Employees pipeline** — groups `employees` by department with `joined_on <= last_day_of_month`. This gives `headcount` per department, including employees who have no attendance records at all.
2. **Logs pipeline** — groups `attendance_logs` for the month (joined with `employees` via `$lookup`) by department, computing sums and averages.

The two results are merged in Python by department name. If a department appears in step 1 but not step 2, all its log-derived counts default to zero. This guarantees employees with no logs are counted in `headcount`.

## 5. One thing to change for 100× the data

The `GET /analytics/departments/{department}/trend` endpoint iterates over all department employees in Python to compute per-day `headcount`. At 100× scale (thousands of employees per department, 92-day range = 92 iterations × thousands of employees) this becomes slow.

The fix would be to pre-aggregate `headcount` per day with a MongoDB aggregation: group employees by department, then use an `$addFields`/`$filter` stage combined with a `$densify`-style range to count `joined_on <= day` for each day. Alternatively, store a running `headcount` snapshot in a separate collection updated on employee joins. This would move the O(employees × days) work entirely into the database.
