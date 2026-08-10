"""DB helpers for the Custos Recorrentes module.

Replaces the former Avenças + Débitos Diretos split.
"""
import calendar
import logging
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
from psycopg2.extras import RealDictCursor
from db.core import db_connection

logger = logging.getLogger(__name__)

__all__ = [
    'get_custos_recorrentes',
    'get_custo_recorrente',
    'create_custo_recorrente',
    'update_custo_recorrente',
    'delete_custo_recorrente',
    'suggest_valor',
    'get_last_invoices',
    'next_due_date_cr',
    'run_backfill_custos_recorrentes',
    'FREQUENCIA_LABELS',
    'TIPOLOGIA_LABELS',
]

FREQUENCIA_LABELS = {
    'semanal':    'Semanal',
    'quinzenal':  'Quinzenal',
    'mensal':     'Mensal',
    'trimestral': 'Trimestral',
    'semestral':  'Semestral',
    'anual':      'Anual',
}

TIPOLOGIA_LABELS = {
    'fixo':     'Fixo',
    'variavel': 'Variável',
}

_PERIOD_MONTHS = {'mensal': 1, 'trimestral': 3, 'semestral': 6, 'anual': 12}
_PERIOD_DAYS   = {'semanal': 7, 'quinzenal': 14}


# ── next_due_date ────────────────────────────────────────────────────────────

