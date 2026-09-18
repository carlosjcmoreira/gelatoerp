# Test commands

The standard merge check remains:

```bash
python -m unittest discover -s tests -p 'test*.py'
```

For an additional reliability check, run:

```bash
python scripts/test_clean_database.py
```

The clean-database runner requires `DATABASE_URL`. It creates a uniquely named
PostgreSQL database, initializes it through the application factory, runs the
same full unittest discovery command with that database selected, and drops it
when the run finishes. This catches test-order and fixture-leakage problems
without reading from or writing to the project's working database. The one
data-specific historical verification class is skipped because an empty
database deliberately has no production `receitas_gelado` history; that class
continues to run in the standard suite.