"""Route smoke test — runs against the Flask test client as an authenticated gestor.

Usage:
    python scripts/smoke_test_routes.py

Exits 0 on success, 1 on any route failure.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from flask_app.app import create_app
from db.connection import db_connection

ROUTES = [
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


def run():
    print("=== Scoopy Route Smoke Test ===\n")
    app = create_app()
    user = get_gestor_user()
    print(f"Authenticating as: {user['username']}\n")

    failures = []
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['user'] = user

        for route in ROUTES:
            try:
                r = client.get(route, follow_redirects=True)
                status = r.status_code
                ok = status < 400
                marker = '✓' if ok else '✗'
                print(f"  {marker} [{status}] {route}")
                if not ok:
                    failures.append((status, route))
            except Exception as exc:
                print(f"  ✗ [ERR] {route}: {exc}")
                failures.append(('ERR', f"{route}: {exc}"))

    print()
    if failures:
        print(f"FAILED — {len(failures)} route(s) returned errors:")
        for status, route in failures:
            print(f"  - [{status}] {route}")
        sys.exit(1)
    else:
        print(f"PASSED — all {len(ROUTES)} routes returned HTTP 2xx.")
        sys.exit(0)


if __name__ == '__main__':
    run()
