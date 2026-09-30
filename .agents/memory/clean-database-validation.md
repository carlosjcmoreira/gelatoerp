---
name: Clean database validation
description: Rules for running the full PostgreSQL test suite against a disposable empty database.
---

The isolated regression run must create a disposable PostgreSQL database, initialize
it through the application factory, execute the normal unittest discovery command
with that database selected, and always remove the database afterward. Checks that
assert historical production data should be explicitly separated from empty-schema
integration checks rather than silently assuming that data exists.

**Why:** An empty database exposed migration-order dependencies and showed that
historical-data assertions are a different kind of check from schema isolation.

**How to apply:** Keep the ordinary merge command available, use the disposable
database command as an additional reliability check, and make any excluded live
data checks explicit in both the test environment and its documentation.

Database test fixtures that use `db_connection()` must explicitly commit setup
and teardown writes; returning a connection to the pool rolls back uncommitted
work. Use a unique per-run test actor as well as cleanup so retries cannot count
rows left by an earlier process.

**Why:** A real-database idempotency test intermittently counted an old fixture
row because its cleanup ran inside a transaction that was rolled back.

**How to apply:** Commit fixture deletes before assertions and after cleanup;
scope deletions to a unique test-run identity.