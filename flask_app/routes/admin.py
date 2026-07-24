"""
flask_app/routes/admin.py
--------------------------
Admin-only utility endpoints.  All routes require the 'acesso_administrativo'
permission (i.e. gestao role or equivalent).

Endpoints
---------
GET /admin/eurokg-snapshot
    Reads the saved Euro/kg baseline snapshot, queries the current production
    totals with the same filters used by get_producao_total_by_period, and
    returns a JSON diff report.  This endpoint is read-only — it never writes
    to the database or the snapshot file.
"""
import os
import json
import calendar
import logging
from datetime import date, datetime
from flask import Blueprint, jsonify, g
from flask_app.auth import perm_required

logger = logging.getLogger(__name__)

admin_bp = Blueprint('admin', __name__)

SNAPSHOT_PATH = os.path.join(
    os.path.dirname(__file__), '..', '..', 'scripts', 'eurokg_snapshot.json'
)

TIPOS_PRODUCAO_EUROKG = ('producao', 'balança')


def _get_excluidos(conn):
    """Return list of sabor names excluded from Euro/kg (conta_eurokg = FALSE)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(nome_corrente, nome) AS canonical "
            "FROM receitas_gelado WHERE conta_eurokg = FALSE"
        )
        return [r[0] for r in cur.fetchall()]


def _snapshot_current(conn):
    """
    Query current monthly Euro/kg totals (last 3 years) from the DB.
    Applies the same WHERE clause as get_producao_total_by_period(para_eurokg=True).
    Returns { "YYYY-MM": { loja: total_kg } }
    """
    excluidos = _get_excluidos(conn)
    year_now = date.today().year
    start_year = year_now - 2

    query = """
        SELECT
            TO_CHAR(data, 'YYYY-MM') AS month,
            loja,
            COALESCE(SUM(quantidade_kg), 0) AS total_kg
        FROM producao
        WHERE tipo = ANY(%s)
          AND sabor IS NOT NULL
          AND data >= %s
    """
    params = [list(TIPOS_PRODUCAO_EUROKG), date(start_year, 1, 1)]
    if excluidos:
        query += " AND sabor != ALL(%s)"
        params.append(excluidos)
    query += " GROUP BY month, loja ORDER BY month, loja"

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    snapshot = {}
    for month, loja, total_kg in rows:
        if month not in snapshot:
            snapshot[month] = {}
        snapshot[month][loja] = float(total_kg)
    return snapshot


def _sabor_breakdown_for_month(conn, month_str: str):
    """
    Returns { loja: { sabor: { total_kg, row_count } } } for a given YYYY-MM.
    Uses the same Euro/kg filter (tipo + sabor exclusions).
    """
    excluidos = _get_excluidos(conn)
    year, month = month_str.split('-')
    last_day = calendar.monthrange(int(year), int(month))[1]
    start = date(int(year), int(month), 1)
    end = date(int(year), int(month), last_day)

    query = """
        SELECT loja, sabor,
               COALESCE(SUM(quantidade_kg), 0) AS total_kg,
               COUNT(*) AS row_count
        FROM producao
        WHERE tipo = ANY(%s)
          AND sabor IS NOT NULL
          AND data >= %s AND data <= %s
    """
    params = [list(TIPOS_PRODUCAO_EUROKG), start, end]
    if excluidos:
        query += " AND sabor != ALL(%s)"
        params.append(excluidos)
    query += " GROUP BY loja, sabor ORDER BY loja, sabor"

    result = {}
    with conn.cursor() as cur:
        cur.execute(query, params)
        for loja, sabor, total_kg, row_count in cur.fetchall():
            if loja not in result:
                result[loja] = {}
            result[loja][sabor] = {
                'total_kg': round(float(total_kg), 4),
                'row_count': int(row_count),
            }
    return result


def _check_old_names_absent(conn):
    """
    Returns list of { sabor, row_count } for old alias names still present in producao.
    An empty list means the migration fully cleaned up old sabor names.
    """
    from scripts.verify_eurokg_sabor_migration import MIGRATION_FIXES
    old_names = [old for old, _ in MIGRATION_FIXES]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sabor, COUNT(*) AS n FROM producao WHERE sabor = ANY(%s) GROUP BY sabor ORDER BY sabor",
            (old_names,)
        )
        return [{'sabor': r[0], 'row_count': int(r[1])} for r in cur.fetchall()]


@admin_bp.route('/eurokg-snapshot')
@perm_required('acesso_administrativo')
def eurokg_snapshot():
    """
    Read-only diff report comparing the saved baseline Euro/kg snapshot
    against the current production database totals.

    Returns JSON:
    {
      "run_at": "<ISO timestamp>",
      "baseline_generated_at": "<ISO timestamp> | null",
      "baseline_available": true | false,
      "months_in_baseline": N,
      "months_in_current": N,
      "grand_total_current_kg": N,
      "diff": [
        {
          "month": "YYYY-MM",
          "loja": "...",
          "baseline_kg": N,
          "current_kg": N,
          "delta_kg": N,
          "sabor_breakdown": { "<sabor>": { "total_kg": N, "row_count": N }, ... }
        },
        ...
      ],
      "old_name_stragglers": [...],
      "status": "ok" | "changed" | "stale_names" | "error"
    }
    """
    from db.connection import db_connection

    run_at = datetime.now().isoformat()

    try:
        # Load baseline
        baseline_data = None
        baseline_available = False
        baseline_generated_at = None
        baseline_totals = {}

        snap_path = os.path.normpath(SNAPSHOT_PATH)
        if os.path.exists(snap_path):
            try:
                with open(snap_path, 'r', encoding='utf-8') as f:
                    baseline_data = json.load(f)
                baseline_available = True
                baseline_generated_at = baseline_data.get('generated_at')
                baseline_totals = baseline_data.get('totals_by_month', {})
            except Exception as exc:
                logger.warning("eurokg_snapshot: could not read baseline: %s", exc)

        with db_connection() as conn:
            # Current snapshot
            current_totals = _snapshot_current(conn)

            # Diff
            diffs = []
            all_months = sorted(set(baseline_totals.keys()) | set(current_totals.keys()))
            changed_months = set()
            for month in all_months:
                b_month = baseline_totals.get(month, {})
                c_month = current_totals.get(month, {})
                all_lojas = sorted(set(b_month.keys()) | set(c_month.keys()))
                for loja in all_lojas:
                    b_kg = b_month.get(loja, 0.0)
                    c_kg = c_month.get(loja, 0.0)
                    delta = round(c_kg - b_kg, 4)
                    if abs(delta) > 0.001:
                        changed_months.add(month)
                        diffs.append({
                            'month': month,
                            'loja': loja,
                            'baseline_kg': round(b_kg, 4),
                            'current_kg': round(c_kg, 4),
                            'delta_kg': delta,
                            'sabor_breakdown': {},
                        })

            # Enrich changed months with per-sabor breakdown
            breakdowns = {}
            for month in changed_months:
                breakdowns[month] = _sabor_breakdown_for_month(conn, month)
            for d in diffs:
                month_bk = breakdowns.get(d['month'], {})
                d['sabor_breakdown'] = month_bk.get(d['loja'], {})

            # Check for stale old-name rows
            try:
                stragglers = _check_old_names_absent(conn)
            except Exception as exc:
                logger.warning("eurokg_snapshot: straggler check failed: %s", exc)
                stragglers = []

        grand_total = sum(
            kg for month_data in current_totals.values() for kg in month_data.values()
        )

        # Determine overall status
        if stragglers:
            status = 'stale_names'
        elif diffs:
            status = 'changed'
        else:
            status = 'ok'

        return jsonify({
            'run_at': run_at,
            'baseline_available': baseline_available,
            'baseline_generated_at': baseline_generated_at,
            'months_in_baseline': len(baseline_totals),
            'months_in_current': len(current_totals),
            'grand_total_current_kg': round(grand_total, 4),
            'diff': diffs,
            'old_name_stragglers': stragglers,
            'status': status,
        })

    except Exception as exc:
        logger.exception("eurokg_snapshot endpoint error: %s", exc)
        return jsonify({
            'run_at': run_at,
            'status': 'error',
            'error': str(exc),
        }), 500