def next_due_date_cr(entry, from_date: date):
    """Return the next occurrence of a custo_recorrente on or after *from_date*."""
    ref = entry.get('data_cobranca')
    if not ref:
        return None
    if isinstance(ref, str):
        try:
            ref = date.fromisoformat(ref)
        except ValueError:
            return None

    freq = entry.get('frequencia', '')

    if from_date <= ref:
        return ref

    if freq in _PERIOD_DAYS:
        days = _PERIOD_DAYS[freq]
        delta = (from_date - ref).days
        n = (delta + days - 1) // days
        return ref + timedelta(days=n * days)

    if freq in _PERIOD_MONTHS:
        months = _PERIOD_MONTHS[freq]
        diff_months = (from_date.year - ref.year) * 12 + (from_date.month - ref.month)
        n = max(0, (diff_months + months - 1) // months)
        cand = ref + relativedelta(months=n * months)
        max_day = calendar.monthrange(cand.year, cand.month)[1]
        cand = date(cand.year, cand.month, min(ref.day, max_day))
        if cand < from_date:
            cand = ref + relativedelta(months=(n + 1) * months)
            max_day = calendar.monthrange(cand.year, cand.month)[1]
            cand = date(cand.year, cand.month, min(ref.day, max_day))
        return cand

    return None


# ── CRUD ─────────────────────────────────────────────────────────────────────

def get_custos_recorrentes(ativo_only: bool = False):
    """Return all entries joined with supplier and cost centre names."""
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        where = "WHERE cr.ativo = TRUE" if ativo_only else ""
        cur.execute(f"""
            SELECT cr.*,
                   s.name  AS supplier_name,
                   cc.name AS centro_custo_name
            FROM custos_recorrentes cr
            JOIN suppliers     s  ON s.id  = cr.supplier_id
            LEFT JOIN cost_centers cc ON cc.id = cr.centro_custo_id
            {where}
            ORDER BY s.name, cr.frequencia
        """)
        return cur.fetchall()


def get_custo_recorrente(entry_id: int):
    """Return a single entry by id, or None."""
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT cr.*,
                   s.name  AS supplier_name,
                   cc.name AS centro_custo_name
            FROM custos_recorrentes cr
            JOIN suppliers     s  ON s.id  = cr.supplier_id
            LEFT JOIN cost_centers cc ON cc.id = cr.centro_custo_id
            WHERE cr.id = %s
        """, (entry_id,))
        return cur.fetchone()


def create_custo_recorrente(supplier_id, tipologia, frequencia, data_cobranca,
                             centro_custo_id=None, valor=None, notas=None, ativo=True):
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO custos_recorrentes
                (supplier_id, tipologia, frequencia, data_cobranca,
                 centro_custo_id, valor, notas, ativo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (supplier_id, tipologia, frequencia, data_cobranca,
              centro_custo_id, valor, notas, ativo))
        new_id = cur.fetchone()[0]
        conn.commit()
        return new_id


def update_custo_recorrente(entry_id: int, **kwargs):
    allowed = {
        'supplier_id', 'tipologia', 'frequencia', 'data_cobranca',
        'centro_custo_id', 'valor', 'notas', 'ativo',
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ', '.join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [entry_id]
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE custos_recorrentes SET {set_clause}, updated_at = NOW() WHERE id = %s",
            values,
        )
        conn.commit()


def delete_custo_recorrente(entry_id: int):
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM custos_recorrentes WHERE id = %s", (entry_id,))
        conn.commit()


# ── Suggestion ───────────────────────────────────────────────────────────────

def suggest_valor(supplier_id: int, frequencia: str):
    """Return average invoice amount per frequency period for the given supplier.

    Returns ``{'valor': float, 'n_periodos': int}`` or ``{'valor': None, 'n_periodos': 0}``.
    """
    trunc_map = {
        'semanal':    "date_trunc('week', issue_date)",
        'quinzenal':  (
            "to_char(issue_date, 'IYYY') || '-' || "
            "lpad(ceil(date_part('doy', issue_date) / 14.0)::text, 3, '0')"
        ),
        'mensal':     "date_trunc('month', issue_date)",
        'trimestral': "date_trunc('quarter', issue_date)",
        'semestral':  (
            "to_char(issue_date, 'YYYY') || '-' || "
            "CASE WHEN EXTRACT(MONTH FROM issue_date) <= 6 THEN '1' ELSE '2' END"
        ),
        'anual':      "date_trunc('year', issue_date)",
    }
    trunc_expr = trunc_map.get(frequencia)
    if not trunc_expr:
        return {'valor': None, 'n_periodos': 0}

    sql = f"""
        SELECT AVG(period_total)::NUMERIC(12,2) AS avg_val,
               COUNT(*)                          AS n_periods
        FROM (
            SELECT {trunc_expr} AS period,
                   SUM(amount_eur)               AS period_total
            FROM invoices
            WHERE supplier_id = %s
              AND status != 'cancelled'
              AND issue_date IS NOT NULL
            GROUP BY {trunc_expr}
        ) sub
    """
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, (supplier_id,))
        row = cur.fetchone()
        if row and row[0] is not None:
            return {'valor': float(row[0]), 'n_periodos': int(row[1])}
        return {'valor': None, 'n_periodos': 0}


# ── Invoices ─────────────────────────────────────────────────────────────────

def get_last_invoices(supplier_id: int, limit: int = 5):
    """Return the last *limit* non-cancelled invoices for a supplier."""
    with db_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT i.id,
                   COALESCE(NULLIF(i.invoice_number, ''), '—') AS invoice_number,
                   i.amount_eur,
                   i.due_date,
                   i.issue_date,
                   i.status
            FROM invoices i
            WHERE i.supplier_id = %s
              AND i.status != 'cancelled'
            ORDER BY COALESCE(i.issue_date, i.due_date) DESC NULLS LAST
            LIMIT %s
        """, (supplier_id, limit))
        return cur.fetchall()


# ── One-time backfill ─────────────────────────────────────────────────────────

_BACKFILL_LOCK = 202621  # distinct from DDL lock 202620


