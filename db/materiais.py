from datetime import date
import logging
from db.connection import get_connection, release_connection
from db.cache import ttl_cache_args, invalidate_prefix

logger = logging.getLogger(__name__)

LOCAIS_STOCK = ['Bolhão', 'Matosinhos', 'Garagem']

CATEGORIAS_MATERIAIS = [
    'Embalagens',
    'Higiene',
    'Cafetaria',
    'Produção',
    'Outro',
]

UNIDADES_MATERIAIS = ['un', 'cx', 'kg', 'l', 'rolo', 'pct', 'par']


# ── Catálogo de Materiais ──────────────────────────────────────────────────────

@ttl_cache_args('materiais', ttl=300)
def list_materiais(apenas_ativos: bool = False) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    query = """
        SELECT id, nome, unidade, categoria, fornecedor, ativo, created_at, updated_at
        FROM materiais
    """
    if apenas_ativos:
        query += " WHERE ativo = TRUE"
    query += " ORDER BY categoria, nome"
    cursor.execute(query)
    rows = cursor.fetchall()
    release_connection(conn)
    return [
        {
            'id': r[0],
            'nome': r[1],
            'unidade': r[2],
            'categoria': r[3],
            'fornecedor': r[4],
            'ativo': r[5],
            'created_at': r[6],
            'updated_at': r[7],
        }
        for r in rows
    ]


