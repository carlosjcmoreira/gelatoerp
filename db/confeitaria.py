import hashlib
import re
from datetime import date, datetime
from uuid import uuid4

from psycopg2.extras import RealDictCursor, execute_values

from db.connection import db_connection


_COUNT_TOKEN_RE = re.compile(r'^[0-9a-f]{32}$')
_MAX_COUNT_QUANTITY = 2_147_483_647


def _count_snapshot_token(rows, count_date, store_id):
    count_rows = ','.join(
        f"{int(row['produto_confeitaria_id'])}:{int(row['id'])}"
        for row in sorted(rows, key=lambda row: int(row['produto_confeitaria_id']))
    )
    payload = f'{count_date.isoformat()}:{store_id}:{count_rows}'
    return hashlib.md5(payload.encode('utf-8')).hexdigest()


def _latest_confeitaria_count_rows(cursor, count_date, store_id):
    cursor.execute("""
        SELECT DISTINCT ON (cs.produto_confeitaria_id)
               cs.id, cs.produto_confeitaria_id, cs.quantidade
        FROM contagem_stock cs
        WHERE cs.tipo='confeitaria' AND cs.origem='contagem'
          AND cs.data=%s AND cs.store_id=%s
          AND cs.produto_confeitaria_id IS NOT NULL
        ORDER BY cs.produto_confeitaria_id, cs.id DESC
    """, (count_date, store_id))
    return cursor.fetchall()


def _validate_count_date(count_date):
    if not isinstance(count_date, date) or isinstance(count_date, datetime):
        raise ValueError('Data da contagem inválida.')


def _validate_store_id(store_id):
    if isinstance(store_id, bool):
        raise ValueError('Loja inválida.') from None
    if isinstance(store_id, int):
        parsed = store_id
    elif isinstance(store_id, str) and store_id.strip().isdigit():
        parsed = int(store_id.strip())
    else:
        raise ValueError('Loja inválida.') from None
    if parsed <= 0:
        raise ValueError('Loja inválida.') from None
    return parsed


def _validate_submitter(submitted_by):
    if not isinstance(submitted_by, str) or not submitted_by.strip():
        raise ValueError('Não foi possível identificar quem submeteu a contagem.')
    submitted_by = submitted_by.strip()
    if len(submitted_by) > 100:
        raise ValueError('O nome do utilizador é demasiado longo.')
    return submitted_by


def get_confeitaria_store_count_grid(count_date, store_id):
    """Return one store's active Confeitaria products and latest physical counts."""
    _validate_count_date(count_date)
    store_id = _validate_store_id(store_id)

    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f'confeitaria-count:{count_date.isoformat()}',),
        )
        cursor.execute("""
            SELECT id, name
            FROM stores
            WHERE id=%s AND supports_vendas=TRUE AND is_active=TRUE
        """, (store_id,))
        store = cursor.fetchone()
        if not store:
            raise ValueError('Loja inválida ou sem acesso ao módulo Vendas.')

        cursor.execute("""
            SELECT id, nome
            FROM produtos_confeitaria
            WHERE ativo=TRUE
            ORDER BY nome
        """)
        products = cursor.fetchall()
        count_rows = _latest_confeitaria_count_rows(
            cursor, count_date, store_id
        )
        counts = {
            int(row['produto_confeitaria_id']): int(row['quantidade'])
            for row in count_rows
        }

    rows = []
    completed = 0
    for product in products:
        quantity = counts.get(int(product['id']))
        completed += quantity is not None
        rows.append({**product, 'count': quantity})

    total = len(rows)
    return {
        'date': count_date,
        'store': store,
        'stores': [store],
        'products': rows,
        'completed': completed,
        'total': total,
        'complete': total > 0 and completed == total,
        'snapshot_token': _count_snapshot_token(
            count_rows, count_date, store_id
        ),
    }


