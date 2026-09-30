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
            """
            SELECT a.origem_id, a.origem_original, a.fornecedor_oficial_id,
                   official_s.name
            FROM artigos_administrativos a
            LEFT JOIN suppliers official_s
                   ON official_s.id = a.fornecedor_oficial_id
            WHERE a.id = %s FOR UPDATE OF a
            """,
            (artigo_id,),
        )
        current = cursor.fetchone()
        if not current:
            return False
        cursor.execute(
            "SELECT id, tipo, supplier_id FROM compras_origens "
            "WHERE id = %s AND ativo = TRUE",
            (origem_id,),
        )
        selected_origin = cursor.fetchone()
        if not selected_origin:
            raise ValueError('A origem selecionada não existe ou está inativa.')
        next_supplier_id = current[2]
        next_supplier_name = current[3]
        if selected_origin[1] == 'fornecedor_externo':
            if (
                next_supplier_id is not None
                and next_supplier_id != selected_origin[2]
            ):
                raise ValueError(
                    'A origem externa e o fornecedor oficial são diferentes. '
                    'Resolva o fornecedor oficial antes de alterar a origem.'
                )
            next_supplier_id = selected_origin[2]
            cursor.execute("SELECT name FROM suppliers WHERE id = %s", (next_supplier_id,))
            supplier = cursor.fetchone()
            next_supplier_name = supplier[0] if supplier else None
        origin_changed = current[0] != origem_id
        supplier_changed = current[2] != next_supplier_id
        if not origin_changed and not supplier_changed:
            return True
        cursor.execute(
            """
            UPDATE artigos_administrativos
               SET origem_id = %s, fornecedor_oficial_id = %s,
                   human_modified_at = NOW(), updated_at = NOW()
             WHERE id = %s
            """,
            (origem_id, next_supplier_id, artigo_id),
        )
        if origin_changed:
            cursor.execute(
                """
                INSERT INTO artigos_administrativos_origem_audit
                    (artigo_id, origem_anterior_id, origem_nova_id,
                     rotulo_original, actor, reason)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (artigo_id, current[0], origem_id, current[1], actor, reason),
            )
        if supplier_changed:
            cursor.execute(
                """
                INSERT INTO artigos_administrativos_fornecedor_audit
                    (artigo_id, fornecedor_anterior_id, fornecedor_anterior_nome,
                     fornecedor_novo_id, fornecedor_novo_nome, actor, reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    artigo_id, current[2], current[3], next_supplier_id,
                    next_supplier_name, actor,
                    reason or 'associação derivada de origem externa confirmada',
                ),
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
                   a.source_version, a.source_row, o.ativo,
                    a.fornecedor_oficial_id, official_s.name,
                    a.categoria_artigo, a.origem_revisao_estado
            FROM artigos_administrativos a
            LEFT JOIN compras_origens o ON o.id = a.origem_id
            LEFT JOIN suppliers official_s
                   ON official_s.id = a.fornecedor_oficial_id
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
            'origem_ativa': r[17],
            'fornecedor_oficial_id': r[18],
            'fornecedor_oficial_nome': r[19],
            'categoria_artigo': r[20],
            'origem_revisao_estado': r[21],
        }
        for r in rows
    ]


def get_artigo_administrativo(artigo_id: int) -> dict | None:
    """Return one catalogue article, including its current origin."""
    return next(
        (a for a in get_artigos_administrativos(apenas_ativos=False)
         if a['id'] == int(artigo_id)),
        None,
    )


def _supplier_identity_confirmed(cursor, invoice_id: int) -> tuple[int | None, bool]:
    cursor.execute(
        """
        SELECT i.supplier_id, i.supplier_name, i.supplier_nif,
               s.name, s.nif
        FROM invoices i
        LEFT JOIN suppliers s ON s.id = i.supplier_id
        WHERE i.id = %s
        """,
        (invoice_id,),
    )
    row = cursor.fetchone()
    if not row or not row[0]:
        return None, False
    supplier_id, invoice_name, invoice_nif, legal_name, legal_nif = row
    names_match = (
        normalise_compras_origin_label(invoice_name)
        == normalise_compras_origin_label(legal_name)
    )
    def nif(value):
        return ''.join(c for c in str(value or '') if c.isdigit()).lstrip('0')
    nifs_match = not invoice_nif or not legal_nif or nif(invoice_nif) == nif(legal_nif)
    return supplier_id, bool(names_match and nifs_match)


