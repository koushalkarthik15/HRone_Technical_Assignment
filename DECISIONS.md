# Design decisions

1. **Indexes.** Unique employee-code and employee/date indexes enforce identity and one daily record. Date/employee and status/date/employee indexes support attendance filtering and ordering. Employee/punch-time finds the latest qualifying punch. Department/code orders employee lists; joined-date/department and department/joined-date cover global and department headcount queries. I rejected a standalone status index: the compound index also supports date filtering and ordering. Index hints are shared with explain queries.

2. **Punch-in race.** Both requests validate the employee and calculate the same attendance date, then attempt insertion. MongoDB's unique employee/date index allows exactly one insert. That request returns 201; the duplicate-key exception becomes 409. There is no check-then-insert assumption.

3. **Ties.** MongoDB ranks by total late minutes alone, using competition ranking. Filtering rank <= limit preserves every tie at the cutoff. Employee code provides deterministic ordering only after ranking.

4. **Headcount.** The summary starts from eligible employees, looks up their monthly logs, and preserves employees with an empty lookup. Each employee contributes one to headcount. Work-hour sums and counts produce a record-weighted average.

5. **At 100x.** I would materialize daily department totals with explicit invalidation when attendance is corrected, reducing repeated historical aggregation while preserving the audit trail.
