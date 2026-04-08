import calendar
import logging
from datetime import date
from dateutil.relativedelta import relativedelta
from psycopg2.extras import RealDictCursor
from db.core import db_connection

logger = logging.getLogger(__name__)

__all__ = [
    'run_migrations_avencas',
    'get_avencas',
    'get_avenca',
    'create_avenca',
    'update_avenca',
    'delete_avenca',
    'next_due_date',
]

_PERIOD_MONTHS = {'mensal': 1, 'trimestral': 3, 'semestral': 6, 'anual': 12}


def run_migrations_avencas():
    """Create the avencas table if it doesn't exist (advisory lock 202614)."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(202614)")
        if not cursor.fetchone()[0]:
            return
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS avencas (
                    id                 SERIAL PRIMARY KEY,
                    nome               VARCHAR(200)   NOT NULL,
                    descricao          TEXT,
                    valor              NUMERIC(12,2)  NOT NULL CHECK (valor > 0),
                    periodicidade      VARCHAR(20)    NOT NULL
                                       CHECK (periodicidade IN ('mensal','trimestral','semestral','anual')),
                    dia_vencimento     SMALLINT       NOT NULL DEFAULT 1 CHECK (dia_vencimento BETWEEN 1 AND 28),
                    data_inicio        DATE           NOT NULL DEFAULT CURRENT_DATE,
                    data_fim           DATE,
                    centro_custo_id    INTEGER        REFERENCES cost_centers(id) ON DELETE SET NULL,
                    categoria_custo_id INTEGER        REFERENCES cost_categories(id) ON DELETE SET NULL,
                    ativo              BOOLEAN        NOT NULL DEFAULT TRUE,
                    observacoes        TEXT,
                    created_at         TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
                    updated_at         TIMESTAMPTZ    NOT NULL DEFAULT NOW()
                )
            """)
            conn.commit()
            logger.info("run_migrations_avencas: table ready")
        finally:
            cursor.execute("SELECT pg_advisory_unlock(202614)")
            conn.commit()


def get_avencas(ativo_only: bool = False):
    """Return all avenças ordered by name, with cost center and category names."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        where = "WHERE a.ativo = TRUE" if ativo_only else ""
        cursor.execute(f"""
            SELECT a.*,
                   cc.name  AS centro_custo_nome,
                   cat.name AS categoria_custo_nome
            FROM avencas a
            LEFT JOIN cost_centers     cc  ON cc.id  = a.centro_custo_id
            LEFT JOIN cost_categories  cat ON cat.id = a.categoria_custo_id
            {where}
            ORDER BY a.nome
        """)
        return cursor.fetchall()


def get_avenca(avenca_id: int):
    """Return a single avença by id, or None if not found."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT a.*,
                   cc.name  AS centro_custo_nome,
                   cat.name AS categoria_custo_nome
            FROM avencas a
            LEFT JOIN cost_centers     cc  ON cc.id  = a.centro_custo_id
            LEFT JOIN cost_categories  cat ON cat.id = a.categoria_custo_id
            WHERE a.id = %s
        """, (avenca_id,))
        return cursor.fetchone()


def create_avenca(nome, valor, periodicidade, dia_vencimento, data_inicio,
                  data_fim=None, centro_custo_id=None, categoria_custo_id=None,
                  descricao=None, observacoes=None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO avencas
                (nome, valor, periodicidade, dia_vencimento, data_inicio, data_fim,
                 centro_custo_id, categoria_custo_id, descricao, observacoes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (nome, valor, periodicidade, dia_vencimento, data_inicio, data_fim,
              centro_custo_id, categoria_custo_id, descricao, observacoes))
        new_id = cursor.fetchone()[0]
        conn.commit()
        return new_id


def update_avenca(avenca_id: int, **kwargs):
    allowed = {
        'nome', 'valor', 'periodicidade', 'dia_vencimento', 'data_inicio',
        'data_fim', 'centro_custo_id', 'categoria_custo_id',
        'descricao', 'observacoes', 'ativo',
    }
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ', '.join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [avenca_id]
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE avencas SET {set_clause}, updated_at = NOW() WHERE id = %s",
            values,
        )
        conn.commit()


def delete_avenca(avenca_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM avencas WHERE id = %s", (avenca_id,))
        conn.commit()


def next_due_date(avenca, from_date: date):
    """Return the next due date for avenca on or after from_date.

    Returns None when data_fim is set and has already passed.

    Algorithm:
      1. The period step is 1/3/6/12 months depending on periodicidade.
      2. Starting from data_inicio, due dates occur every step months
         on dia_vencimento (clamped to month's last day).
      3. Find the smallest N >= 0 such that due_date(N) >= from_date.
      4. Return None if due_date > data_fim.
    """
    step = _PERIOD_MONTHS.get(avenca['periodicidade'], 1)
    start = avenca['data_inicio']
    dia = int(avenca['dia_vencimento'])
    data_fim = avenca['data_fim']

    if from_date < start:
        from_date = start

    def make_candidate(n_steps: int) -> date:
        d = start + relativedelta(months=n_steps * step)
        max_day = calendar.monthrange(d.year, d.month)[1]
        return date(d.year, d.month, min(dia, max_day))

    diff_months = (from_date.year - start.year) * 12 + (from_date.month - start.month)
    n = max(0, diff_months // step)

    cand = make_candidate(n)
    if cand < from_date:
        cand = make_candidate(n + 1)

    if data_fim and cand > data_fim:
        return None

    return cand
