#!/usr/bin/env python3
"""
Scoopy — Script de export de dados de produção do Replit (Opção C / fallback CSV).

Uso (no terminal do Replit):
    python scripts/export_prod_data.py

Cria um directório  export_YYYYMMDD_HHMM/  com um CSV por tabela e um ZIP final.

NOTA: colunas BYTEA (ex: invoices.pdf_data) são exportadas como hex.
      Para um dump completo e fiel, usar preferencialmente:
        pg_dump $DATABASE_URL --format=custom --compress=9 --file=dump.dump
"""
import csv
import io
import os
import sys
import zipfile
from datetime import datetime

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    sys.exit("psycopg2 não instalado. Corre: pip install psycopg2-binary")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    sys.exit("DATABASE_URL não definido. Certifica-te que estás no ambiente correcto.")

SKIP_TABLES = set()

TABLE_ORDER = [
    "db_schema_version",
    "system_config",
    "regras_negocio",
    "motivos_quebra",
    "tile_config",
    "stores",
    "store_aliases",
    "users",
    "sessions",
    "user_store_vendas",
    "gramas_gelado",
    "gelado_por_tipologia",
    "tipologias_pastelaria",
    "coberturas",
    "produtos_pastelaria",
    "produtos_confeitaria",
    "receitas_gelado",
    "produtos_rececao",
    "produtos_vendas_config",
    "artigos_administrativos",
    "materiais",
    "suppliers",
    "cost_centers",
    "cost_categories",
    "colaboradores",
    "colaborador_centro_custo",
    "credit_contracts",
    "bank_balance_entries",
    "contract_payment_revisions",
    "confirming_parcelas",
    "payment_methods_config",
    "vat_periods",
    "config_preco_caixa_kg",
    "cashflow_config",
    "avencas",
    "ordem_producao",
    "plano_producao",
    "plano_producao_pastelaria",
    "plano_producao_confeitaria",
    "producao",
    "producao_pastelaria",
    "producao_confeitaria",
    "quebras",
    "ajustes_producao",
    "stock_gelado",
    "stock_producao",
    "stock_producao_pastelaria",
    "stock_producao_confeitaria",
    "contagem_stock",
    "rececao_stock",
    "rececao_mercadoria",
    "ordens_transferencia",
    "transferencias",
    "stock_materiais",
    "movimentos_stock_materiais",
    "vendas",
    "vendas_detalhe",
    "stock_inicial",
    "fecho_caixa",
    "sales_historico",
    "sales_historico_import_log",
    "weather_data",
    "sales_forecasts",
    "forecast_meteo_config",
    "artigos_evento",
    "event_clients",
    "lead_requests",
    "events",
    "eventos",
    "evento_items",
    "quote_items",
    "transferencias_eventos",
    "invoices",
    "invoice_linhas",
    "invoice_payments",
    "tarefas",
    "tarefas_registos",
]


def get_all_tables(cur):
    cur.execute("""
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename
    """)
    return {row[0] for row in cur.fetchall()}


def get_bytea_columns(cur, table):
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
          AND data_type = 'bytea'
    """, (table,))
    return {row[0] for row in cur.fetchall()}


def export_table(cur, table, bytea_cols, out_dir):
    bytea_clause = ", ".join(
        f"encode({col}, 'hex') AS {col}" if col in bytea_cols else col
        for col in get_column_names(cur, table)
    )
    cur.execute(f"SELECT {bytea_clause} FROM {table}")
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    path = os.path.join(out_dir, f"{table}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        writer.writerows(rows)
    return len(rows)


def get_column_names(cur, table):
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
    """, (table,))
    return [row[0] for row in cur.fetchall()]


def main():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_dir = f"export_{timestamp}"
    zip_path = f"{out_dir}.zip"

    print(f"Scoopy — Export de dados de produção")
    print(f"DATABASE_URL: {DATABASE_URL[:40]}...")
    print()

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    existing_tables = get_all_tables(cur)
    print(f"Tabelas encontradas na BD: {len(existing_tables)}")
    print()

    os.makedirs(out_dir, exist_ok=True)

    ordered = [t for t in TABLE_ORDER if t in existing_tables]
    remaining = sorted(existing_tables - set(TABLE_ORDER) - SKIP_TABLES)
    tables_to_export = ordered + remaining

    total_rows = 0
    exported = []
    errors = []

    for table in tables_to_export:
        try:
            bytea_cols = get_bytea_columns(cur, table)
            n = export_table(cur, table, bytea_cols, out_dir)
            total_rows += n
            bytea_note = f" [BYTEA→hex: {', '.join(bytea_cols)}]" if bytea_cols else ""
            print(f"  ✓ {table:<45} {n:>6} linhas{bytea_note}")
            exported.append(table)
        except Exception as e:
            print(f"  ✗ {table}: {e}")
            errors.append(table)

    cur.close()
    conn.close()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for table in exported:
            csv_path = os.path.join(out_dir, f"{table}.csv")
            zf.write(csv_path, f"{table}.csv")

    print()
    print(f"{'─'*60}")
    print(f"Export concluído!")
    print(f"  Tabelas exportadas : {len(exported)}")
    print(f"  Tabelas com erro   : {len(errors)}")
    print(f"  Total de linhas    : {total_rows:,}")
    print(f"  Directório         : {out_dir}/")
    print(f"  ZIP                : {zip_path}")
    print()

    if errors:
        print(f"ERROS nas tabelas: {', '.join(errors)}")
        print()

    print("Próximos passos:")
    print("  1. Descarrega o ZIP via Replit Files UI")
    print("  2. Ou: scp {} deploy@HETZNER_IP:/var/backups/scoopy/".format(zip_path))
    print()
    print("NOTA: colunas BYTEA (pdf_data em invoices) estão em hex no CSV.")
    print("      Para migração completa com PDFs, usar pg_dump:")
    print("      pg_dump $DATABASE_URL --format=custom --compress=9 --file=dump.dump")


if __name__ == "__main__":
    main()
