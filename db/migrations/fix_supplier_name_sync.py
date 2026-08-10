"""Migration: Fix desynchronised supplier_name on existing invoices.

Invoices imported before canonical-name propagation was added may have
supplier_name values that differ from the current suppliers.name.  This
script detects and corrects those rows in one UPDATE statement, then
verifies the fix.

Run directly:
    python -m db.migrations.fix_supplier_name_sync
"""
import sys
import os

# Allow running from the repo root as `python -m db.migrations.fix_supplier_name_sync`
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from db.faturas import get_supplier_name_mismatches
from db.connection import db_connection


def run() -> None:
    # --- Diagnose ---
    mismatches = get_supplier_name_mismatches()
    if not mismatches:
        print("✅ No supplier_name mismatches found — nothing to fix.")
        return

    print(f"⚠️  Found {len(mismatches)} invoice(s) with a desynchronised supplier_name:")
    for row in mismatches:
        print(
            f"   invoice_id={row['invoice_id']}  supplier_id={row['supplier_id']}"
            f"  stored={row['stored_name']!r}  canonical={row['canonical_name']!r}"
        )

    # --- Fix ---
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE invoices
            SET    supplier_name = s.name
            FROM   suppliers s
            WHERE  s.id = invoices.supplier_id
              AND  invoices.supplier_name IS DISTINCT FROM s.name
        """)
        updated = cursor.rowcount
        conn.commit()

    print(f"✅ Updated {updated} invoice(s).")

    # --- Verify ---
    remaining = get_supplier_name_mismatches()
    if remaining:
        print(f"❌ {len(remaining)} mismatch(es) still remain after the update — investigate manually.")
        sys.exit(1)
    else:
        print("✅ Verification passed — zero mismatches remaining.")


if __name__ == "__main__":
    run()
