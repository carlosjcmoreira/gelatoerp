import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
import hashlib
import re
import unicodedata
from db.connection import db_connection, logger
from db.cache import ttl_cache_args, invalidate_prefix
import json


COMPRAS_ORIGIN_TYPES = {
    'fornecedor_externo',
    'centro_interno',
    'categoria_operacional',
    'por_resolver',
}


def normalise_compras_origin_label(value: str) -> str:
    """Return a stable comparison form for spreadsheet origin labels."""
    value = ' '.join(str(value or '').split()).strip()
    return ''.join(
        char for char in unicodedata.normalize('NFKD', value.casefold())
        if not unicodedata.combining(char)
    )


def classify_compras_origin_label(label: str) -> dict:
    """Classify a spreadsheet label without inventing a supplier identity.

    Only the explicitly confirmed operational meanings are classified here.
    Everything else remains unresolved until a human links it to a canonical
    supplier.
    """
    display_label = ' '.join(str(label or '').split()).strip()
    comparison = normalise_compras_origin_label(display_label)
    known = {
        'matosinhos': {
            'key': 'centro:matosinhos',
            'tipo': 'centro_interno',
            'nome': 'Matosinhos',
            'store_name': 'Matosinhos',
        },
        'moedas': {
            'key': 'categoria:moedas',
            'tipo': 'categoria_operacional',
            'nome': 'Moedas',
            'store_name': 'Matosinhos',
        },
        'grafica': {
            'key': 'categoria:grafica',
            'tipo': 'categoria_operacional',
            'nome': 'Gráfica',
            'store_name': None,
        },
    }
    if comparison in known:
        result = dict(known[comparison])
        result['rotulo_original'] = display_label
        return result

    digest = hashlib.sha256(comparison.encode('utf-8')).hexdigest()[:24]
    return {
        'key': f'por-resolver:{digest}',
        'tipo': 'por_resolver',
        'nome': display_label or 'Origem por resolver',
        'store_name': None,
        'rotulo_original': display_label,
    }


def catalog_key_for(origin_label: str, product: str) -> str:
    """Build a stable identity from the validated source origin and product."""
    value = (
        normalise_compras_origin_label(origin_label)
        + '\x00'
        + normalise_compras_origin_label(product)
    )
    return f'bolhao:{hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]}'


def infer_artigo_unidade(product: str) -> str | None:
    """Infer only explicit units; ambiguous packaging stays blank."""
    value = normalise_compras_origin_label(product)
    if re.search(r'\(\s*kg\s*\)', value) or re.search(r'\b\d+\s*kg\b', value):
        return 'kg'
    if re.search(r'\(\s*l\s*\)', value) or re.search(r'\b\d+\s*l\b', value):
        return 'l'
    if re.search(r'\brolo\b', value):
        return 'rolo'
    if re.search(r'\bpct\b|\bpacote\b', value):
        return 'pct'
    if re.search(r'\bcaixa\b|\bcartao\b', value):
        return 'cx'
    if re.search(r'\bund\b|\bunidade\b|\bmanga\b', value):
        return 'und'
    return None


def get_compras_origens(apenas_ativos: bool = True, tipo: str = None) -> list:
    """List purchasing origins with canonical supplier/store references."""
    with db_connection() as conn:
        cursor = conn.cursor()
        query = """
            SELECT o.id, o.chave, o.tipo, o.nome, o.rotulo_original,
                   o.supplier_id, s.name, o.store_id, st.name, o.ativo
            FROM compras_origens o
            LEFT JOIN suppliers s ON s.id = o.supplier_id
            LEFT JOIN stores st ON st.id = o.store_id
        """
        clauses = []
        params = []
        if apenas_ativos:
            clauses.append("o.ativo = TRUE")
        if tipo:
            if tipo not in COMPRAS_ORIGIN_TYPES:
                raise ValueError('Tipo de origem inválido.')
            clauses.append("o.tipo = %s")
            params.append(tipo)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY o.tipo, o.nome"
        cursor.execute(query, params)
        rows = cursor.fetchall()
    return [
        {
            'id': row[0],
            'chave': row[1],
            'tipo': row[2],
            'nome': row[3],
            'rotulo_original': row[4],
            'supplier_id': row[5],
            'supplier_name': row[6],
            'store_id': row[7],
            'store_name': row[8],
            'ativo': row[9],
        }
        for row in rows
    ]