def get_material(material_id: int) -> dict | None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, nome, unidade, categoria, fornecedor, ativo
        FROM materiais WHERE id = %s
    """, (material_id,))
    r = cursor.fetchone()
    release_connection(conn)
    if not r:
        return None
    return {'id': r[0], 'nome': r[1], 'unidade': r[2], 'categoria': r[3],
            'fornecedor': r[4], 'ativo': r[5]}


def upsert_material(nome: str, unidade: str, categoria: str,
                    fornecedor: str = None, material_id: int = None) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    if material_id:
        cursor.execute("""
            UPDATE materiais
            SET nome = %s, unidade = %s, categoria = %s, fornecedor = %s,
                updated_at = NOW()
            WHERE id = %s
            RETURNING id
        """, (nome, unidade, categoria, fornecedor or None, material_id))
    else:
        cursor.execute("""
            INSERT INTO materiais (nome, unidade, categoria, fornecedor)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (nome) DO UPDATE
              SET unidade = EXCLUDED.unidade,
                  categoria = EXCLUDED.categoria,
                  fornecedor = EXCLUDED.fornecedor,
                  updated_at = NOW()
            RETURNING id
        """, (nome, unidade, categoria, fornecedor or None))
    row = cursor.fetchone()
    conn.commit()
    release_connection(conn)
    invalidate_prefix('materiais')
    return row[0] if row else None


def toggle_material_ativo(material_id: int, ativo: bool):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE materiais SET ativo = %s, updated_at = NOW() WHERE id = %s",
        (ativo, material_id)
    )
    conn.commit()
    release_connection(conn)
    invalidate_prefix('materiais')



# ── Stock e Movimentos ─────────────────────────────────────────────────────────

def get_stock_atual(local: str = None) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    if local:
        cursor.execute("""
            SELECT m.id, m.nome, m.unidade, m.categoria, m.fornecedor,
                   COALESCE(s.quantidade, 0) AS quantidade
            FROM materiais m
            LEFT JOIN stock_materiais s
                   ON s.material_id = m.id AND s.local = %s
            WHERE m.ativo = TRUE
            ORDER BY m.categoria, m.nome
        """, (local,))
    else:
        cursor.execute("""
            SELECT m.id, m.nome, m.unidade, m.categoria, m.fornecedor,
                   s.local,
                   COALESCE(s.quantidade, 0) AS quantidade
            FROM materiais m
            LEFT JOIN stock_materiais s ON s.material_id = m.id
            WHERE m.ativo = TRUE
            ORDER BY m.categoria, m.nome, s.local
        """)
    rows = cursor.fetchall()
    release_connection(conn)
    if local:
        return [
            {'material_id': r[0], 'nome': r[1], 'unidade': r[2],
             'categoria': r[3], 'fornecedor': r[4], 'quantidade': float(r[5])}
            for r in rows
        ]
    return [
        {'material_id': r[0], 'nome': r[1], 'unidade': r[2],
         'categoria': r[3], 'fornecedor': r[4], 'local': r[5],
         'quantidade': float(r[6])}
        for r in rows
    ]


def _upsert_stock_materiais(cursor, material_id: int, local: str, delta: float):
    cursor.execute("""
        INSERT INTO stock_materiais (material_id, local, quantidade)
        VALUES (%s, %s, %s)
        ON CONFLICT (material_id, local)
        DO UPDATE SET quantidade = stock_materiais.quantidade + EXCLUDED.quantidade,
                      updated_at = NOW()
    """, (material_id, local, delta))


def add_movimento_stock(material_id: int, local: str, tipo: str,
                        quantidade: float, utilizador: str = None,
                        notas: str = None, data: date = None,
                        invoice_id: int = None) -> int:
    if tipo not in ('entrada', 'saida', 'contagem', 'ajuste'):
        raise ValueError(f"tipo inválido: {tipo!r}")
    if local not in LOCAIS_STOCK:
        raise ValueError(f"local inválido: {local!r}")
    if tipo == 'entrada' and quantidade <= 0:
        raise ValueError("entrada requer quantidade > 0")
    if tipo == 'saida' and quantidade <= 0:
        raise ValueError("saida requer quantidade > 0")
    if tipo == 'contagem' and quantidade < 0:
        raise ValueError("contagem requer quantidade >= 0")

    data = data or date.today()
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO movimentos_stock_materiais
               (material_id, local, tipo, quantidade, data, invoice_id, notas, utilizador)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (material_id, local, tipo, quantidade, data, invoice_id, notas, utilizador))
    movimento_id = cursor.fetchone()[0]

    if tipo == 'contagem':
        cursor.execute("""
            INSERT INTO stock_materiais (material_id, local, quantidade)
            VALUES (%s, %s, %s)
            ON CONFLICT (material_id, local)
            DO UPDATE SET quantidade = EXCLUDED.quantidade, updated_at = NOW()
        """, (material_id, local, quantidade))
    elif tipo in ('entrada', 'ajuste') and quantidade >= 0:
        _upsert_stock_materiais(cursor, material_id, local, quantidade)
    elif tipo == 'saida' or (tipo == 'ajuste' and quantidade < 0):
        _upsert_stock_materiais(cursor, material_id, local, -abs(quantidade))

    conn.commit()
    release_connection(conn)
    return movimento_id


def get_movimentos_stock_count(local: str = None, tipo: str = None) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    where = []
    params = []
    if local:
        where.append("mv.local = %s")
        params.append(local)
    if tipo:
        where.append("mv.tipo = %s")
        params.append(tipo)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    cursor.execute(f"SELECT COUNT(*) FROM movimentos_stock_materiais mv {clause}", params)
    count = cursor.fetchone()[0]
    release_connection(conn)
    return count


# ── Linhas de Fatura (invoice line items) ─────────────────────────────────────

def get_invoice_linhas(invoice_id: int) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT il.id, il.invoice_id, il.material_id, m.nome AS material_nome,
               il.descricao, il.quantidade, il.unidade, il.preco_unitario,
               il.stock_registado
        FROM invoice_linhas il
        LEFT JOIN materiais m ON m.id = il.material_id
        WHERE il.invoice_id = %s
        ORDER BY il.id
    """, (invoice_id,))
    rows = cursor.fetchall()
    release_connection(conn)
    return [
        {
            'id': r[0],
            'invoice_id': r[1],
            'material_id': r[2],
            'material_nome': r[3],
            'descricao': r[4],
            'quantidade': float(r[5]),
            'unidade': r[6],
            'preco_unitario': float(r[7]) if r[7] is not None else None,
            'stock_registado': r[8],
        }
        for r in rows
    ]


