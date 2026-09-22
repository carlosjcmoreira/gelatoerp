import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
import hashlib
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
                   o.supplier_id, o.store_id, a.origem_original
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
        }
        for r in rows
    ]


def add_artigo_administrativo(fornecedor: str, produto: str) -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO artigos_administrativos (fornecedor, produto) VALUES (%s, %s)", (fornecedor, produto))
            conn.commit()
            success = True
        except:
            conn.rollback()
            success = False
    invalidate_prefix('artigos_administrativos')
    return success


def update_artigo_administrativo(artigo_id: int, fornecedor: str, produto: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE artigos_administrativos SET fornecedor = %s, produto = %s WHERE id = %s", (fornecedor, produto, artigo_id))
        conn.commit()
    invalidate_prefix('artigos_administrativos')


def toggle_artigo_administrativo(artigo_id: int, ativo: bool):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE artigos_administrativos SET ativo = %s WHERE id = %s", (ativo, artigo_id))
        conn.commit()
    invalidate_prefix('artigos_administrativos')


def delete_artigo_administrativo(artigo_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM artigos_administrativos WHERE id = %s", (artigo_id,))
        conn.commit()
    invalidate_prefix('artigos_administrativos')


def seed_artigos_administrativos():
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM artigos_administrativos")
        if cursor.fetchone()[0] > 0:
            return
        artigos = [
            ('Makro', 'Canela em pó 500g'), ('Makro', 'Mel (kg)'), ('Makro', 'Granola (pacote com 800g'),
            ('Makro', 'Bolacha Maria (pacote com 800g)'), ('Makro', 'Açúcar para Café'),
            ('Makro', 'Adoçante Café'), ('Makro', 'Álcool 96% 100ml'), ('Makro', 'Esponja und.'),
            ('Makro', 'Papel vegetal'), ('Makro', 'Saco congelação 1 l'), ('Makro', 'Saco congelação 3l'),
            ('Makro', 'Cheirinho casa de banho Spray'), ('Makro', 'Lixívia Spray'),
            ('Makro', 'Detergente louça 4l'), ('Makro', 'Caixa Kraft pequena (pacote 50und.)'),
            ('Makro', 'Caixa Kraft grande (und)'), ('Makro', 'Caneta permanente preta (und)'),
            ('Makro', 'Neoblanc - Tira manchas (2l)'),
            ('Porto Higiene', 'Rolo de papel térmico 80x60x11 (caixa - 10 und.)'),
            ('Porto Higiene', 'Rolo de papel térmico 57x40x11 (cartão - 10 und.)'),
            ('Porto Higiene', 'Luva S (caixa)'), ('Porto Higiene', 'Luva M (caixa)'),
            ('Porto Higiene', 'Luva L (caixa)'), ('Porto Higiene', 'Saco de lixo 5l (pct - 10und)'),
            ('Porto Higiene', 'Saco de lixo 50l (pct -10 und)'), ('Porto Higiene', 'Saco de lixo 100l (pct - 10Und.)'),
            ('Porto Higiene', 'Película Aderente'), ('Porto Higiene', 'Pezinhos/Protetor de calçados'),
            ('Porto Higiene', 'Desengordurante (und - 5l)'), ('Porto Higiene', 'Sabonete WC (und - 5l)'),
            ('Porto Higiene', 'Abrilhantador máquina de louça (und - 5l)'),
            ('Porto Higiene', 'Desinfetante para superfícies Ecomix'),
            ('Porto Higiene', 'Detergente máquina de lavar louça (und - 5l)'),
            ('Porto Higiene', 'Desinfetante VT 10 - Álcool 70% (und - 5l)'),
            ('Porto Higiene', 'Desinfetante WC chão (und - 5l)'),
            ('Porto Higiene', 'Desinfetante chão - Bioalcool (und - 5l)'),
            ('Porto Higiene', 'Descalcificador (und - 5l)'), ('Porto Higiene', 'Guardanapos tipo L caixa'),
            ('Porto Higiene', 'Papel Autocorte (6 und)'), ('Porto Higiene', 'Papel Zigzag'),
            ('Porto Higiene', 'Papel Higiênico (12 und)'), ('Porto Higiene', 'Líquido de limpeza WC - (und - 5l)'),
            ('Porto Higiene', 'Touca preta redinha - (100 und)'),
            ('Greenpack', 'Tampas capuccino'), ('Greenpack', 'Tampa Milkshake'),
            ('Greenpack', 'Saco de papel kraft para cones'), ('Greenpack', 'Sacos de cookies'),
            ('Greenpack', 'Saco plástico para pastelaria'), ('Greenpack', 'Pratinho para Nivottos'),
            ('Greenpack', 'Copo para Água'), ('Greenpack', 'Copos takeway kraft'),
            ('Greenpack', 'Colheres Café embalada indivialmente 9cm'),
            ('Sumol Compal', 'Água sem gás 330ml'), ('Sumol Compal', 'Água com gás frize'),
            ('Sumol Compal', 'Frizze limão'), ('Sumol Compal', 'Frizze maracujá'),
            ('Sumol Compal', 'Frizze laranja'), ('Sumol Compal', 'Pepsi'),
            ('Sumol Compal', 'Pepsi zero'), ('Sumol Compal', 'Compal maga/laranja'),
            ('Sumol Compal', 'Compal pêssego'),
            ('SUVITA', 'Caixa Take Away 500g'), ('SUVITA', 'Caixa Take Away 1000g'),
            ('SUVITA', 'Caixa Take Away 1500g'), ('SUVITA', 'Pistacchio em pedaços'),
            ('SUVITA', 'Colheres para gelado'), ('SUVITA', 'Cone pequeno 40" (caixa 480 und)'),
            ('SUVITA', 'Cone Médio 45" (caixa 420 und.)'),
            ('LACTOGAL', 'Leite inteiro Gresso (l)'), ('LACTOGAL', 'Manteiga com sal Gresso (kg)'),
            ('MATOSINHOS', 'Copo mini (manga- 50und)'), ('MATOSINHOS', 'Copo pequeno (manga- 50und)'),
            ('MATOSINHOS', 'Copo médio (manga- 50und)'), ('MATOSINHOS', 'Copo grande (manga- 50und)'),
            ('MATOSINHOS', 'Copo max (manga- 50und)'), ('MATOSINHOS', 'Copo Café (manga- 50und)'),
            ('MATOSINHOS', 'Copo Cappucino/Branco Nivà (manga- 50und)'),
            ('MATOSINHOS', 'Copo Milkshake (manga- 50und)'), ('MATOSINHOS', 'Colheres brancas cartão Açaí'),
            ('MATOSINHOS', 'Guardanapos Nivà'), ('MATOSINHOS', 'Ricotta (kg)'),
            ('MATOSINHOS', 'Cannolo (und)'), ('MATOSINHOS', 'Chocolate 64% (Cannolo) - (und)'),
            ('MATOSINHOS', 'Brioche (und.)'), ('MATOSINHOS', 'Saco Nivà (und)'),
            ('MATOSINHOS', 'Fita Niva caixas Take away (rolo)'),
            ('MATOSINHOS', 'Capa para cone/Porta cones roxo'), ('MATOSINHOS', 'Base Branca Nivà'),
            ('MATOSINHOS', 'Cerejas em calda'), ('MATOSINHOS', 'Esfregona'),
            ('MATOSINHOS', 'Palhinha milkshake'), ('MATOSINHOS', 'Pote takeway p/ amarena'),
            ('Progelcone', 'Mini Cone'),
            ('CAFÉ ILLY', 'Café Classico'), ('CAFÉ ILLY', 'Descafeinado'),
            ('CAFÉ ILLY', 'Chá black'), ('CAFÉ ILLY', 'Chá lime'),
            ('CAFÉ ILLY', 'Chá citrus'), ('CAFÉ ILLY', 'Chá Red fruits'),
            ('GRÁFICA', 'Registro de temperaturas'), ('GRÁFICA', 'Limpeza WC'),
            ('GRÁFICA', 'Limpeza área clientes'), ('GRÁFICA', 'Limpeza zona de produção'),
            ('GRÁFICA', 'Rastreabilidade'), ('GRÁFICA', 'Entrada de mercadorias'),
            ('GRÁFICA', 'Fecho de caixa'),
        ]
        for forn, prod in artigos:
            cursor.execute("INSERT INTO artigos_administrativos (fornecedor, produto) VALUES (%s, %s) ON CONFLICT DO NOTHING", (forn, prod))
        conn.commit()