def get_invoice_linha_artigo_suggestions(invoice_id: int) -> dict:
    """Return exact, supplier-scoped catalogue suggestions for invoice lines.

    Internal, operational, unresolved, inactive, and supplier-conflicting
    origins deliberately never enter the suggestion set.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        supplier_id, supplier_confirmed = _supplier_identity_confirmed(cursor, invoice_id)
        cursor.execute(
            """
            SELECT il.id, il.artigo_id, il.descricao,
                   a.id, a.fornecedor, a.produto, a.unidade,
                   o.tipo, o.supplier_id
            FROM invoice_linhas il
            LEFT JOIN artigos_administrativos a ON a.id = il.artigo_id
            LEFT JOIN compras_origens o ON o.id = a.origem_id
            WHERE il.invoice_id = %s
            ORDER BY il.id
            """,
            (invoice_id,),
        )
        lines = cursor.fetchall()
        candidates = []
        if supplier_confirmed:
            cursor.execute(
                """
                SELECT a.id, a.fornecedor, a.produto, a.unidade,
                       o.id, o.nome
                FROM artigos_administrativos a
                JOIN compras_origens o ON o.id = a.origem_id
                WHERE a.ativo = TRUE AND o.ativo = TRUE
                  AND o.tipo = 'fornecedor_externo'
                  AND o.supplier_id = %s
                ORDER BY a.produto, a.id
                """,
                (supplier_id,),
            )
            candidates = cursor.fetchall()

    result = {}
    for row in lines:
        line_id, current_id, description = row[:3]
        exact = [
            {
                'id': candidate[0],
                'fornecedor': candidate[1],
                'produto': candidate[2],
                'unidade': candidate[3],
                'origem_id': candidate[4],
                'origem_nome': candidate[5],
            }
            for candidate in candidates
            if normalise_compras_origin_label(description)
            == normalise_compras_origin_label(candidate[2])
        ]
        if not supplier_confirmed:
            state = 'supplier_unconfirmed'
        elif len(exact) == 1:
            state = 'exact'
        elif len(exact) > 1:
            state = 'ambiguous'
        else:
            state = 'unresolved'
        result[line_id] = {
            'state': state,
            'supplier_confirmed': supplier_confirmed,
            'current_artigo_id': current_id,
            'current_artigo_nome': row[5],
            'suggestions': exact,
        }
    return result


def get_invoice_linha_artigo_audit(linha_id: int) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT h.id, h.artigo_anterior_id, old.produto,
                   h.artigo_novo_id, new.produto, h.decisao,
                   h.motivo, h.alterado_por, h.alterado_em
            FROM invoice_linha_artigo_audit h
            LEFT JOIN artigos_administrativos old ON old.id = h.artigo_anterior_id
            LEFT JOIN artigos_administrativos new ON new.id = h.artigo_novo_id
            WHERE h.invoice_linha_id = %s
            ORDER BY h.alterado_em ASC, h.id ASC
            """,
            (linha_id,),
        )
        rows = cursor.fetchall()
    return [
        {
            'id': r[0], 'artigo_anterior_id': r[1], 'artigo_anterior_nome': r[2],
            'artigo_novo_id': r[3], 'artigo_novo_nome': r[4], 'decisao': r[5],
            'motivo': r[6], 'alterado_por': r[7], 'alterado_em': r[8],
        }
        for r in rows
    ]


def _audit_invoice_linha_artigo(cursor, invoice_id, linha_id, old_id, new_id,
                                decision, actor, reason):
    cursor.execute(
        """
        INSERT INTO invoice_linha_artigo_audit
            (invoice_linha_id, invoice_id, artigo_anterior_id, artigo_novo_id,
             decisao, motivo, alterado_por)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (linha_id, invoice_id, old_id, new_id, decision, reason, actor),
    )


def link_invoice_linha_artigo(invoice_id: int, linha_id: int, artigo_id: int,
                              actor: str = 'sistema', reason: str = None) -> bool:
    """Explicitly link a line to a catalogue article without rewriting snapshots."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT il.artigo_id, i.supplier_id, a.ativo,
                   o.tipo, o.supplier_id
            FROM invoice_linhas il
            JOIN invoices i ON i.id = il.invoice_id
            JOIN artigos_administrativos a ON a.id = %s
            LEFT JOIN compras_origens o ON o.id = a.origem_id
            WHERE il.id = %s AND il.invoice_id = %s
            FOR UPDATE OF il
            """,
            (artigo_id, linha_id, invoice_id),
        )
        row = cursor.fetchone()
        if not row:
            return False
        old_id, invoice_supplier_id, active, origin_type, origin_supplier_id = row
        if not active:
            raise ValueError('O produto selecionado está inativo.')
        if origin_type == 'fornecedor_externo' and (
            not invoice_supplier_id or origin_supplier_id != invoice_supplier_id
        ):
            raise ValueError('O produto pertence a outro fornecedor canónico.')
        cursor.execute(
            "UPDATE invoice_linhas SET artigo_id = %s, updated_at = NOW() "
            "WHERE id = %s AND invoice_id = %s",
            (artigo_id, linha_id, invoice_id),
        )
        _audit_invoice_linha_artigo(
            cursor, invoice_id, linha_id, old_id, artigo_id, 'ligar', actor, reason
        )
        conn.commit()
    invalidate_prefix('artigos_administrativos')
    return True


def resolve_invoice_linha_artigo(invoice_id: int, linha_id: int,
                                  actor: str = 'sistema', reason: str = None) -> bool:
    """Clear a catalogue link and retain the human unresolved decision."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT artigo_id FROM invoice_linhas WHERE id = %s AND invoice_id = %s FOR UPDATE",
            (linha_id, invoice_id),
        )
        row = cursor.fetchone()
        if not row:
            return False
        cursor.execute(
            "UPDATE invoice_linhas SET artigo_id = NULL, updated_at = NOW() "
            "WHERE id = %s AND invoice_id = %s",
            (linha_id, invoice_id),
        )
        _audit_invoice_linha_artigo(
            cursor, invoice_id, linha_id, row[0], None, 'por_resolver', actor, reason
        )
        conn.commit()
    return True