def set_artigo_origem(artigo_id: int, origem_id: int, actor: str = 'sistema',
                      reason: str = None) -> bool:
    """Change an article origin and append an auditable classification record."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT origem_id, origem_original FROM artigos_administrativos "
            "WHERE id = %s FOR UPDATE",
            (artigo_id,),
        )
        current = cursor.fetchone()
        if not current:
            return False
        cursor.execute("SELECT id FROM compras_origens WHERE id = %s AND ativo = TRUE", (origem_id,))
        if not cursor.fetchone():
            raise ValueError('A origem selecionada não existe ou está inativa.')
        if current[0] == origem_id:
            return True
        cursor.execute(
            "UPDATE artigos_administrativos SET origem_id = %s WHERE id = %s",
            (origem_id, artigo_id),
        )
        cursor.execute(
            """
            INSERT INTO artigos_administrativos_origem_audit
                (artigo_id, origem_anterior_id, origem_nova_id,
                 rotulo_original, actor, reason)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (artigo_id, current[0], origem_id, current[1], actor, reason),
        )
        conn.commit()
    invalidate_prefix('artigos_administrativos')
    return True


def get_artigo_origem_history(artigo_id: int) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT h.id, h.artigo_id, h.origem_anterior_id, old.nome,
                   h.origem_nova_id, new.nome, h.rotulo_original,
                   h.actor, h.reason, h.created_at
            FROM artigos_administrativos_origem_audit h
            LEFT JOIN compras_origens old ON old.id = h.origem_anterior_id
            LEFT JOIN compras_origens new ON new.id = h.origem_nova_id
            WHERE h.artigo_id = %s
            ORDER BY h.created_at DESC, h.id DESC
            """,
            (artigo_id,),
        )
        rows = cursor.fetchall()
    return [
        {
            'id': row[0],
            'artigo_id': row[1],
            'origem_anterior_id': row[2],
            'origem_anterior_nome': row[3],
            'origem_nova_id': row[4],
            'origem_nova_nome': row[5],
            'rotulo_original': row[6],
            'actor': row[7],
            'reason': row[8],
            'created_at': row[9],
        }
        for row in rows
    ]


@ttl_cache_args('artigos_administrativos', ttl=600)
def get_artigos_administrativos(apenas_ativos: bool = True) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        query = """
            SELECT a.id, a.fornecedor, a.produto, a.ativo,
                   a.origem_id, o.chave, o.tipo, o.nome,
                   o.supplier_id, o.store_id, a.origem_original,
                   a.marca, a.unidade, a.catalog_key, a.source_dataset,
                   a.source_version, a.source_row
            FROM artigos_administrativos a
            LEFT JOIN compras_origens o ON o.id = a.origem_id
        """
        if apenas_ativos:
            query += " WHERE a.ativo = TRUE"
        query += " ORDER BY a.fornecedor, a.produto"
        cursor.execute(query)
        rows = cursor.fetchall()
    return [
        {
            'id': r[0], 'fornecedor': r[1], 'produto': r[2], 'ativo': r[3],
            'origem_id': r[4], 'origem_chave': r[5], 'origem_tipo': r[6],
            'origem_nome': r[7], 'origem_supplier_id': r[8],
            'origem_store_id': r[9], 'origem_original': r[10],
            'marca': r[11], 'unidade': r[12],
            'catalog_key': r[13], 'source_dataset': r[14],
            'source_version': r[15], 'source_row': r[16],
        }
        for r in rows
    ]


def add_artigo_administrativo(fornecedor: str, produto: str, marca: str = None,
                              unidade: str = None, origem_id: int = None,
                              actor: str = 'sistema') -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            if origem_id is None:
                origin = classify_compras_origin_label(fornecedor)
                if origin.get('store_name'):
                    cursor.execute(
                        """
                        INSERT INTO compras_origens
                            (chave, tipo, nome, rotulo_original, store_id)
                        SELECT %s, %s, %s, %s, s.id
                        FROM stores s
                        WHERE LOWER(s.name) = LOWER(%s)
                        ON CONFLICT (chave) DO NOTHING
                        """,
                        (
                            origin['key'], origin['tipo'], origin['nome'],
                            origin['rotulo_original'], origin['store_name'],
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO compras_origens (chave, tipo, nome, rotulo_original)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (chave) DO NOTHING
                        """,
                        (
                            origin['key'], origin['tipo'], origin['nome'],
                            origin['rotulo_original'],
                        ),
                    )
                cursor.execute(
                    "SELECT id FROM compras_origens WHERE chave = %s",
                    (origin['key'],),
                )
                origin_row = cursor.fetchone()
                origem_id = origin_row[0] if origin_row else None
            cursor.execute(
                """
                INSERT INTO artigos_administrativos
                    (fornecedor, produto, marca, unidade, origem_id,
                     origem_original, human_modified_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                """,
                (fornecedor, produto, marca or None, unidade or None,
                 origem_id, fornecedor),
            )
            conn.commit()
            success = True
        except psycopg2.Error:
            conn.rollback()
            success = False
    invalidate_prefix('artigos_administrativos')
    return success


