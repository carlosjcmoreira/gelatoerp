#!/usr/bin/env python3
"""
Splits the monolithic database.py into domain modules under db/.
Strategy:
  - connection.py  : raw extract (already has imports in body, lines 1-122)
  - all others     : prepend the correct import header, then extract body
  - database.py    : becomes a thin backward-compat shim at the end

Run from repo root: python scripts/split_database.py
"""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_SRC = os.path.join(ROOT, 'database.py')
OUT_DIR = os.path.join(ROOT, 'db')
os.makedirs(OUT_DIR, exist_ok=True)

with open(DB_SRC, encoding='utf-8') as f:
    ALL = f.readlines()

TOTAL = len(ALL)

def body(start, end):
    """Lines start..end (1-indexed, inclusive)."""
    return ''.join(ALL[start-1:end])


# ─── import headers ───────────────────────────────────────────────────────────

H_BASE = """\
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger
"""

H_PD  = "import pandas as pd\n"
H_JSON = "import json\n"
H_OS  = "import os\n"
H_AUTH_EXTRA = (
    "from db.connection import hash_password, verify_password\n"
    "import secrets\n"
)

# ─── module specs ─────────────────────────────────────────────────────────────
# (output_filename, header_string_or_None, start_line, end_line)
MODULES = [
    # connection: raw body only — it already has all top-level imports
    ("connection.py",  None,                              1,    122),

    # schema: init_database, run_migrations, run_faturas_migrations
    ("schema.py",      H_BASE + H_JSON + H_OS,          123,  1194),

    # config: get/set_system_config
    ("config.py",      H_BASE,                          1195, 1219),

    # auth: sessions, users  (+ get_active_venda_stores / get_store_by_id
    #       which are physically interspersed at lines 1354-1373 in the file)
    ("auth.py",        H_BASE + H_AUTH_EXTRA + H_JSON,  1220, 1407),

    # stores: store management
    ("stores.py",      H_BASE + H_JSON,                 1409, 1512),

    # producao: ice-cream production, KPIs, quebras, vendas core
    ("producao.py",    H_BASE + H_PD + H_JSON,          1513, 2744),

    # pastelaria: pastry/confectionery production, stock, products catalogue,
    #             motivos quebra, receitas gelado, rececao mercadoria,
    #             vendas detalhe, stock gelado, regras negocio,
    #             produtos vendas config, gramas gelado, consumo mensal,
    #             product dashboard
    ("pastelaria.py",  H_BASE + H_PD + H_JSON + H_OS,  2745, 3962),

    # plano: daily production plan, ordem producao, stock producao gelado,
    #        transferencias
    ("plano.py",       H_BASE + H_JSON,                 3963, 4556),

    # artigos: artigos administrativos + seed
    ("artigos.py",     H_BASE + H_JSON,                 4558, 4683),

    # area: planos de área (pastelaria/confeitaria), stock por área
    ("area.py",        H_BASE,                          4684, 4917),

    # credito: credit contracts, bank balances, dashboard
    ("credito.py",     H_BASE + H_JSON,                 4918, 5218),

    # eventos: M-Eventos leads, events, clients, quotes
    ("eventos.py",     H_BASE + H_JSON,                 5219, 5772),

    # faturas: suppliers + invoices + stores_list + suggest_subfolder
    ("faturas.py",     H_BASE + H_JSON + H_OS,          5773, TOTAL),
]

# ─── write modules ────────────────────────────────────────────────────────────
module_names = []
for filename, header, start, end in MODULES:
    content = body(start, end)
    if header:
        content = header.rstrip('\n') + '\n\n' + content
    path = os.path.join(OUT_DIR, filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    n = filename.replace('.py', '')
    module_names.append(n)
    lines_written = end - start + 1
    print(f"  db/{filename:<22} {lines_written:>5} lines  [{start}:{end}]")

# ─── __init__.py : re-exports everything so `from db import *` works ──────────
init_lines = ["# Auto-generated — re-exports all public symbols from domain modules\n"]
for mod in module_names:
    init_lines.append(f"from db.{mod} import *  # noqa: F401,F403\n")
init_path = os.path.join(OUT_DIR, '__init__.py')
with open(init_path, 'w', encoding='utf-8') as f:
    f.writelines(init_lines)
print(f"  db/__init__.py")

print(f"\nAll {len(module_names)} modules written to db/")