def upsert_invoice_linha(invoice_id: int, descricao: str, quantidade: float,
                         unidade: str, material_id: int = None,
                         preco_unitario: float = None,
                         linha_id: int = None) -> int:
    if not descricao or not descricao.strip():
        raise ValueError("descricao é obrigatória")
    if quantidade is None or float(quantidade) <= 0:
        raise ValueError("quantidade deve ser > 0")
    conn = get_connection()
    cursor = conn.cursor()
    if linha_id:
        cursor.execute("""
            UPDATE invoice_linhas
            SET material_id = %s, descricao = %s, quantidade = %s,
                unidade = %s, preco_unitario = %s, updated_at = NOW()
            WHERE id = %s AND invoice_id = %s
            RETURNING id
        """, (material_id or None, descricao, quantidade,
              unidade, preco_unitario, linha_id, invoice_id))
    else:
        cursor.execute("""
            INSERT INTO invoice_linhas
                   (invoice_id, material_id, descricao, quantidade, unidade, preco_unitario)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (invoice_id, material_id or None, descricao, quantidade,
              unidade, preco_unitario))
    row = cursor.fetchone()
    conn.commit()
    release_connection(conn)
    return row[0] if row else None


def delete_invoice_linha(linha_id: int, invoice_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM invoice_linhas WHERE id = %s AND invoice_id = %s",
        (linha_id, invoice_id)
    )
    conn.commit()
    release_connection(conn)


_STORE_NAME_TO_LOCAL = {
    'matosinhos': 'Matosinhos',
    'bolhão': 'Bolhão',
    'bolhao': 'Bolhão',
}


def derive_local_from_store(store_name: str) -> str | None:
    """Map a store name to a LOCAIS_STOCK value, or None if unknown."""
    if not store_name:
        return None
    return _STORE_NAME_TO_LOCAL.get(store_name.lower().strip())


def registar_entradas_stock_fatura(invoice_id: int, utilizador: str,
                                   local: str) -> dict:
    """
    For each invoice_linha that has a material_id,
    create a stock 'entrada' movement atomically.
    Marks each linha as stock_registado and sets invoice.stock_registado_at.
    Uses atomic UPDATE with WHERE stock_registado_at IS NULL to prevent
    double execution under concurrency.
    Returns {'registadas': N}.
    Raises ValueError if already registered or local is invalid.
    """
    if local not in LOCAIS_STOCK:
        raise ValueError(f"local inválido: {local!r}")
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE invoices
            SET stock_registado_at = NOW(), stock_registado_por = %s
            WHERE id = %s AND stock_registado_at IS NULL
            RETURNING id
        """, (utilizador, invoice_id))
        claimed = cursor.fetchone()
        if not claimed:
            conn.rollback()
            raise ValueError("O stock desta fatura já foi registado.")

        cursor.execute("""
            SELECT id, material_id, quantidade
            FROM invoice_linhas
            WHERE invoice_id = %s AND material_id IS NOT NULL
        """, (invoice_id,))
        linhas = cursor.fetchall()
        registadas = 0
        for linha in linhas:
            linha_id, material_id, quantidade = linha
            cursor.execute("""
                INSERT INTO movimentos_stock_materiais
                       (material_id, local, tipo, quantidade, data, invoice_id, utilizador)
                VALUES (%s, %s, 'entrada', %s, CURRENT_DATE, %s, %s)
            """, (material_id, local, float(quantidade), invoice_id, utilizador))
            cursor.execute("""
                INSERT INTO stock_materiais (material_id, local, quantidade)
                VALUES (%s, %s, %s)
                ON CONFLICT (material_id, local)
                DO UPDATE SET quantidade = stock_materiais.quantidade + EXCLUDED.quantidade,
                              updated_at = NOW()
            """, (material_id, local, float(quantidade)))
            cursor.execute(
                "UPDATE invoice_linhas SET stock_registado = TRUE, updated_at = NOW() WHERE id = %s",
                (linha_id,)
            )
            registadas += 1

        conn.commit()
        invalidate_prefix('materiais')
        return {'registadas': registadas}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        release_connection(conn)


def get_movimentos_stock(local: str = None, material_id: int = None,
                         tipo: str = None, limit: int = 200,
                         offset: int = 0) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    where = []
    params = []
    if local:
        where.append("mv.local = %s")
        params.append(local)
    if material_id:
        where.append("mv.material_id = %s")
        params.append(material_id)
    if tipo:
        where.append("mv.tipo = %s")
        params.append(tipo)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.extend([limit, offset])
    cursor.execute(f"""
        SELECT mv.id, mv.data, m.nome, m.unidade, mv.local, mv.tipo,
               mv.quantidade, mv.notas, mv.utilizador, mv.invoice_id
        FROM movimentos_stock_materiais mv
        JOIN materiais m ON m.id = mv.material_id
        {clause}
        ORDER BY mv.created_at DESC
        LIMIT %s OFFSET %s
    """, params)
    rows = cursor.fetchall()
    release_connection(conn)
    return [
        {
            'id': r[0], 'data': r[1], 'nome': r[2], 'unidade': r[3],
            'local': r[4], 'tipo': r[5], 'quantidade': float(r[6]),
            'notas': r[7], 'utilizador': r[8], 'invoice_id': r[9],
        }
        for r in rows
    ]
