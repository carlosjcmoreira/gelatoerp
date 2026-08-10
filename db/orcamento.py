"""Budget (Orçamento) data model.

Stores monthly budget targets by (year, month, store, line).

line_key conventions:
  - 'vendas'    : total sales budget
  - 'cmvmc'     : cost of goods sold budget
  - 'cat_<id>'  : operating cost category budget (maps to cost_categories.id)
"""
import logging
from db.connection import db_connection, db_retry

logger = logging.getLogger(__name__)

_LOCK_ORCAMENTO = 202630


def run_migrations_orcamento() -> None:
    """Create orcamento table. Advisory lock 202630 for cross-worker idempotency."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_ORCAMENTO,))
        if not cursor.fetchone()[0]:
            logger.info("run_migrations_orcamento: lock held by another worker, skipping")
            return

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orcamento (
                id          SERIAL PRIMARY KEY,
                ano         INT NOT NULL,
                mes         INT NOT NULL CHECK (mes BETWEEN 1 AND 12),
                store_id    INT,
                line_key    VARCHAR(100) NOT NULL,
                valor_euros NUMERIC(12,2) NOT NULL DEFAULT 0,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Functional unique index: COALESCE(store_id, 0) so a NULL store_id
        # (global budget) is distinguishable from store-specific rows.
        # store_id=0 is never a valid stores.id (SERIAL starts at 1).
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_orcamento
            ON orcamento (ano, mes, COALESCE(store_id, 0), line_key)
        """)

        conn.commit()
        logger.info("run_migrations_orcamento: orcamento table ready")


def get_valid_line_keys() -> set:
    """Return the set of currently accepted line_key values.

    Includes the two built-in keys ('vendas', 'cmvmc') plus one 'cat_<id>'
    entry for every row in cost_categories.  Results are NOT cached because
    categories can change at runtime.
    """
    valid = {'vendas', 'cmvmc'}
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM cost_categories")
        for row in cursor.fetchall():
            valid.add(f'cat_{row[0]}')
    return valid


@db_retry
def get_orcamento(ano: int, store_id=None) -> dict:
    """Return {line_key: {month_int: valor_euros}} for year + store.

    store_id=None → global budget (store_id IS NULL rows).
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        if store_id is None:
            cursor.execute(
                "SELECT line_key, mes, valor_euros "
                "FROM orcamento WHERE ano = %s AND store_id IS NULL",
                (ano,)
            )
        else:
            cursor.execute(
                "SELECT line_key, mes, valor_euros "
                "FROM orcamento WHERE ano = %s AND store_id = %s",
                (ano, store_id)
            )
        rows = cursor.fetchall()

    result: dict = {}
    for line_key, mes, valor in rows:
        result.setdefault(line_key, {})[mes] = float(valor)
    return result


@db_retry
def get_orcamento_all_stores(ano: int) -> dict:
    """Return {store_id_or_None: {line_key: {month: valor}}} for a full year.

    Useful for consolidated mapa where we need every store's budget at once.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT store_id, line_key, mes, valor_euros FROM orcamento WHERE ano = %s",
            (ano,)
        )
        rows = cursor.fetchall()

    result: dict = {}
    for store_id, line_key, mes, valor in rows:
        key = store_id  # None for global
        result.setdefault(key, {}).setdefault(line_key, {})[mes] = float(valor)
    return result


def upsert_orcamento(ano: int, mes: int, store_id, line_key: str, valor: float) -> None:
    """Insert or update a single budget cell."""
    sid = int(store_id) if store_id else None
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO orcamento (ano, mes, store_id, line_key, valor_euros, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (ano, mes, COALESCE(store_id, 0), line_key) DO UPDATE
                SET valor_euros = EXCLUDED.valor_euros,
                    updated_at  = NOW()
        """, (ano, mes, sid, line_key, valor))
        conn.commit()


def bulk_upsert_orcamento(rows: list) -> int:
    """Bulk upsert budget rows.

    Each row dict: {ano, mes, store_id (int or None), line_key, valor_euros}.
    Returns the number of rows processed.
    """
    if not rows:
        return 0
    with db_connection() as conn:
        cursor = conn.cursor()
        for r in rows:
            sid = int(r['store_id']) if r.get('store_id') else None
            cursor.execute("""
                INSERT INTO orcamento (ano, mes, store_id, line_key, valor_euros, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (ano, mes, COALESCE(store_id, 0), line_key) DO UPDATE
                    SET valor_euros = EXCLUDED.valor_euros,
                        updated_at  = NOW()
            """, (r['ano'], r['mes'], sid, r['line_key'],
                  float(r.get('valor_euros', 0))))
        conn.commit()
    return len(rows)