def save_confeitaria_store_counts(
    count_date, store_id, values, snapshot_token, submitted_by=None
):
    """Append a complete, audited Confeitaria count snapshot for one store."""
    _validate_count_date(count_date)
    store_id = _validate_store_id(store_id)
    submitted_by = _validate_submitter(submitted_by)
    snapshot_token = str(snapshot_token or '')
    if not _COUNT_TOKEN_RE.fullmatch(snapshot_token):
        raise ValueError('A versão da grelha é inválida.') from None

    normalized = {}
    try:
        value_rows = list(values)
    except TypeError:
        raise ValueError('A grelha recebida é inválida.') from None
    for item in value_rows:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError('A grelha recebida é inválida.')
        product_id, quantity = item
        if (
            isinstance(product_id, bool) or not isinstance(product_id, int)
            or product_id <= 0
        ):
            raise ValueError('Produto inválido.')
        if product_id in normalized:
            raise ValueError('A grelha contém produtos repetidos.')
        if (
            isinstance(quantity, bool) or not isinstance(quantity, int)
            or quantity < 0 or quantity > _MAX_COUNT_QUANTITY
        ):
            raise ValueError(
                'As contagens devem ser números inteiros não negativos.'
            )
        normalized[product_id] = quantity

    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f'confeitaria-count:{count_date.isoformat()}',),
        )
        cursor.execute("""
            SELECT id, name
            FROM stores
            WHERE id=%s AND supports_vendas=TRUE AND is_active=TRUE
            FOR SHARE
        """, (store_id,))
        store = cursor.fetchone()
        if not store:
            raise ValueError('Loja inválida ou sem acesso ao módulo Vendas.')

        latest_rows = _latest_confeitaria_count_rows(
            cursor, count_date, store_id
        )
        if _count_snapshot_token(latest_rows, count_date, store_id) != snapshot_token:
            raise ValueError(
                'Esta grelha foi alterada por outro utilizador. '
                'Atualize a página antes de guardar.'
            )

        cursor.execute("""
            SELECT id, nome
            FROM produtos_confeitaria
            WHERE ativo=TRUE
            ORDER BY nome
            FOR SHARE
        """)
        products = cursor.fetchall()
        expected = {int(product['id']) for product in products}
        if not expected or set(normalized) != expected:
            raise ValueError(
                'Preencha todas as contagens da grelha antes de guardar.'
            )
        product_names = {
            int(product['id']): product['nome'] for product in products
        }
        submission_id = str(uuid4())
        rows = [
            (
                count_date, store['name'], product_names[product_id], quantity,
                'confeitaria', 'contagem', product_id, store_id,
                submission_id, submitted_by,
            )
            for product_id, quantity in normalized.items()
        ]
        execute_values(cursor, """
            INSERT INTO contagem_stock
                (data, loja, produto, quantidade, tipo, origem,
                 produto_confeitaria_id, store_id, submission_id, submitted_by)
            VALUES %s
        """, rows)
        conn.commit()
    return len(rows)


def get_confeitaria_count_submission_history(count_date, store_id):
    """Return audited count batches for one stable store and date."""
    _validate_count_date(count_date)
    store_id = _validate_store_id(store_id)
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT submission_id, data, store_id, loja AS store_name,
                   submitted_by, MIN(submitted_at) AS submitted_at,
                   COUNT(*) AS product_count
            FROM contagem_stock
            WHERE tipo='confeitaria' AND origem='contagem'
              AND submission_id IS NOT NULL
              AND store_id=%s AND data=%s
            GROUP BY submission_id, data, store_id, loja, submitted_by
            ORDER BY MIN(submitted_at) DESC, submission_id DESC
        """, (store_id, count_date))
        return cursor.fetchall()


def get_confeitaria_stock_count_overview(active_store_ids):
    """Read latest store counts and full history without matching by labels."""
    if not isinstance(active_store_ids, (list, tuple, set)):
        raise ValueError('Lista de lojas inválida.')
    store_ids = sorted({
        _validate_store_id(store_id) for store_id in active_store_ids
    })

    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        latest_counts = []
        if store_ids:
            cursor.execute("""
                SELECT DISTINCT ON (
                           cs.produto_confeitaria_id, cs.store_id
                       )
                       cs.id, cs.data, cs.store_id,
                       cs.produto_confeitaria_id, cs.quantidade
                FROM contagem_stock cs
                JOIN stores s
                  ON s.id=cs.store_id
                 AND s.supports_vendas=TRUE
                 AND s.is_active=TRUE
                WHERE cs.tipo='confeitaria'
                  AND cs.origem='contagem'
                  AND cs.store_id=ANY(%s)
                  AND cs.produto_confeitaria_id IS NOT NULL
                ORDER BY cs.produto_confeitaria_id, cs.store_id,
                         cs.data DESC, cs.id DESC
            """, (store_ids,))
            latest_counts = cursor.fetchall()

        cursor.execute("""
            SELECT cs.id, cs.data, cs.loja AS loja_registada,
                   cs.store_id, s.name AS loja_atual,
                   s.is_active AS loja_ativa,
                   cs.origem AS origem,
                   cs.produto AS produto_registado,
                   cs.produto_confeitaria_id,
                   p.nome AS produto_atual, p.ativo AS produto_ativo,
                   cs.quantidade, cs.submitted_by, cs.submitted_at
            FROM contagem_stock cs
            LEFT JOIN stores s ON s.id=cs.store_id
            LEFT JOIN produtos_confeitaria p
              ON p.id=cs.produto_confeitaria_id
            WHERE cs.tipo='confeitaria'
            ORDER BY cs.data DESC, cs.id DESC
        """)
        history = cursor.fetchall()

    return {
        'latest_counts': latest_counts,
        'history': history,
    }