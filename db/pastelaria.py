import psycopg2
from psycopg2.extras import RealDictCursor, DictCursor, execute_values
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger, db_retry
from db.cache import ttl_cache, invalidate, invalidate_prefix
from db.stores import get_store_id_by_name
from db.producao import get_sabores_mapping
import pandas as pd
import json
import os

def generate_lote_pastelaria(data: date) -> str:
    return f"P{data.strftime('%d%m%y')}"

def generate_lote_confeitaria(data: date) -> str:
    return f"C{data.strftime('%d%m%y')}"

def add_producao_pastelaria(data: date, loja: str, produto: str, quantidade: int):
    store_id = get_store_id_by_name(loja)
    lote = generate_lote_pastelaria(data)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO producao_pastelaria (data, loja, produto, quantidade, lote, store_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (data, loja, produto, quantidade, lote, store_id))
        conn.commit()
    return lote

def get_pastelaria_df(loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    query = "SELECT * FROM producao_pastelaria WHERE 1=1"
    params = []
    if loja:
        query += " AND loja = %s"
        params.append(loja)
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    query += " ORDER BY data DESC"
    with db_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)

def get_pastelaria_stock() -> pd.DataFrame:
    query = """
        SELECT loja, produto, SUM(quantidade) as stock
        FROM producao_pastelaria
        GROUP BY loja, produto
        ORDER BY loja, produto
    """
    with db_connection() as conn:
        return pd.read_sql_query(query, conn)

def add_producao_confeitaria(data: date, loja: str, produto: str, quantidade: int):
    store_id = get_store_id_by_name(loja)
    lote = generate_lote_confeitaria(data)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO producao_confeitaria (data, loja, produto, quantidade, lote, store_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (data, loja, produto, quantidade, lote, store_id))
        conn.commit()
    return lote

def get_confeitaria_df(loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    query = "SELECT * FROM producao_confeitaria WHERE 1=1"
    params = []
    if loja:
        query += " AND loja = %s"
        params.append(loja)
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    query += " ORDER BY data DESC"
    with db_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)

def get_confeitaria_stock() -> pd.DataFrame:
    query = """
        SELECT loja, produto, SUM(quantidade) as stock
        FROM producao_confeitaria
        GROUP BY loja, produto
        ORDER BY loja, produto
    """
    with db_connection() as conn:
        return pd.read_sql_query(query, conn)

def add_contagem_stock(data: date, loja: str, produto: str, quantidade: int, tipo: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO contagem_stock (data, loja, produto, quantidade, tipo) VALUES (%s, %s, %s, %s, %s)",
            (data, loja, produto, quantidade, tipo)
        )
        conn.commit()

def get_contagem_stock_df(tipo: str, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    query = "SELECT * FROM contagem_stock WHERE tipo = %s"
    params = [tipo]
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    query += " ORDER BY data DESC, produto"
    with db_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)

def add_ajuste_producao(ano: int, mes: int, quantidade_kg: float, descricao: str = None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO ajustes_producao (ano, mes, quantidade_kg, descricao) VALUES (%s, %s, %s, %s)",
            (ano, mes, quantidade_kg, descricao)
        )
        conn.commit()

def get_ajustes_producao(ano: int = None) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        if ano:
            cursor.execute("SELECT id, ano, mes, quantidade_kg, descricao, created_at FROM ajustes_producao WHERE ano = %s ORDER BY mes", (ano,))
        else:
            cursor.execute("SELECT id, ano, mes, quantidade_kg, descricao, created_at FROM ajustes_producao ORDER BY ano, mes")
        rows = cursor.fetchall()
    return [{'id': r[0], 'ano': r[1], 'mes': r[2], 'quantidade_kg': r[3], 'descricao': r[4], 'created_at': r[5]} for r in rows]

