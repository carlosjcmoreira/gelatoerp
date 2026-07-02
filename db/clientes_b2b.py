"""DB layer for B2B/Events clients (clientes_b2b table)."""
import logging
from db.connection import db_connection

logger = logging.getLogger(__name__)


def upsert_cliente(codigo: str, nome: str, nif: str) -> int:
    """Insert or update a client by NIF. Returns the client id."""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO clientes_b2b (codigo, nome, nif)
            VALUES (%s, %s, %s)
            ON CONFLICT (nif) DO UPDATE
                SET codigo = EXCLUDED.codigo,
                    nome   = EXCLUDED.nome,
                    updated_at = NOW()
            RETURNING id
        """, (str(codigo).strip(), nome.strip(), str(nif).strip()))
        row = cur.fetchone()
        conn.commit()
        return row[0]


def list_clientes(tipo: str = None) -> list:
    """Return all clients, optionally filtered by tipo ('b2b' or 'eventos')."""
    with db_connection() as conn:
        cur = conn.cursor()
        if tipo:
            cur.execute("""
                SELECT id, codigo, nome, nif, tipo, incluir_mapas, criado_em, updated_at
                FROM clientes_b2b
                WHERE tipo = %s
                ORDER BY nome
            """, (tipo,))
        else:
            cur.execute("""
                SELECT id, codigo, nome, nif, tipo, incluir_mapas, criado_em, updated_at
                FROM clientes_b2b
                ORDER BY nome
            """)
        cols = ['id', 'codigo', 'nome', 'nif', 'tipo', 'incluir_mapas', 'criado_em', 'updated_at']
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_cliente_by_id(cliente_id: int) -> dict:
    """Return a single client by id, or None."""
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, codigo, nome, nif, tipo, incluir_mapas, criado_em, updated_at
            FROM clientes_b2b WHERE id = %s
        """, (cliente_id,))
        row = cur.fetchone()
        if not row:
            return None
        cols = ['id', 'codigo', 'nome', 'nif', 'tipo', 'incluir_mapas', 'criado_em', 'updated_at']
        return dict(zip(cols, row))


def update_cliente(cliente_id: int, tipo: str = None, incluir_mapas: bool = None,
                   nome: str = None, nif: str = None, codigo: str = None):
    """Update editable fields on a client."""
    fields = []
    vals = []
    if tipo is not None:
        fields.append("tipo = %s")
        vals.append(tipo)
    if incluir_mapas is not None:
        fields.append("incluir_mapas = %s")
        vals.append(incluir_mapas)
    if nome is not None:
        fields.append("nome = %s")
        vals.append(nome.strip())
    if nif is not None:
        fields.append("nif = %s")
        vals.append(str(nif).strip())
    if codigo is not None:
        fields.append("codigo = %s")
        vals.append(str(codigo).strip())
    if not fields:
        return
    fields.append("updated_at = NOW()")
    vals.append(cliente_id)
    with db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE clientes_b2b SET {', '.join(fields)} WHERE id = %s",
            vals
        )
        conn.commit()
