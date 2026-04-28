"""Route smoke test — runs against the Flask test client as an authenticated gestor.

Usage:
    python scripts/smoke_test_routes.py

Exits 0 on success, 1 on any route failure.
"""
import sys
import os
import re

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from flask_app.app import create_app
from db.connection import db_connection

STATIC_ROUTES = [
    '/',
    '/eurokg/',
    '/eurokg/pesagens',
    '/producao/',
    '/pastelaria/',
    '/confeitaria/',
    '/gestor/',
    '/compras/',
    '/logistica/',
    '/eventos/',
    '/vendas/',
    '/financeiro/',
    '/financeiro/credito/',
    '/financeiro/faturas/',
    '/financeiro/pagamentos/',
    '/financeiro/cashflow/',
    '/financeiro/centros-custo/',
    '/financeiro/categorias/',
    '/financeiro/avencas/',
    '/meteorologia/',
    '/forecast/',
    '/tarefas/',
    '/agente/',
]


def get_gestor_user():
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, username, role,
                      acesso_gestor, acesso_producao, acesso_eurokg,
                      acesso_pastelaria, acesso_confeitaria, acesso_vendas,
                      acesso_administrativo, acesso_financeiro,
                      acesso_eventos, acesso_tarefas
               FROM users WHERE acesso_gestor = TRUE LIMIT 1"""
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError("No gestor user found in DB — cannot run smoke test.")
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def get_dynamic_routes():
    """Build store-specific routes from the live DB.

    Returns (routes, discovery_failed) — callers treat a True discovery_failed
    as a hard failure so deploy health checks don't silently lose coverage.
    """
    routes = []
    try:
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM stores WHERE is_active = TRUE ORDER BY id LIMIT 3"
            )
            for (store_id,) in cur.fetchall():
                routes.append(f'/loja/{store_id}')
        return routes, False
    except Exception as exc:
        print(f"  [ERROR] Dynamic route discovery failed: {exc}")
        return [], True


def _strip_html(text: str, max_len: int = 300) -> str:
    """Remove HTML tags and collapse whitespace for a readable snippet."""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > max_len:
        text = text[:max_len] + '…'
    return text


def run():
    print("=== Scoopy Route Smoke Test ===\n")
    app = create_app()
    user = get_gestor_user()
    print(f"Authenticating as: {user['username']}\n")

    dynamic_routes, discovery_failed = get_dynamic_routes()
    routes = STATIC_ROUTES + dynamic_routes
    print(f"Checking {len(STATIC_ROUTES)} static + {len(dynamic_routes)} dynamic route(s):\n")

    failures = []
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['user'] = user

        for route in routes:
            try:
                r = client.get(route, follow_redirects=True)
                status = r.status_code
                ok = status < 400
                marker = '✓' if ok else '✗'
                print(f"  {marker} [{status}] {route}")
                if not ok:
                    snippet = _strip_html(r.get_data(as_text=True))
                    failures.append((status, route, snippet))
            except Exception as exc:
                print(f"  ✗ [ERR] {route}: {exc}")
                failures.append(('ERR', route, str(exc)))

    print()
    if discovery_failed:
        failures.append(('ERR', '<dynamic route discovery>', 'DB lookup failed — loja routes not verified'))

    if failures:
        print(f"FAILED — {len(failures)} issue(s) detected:\n")
        for status, route, snippet in failures:
            print(f"  [{status}] {route}")
            if snippet:
                print(f"          {snippet}\n")
        sys.exit(1)
    else:
        print(f"PASSED — all {len(routes)} routes returned HTTP 2xx.")
        sys.exit(0)


if __name__ == '__main__':
    run()