def update_artigo_administrativo(artigo_id: int, fornecedor: str, produto: str,
                                 marca: str = None, unidade: str = None,
                                 origem_id: int = None, actor: str = 'sistema'):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT origem_id, origem_original FROM artigos_administrativos "
            "WHERE id = %s FOR UPDATE",
            (artigo_id,),
        )
        current = cursor.fetchone()
        if not current:
            return False
        if origem_id is not None:
            cursor.execute(
                "SELECT id FROM compras_origens WHERE id = %s AND ativo = TRUE",
                (origem_id,),
            )
            if not cursor.fetchone():
                raise ValueError('A origem selecionada não existe ou está inativa.')
        cursor.execute(
            """
            UPDATE artigos_administrativos
               SET fornecedor = %s, produto = %s, marca = %s, unidade = %s,
                   origem_id = COALESCE(%s, origem_id),
                   origem_original = COALESCE(origem_original, %s),
                   human_modified_at = NOW(), updated_at = NOW()
             WHERE id = %s
            """,
            (fornecedor, produto, marca or None, unidade or None, origem_id,
             fornecedor, artigo_id),
        )
        if origem_id is not None and current[0] != origem_id:
            cursor.execute(
                """
                INSERT INTO artigos_administrativos_origem_audit
                    (artigo_id, origem_anterior_id, origem_nova_id,
                     rotulo_original, actor, reason)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    artigo_id, current[0], origem_id,
                    current[1] or fornecedor, actor, 'edição do catálogo',
                ),
            )
        conn.commit()
    invalidate_prefix('artigos_administrativos')
    return True


def toggle_artigo_administrativo(artigo_id: int, ativo: bool):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE artigos_administrativos SET ativo = %s, updated_at = NOW(), "
            "human_modified_at = NOW() WHERE id = %s",
            (ativo, artigo_id),
        )
        conn.commit()
    invalidate_prefix('artigos_administrativos')


def delete_artigo_administrativo(artigo_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE artigos_administrativos SET ativo = FALSE, updated_at = NOW(), "
            "human_modified_at = NOW() WHERE id = %s",
            (artigo_id,),
        )
        conn.commit()
    invalidate_prefix('artigos_administrativos')


def seed_artigos_administrativos():
    from db.schema import run_migrations_compras_catalogo
    return run_migrations_compras_catalogo()