def create_artigo_from_invoice_linha(invoice_id: int, linha_id: int,
                                     actor: str = 'sistema', reason: str = None) -> int:
    """Create a human-confirmed catalogue article from a line and link it."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT il.artigo_id, il.descricao, il.unidade, i.supplier_id,
                   i.supplier_name, s.name
            FROM invoice_linhas il
            JOIN invoices i ON i.id = il.invoice_id
            LEFT JOIN suppliers s ON s.id = i.supplier_id
            WHERE il.id = %s AND il.invoice_id = %s
            FOR UPDATE OF il
            """,
            (linha_id, invoice_id),
        )
        row = cursor.fetchone()
        if not row:
            return None
        if row[0]:
            raise ValueError('A linha já está ligada a um produto.')
        _, description, unit, supplier_id, supplier_label, legal_supplier = row
        supplier_label = legal_supplier or supplier_label or 'Origem por resolver'
        if supplier_id:
            cursor.execute(
                """
                SELECT id FROM compras_origens
                WHERE ativo = TRUE AND tipo = 'fornecedor_externo'
                  AND supplier_id = %s
                ORDER BY id LIMIT 1
                """,
                (supplier_id,),
            )
            origin = cursor.fetchone()
            if origin:
                origin_id = origin[0]
            else:
                cursor.execute(
                    """
                    INSERT INTO compras_origens
                        (chave, tipo, nome, rotulo_original, supplier_id)
                    VALUES (%s, 'fornecedor_externo', %s, %s, %s)
                    ON CONFLICT (chave) DO UPDATE SET supplier_id = EXCLUDED.supplier_id
                    RETURNING id
                    """,
                    (f'fornecedor:{supplier_id}', supplier_label, supplier_label, supplier_id),
                )
                origin_id = cursor.fetchone()[0]
        else:
            origin_data = classify_compras_origin_label(supplier_label)
            cursor.execute(
                """
                INSERT INTO compras_origens (chave, tipo, nome, rotulo_original)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (chave) DO UPDATE SET nome = EXCLUDED.nome
                RETURNING id
                """,
                (origin_data['key'], origin_data['tipo'], origin_data['nome'],
                 origin_data['rotulo_original']),
            )
            origin_id = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO artigos_administrativos
                (fornecedor, produto, unidade, origem_id, origem_original,
                 human_modified_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
            RETURNING id
            """,
            (supplier_label, description, unit, origin_id, supplier_label),
        )
        artigo_id = cursor.fetchone()[0]
        cursor.execute(
            "UPDATE invoice_linhas SET artigo_id = %s, updated_at = NOW() "
            "WHERE id = %s AND invoice_id = %s",
            (artigo_id, linha_id, invoice_id),
        )
        _audit_invoice_linha_artigo(
            cursor, invoice_id, linha_id, None, artigo_id, 'criar_e_ligar',
            actor, reason,
        )
        conn.commit()
    invalidate_prefix('artigos_administrativos')
    return artigo_id


def get_artigo_comercial_history(artigo_id: int) -> dict:
    """Return purchase snapshots from confirmed, commercially eligible invoices."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT il.invoice_id, i.issue_date, i.invoice_number,
                   i.supplier_name, il.descricao, il.quantidade, il.unidade,
                   il.preco_unitario, i.status, i.document_type
            FROM invoice_linhas il
            JOIN invoices i ON i.id = il.invoice_id
            WHERE il.artigo_id = %s
              AND i.status IN ('scheduled', 'paid')
              AND i.document_type IN ('fatura', 'nota_debito')
              AND (
                    i.cfo_confirmed_date IS NOT NULL
                 OR i.paid_date IS NOT NULL
                 OR EXISTS (
                     SELECT 1 FROM invoice_payments ip
                     WHERE ip.invoice_id = i.id AND ip.confirmed_date IS NOT NULL
                 )
              )
            ORDER BY i.issue_date DESC NULLS LAST, i.id DESC, il.id DESC
            """,
            (artigo_id,),
        )
        rows = cursor.fetchall()
    history = [
        {
            'invoice_id': r[0], 'issue_date': r[1], 'invoice_number': r[2],
            'supplier_name': r[3], 'descricao': r[4], 'quantidade': float(r[5]),
            'unidade': r[6], 'preco_unitario': float(r[7]) if r[7] is not None else None,
            'status': r[8], 'document_type': r[9],
        }
        for r in rows
    ]
    last = history[0] if history else None
    return {
        'artigo_id': artigo_id,
        'ultima_compra': last['issue_date'] if last else None,
        'ultimo_custo': last['preco_unitario'] if last else None,
        'historico': history,
    }


def add_artigo_administrativo(produto: str, supplier_id: int,
                              marca: str = None, unidade: str = None,
                              actor: str = 'sistema',
                              categoria_artigo: str = 'Por classificar') -> bool:
    from db.compras_article_categories import validate_article_category

    categoria_artigo = validate_article_category(categoria_artigo)
    if not isinstance(supplier_id, int) or isinstance(supplier_id, bool) or supplier_id <= 0:
        raise ValueError('Selecione um fornecedor válido.')
    produto = str(produto or '').strip()
    if not produto:
        raise ValueError('Indique o nome do artigo.')

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM suppliers WHERE id = %s FOR KEY SHARE",
            (supplier_id,),
        )
        supplier_row = cursor.fetchone()
        supplier_name = str(supplier_row[0] or '').strip() if supplier_row else ''
        if not supplier_name:
            raise ValueError('O fornecedor selecionado não existe.')

        try:
            cursor.execute(
                """
                INSERT INTO artigos_administrativos
                    (fornecedor, produto, marca, unidade,
                     fornecedor_oficial_id, categoria_artigo,
                     human_modified_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                """,
                (supplier_name, produto, marca or None, unidade or None,
                 supplier_id, categoria_artigo),
            )
            conn.commit()
            success = True
        except psycopg2.Error:
            conn.rollback()
            success = False
    invalidate_prefix('artigos_administrativos')
    return success


def _update_artigo_administrativo_in_transaction(
        cursor, artigo_id: int, fornecedor: str, produto: str,
        marca: str = None, unidade: str = None, origem_id: int = None,
        actor: str = 'sistema', categoria_artigo: str | None = None):
    from db.compras_article_categories import (
        UNCATEGORIZED,
        validate_article_category,
    )

    if categoria_artigo is not None:
        categoria_artigo = validate_article_category(categoria_artigo)
    cursor.execute(
        """
        SELECT a.fornecedor, a.produto, a.marca, a.unidade,
               a.origem_id, a.origem_original, o.tipo, o.supplier_id,
                a.fornecedor_oficial_id, official_s.name,
                a.categoria_artigo
        FROM artigos_administrativos a
        LEFT JOIN compras_origens o ON o.id = a.origem_id
        LEFT JOIN suppliers official_s
               ON official_s.id = a.fornecedor_oficial_id
        WHERE a.id = %s
        FOR UPDATE OF a
        """,
        (artigo_id,),
    )
    current = cursor.fetchone()
    if not current:
        return None
    current_category = current[10] if len(current) > 10 else UNCATEGORIZED
    next_category = (
        current_category
        if categoria_artigo is None
        else categoria_artigo
    )
    next_origin_id = current[4] if origem_id is None else origem_id
    next_origin_type = current[6]
    next_official_supplier_id = current[8]
    next_official_supplier_name = current[9]
    if origem_id is not None:
        cursor.execute(
            """
            SELECT id, tipo, supplier_id
            FROM compras_origens WHERE id = %s AND ativo = TRUE
            """,
            (origem_id,),
        )
        selected_origin = cursor.fetchone()
        if not selected_origin:
            raise ValueError('A origem selecionada não existe ou está inativa.')
        next_origin_type = selected_origin[1]
        if next_origin_type == 'fornecedor_externo':
            if (
                next_official_supplier_id is not None
                and next_official_supplier_id != selected_origin[2]
            ):
                raise ValueError(
                    'A origem externa e o fornecedor oficial são diferentes. '
                    'Resolva o fornecedor oficial antes de alterar a origem.'
                )
            next_official_supplier_id = selected_origin[2]
            cursor.execute(
                "SELECT name FROM suppliers WHERE id = %s",
                (next_official_supplier_id,),
            )
            supplier_row = cursor.fetchone()
            next_official_supplier_name = supplier_row[0] if supplier_row else None

    unchanged = (
        current[0] == fornecedor
        and current[1] == produto
        and (current[2] or None) == (marca or None)
        and (current[3] or None) == (unidade or None)
        and current[4] == next_origin_id
        and current[8] == next_official_supplier_id
        and current_category == next_category
    )
    if unchanged:
        return {
            'found': True,
            'changed': False,
            'origin_type': next_origin_type,
        }

    cursor.execute(
        """
        UPDATE artigos_administrativos
           SET fornecedor = %s, produto = %s, marca = %s, unidade = %s,
               origem_id = %s, fornecedor_oficial_id = %s,
               categoria_artigo = %s,
               origem_original = COALESCE(origem_original, %s),
               human_modified_at = CASE
                   WHEN fornecedor IS DISTINCT FROM %s
                     OR produto IS DISTINCT FROM %s
                     OR marca IS DISTINCT FROM %s
                     OR unidade IS DISTINCT FROM %s
                     OR origem_id IS DISTINCT FROM %s
                     OR fornecedor_oficial_id IS DISTINCT FROM %s
                   THEN NOW() ELSE human_modified_at END,
               updated_at = NOW()
         WHERE id = %s
        """,
        (fornecedor, produto, marca or None, unidade or None, next_origin_id,
         next_official_supplier_id, next_category, fornecedor,
         fornecedor, produto, marca or None, unidade or None, next_origin_id,
         next_official_supplier_id, artigo_id),
    )
    if current_category != next_category:
        cursor.execute(
            """
            INSERT INTO artigos_administrativos_categoria_audit
                (artigo_id, categoria_anterior, categoria_nova, actor)
            VALUES (%s, %s, %s, %s)
            """,
            (artigo_id, current_category, next_category, actor),
        )
    if next_origin_id is not None and current[4] != next_origin_id:
        cursor.execute(
            """
            INSERT INTO artigos_administrativos_origem_audit
                (artigo_id, origem_anterior_id, origem_nova_id,
                 rotulo_original, actor, reason)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                artigo_id, current[4], next_origin_id,
                current[5] or current[0], actor, 'edição do catálogo',
            ),
        )
    if current[8] != next_official_supplier_id:
        cursor.execute(
            """
            INSERT INTO artigos_administrativos_fornecedor_audit
                (artigo_id, fornecedor_anterior_id, fornecedor_anterior_nome,
                 fornecedor_novo_id, fornecedor_novo_nome, actor, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                artigo_id, current[8], current[9],
                next_official_supplier_id, next_official_supplier_name,
                actor, 'associação derivada de origem externa confirmada',
            ),
        )
    return {
        'found': True,
        'changed': True,
        'origin_type': next_origin_type,
    }


def update_artigo_administrativo(artigo_id: int, fornecedor: str, produto: str,
                                 marca: str = None, unidade: str = None,
                                 origem_id: int = None, actor: str = 'sistema',
                                 categoria_artigo: str | None = None):
    with db_connection() as conn:
        result = _update_artigo_administrativo_in_transaction(
            conn.cursor(), artigo_id, fornecedor, produto, marca, unidade,
            origem_id, actor, categoria_artigo,
        )
        if result and result['changed']:
            conn.commit()
    if result and result['changed']:
        invalidate_prefix('artigos_administrativos')
    return result


def update_artigos_administrativos_bulk(changes: list[dict],
                                        actor: str = 'sistema') -> dict:
    """Validate and save multiple catalogue rows in one transaction."""
    if not isinstance(changes, list) or not changes:
        raise ValueError('Selecione pelo menos um artigo para guardar.')

    article_ids = []
    for change in changes:
        article_id = change.get('artigo_id') if isinstance(change, dict) else None
        if not isinstance(article_id, int) or article_id <= 0:
            raise ValueError('Um dos artigos selecionados é inválido.')
        article_ids.append(article_id)
    if len(article_ids) != len(set(article_ids)):
        raise ValueError('A lista contém artigos repetidos.')

    ordered_changes = sorted(changes, key=lambda change: change['artigo_id'])
    with db_connection() as conn:
        cursor = conn.cursor()
        results = []
        for change in ordered_changes:
            try:
                result = _update_artigo_administrativo_in_transaction(
                    cursor,
                    change['artigo_id'],
                    change['fornecedor'],
                    change['produto'],
                    marca=change.get('marca'),
                    unidade=change.get('unidade'),
                    origem_id=change.get('origem_id'),
                    actor=actor,
                    categoria_artigo=change.get('categoria_artigo'),
                )
            except ValueError as exc:
                raise ValueError(
                    f'Artigo #{change["artigo_id"]}: {exc}'
                ) from exc
            if result is None:
                raise ValueError(
                    f'Artigo #{change["artigo_id"]} não encontrado. '
                    'Nenhuma alteração foi guardada.'
                )
            results.append(result)
        changed_count = sum(result['changed'] for result in results)
        if changed_count:
            conn.commit()

    if changed_count:
        invalidate_prefix('artigos_administrativos')
    return {
        'updated': changed_count,
        'unchanged': len(results) - changed_count,
        'unresolved': sum(
            result['origin_type'] in (None, 'por_resolver')
            for result in results
        ),
    }


def confirm_artigo_fornecedor(artigo_id: int, supplier_id: int,
                              actor: str = 'sistema') -> dict | None:
    """Link one article to an existing canonical supplier and audit the change."""
    if not isinstance(artigo_id, int) or artigo_id <= 0:
        raise ValueError('Artigo inválido.')
    if not isinstance(supplier_id, int) or supplier_id <= 0:
        raise ValueError('Fornecedor inválido.')

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT a.origem_id, a.origem_original, a.fornecedor,
                   o.tipo, o.supplier_id, a.fornecedor_oficial_id,
                   official_s.name
            FROM artigos_administrativos a
            LEFT JOIN compras_origens o ON o.id = a.origem_id
            LEFT JOIN suppliers official_s
                   ON official_s.id = a.fornecedor_oficial_id
            WHERE a.id = %s
            FOR UPDATE OF a
            """,
            (artigo_id,),
        )
        article = cursor.fetchone()
        if not article:
            return None
        already_linked_to_supplier = (
            article[3] == 'fornecedor_externo'
            and article[4] == supplier_id
        )
        if (
            article[3] not in (None, 'por_resolver', 'fornecedor_externo')
            or (
                article[3] == 'fornecedor_externo'
                and not already_linked_to_supplier
            )
        ):
            raise ValueError(
                'Este artigo já tem outra origem confirmada. '
                'Use o seletor de origem para a alterar.'
            )
        if (
            article[5] is not None
            and article[5] != supplier_id
        ):
            raise ValueError(
                'O fornecedor oficial selecionado é diferente. '
                'Corrija a associação antes de confirmar esta origem.'
            )

        cursor.execute(
            "SELECT id, name FROM suppliers WHERE id = %s",
            (supplier_id,),
        )
        supplier = cursor.fetchone()
        if not supplier or not supplier[1]:
            raise ValueError('O fornecedor selecionado não existe.')
        supplier_name = supplier[1].strip()
        if not supplier_name:
            raise ValueError('O fornecedor selecionado não tem nome válido.')
        origin_key = f'fornecedor:{supplier_id}'

        cursor.execute(
            """
            SELECT id, ativo
            FROM compras_origens
            WHERE tipo = 'fornecedor_externo' AND supplier_id = %s
            ORDER BY ativo DESC, id
            LIMIT 1
            FOR UPDATE
            """,
            (supplier_id,),
        )
        existing_origin = cursor.fetchone()
        if existing_origin:
            if not existing_origin[1]:
                raise ValueError(
                    'A origem deste fornecedor está inativa; confirme-a antes de a associar.'
                )
            origin_id = existing_origin[0]
        else:
            cursor.execute(
                """
                INSERT INTO compras_origens
                    (chave, tipo, nome, rotulo_original, supplier_id)
                VALUES (%s, 'fornecedor_externo', %s, %s, %s)
                ON CONFLICT (chave) DO NOTHING
                RETURNING id
                """,
                (origin_key, supplier_name, supplier_name, supplier_id),
            )
            created_origin = cursor.fetchone()
            if created_origin:
                origin_id = created_origin[0]
            else:
                cursor.execute(
                    """
                    SELECT id, tipo, supplier_id, ativo
                    FROM compras_origens
                    WHERE chave = %s
                    FOR UPDATE
                    """,
                    (origin_key,),
                )
                raced_origin = cursor.fetchone()
                if (
                    not raced_origin
                    or raced_origin[1] != 'fornecedor_externo'
                    or raced_origin[2] != supplier_id
                    or not raced_origin[3]
                ):
                    raise ValueError(
                        'Já existe uma origem incompatível ou inativa para este fornecedor.'
                    )
                origin_id = raced_origin[0]

        origin_changed = article[0] != origin_id
        supplier_changed = article[5] != supplier_id
        if not origin_changed and not supplier_changed:
            return {
                'changed': False,
                'supplier_name': supplier_name,
            }

        cursor.execute(
            """
            UPDATE artigos_administrativos
            SET origem_id = %s, fornecedor_oficial_id = %s,
                human_modified_at = NOW(), updated_at = NOW()
            WHERE id = %s
            """,
            (origin_id, supplier_id, artigo_id),
        )
        if origin_changed:
            cursor.execute(
                """
                INSERT INTO artigos_administrativos_origem_audit
                    (artigo_id, origem_anterior_id, origem_nova_id,
                     rotulo_original, actor, reason)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    artigo_id, article[0], origin_id,
                    article[1] or article[2], actor,
                    'confirmação de fornecedor no catálogo',
                ),
            )
        if supplier_changed:
            cursor.execute(
                """
                INSERT INTO artigos_administrativos_fornecedor_audit
                    (artigo_id, fornecedor_anterior_id, fornecedor_anterior_nome,
                     fornecedor_novo_id, fornecedor_novo_nome, actor, reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    artigo_id, article[5], article[6], supplier_id, supplier_name,
                    actor, 'confirmação da origem externa no catálogo',
                ),
            )
        conn.commit()

    invalidate_prefix('artigos_administrativos')
    return {
        'changed': True,
        'supplier_name': supplier_name,
    }