def delete_ajuste_producao(ajuste_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM ajustes_producao WHERE id = %s", (ajuste_id,))
        conn.commit()

def delete_contagem_stock(contagem_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM contagem_stock WHERE id = %s", (contagem_id,))
        conn.commit()

def get_produtos_confeitaria() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nome FROM produtos_confeitaria WHERE ativo = TRUE ORDER BY nome")
        return [row[0] for row in cursor.fetchall()]

def add_produto_confeitaria(nome: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO produtos_confeitaria (nome) VALUES (%s)", (nome,))
            conn.commit()
            return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False

def update_produto_confeitaria(id: int, nome: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE produtos_confeitaria SET nome = %s WHERE id = %s", (nome, id))
        conn.commit()

def delete_produto_confeitaria(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM produtos_confeitaria WHERE id = %s", (id,))
        conn.commit()

def get_all_produtos_confeitaria() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, nome, ativo FROM produtos_confeitaria ORDER BY nome")
        return [{'id': row[0], 'nome': row[1], 'ativo': row[2]} for row in cursor.fetchall()]

@ttl_cache('motivos_quebra', ttl=300)
def get_motivos_quebra() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nome FROM motivos_quebra WHERE ativo = TRUE ORDER BY nome")
        return [row[0] for row in cursor.fetchall()]

def get_all_motivos_quebra() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, nome, ativo FROM motivos_quebra ORDER BY nome")
        return [{'id': row[0], 'nome': row[1], 'ativo': row[2]} for row in cursor.fetchall()]

def add_motivo_quebra(nome: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO motivos_quebra (nome) VALUES (%s)", (nome,))
            conn.commit()
            success = True
        except psycopg2.IntegrityError:
            conn.rollback()
            success = False
    invalidate('motivos_quebra')
    return success

def update_motivo_quebra(id: int, nome: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE motivos_quebra SET nome = %s WHERE id = %s", (nome, id))
        conn.commit()
    invalidate('motivos_quebra')

def delete_motivo_quebra(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM motivos_quebra WHERE id = %s", (id,))
        conn.commit()
    invalidate('motivos_quebra')

def get_produtos_pastelaria() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT tipologia, sabor, cobertura FROM produtos_pastelaria WHERE ativo = TRUE ORDER BY tipologia, sabor")
        produtos = []
        for row in cursor.fetchall():
            tipologia = row[0] or ''
            sabor = row[1] or ''
            cobertura = row[2] or ''
            parts = [tipologia]
            if sabor:
                parts.append(sabor)
            if cobertura:
                parts.append(cobertura)
            produtos.append(', '.join(parts))
    return produtos


def get_bolo_tamanhos() -> list:
    """Return active cake sizes in their configured display order."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT nome
            FROM tamanhos_bolo_pastelaria
            WHERE ativo = TRUE
            ORDER BY ordem, nome
        """)
        return [row[0] for row in cursor.fetchall()]


def build_bolo_product_label(tamanho: str, sabores: list, cobertura: str = '') -> str:
    """Build the stable display/stock key for a configured cake."""
    sabores_text = ' + '.join(sabores)
    label = f"Bolo — {tamanho} — {sabores_text}"
    if cobertura:
        label += f" — Cobertura: {cobertura}"
    return label

def get_all_produtos_pastelaria() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, tipologia, sabor, cobertura, ativo FROM produtos_pastelaria ORDER BY tipologia, sabor")
        return [{'id': row[0], 'tipologia': row[1] or '', 'sabor': row[2] or '', 'cobertura': row[3] or '', 'ativo': row[4]} for row in cursor.fetchall()]

def add_produto_pastelaria(tipologia: str, sabor: str = '', cobertura: str = ''):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO produtos_pastelaria (tipologia, sabor, cobertura) VALUES (%s, %s, %s)", (tipologia, sabor, cobertura))
            conn.commit()
            return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False

def update_produto_pastelaria(id: int, tipologia: str, sabor: str = '', cobertura: str = ''):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE produtos_pastelaria SET tipologia = %s, sabor = %s, cobertura = %s WHERE id = %s", (tipologia, sabor, cobertura, id))
        conn.commit()

def delete_produto_pastelaria(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM produtos_pastelaria WHERE id = %s", (id,))
        conn.commit()


def delete_produtos_pastelaria_bulk(ids: list):
    if not ids:
        return 0
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM produtos_pastelaria WHERE id = ANY(%s)", (ids,))
        deleted = cursor.rowcount
        conn.commit()
    return deleted

def get_gelado_por_tipologia() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, tipologia, quantidade_gelado_g FROM gelado_por_tipologia ORDER BY tipologia")
        return [{'id': row[0], 'tipologia': row[1], 'quantidade_gelado_g': row[2] or 0} for row in cursor.fetchall()]

def add_gelado_por_tipologia(tipologia: str, quantidade_gelado_g: float = 0):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO gelado_por_tipologia (tipologia, quantidade_gelado_g) VALUES (%s, %s)", (tipologia, quantidade_gelado_g))
            conn.commit()
            return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False

def update_gelado_por_tipologia(id: int, tipologia: str, quantidade_gelado_g: float = 0):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE gelado_por_tipologia SET tipologia = %s, quantidade_gelado_g = %s WHERE id = %s", (tipologia, quantidade_gelado_g, id))
        conn.commit()

def delete_gelado_por_tipologia(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM gelado_por_tipologia WHERE id = %s", (id,))
        conn.commit()

def get_gelado_peso_by_tipologia(tipologia_nome: str) -> float:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT quantidade_gelado_g FROM gelado_por_tipologia WHERE tipologia = %s", (tipologia_nome,))
        row = cursor.fetchone()
    return row[0] if row else 0

def update_gelado_peso_by_tipologia(tipologia_nome: str, quantidade_gelado_g: float = 0):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO gelado_por_tipologia (tipologia, quantidade_gelado_g) VALUES (%s, %s) ON CONFLICT (tipologia) DO UPDATE SET quantidade_gelado_g = %s", (tipologia_nome, quantidade_gelado_g, quantidade_gelado_g))
            conn.commit()
        except psycopg2.Error:
            conn.rollback()

def get_coberturas() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nome FROM coberturas WHERE ativo = TRUE ORDER BY nome")
        return [row[0] for row in cursor.fetchall()]

def get_all_coberturas() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, nome, ativo FROM coberturas ORDER BY nome")
        return [{'id': row[0], 'nome': row[1], 'ativo': row[2]} for row in cursor.fetchall()]

def add_cobertura(nome: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO coberturas (nome) VALUES (%s)", (nome,))
            conn.commit()
            return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False

def update_cobertura(id: int, nome: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE coberturas SET nome = %s WHERE id = %s", (nome, id))
        conn.commit()

def delete_cobertura(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM coberturas WHERE id = %s", (id,))
        conn.commit()

def get_produtos_rececao() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nome, tipo FROM produtos_rececao WHERE ativo = TRUE ORDER BY nome")
        return [{'nome': row[0], 'tipo': row[1]} for row in cursor.fetchall()]

def get_all_produtos_rececao() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, nome, tipo, ativo FROM produtos_rececao ORDER BY nome")
        return [{'id': row[0], 'nome': row[1], 'tipo': row[2], 'ativo': row[3]} for row in cursor.fetchall()]

def add_produto_rececao(nome: str, tipo: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO produtos_rececao (nome, tipo) VALUES (%s, %s)", (nome, tipo))
            conn.commit()
            return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False

def update_produto_rececao(id: int, nome: str, tipo: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE produtos_rececao SET nome = %s, tipo = %s WHERE id = %s", (nome, tipo, id))
        conn.commit()

def delete_produto_rececao(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM produtos_rececao WHERE id = %s", (id,))
        conn.commit()

@ttl_cache('receitas_gelado', ttl=600)
def get_receitas_gelado() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nome FROM receitas_gelado WHERE ativo = TRUE ORDER BY nome")
        return [row[0] for row in cursor.fetchall()]

def get_all_receitas_gelado() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, nome, nome_corrente, ativo, COALESCE(conta_eurokg, TRUE) FROM receitas_gelado ORDER BY COALESCE(nome_corrente, nome)")
        return [{'id': row[0], 'nome': row[1], 'nome_corrente': row[2] or '', 'ativo': row[3], 'conta_eurokg': row[4]} for row in cursor.fetchall()]

def add_receita_gelado(nome: str, nome_corrente: str = None):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO receitas_gelado (nome, nome_corrente) VALUES (%s, %s)", (nome, nome_corrente))
            conn.commit()
            success = True
        except psycopg2.IntegrityError:
            conn.rollback()
            success = False
    invalidate_prefix('sabores_list')
    invalidate('sabores_mapping', 'sabores_excluidos', 'receitas_gelado')
    return success

def update_receita_gelado(id: int, nome: str, nome_corrente: str = None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE receitas_gelado SET nome = %s, nome_corrente = %s WHERE id = %s", (nome, nome_corrente, id))
        conn.commit()
    invalidate_prefix('sabores_list')
    invalidate('sabores_mapping', 'sabores_excluidos', 'receitas_gelado')

def update_receita_gelado_ativo(id: int, ativo: bool):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE receitas_gelado SET ativo = %s WHERE id = %s", (ativo, id))
        conn.commit()
    invalidate_prefix('sabores_list')
    invalidate('sabores_mapping', 'sabores_excluidos', 'receitas_gelado')

def update_receita_gelado_conta_eurokg(id: int, conta_eurokg: bool):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE receitas_gelado SET conta_eurokg = %s WHERE id = %s", (conta_eurokg, id))
        conn.commit()
    invalidate_prefix('sabores_list')
    invalidate('sabores_mapping', 'sabores_excluidos', 'receitas_gelado')

def delete_receita_gelado(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM receitas_gelado WHERE id = %s", (id,))
        conn.commit()
    invalidate_prefix('sabores_list')
    invalidate('sabores_mapping', 'sabores_excluidos', 'receitas_gelado')

def add_rececao_mercadoria(data: date, loja: str, tipo_produto: str, quantidade: float, unidade: str, produto: str = None, sabor: str = None, lote: str = None):
    store_id = get_store_id_by_name(loja)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO rececao_mercadoria (data, loja, tipo_produto, produto, sabor, lote, quantidade, unidade, store_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (data, loja, tipo_produto, produto, sabor, lote, quantidade, unidade, store_id))
        conn.commit()

def get_rececao_mercadoria_df(loja: str = None, data_inicio: date = None, data_fim: date = None) -> pd.DataFrame:
    query = "SELECT * FROM rececao_mercadoria WHERE 1=1"
    params = []
    if loja:
        query += " AND loja = %s"
        params.append(loja)
    if data_inicio:
        query += " AND data >= %s"
        params.append(data_inicio)
    if data_fim:
        query += " AND data <= %s"
        params.append(data_fim)
    query += " ORDER BY data DESC, id DESC"
    with db_connection() as conn:
        return pd.read_sql_query(query, conn, params=params)

def delete_rececao_mercadoria(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM rececao_mercadoria WHERE id = %s", (id,))
        conn.commit()

def add_venda_detalhe(data: date, loja: str, produto: str, quantidade: int, categoria: str = None,
                       valor_euros: float = None, valor_sem_iva_euros: float = None):
    store_id = get_store_id_by_name(loja)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO vendas_detalhe (data, loja, produto, categoria, quantidade, valor_euros, store_id, valor_sem_iva_euros)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ''', (data, loja, produto, categoria, quantidade, valor_euros, store_id, valor_sem_iva_euros))
        conn.commit()


def add_venda_detalhe_batch(records: list, pre_delete_pairs: list = None) -> int:
    """Insert multiple vendas_detalhe records in a single transaction.

    Each record is a dict with keys: data, loja, produto, quantidade,
    categoria (optional), valor_euros (optional), valor_sem_iva_euros (optional —
    the real net-of-VAT amount, when the source file provides it; NULL means the
    row's IVA is estimated rather than real, see compute_vat_period).

    If ``pre_delete_pairs`` is provided (list of ``(date, loja)`` tuples),
    those rows are deleted atomically before the insert so that replace
    semantics are safe: if the insert fails the delete is also rolled back.

    Returns the number of rows inserted.
    """
    if not records and not pre_delete_pairs:
        return 0
    store_id_cache = {}
    rows = []
    for rec in records:
        loja = rec['loja']
        if loja not in store_id_cache:
            store_id_cache[loja] = get_store_id_by_name(loja)
        rows.append((
            rec['data'],
            loja,
            rec['produto'],
            rec.get('categoria'),
            rec['quantidade'],
            rec.get('valor_euros'),
            store_id_cache[loja],
            rec.get('valor_sem_iva_euros'),
        ))
    if pre_delete_pairs and not rows:
        raise ValueError(
            f"pre_delete_pairs contains {len(pre_delete_pairs)} pair(s) but the "
            "parsed batch is empty — refusing to delete existing data without replacement. "
            "Check that the file contains valid sales rows for the expected lojas."
        )
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            if pre_delete_pairs:
                for d, loja in pre_delete_pairs:
                    cursor.execute(
                        "DELETE FROM vendas_detalhe WHERE data = %s AND loja = %s",
                        (d, loja)
                    )
            if rows:
                execute_values(
                    cursor,
                    '''INSERT INTO vendas_detalhe
                       (data, loja, produto, categoria, quantidade, valor_euros, store_id, valor_sem_iva_euros)
                       VALUES %s''',
                    rows,
                    page_size=500,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return len(rows)


def get_vendas_detalhe_df(loja: str = None, data_inicio: date = None, data_fim: date = None, categoria: str = None, limit: int = 500) -> list:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        query = "SELECT * FROM vendas_detalhe WHERE 1=1"
        params = []
        if loja:
            query += " AND loja = %s"
            params.append(loja)
        if data_inicio:
            query += " AND data >= %s"
            params.append(data_inicio)
        if data_fim:
            query += " AND data <= %s"
            params.append(data_fim)
        if categoria:
            query += " AND categoria = %s"
            params.append(categoria)
        query += " ORDER BY data DESC, id DESC"
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        cursor.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]

def add_stock_gelado(data: date, loja: str, sabor: str, quantidade_kg: float, tipo: str, local: str = None) -> int:
    store_id = get_store_id_by_name(loja)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO stock_gelado (data, loja, sabor, quantidade_kg, tipo, local, store_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (data, loja, sabor, quantidade_kg, tipo, local, store_id))
        new_id = cursor.fetchone()[0]
        conn.commit()
    return new_id


def add_stock_gelado_carapinas(stock_gelado_id: int, carapinas: list) -> None:
    """Save individual carapina weights for a stock_gelado record.

    Only stores data when there are 2 or more carapinas — a single carapina
    value adds no information beyond the total already stored in stock_gelado.
    """
    if not carapinas or len(carapinas) < 2:
        return
    with db_connection() as conn:
        cursor = conn.cursor()
        execute_values(
            cursor,
            "INSERT INTO stock_gelado_carapinas (stock_gelado_id, carapina_numero, quantidade_kg) VALUES %s",
            [(stock_gelado_id, i + 1, float(kg)) for i, kg in enumerate(carapinas)],
        )
        conn.commit()


def get_carapinas_for_stock_ids(stock_ids: list) -> dict:
    """Return carapina breakdown for a list of stock_gelado IDs.

    Returns {stock_id: [kg1, kg2, ...]} ordered by carapina_numero.
    Only IDs that have carapina records are included in the result.
    """
    if not stock_ids:
        return {}
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT stock_gelado_id, quantidade_kg
            FROM stock_gelado_carapinas
            WHERE stock_gelado_id = ANY(%s)
            ORDER BY stock_gelado_id, carapina_numero
        """, (list(stock_ids),))
        rows = cursor.fetchall()
    result: dict = {}
    for stock_id, kg in rows:
        if stock_id not in result:
            result[stock_id] = []
        result[stock_id].append(float(kg))
    return result


def add_stock_gelado_bulk(entries: list, loja: str) -> int:
    """
    Insert multiple stock_gelado records atomically in a single transaction.
    Each entry is a dict with keys: data (date), sabor (str), quantidade_kg (float), tipo (str).
    Returns the number of rows inserted.
    Raises on any DB error — no partial writes.

    Note: this function inserts directly via execute_values rather than looping
    add_stock_gelado, because each call to add_stock_gelado opens its own connection
    and commits immediately, making atomicity impossible. If add_stock_gelado ever
    gains pre-insert business logic (e.g. duplicate checks, quota validation), that
    logic should be replicated or extracted into a shared helper and applied here too.
    """
    if not entries:
        return 0
    store_id = get_store_id_by_name(loja)
    rows = [
        (e['data'], loja, e['sabor'], e['quantidade_kg'], e['tipo'], None, store_id)
        for e in entries
    ]
    with db_connection() as conn:
        try:
            cursor = conn.cursor()
            execute_values(
                cursor,
                '''INSERT INTO stock_gelado (data, loja, sabor, quantidade_kg, tipo, local, store_id)
                   VALUES %s
                   ON CONFLICT DO NOTHING''',
                rows,
            )
            inserted = cursor.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return inserted if inserted >= 0 else len(rows)


def upsert_stock_gelado_matosinhos(data: date, sabor: str, quantidade_kg: float) -> None:
    """
    Upsert a stock_gelado record for Matosinhos/inicio.  If a row already
    exists for (data, Matosinhos, sabor, inicio), it is updated in-place;
    otherwise a new row is inserted.  Sabor name is normalised to its
    canonical form before the upsert so case variants are collapsed.

    Uses ON CONFLICT on the uq_stock_gelado_data_loja_sabor_tipo unique
    constraint — no race conditions possible.
    """
    from sabor_utils import normalise_sabor
    sabor = normalise_sabor(sabor)
    store_id = get_store_id_by_name('Matosinhos')
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO stock_gelado (data, loja, sabor, quantidade_kg, tipo, store_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (data, loja, sabor, tipo)
            DO UPDATE SET quantidade_kg = EXCLUDED.quantidade_kg,
                          store_id     = EXCLUDED.store_id
        ''', (data, 'Matosinhos', sabor, quantidade_kg, 'inicio', store_id))
        conn.commit()


def get_pesagem_comparison(loja: str, tipo: str, data_atual: date = None, local: str = None):
    if data_atual is None:
        data_atual = date.today()

    local_filter = " AND local = %s" if local else ""

    def p(*dates):
        params = [loja, tipo] + list(dates)
        if local:
            params.append(local)
        return params

    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(f"""
            SELECT DISTINCT data FROM stock_gelado
            WHERE loja = %s AND tipo = %s AND data < %s
              AND sabor IS NOT NULL
              {local_filter}
            ORDER BY data DESC LIMIT 1
        """, p(data_atual))
        row = cursor.fetchone()
        last_date = row[0] if row else None

        cursor.execute(f"""
            SELECT sabor, SUM(quantidade_kg) FROM stock_gelado
            WHERE loja = %s AND tipo = %s AND data = %s
              AND sabor IS NOT NULL
              {local_filter}
            GROUP BY sabor
        """, p(data_atual))
        hoje = {r[0]: float(r[1]) for r in cursor.fetchall()}

        anterior = {}
        if last_date:
            cursor.execute(f"""
                SELECT sabor, SUM(quantidade_kg) FROM stock_gelado
                WHERE loja = %s AND tipo = %s AND data = %s
                  AND sabor IS NOT NULL
                  {local_filter}
                GROUP BY sabor
            """, p(last_date))
            anterior = {r[0]: float(r[1]) for r in cursor.fetchall()}

    return last_date, anterior, hoje

@db_retry
def get_stock_gelado_df(loja: str = None, tipo: str = None, data_inicio: date = None, data_fim: date = None) -> list:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        query = "SELECT * FROM stock_gelado WHERE 1=1"
        params = []
        if loja:
            query += " AND loja = %s"
            params.append(loja)
        if tipo:
            query += " AND tipo = %s"
            params.append(tipo)
        if data_inicio:
            query += " AND data >= %s"
            params.append(data_inicio)
        if data_fim:
            query += " AND data <= %s"
            params.append(data_fim)
        query += " ORDER BY data DESC, id DESC"
        cursor.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]

def get_stock_gelado_by_id(stock_id: int):
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=DictCursor)
        cursor.execute("SELECT * FROM stock_gelado WHERE id = %s", (stock_id,))
        row = cursor.fetchone()
    return dict(row) if row else None

def delete_stock_gelado(stock_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM stock_gelado WHERE id = %s", (stock_id,))
        conn.commit()

def delete_stock_gelado_by_date(loja: str, data_date) -> int:
    """Delete all fim-de-dia stock_gelado rows for a given loja + date.

    Returns the number of rows deleted.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM stock_gelado WHERE loja = %s AND data = %s AND tipo = 'fim'",
            (loja, data_date),
        )
        deleted = cursor.rowcount
        conn.commit()
    return deleted


def update_stock_gelado(stock_id: int, quantidade_kg: float, loja: str = None, nova_data: date = None):
    with db_connection() as conn:
        cursor = conn.cursor()
        if loja:
            if nova_data:
                cursor.execute(
                    "UPDATE stock_gelado SET quantidade_kg = %s, data = %s WHERE id = %s AND loja = %s",
                    (quantidade_kg, nova_data, stock_id, loja)
                )
            else:
                cursor.execute(
                    "UPDATE stock_gelado SET quantidade_kg = %s WHERE id = %s AND loja = %s",
                    (quantidade_kg, stock_id, loja)
                )
        else:
            if nova_data:
                cursor.execute(
                    "UPDATE stock_gelado SET quantidade_kg = %s, data = %s WHERE id = %s",
                    (quantidade_kg, nova_data, stock_id)
                )
            else:
                cursor.execute(
                    "UPDATE stock_gelado SET quantidade_kg = %s WHERE id = %s",
                    (quantidade_kg, stock_id)
                )
        conn.commit()

def get_pesagens_recentes(n: int = 3) -> dict:
    """Return the last n pesagem dates and per-sabor kg.
    Matosinhos: reads pesagem_matosinhos from plano_producao (morning weigh-in during planning).
    Bolhão: reads from stock_gelado tipo=fim (end-of-day stock check)."""
    with db_connection() as conn:
        cursor = conn.cursor()
        result = {}

        cursor.execute("""
            SELECT DISTINCT data FROM plano_producao
            WHERE pesagem_matosinhos > 0 AND sabor IS NOT NULL
            ORDER BY data DESC LIMIT %s
        """, (n,))
        datas_mat = [r[0] for r in cursor.fetchall()]
        pivot_mat = {}
        if datas_mat:
            cursor.execute("""
                SELECT data, sabor, pesagem_matosinhos
                FROM plano_producao
                WHERE pesagem_matosinhos > 0 AND sabor IS NOT NULL AND data = ANY(%s)
                ORDER BY sabor, data
            """, (datas_mat,))
            for r in cursor.fetchall():
                d, s, kg = r[0], r[1], float(r[2])
                if s not in pivot_mat:
                    pivot_mat[s] = {}
                pivot_mat[s][d] = kg
        result['matosinhos'] = {'loja': 'Matosinhos', 'tipo': 'inicio', 'datas': datas_mat, 'pivot': pivot_mat}

        cursor.execute("""
            SELECT DISTINCT data FROM stock_gelado
            WHERE loja = 'Bolhão' AND tipo = 'fim' AND sabor IS NOT NULL
            ORDER BY data DESC LIMIT %s
        """, (n,))
        datas_bol = [r[0] for r in cursor.fetchall()]
        pivot_bol = {}
        if datas_bol:
            cursor.execute("""
                SELECT data, sabor, SUM(quantidade_kg)
                FROM stock_gelado
                WHERE loja = 'Bolhão' AND tipo = 'fim' AND sabor IS NOT NULL AND data = ANY(%s)
                GROUP BY data, sabor
                ORDER BY sabor, data
            """, (datas_bol,))
            for r in cursor.fetchall():
                d, s, kg = r[0], r[1], float(r[2])
                if s not in pivot_bol:
                    pivot_bol[s] = {}
                pivot_bol[s][d] = kg
        result['bolhao'] = {'loja': 'Bolhão', 'tipo': 'fim', 'datas': datas_bol, 'pivot': pivot_bol}

    return result


def get_latest_stock_by_sabor(loja: str, tipo: str) -> pd.DataFrame:
    today = date.today()

    mapping = get_sabores_mapping()
    reverse_mapping = {}
    for nome_receita, nome_corrente in mapping.items():
        reverse_mapping[nome_receita] = nome_corrente
        reverse_mapping[nome_corrente] = nome_corrente

    sabores_correntes = sorted(set(mapping.values()))

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT sg.id, sg.sabor, sg.quantidade_kg, sg.data
            FROM stock_gelado sg
            WHERE sg.loja = %s AND sg.tipo = %s
            ORDER BY sg.id DESC
        """, (loja, tipo))
        rows = cursor.fetchall()

    before_today = {}
    today_data = {}

    for row_id, sabor_raw, qtd, data in rows:
        sabor = reverse_mapping.get(sabor_raw, sabor_raw)
        if data < today:
            if sabor not in before_today:
                before_today[sabor] = {'quantidade_kg': qtd, 'data': data}
        elif data == today:
            if sabor not in today_data:
                today_data[sabor] = qtd

    result = []
    for sabor in sabores_correntes:
        bt = before_today.get(sabor, {})
        result.append({
            'sabor': sabor,
            'quantidade_kg': bt.get('quantidade_kg', 0),
            'data': bt.get('data', None),
            'pesagem_hoje': today_data.get(sabor, None)
        })

    return pd.DataFrame(result)

def add_stock_gelado_batch(data: date, loja: str, stocks: list, tipo: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        for stock in stocks:
            if stock['quantidade'] is not None and stock['quantidade'] >= 0:
                cursor.execute('''
                    INSERT INTO stock_gelado (data, loja, sabor, quantidade_kg, tipo)
                    VALUES (%s, %s, %s, %s, %s)
                ''', (data, loja, stock['sabor'], stock['quantidade'], tipo))
        conn.commit()

def get_vendas_bolhao_dashboard_data(loja: str = 'Bolhão') -> list:
    today = date.today()
    yesterday = today - timedelta(days=1)

    mapping = get_sabores_mapping()
    reverse_mapping = {}
    for nome_receita, nome_corrente in mapping.items():
        reverse_mapping[nome_receita] = nome_corrente
        reverse_mapping[nome_corrente] = nome_corrente

    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT DISTINCT ON (sabor) sabor, quantidade_kg
            FROM stock_gelado
            WHERE loja = %s AND tipo IN ('fim', 'inicio') AND data = %s
            ORDER BY sabor, CASE WHEN tipo = 'fim' THEN 0 ELSE 1 END, id DESC
        """, (loja, yesterday))
        pesagem_ontem_raw = {}
        for sabor_raw, qtd in cursor.fetchall():
            sabor = reverse_mapping.get(sabor_raw, sabor_raw)
            pesagem_ontem_raw[sabor] = qtd

        cursor.execute("""
            SELECT sabor, SUM(quantidade) as total
            FROM rececao_mercadoria
            WHERE loja = %s AND data = %s AND sabor IS NOT NULL
            AND unidade = 'kg'
            GROUP BY sabor
        """, (loja, today))
        rececao_hoje_raw = {}
        for sabor_raw, total in cursor.fetchall():
            sabor = reverse_mapping.get(sabor_raw, sabor_raw)
            rececao_hoje_raw[sabor] = rececao_hoje_raw.get(sabor, 0) + float(total)

        cursor.execute("""
            SELECT DISTINCT ON (sabor) sabor, quantidade_kg
            FROM stock_gelado
            WHERE loja = %s AND tipo IN ('fim', 'inicio') AND data = %s
            ORDER BY sabor, CASE WHEN tipo = 'fim' THEN 0 ELSE 1 END, id DESC
        """, (loja, today))
        pesagem_hoje_raw = {}
        for sabor_raw, qtd in cursor.fetchall():
            sabor = reverse_mapping.get(sabor_raw, sabor_raw)
            pesagem_hoje_raw[sabor] = qtd

    results = []
    all_sabores = sorted(set(pesagem_ontem_raw.keys()) | set(rececao_hoje_raw.keys()))
    for sabor in all_sabores:
        results.append({
            'Sabor': sabor,
            'Pesagem Ontem (kg)': round(pesagem_ontem_raw.get(sabor, 0), 3),
            'Recebido Hoje (kg)': round(rececao_hoje_raw.get(sabor, 0), 3),
            'Pesagem Fim Dia (kg)': round(pesagem_hoje_raw[sabor], 3) if sabor in pesagem_hoje_raw else None
        })

    return results


def get_regras_negocio(area: str = None) -> list:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        if area:
            cursor.execute("SELECT * FROM regras_negocio WHERE area = %s ORDER BY palavra_chave", (area,))
        else:
            cursor.execute("SELECT * FROM regras_negocio ORDER BY area, palavra_chave")
        rows = cursor.fetchall()
    return [dict(r) for r in rows]

def add_regra_negocio(area: str, palavra_chave: str) -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO regras_negocio (area, palavra_chave) VALUES (%s, %s)", (area, palavra_chave))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False

def update_regra_negocio(regra_id: int, palavra_chave: str) -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE regras_negocio SET palavra_chave = %s WHERE id = %s", (palavra_chave, regra_id))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False

def delete_regra_negocio(regra_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM regras_negocio WHERE id = %s", (regra_id,))
        conn.commit()

def get_vendas_diarias_diagnostico(data_inicio: date, data_fim: date) -> list:
    """Return daily gelado_kpi sales totals per loja for the given date range.

    Each row is a dict with:
      data, loja, total_euros, n_produtos
    Only products with gelado_kpi=TRUE are included.
    Days/lojas with no rows are NOT included (caller must detect gaps).
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT vd.data, vd.loja,
                   COALESCE(SUM(vd.valor_euros), 0) AS total_euros,
                   COUNT(DISTINCT vd.produto) AS n_produtos
            FROM vendas_detalhe vd
            INNER JOIN produtos_vendas_config pvc ON vd.produto = pvc.produto
            WHERE pvc.gelado_kpi = TRUE
              AND vd.data >= %s AND vd.data <= %s
            GROUP BY vd.data, vd.loja
            ORDER BY vd.data, vd.loja
        """, (data_inicio, data_fim))
        rows = cursor.fetchall()
    return [{'data': r[0], 'loja': r[1], 'total_euros': float(r[2]), 'n_produtos': int(r[3])} for r in rows]


def delete_vendas_detalhe_by_date_loja_pairs(pairs: list) -> int:
    """Delete all vendas_detalhe rows matching any (date, loja) in ``pairs``.

    ``pairs`` is a list of (date_obj, loja_name) tuples.
    Returns the total number of deleted rows.
    Used to clear existing data before a targeted reimport (replace semantics).
    """
    if not pairs:
        return 0
    with db_connection() as conn:
        cursor = conn.cursor()
        total_deleted = 0
        for d, loja in pairs:
            cursor.execute(
                "DELETE FROM vendas_detalhe WHERE data = %s AND loja = %s",
                (d, loja)
            )
            total_deleted += cursor.rowcount
        conn.commit()
    return total_deleted


def check_vendas_dates_have_data(datas: list, loja: str = None) -> list:
    """Return which dates from ``datas`` already have rows in vendas_detalhe.

    Used to warn the user before re-importing a file that may duplicate data.
    If ``loja`` is given, filter to that loja; otherwise any loja counts.
    Returns a list of date objects that already have data.
    """
    if not datas:
        return []
    with db_connection() as conn:
        cursor = conn.cursor()
        if loja:
            cursor.execute("""
                SELECT DISTINCT data FROM vendas_detalhe
                WHERE data = ANY(%s) AND loja = %s
            """, (datas, loja))
        else:
            cursor.execute("""
                SELECT DISTINCT data FROM vendas_detalhe
                WHERE data = ANY(%s)
            """, (datas,))
        rows = cursor.fetchall()
    return [r[0] for r in rows]


def sync_produtos_vendas_config():
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT produto FROM vendas_detalhe ORDER BY produto")
        produtos = [r[0] for r in cursor.fetchall()]

        cursor.execute("SELECT palavra_chave FROM regras_negocio WHERE area = 'gelado_kpi'")
        palavras_gelado = [r[0].lower() for r in cursor.fetchall()]
        cursor.execute("SELECT palavra_chave FROM regras_negocio WHERE area = 'pastelaria'")
        palavras_pastelaria = [r[0].lower() for r in cursor.fetchall()]
        cursor.execute("SELECT palavra_chave FROM regras_negocio WHERE area = 'confeitaria'")
        palavras_confeitaria = [r[0].lower() for r in cursor.fetchall()]

        if produtos:
            rows = []
            for produto in produtos:
                p_lower = produto.lower()
                is_gelado = any(kw in p_lower for kw in palavras_gelado) if palavras_gelado else False
                is_pastelaria = any(kw in p_lower for kw in palavras_pastelaria) if palavras_pastelaria else False
                is_confeitaria = any(kw in p_lower for kw in palavras_confeitaria) if palavras_confeitaria else False
                rows.append((produto, is_gelado, is_pastelaria, is_confeitaria))

            execute_values(
                cursor,
                """INSERT INTO produtos_vendas_config (produto, gelado_kpi, pastelaria, confeitaria)
                   VALUES %s ON CONFLICT (produto) DO NOTHING""",
                rows,
                page_size=200,
            )

        conn.commit()

def get_produtos_vendas_config() -> list:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM produtos_vendas_config ORDER BY produto")
        return [dict(r) for r in cursor.fetchall()]

def update_produto_vendas_config(produto_id: int, gelado_kpi: bool, pastelaria: bool, confeitaria: bool):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE produtos_vendas_config
            SET gelado_kpi = %s, pastelaria = %s, confeitaria = %s
            WHERE id = %s
        """, (gelado_kpi, pastelaria, confeitaria, produto_id))
        conn.commit()

def update_produtos_vendas_config_batch(updates: list):
    with db_connection() as conn:
        cursor = conn.cursor()
        for u in updates:
            cursor.execute("""
                UPDATE produtos_vendas_config
                SET gelado_kpi = %s, pastelaria = %s, confeitaria = %s
                WHERE id = %s
            """, (u['gelado_kpi'], u['pastelaria'], u['confeitaria'], u['id']))
        conn.commit()


def update_conta_vendas_diarias_batch(updates: list):
    """Set conta_vendas_diarias flag for a batch of produtos_vendas_config rows.

    Each entry in *updates* must have keys: id (int), conta (bool).
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        for u in updates:
            cursor.execute("""
                UPDATE produtos_vendas_config
                SET conta_vendas_diarias = %s
                WHERE id = %s
            """, (u['conta'], u['id']))
        conn.commit()


def update_b2b_vendas_diarias_batch(updates: list):
    """Set b2b flag for a batch of produtos_vendas_config rows.

    Each entry in *updates* must have keys: id (int), b2b (bool).

    Marking a product as b2b is retroactive: the Dashboard de Vendas
    aggregation joins on this column at query time, so all existing
    vendas_detalhe history for the product (2025 and 2026) moves into the
    B2B channel immediately, not just future sales.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        for u in updates:
            cursor.execute("""
                UPDATE produtos_vendas_config
                SET b2b = %s
                WHERE id = %s
            """, (u['b2b'], u['id']))
        conn.commit()

def get_produtos_by_area(area: str) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        if area == 'confeitaria':
            cursor.execute("SELECT produto FROM produtos_vendas_config WHERE confeitaria = TRUE ORDER BY produto")
        elif area == 'pastelaria':
            cursor.execute("SELECT produto FROM produtos_vendas_config WHERE pastelaria = TRUE ORDER BY produto")
        else:
            cursor.execute("SELECT produto FROM produtos_vendas_config WHERE gelado_kpi = TRUE ORDER BY produto")
        return [r[0] for r in cursor.fetchall()]

def delete_vendas_detalhe_by_dates(data_inicio: date, data_fim: date, loja: str = None) -> int:
    with db_connection() as conn:
        cursor = conn.cursor()
        if loja:
            cursor.execute("DELETE FROM vendas_detalhe WHERE data >= %s AND data <= %s AND loja = %s", (data_inicio, data_fim, loja))
        else:
            cursor.execute("DELETE FROM vendas_detalhe WHERE data >= %s AND data <= %s", (data_inicio, data_fim))
        deleted = cursor.rowcount
        conn.commit()
    return deleted

def get_gramas_gelado() -> list:
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM gramas_gelado ORDER BY artigo")
        return [dict(r) for r in cursor.fetchall()]

def update_gramas_gelado(artigo_id: int, artigo: str, gramas: float) -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE gramas_gelado SET artigo = %s, gramas = %s WHERE id = %s", (artigo, gramas, artigo_id))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False

def add_gramas_gelado(artigo: str, gramas: float) -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO gramas_gelado (artigo, gramas) VALUES (%s, %s)", (artigo, gramas))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False

def delete_gramas_gelado(artigo_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM gramas_gelado WHERE id = %s", (artigo_id,))
        conn.commit()

def get_consumo_gelado_mensal(loja: str = None) -> pd.DataFrame:
    query = """
        SELECT
            vd.produto,
            TO_CHAR(vd.data, 'YYYY-MM') as mes,
            SUM(vd.quantidade) as quantidade_vendida
        FROM vendas_detalhe vd
        INNER JOIN produtos_vendas_config pvc ON vd.produto = pvc.produto
        WHERE pvc.gelado_kpi = TRUE
    """
    params = []
    if loja:
        query += " AND vd.loja = %s"
        params.append(loja)
    query += " GROUP BY vd.produto, TO_CHAR(vd.data, 'YYYY-MM') ORDER BY vd.produto, mes"

    with db_connection() as conn:
        gramas_df = pd.read_sql_query("SELECT artigo, gramas FROM gramas_gelado", conn)
        vendas_df = pd.read_sql_query(query, conn, params=params)

    if vendas_df.empty:
        return vendas_df

    def find_gramas(produto):
        for _, row in gramas_df.iterrows():
            if row['artigo'].lower() in produto.lower():
                return row['gramas']
        return 0

    vendas_df['gramas_por_unidade'] = vendas_df['produto'].apply(find_gramas)
    vendas_df['consumo_kg'] = (vendas_df['quantidade_vendida'] * vendas_df['gramas_por_unidade']) / 1000

    return vendas_df


def get_product_dashboard_summary(area: str) -> pd.DataFrame:
    if area == 'pastelaria':
        prod_table = 'producao_pastelaria'
        config_field = 'pastelaria'
    else:
        prod_table = 'producao_confeitaria'
        config_field = 'confeitaria'

    today = date.today()
    week_ago = today - timedelta(days=7)

    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(f"SELECT produto FROM produtos_vendas_config WHERE {config_field} = TRUE ORDER BY produto")
        produtos = [row[0] for row in cursor.fetchall()]

        if not produtos:
            if area == 'pastelaria':
                cursor.execute("SELECT nome FROM produtos_pastelaria WHERE ativo = TRUE ORDER BY nome")
            else:
                cursor.execute("SELECT nome FROM produtos_confeitaria WHERE ativo = TRUE ORDER BY nome")
            produtos = [row[0] for row in cursor.fetchall()]

        if not produtos:
            return pd.DataFrame()

        cursor.execute(f"""
            SELECT DISTINCT ON (produto) produto, data, quantidade
            FROM {prod_table}
            ORDER BY produto, data DESC, id DESC
        """)
        all_prod_map = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
        last_prod_map = {}
        for p in produtos:
            if p in all_prod_map:
                last_prod_map[p] = all_prod_map[p]
            else:
                for prod_name, val in all_prod_map.items():
                    if p.lower() in prod_name.lower() or prod_name.lower() in p.lower():
                        last_prod_map[p] = val
                        break

        cursor.execute("""
            SELECT produto, loja, SUM(quantidade) as total
            FROM vendas_detalhe
            WHERE LOWER(produto) IN (SELECT LOWER(produto) FROM produtos_vendas_config WHERE """ + config_field + """ = TRUE)
            AND data >= %s AND data <= %s
            GROUP BY produto, loja
        """, (week_ago, today))
        vendas_semana = {}
        for row in cursor.fetchall():
            produto_venda = row[0]
            loja = row[1]
            total = int(row[2]) if row[2] else 0
            if produto_venda not in vendas_semana:
                vendas_semana[produto_venda] = {}
            vendas_semana[produto_venda][loja] = total

        cursor.execute("""
            SELECT DISTINCT ON (produto, loja) produto, loja, quantidade
            FROM contagem_stock
            WHERE tipo = %s
            ORDER BY produto, loja, data DESC, id DESC
        """, (area,))
        stock_map = {}
        for row in cursor.fetchall():
            produto_stock = row[0]
            loja = row[1]
            qtd = int(row[2]) if row[2] else 0
            if produto_stock not in stock_map:
                stock_map[produto_stock] = {}
            stock_map[produto_stock][loja] = qtd

    results = []
    for produto in produtos:
        lp = last_prod_map.get(produto)
        ultima_prod = f"{lp[1]} un ({lp[0].strftime('%d/%m')})" if lp else "-"

        vendas_mat = 0
        vendas_bol = 0
        for vp, lojas in vendas_semana.items():
            if produto.lower() in vp.lower() or vp.lower() in produto.lower():
                vendas_mat += lojas.get('Matosinhos', 0)
                vendas_bol += lojas.get('Bolhão', 0)

        stock_mat = stock_map.get(produto, {}).get('Matosinhos', 0)
        stock_bol = stock_map.get(produto, {}).get('Bolhão', 0)

        results.append({
            'Produto': produto,
            'Última Produção': ultima_prod,
            'Vendas Semana Mat.': int(vendas_mat),
            'Vendas Semana Bol.': int(vendas_bol),
            'Stock Mat.': int(stock_mat),
            'Stock Bol.': int(stock_bol),
        })

    return pd.DataFrame(results)

def get_tipologias_pastelaria() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nome FROM tipologias_pastelaria WHERE ativo = TRUE ORDER BY nome")
        return [row[0] for row in cursor.fetchall()]

def get_all_tipologias_pastelaria() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, nome, ativo FROM tipologias_pastelaria ORDER BY nome")
        return [{'id': row[0], 'nome': row[1], 'ativo': row[2]} for row in cursor.fetchall()]

def add_tipologia_pastelaria(nome: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO tipologias_pastelaria (nome) VALUES (%s)", (nome,))
            conn.commit()
            return True
        except psycopg2.IntegrityError:
            conn.rollback()
            return False

def delete_tipologia_pastelaria(id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tipologias_pastelaria WHERE id = %s", (id,))
        conn.commit()


def get_precos_caixa_kg_historico() -> list:
    """Return all config_preco_caixa_kg rows ordered by data_inicio DESC."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT id, data_inicio, preco_kg, created_at
            FROM config_preco_caixa_kg
            ORDER BY data_inicio DESC
        """)
        return [dict(r) for r in cursor.fetchall()]


def add_preco_caixa_kg(data_inicio: date, preco_kg: float) -> None:
    """Insert or update a price period in config_preco_caixa_kg."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO config_preco_caixa_kg (data_inicio, preco_kg)
            VALUES (%s, %s)
            ON CONFLICT (data_inicio) DO UPDATE SET preco_kg = EXCLUDED.preco_kg
        """, (data_inicio, preco_kg))
        conn.commit()


def delete_preco_caixa_kg(id: int) -> bool:
    """Delete a price period by id. Returns False if it is the base record (first ever)."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT MIN(id) FROM config_preco_caixa_kg")
        min_id = cursor.fetchone()[0]
        if min_id is None or int(id) == int(min_id):
            return False
        cursor.execute("DELETE FROM config_preco_caixa_kg WHERE id = %s", (id,))
        conn.commit()
    return True


def get_volume_por_produto(data_inicio: date = None, data_fim: date = None, loja: str = None) -> list:
    """Return per-product sales volume for the eurokg section.

    Returns a list of dicts with:
        produto       str   — product name
        gelado_kpi    bool  — included in €/kg KPI
        caixa_loja    bool  — in-store box sold by weight (excluded from KPI)
        canal         str   — Uber | Bolt | Glovo | Loja | Directo
        unidades      int   — number of sale lines in the period
        valor_euros   float — total value sold
        kg_estimado   float | None — estimated kg for caixa_loja (value/preco_kg_per_date);
                      NULL when no price is configured in config_preco_caixa_kg for the
                      sale date, or for non-caixa_loja products.

    Only products with gelado_kpi=TRUE or caixa_loja=TRUE are returned so the page
    focuses on the gelado product mix. kg_estimado uses effective-dated pricing from
    config_preco_caixa_kg (most recent data_inicio <= vd.data). Rows without a
    matching price record return NULL for kg_estimado rather than a silent default.
    """
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        query = """
            SELECT
                vd.produto,
                pvc.gelado_kpi,
                pvc.caixa_loja,
                CASE
                    WHEN vd.produto ILIKE '%%Uber%%'  THEN 'Uber'
                    WHEN vd.produto ILIKE '%%Bolt%%'  THEN 'Bolt'
                    WHEN vd.produto ILIKE '%%Glovo%%' THEN 'Glovo'
                    WHEN pvc.caixa_loja = TRUE        THEN 'Loja'
                    ELSE 'Directo'
                END AS canal,
                COUNT(*) AS unidades,
                COALESCE(SUM(vd.valor_euros), 0) AS valor_euros,
                CASE
                    WHEN pvc.caixa_loja = TRUE THEN
                        SUM(
                            vd.valor_euros /
                            (SELECT ck.preco_kg
                             FROM config_preco_caixa_kg ck
                             WHERE ck.data_inicio <= vd.data
                             ORDER BY ck.data_inicio DESC
                             LIMIT 1)
                        )
                    ELSE NULL
                END AS kg_estimado
            FROM vendas_detalhe vd
            INNER JOIN produtos_vendas_config pvc ON vd.produto = pvc.produto
            WHERE (pvc.gelado_kpi = TRUE OR pvc.caixa_loja = TRUE)
        """
        params = []
        if loja:
            query += " AND vd.loja = %s"
            params.append(loja)
        if data_inicio:
            query += " AND vd.data >= %s"
            params.append(data_inicio)
        if data_fim:
            query += " AND vd.data <= %s"
            params.append(data_fim)
        query += """
            GROUP BY vd.produto, pvc.gelado_kpi, pvc.caixa_loja,
                     CASE
                         WHEN vd.produto ILIKE '%%Uber%%'  THEN 'Uber'
                         WHEN vd.produto ILIKE '%%Bolt%%'  THEN 'Bolt'
                         WHEN vd.produto ILIKE '%%Glovo%%' THEN 'Glovo'
                         WHEN pvc.caixa_loja = TRUE        THEN 'Loja'
                         ELSE 'Directo'
                     END
            ORDER BY valor_euros DESC
        """
        cursor.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]