def run_backfill_custos_recorrentes():
    """Idempotent one-time migration of debitos_directos JSON + avencas table rows.

    Tracked via the cashflow_config key ``custos_recorrentes_backfill_done``.

    Uses a *blocking* PostgreSQL advisory lock (202621) so exactly one Gunicorn
    worker performs the inserts.  Other workers block at the lock acquisition
    until the winner commits, then re-check the completion key and exit.

    Each supplier creation uses a SAVEPOINT so a unique-constraint collision
    never aborts the outer transaction.
    """
    import json as _json
    import calendar as _cal
    from datetime import date as _date

    with db_connection() as conn:
        cur = conn.cursor()

        # Fast path — skip without locking if already done on a prior startup.
        cur.execute(
            "SELECT 1 FROM cashflow_config WHERE key = 'custos_recorrentes_backfill_done'"
        )
        if cur.fetchone():
            return

        # Blocking advisory lock — exactly one worker runs the backfill.
        # Others wait here until the winner commits and releases the lock.
        cur.execute("SELECT pg_advisory_lock(%s)", (_BACKFILL_LOCK,))
        try:
            # Re-check after acquiring: another worker may have just finished.
            cur.execute(
                "SELECT 1 FROM cashflow_config WHERE key = 'custos_recorrentes_backfill_done'"
            )
            if cur.fetchone():
                return

            today    = _date.today()
            inserted = 0
            failed: list = []   # per-record error messages; non-empty → do not mark done

            def _find_or_create_supplier(name: str):
                """Return supplier id, creating a row only if no name match exists.

                A SAVEPOINT guards the INSERT so a unique-constraint violation
                (name already present from a concurrent insert) is rolled back at
                savepoint level without aborting the outer transaction.
                """
                cur.execute(
                    "SELECT id FROM suppliers WHERE lower(name) = lower(%s) LIMIT 1",
                    (name,),
                )
                row = cur.fetchone()
                if row:
                    return row[0]
                cur.execute("SAVEPOINT sp_create_supplier")
                try:
                    cur.execute(
                        "INSERT INTO suppliers (name, created_at, updated_at)"
                        " VALUES (%s, NOW(), NOW()) RETURNING id",
                        (name,),
                    )
                    new_row = cur.fetchone()
                    cur.execute("RELEASE SAVEPOINT sp_create_supplier")
                    return new_row[0] if new_row else None
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_create_supplier")
                    # Race/unique-constraint — row may now exist, re-try SELECT
                    cur.execute(
                        "SELECT id FROM suppliers WHERE lower(name) = lower(%s) LIMIT 1",
                        (name,),
                    )
                    row = cur.fetchone()
                    return row[0] if row else None

            def _lookup_cc(name_str: str):
                if not name_str:
                    return None
                cur.execute(
                    "SELECT id FROM cost_centers WHERE lower(name) = lower(%s) LIMIT 1",
                    (name_str,),
                )
                row = cur.fetchone()
                return row[0] if row else None

            def _already_migrated(supp_id: int, notas_marker: str) -> bool:
                """True if this supplier was already inserted by a previous backfill run.

                Checked before every INSERT so retries after partial failures do
                not create duplicate records.
                """
                cur.execute(
                    "SELECT 1 FROM custos_recorrentes"
                    " WHERE supplier_id = %s AND notas = %s LIMIT 1",
                    (supp_id, notas_marker),
                )
                return cur.fetchone() is not None

            # ── 1. Backfill from debitos_directos JSON ────────────────────
            # Each source record is processed in its own SAVEPOINT so a bad row
            # cannot abort the outer transaction.  Errors go into `failed`; the
            # completion marker is only written when `failed` is empty.
            try:
                cur.execute(
                    "SELECT value FROM cashflow_config WHERE key = 'debitos_directos'"
                )
                cfg_row = cur.fetchone()
            except Exception as exc:
                failed.append(f"debitos_directos query: {exc}")
                cfg_row = None

            if cfg_row and cfg_row[0]:
                try:
                    debitos = _json.loads(cfg_row[0])
                except _json.JSONDecodeError as exc:
                    failed.append(f"debitos_directos JSON parse: {exc}")
                    debitos = []

                _DEBITO_MARKER = 'Migrado automaticamente de Débitos Diretos'
                for d in debitos:
                    label     = (d.get('label') or '').strip()
                    valor_eur = d.get('valor_eur')
                    dia_raw   = d.get('dia_debito')
                    cc_str    = (d.get('centro_custo') or '').strip()
                    if not label or not valor_eur:
                        continue   # genuinely empty/placeholder entry
                    cur.execute("SAVEPOINT sp_debito_row")
                    try:
                        supp_id = _find_or_create_supplier(label)
                        if not supp_id:
                            raise ValueError(
                                f"could not find or create supplier '{label}'"
                            )
                        if _already_migrated(supp_id, _DEBITO_MARKER):
                            cur.execute("RELEASE SAVEPOINT sp_debito_row")
                            inserted += 1
                            continue
                        cc_id     = _lookup_cc(cc_str)
                        dia       = int(dia_raw)
                        max_day   = _cal.monthrange(today.year, today.month)[1]
                        data_cobr = _date(today.year, today.month, min(dia, max_day))
                        cur.execute(
                            """
                            INSERT INTO custos_recorrentes
                                (supplier_id, tipologia, frequencia, data_cobranca,
                                 centro_custo_id, valor, notas, ativo)
                            VALUES (%s, 'fixo', 'mensal', %s, %s, %s, %s, TRUE)
                            """,
                            (supp_id, data_cobr, cc_id, valor_eur, _DEBITO_MARKER),
                        )
                        cur.execute("RELEASE SAVEPOINT sp_debito_row")
                        inserted += 1
                    except Exception as row_exc:
                        cur.execute("ROLLBACK TO SAVEPOINT sp_debito_row")
                        failed.append(f"debito '{label}': {row_exc}")
                        logger.warning(
                            "run_backfill_custos_recorrentes: debito '%s': %s",
                            label, row_exc,
                        )

            # ── 2. Backfill from avencas table (if it exists and has rows) ─
            try:
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'public' AND table_name = 'avencas'
                    )
                    """
                )
                avencas_exists = cur.fetchone()[0]
            except Exception as exc:
                failed.append(f"avencas existence check: {exc}")
                avencas_exists = False

            if avencas_exists:
                try:
                    cur.execute(
                        """
                        SELECT nome, valor, periodicidade, dia_vencimento,
                               data_inicio, centro_custo_id, observacoes
                        FROM avencas WHERE ativo = TRUE
                        """
                    )
                    avenca_rows = cur.fetchall()
                except Exception as exc:
                    failed.append(f"avencas query: {exc}")
                    avenca_rows = []

                _AVENCA_MARKER = 'Migrado automaticamente de Avenças'
                for (nome, valor, periodicidade, dia_ven,
                     data_inicio, cc_id, obs) in avenca_rows:
                    if not nome or not valor:
                        continue
                    cur.execute("SAVEPOINT sp_avenca_row")
                    try:
                        supp_id = _find_or_create_supplier(nome)
                        if not supp_id:
                            raise ValueError(
                                f"could not find or create supplier '{nome}'"
                            )
                        if _already_migrated(supp_id, _AVENCA_MARKER):
                            cur.execute("RELEASE SAVEPOINT sp_avenca_row")
                            inserted += 1
                            continue
                        max_day   = _cal.monthrange(today.year, today.month)[1]
                        day       = min(int(dia_ven or 1), max_day)
                        data_cobr = data_inicio or _date(today.year, today.month, day)
                        cur.execute(
                            """
                            INSERT INTO custos_recorrentes
                                (supplier_id, tipologia, frequencia, data_cobranca,
                                 centro_custo_id, valor, notas, ativo)
                            VALUES (%s, 'fixo', %s, %s, %s, %s, %s, TRUE)
                            """,
                            (supp_id, periodicidade, data_cobr, cc_id, valor,
                             obs or _AVENCA_MARKER),
                        )
                        cur.execute("RELEASE SAVEPOINT sp_avenca_row")
                        inserted += 1
                    except Exception as row_exc:
                        cur.execute("ROLLBACK TO SAVEPOINT sp_avenca_row")
                        failed.append(f"avenca '{nome}': {row_exc}")
                        logger.warning(
                            "run_backfill_custos_recorrentes: avenca '%s': %s",
                            nome, row_exc,
                        )

            # ── Mark done ONLY when every source record succeeded ─────────
            if failed:
                # Commit successfully migrated records but do not write the
                # completion marker so the next startup retries the failures.
                conn.commit()
                logger.error(
                    "run_backfill_custos_recorrentes: %d failure(s) — "
                    "will retry on next startup. Failures: %s",
                    len(failed), failed,
                )
            else:
                cur.execute(
                    """
                    INSERT INTO cashflow_config (key, value, updated_at)
                    VALUES ('custos_recorrentes_backfill_done', 'true', NOW())
                    ON CONFLICT (key) DO UPDATE SET value = 'true', updated_at = NOW()
                    """,
                )
                conn.commit()
                logger.info(
                    "run_backfill_custos_recorrentes: migrated %d entries", inserted
                )
        finally:
            try:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_BACKFILL_LOCK,))
                conn.commit()
            except Exception:
                pass