def set_artigo_fornecedor_oficial(
    artigo_id: int,
    supplier_id: int | None,
    actor: str = 'sistema',
    reason: str = 'edição do fornecedor oficial no catálogo',
) -> dict | None:
    """Set an article's official supplier without changing its operational origin."""
    if not isinstance(artigo_id, int) or artigo_id <= 0:
        raise ValueError('Artigo inválido.')
    if supplier_id is not None and (
        not isinstance(supplier_id, int) or supplier_id <= 0
    ):
        raise ValueError('Fornecedor inválido.')

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT a.fornecedor_oficial_id, official_s.name,
                   o.tipo, o.supplier_id
            FROM artigos_administrativos a
            LEFT JOIN suppliers official_s
                   ON official_s.id = a.fornecedor_oficial_id
            LEFT JOIN compras_origens o ON o.id = a.origem_id
            WHERE a.id = %s
            FOR UPDATE OF a
            """,
            (artigo_id,),
        )
        current = cursor.fetchone()
        if not current:
            return None

        origin_type, origin_supplier_id = current[2], current[3]
        if origin_type == 'fornecedor_externo':
            if supplier_id is None:
                raise ValueError(
                    'Um artigo com origem externa confirmada tem de manter '
                    'o mesmo fornecedor oficial.'
                )
            if supplier_id != origin_supplier_id:
                raise ValueError(
                    'O fornecedor oficial tem de corresponder à origem externa.'
                )

        next_supplier_name = None
        if supplier_id is not None:
            cursor.execute(
                "SELECT name FROM suppliers WHERE id = %s",
                (supplier_id,),
            )
            supplier = cursor.fetchone()
            if not supplier or not str(supplier[0] or '').strip():
                raise ValueError('O fornecedor selecionado não existe.')
            next_supplier_name = str(supplier[0]).strip()

        if current[0] == supplier_id:
            return {
                'found': True,
                'changed': False,
                'supplier_name': next_supplier_name,
            }

        cursor.execute(
            """
            UPDATE artigos_administrativos
               SET fornecedor_oficial_id = %s,
                   human_modified_at = NOW(), updated_at = NOW()
             WHERE id = %s
            """,
            (supplier_id, artigo_id),
        )
        cursor.execute(
            """
            INSERT INTO artigos_administrativos_fornecedor_audit
                (artigo_id, fornecedor_anterior_id, fornecedor_anterior_nome,
                 fornecedor_novo_id, fornecedor_novo_nome, actor, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                artigo_id, current[0], current[1], supplier_id,
                next_supplier_name, actor, str(reason or '').strip()[:1000],
            ),
        )
        conn.commit()

    invalidate_prefix('artigos_administrativos')
    return {
        'found': True,
        'changed': True,
        'supplier_name': next_supplier_name,
    }


