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


def get_movimentos_stock(local: str = None, material_id: int = None,
                         limit: int = 200) -> list:
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
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    cursor.execute(f"""
        SELECT mv.id, mv.data, m.nome, m.unidade, mv.local, mv.tipo,
               mv.quantidade, mv.notas, mv.utilizador, mv.invoice_id
        FROM movimentos_stock_materiais mv
        JOIN materiais m ON m.id = mv.material_id
        {clause}
        ORDER BY mv.created_at DESC
        LIMIT %s
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