def review_artigo_origem(
    artigo_id: int,
    supplier_id: int | None,
    actor: str = 'sistema',
) -> dict | None:
    """Record a human decision about a legacy non-supplier origin.

    Confirming a supplier updates only the independent official-supplier link.
    Keeping the article pending preserves any link already on the article.
    Neither action changes the legacy origin label or historical documents.
    """
    if not isinstance(artigo_id, int) or isinstance(artigo_id, bool) or artigo_id <= 0:
        raise ValueError('Artigo inválido.')
    if supplier_id is not None and (
        not isinstance(supplier_id, int)
        or isinstance(supplier_id, bool)
        or supplier_id <= 0
    ):
        raise ValueError('Fornecedor inválido.')

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT a.origem_revisao_estado, a.origem_id, a.origem_original,
                   a.fornecedor, a.fornecedor_oficial_id, official_s.name,
                   o.tipo
            FROM artigos_administrativos a
            LEFT JOIN compras_origens o ON o.id = a.origem_id
            LEFT JOIN suppliers official_s
                   ON official_s.id = a.fornecedor_oficial_id
            WHERE a.id = %s
            FOR UPDATE OF a
            """,
            (artigo_id,),
        )
        current = cursor.fetchone()
        if not current:
            return None

        review_state, origin_id, original_label, legacy_label = current[:4]
        previous_supplier_id, previous_supplier_name, origin_type = current[4:]
        if origin_type not in ('centro_interno', 'categoria_operacional'):
            raise ValueError('Este artigo não tem uma origem herdada para rever.')
        if review_state not in ('por_rever', 'revisto'):
            raise ValueError('Este artigo não está marcado para revisão da origem.')

        if supplier_id is None:
            next_review_state = 'por_rever'
            next_supplier_id = previous_supplier_id
            next_supplier_name = previous_supplier_name
            reason = 'gestor manteve a origem herdada por rever'
        else:
            cursor.execute(
                "SELECT name FROM suppliers WHERE id = %s",
                (supplier_id,),
            )
            supplier = cursor.fetchone()
            if not supplier or not str(supplier[0] or '').strip():
                raise ValueError('O fornecedor selecionado não existe.')
            next_review_state = 'revisto'
            next_supplier_id = supplier_id
            next_supplier_name = str(supplier[0]).strip()
            reason = 'gestor confirmou o fornecedor na revisão da origem herdada'

        state_changed = review_state != next_review_state
        supplier_changed = previous_supplier_id != next_supplier_id
        changed = state_changed or supplier_changed
        if changed:
            cursor.execute(
                """
                UPDATE artigos_administrativos
                   SET origem_revisao_estado = %s,
                       fornecedor_oficial_id = %s,
                       human_modified_at = NOW(), updated_at = NOW()
                 WHERE id = %s
                """,
                (next_review_state, next_supplier_id, artigo_id),
            )

        if supplier_changed:
            cursor.execute(
                """
                INSERT INTO artigos_administrativos_fornecedor_audit
                    (artigo_id, fornecedor_anterior_id, fornecedor_anterior_nome,
                     fornecedor_novo_id, fornecedor_novo_nome, actor, reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    artigo_id, previous_supplier_id, previous_supplier_name,
                    next_supplier_id, next_supplier_name, actor,
                    'confirmação de fornecedor na revisão da origem herdada',
                ),
            )

        cursor.execute(
            """
            INSERT INTO artigos_administrativos_origem_revisao_audit
                (artigo_id, estado_anterior, estado_novo, origem_id,
                 origem_tipo, rotulo_original,
                 fornecedor_anterior_id, fornecedor_anterior_nome,
                 fornecedor_novo_id, fornecedor_novo_nome, actor, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                artigo_id, review_state, next_review_state, origin_id,
                origin_type, original_label or legacy_label,
                previous_supplier_id, previous_supplier_name,
                next_supplier_id, next_supplier_name,
                str(actor or 'sistema')[:255], reason,
            ),
        )
        conn.commit()

    invalidate_prefix('artigos_administrativos')
    return {
        'found': True,
        'changed': changed,
        'estado': next_review_state,
        'supplier_id': next_supplier_id,
        'supplier_name': next_supplier_name,
    }


def get_artigo_origem_revisao_history(artigo_id: int) -> list:
    """Return the immutable decision history for a legacy-origin review."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT h.id, h.estado_anterior, h.estado_novo, h.origem_tipo,
                   h.rotulo_original,
                   h.fornecedor_anterior_id, h.fornecedor_anterior_nome,
                   h.fornecedor_novo_id, h.fornecedor_novo_nome,
                   h.actor, h.reason, h.created_at
            FROM artigos_administrativos_origem_revisao_audit h
            WHERE h.artigo_id = %s
            ORDER BY h.created_at DESC, h.id DESC
            """,
            (artigo_id,),
        )
        rows = cursor.fetchall()
    return [
        {
            'id': row[0],
            'estado_anterior': row[1],
            'estado_novo': row[2],
            'origem_tipo': row[3],
            'rotulo_original': row[4],
            'fornecedor_anterior_id': row[5],
            'fornecedor_anterior_nome': row[6],
            'fornecedor_novo_id': row[7],
            'fornecedor_novo_nome': row[8],
            'actor': row[9],
            'reason': row[10],
            'created_at': row[11],
        }
        for row in rows
    ]


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
