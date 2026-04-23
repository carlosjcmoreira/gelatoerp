import logging
from datetime import date
from db.connection import db_connection, get_connection, release_connection

logger = logging.getLogger(__name__)

DIAS_SEMANA = ['Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado', 'Domingo']
FREQUENCIAS = {'diaria': 'Diária', 'semanal': 'Semanal', 'mensal': 'Mensal'}
TIPOS = {'abertura': 'Abertura', 'fecho': 'Fecho'}
EQUIPAS = {'Produção': 'Produção', 'Vendas': 'Vendas', 'Logística': 'Logística', 'Compras': 'Compras'}

_TAREFA_SELECT = """
    SELECT t.id, t.nome, t.tipo, t.frequencia, t.dia_semana, t.dia_mes,
           t.utilizador_id, t.ativo, t.created_at,
           t.loja_id, t.equipa,
           u.username AS utilizador_nome,
           s.name     AS loja_nome
    FROM tarefas t
    LEFT JOIN users   u ON u.id = t.utilizador_id
    LEFT JOIN stores  s ON s.id = t.loja_id
"""


def get_all_active_stores():
    """Return all active stores as list of {id, name} dicts."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, name FROM stores WHERE is_active = TRUE ORDER BY name"
        )
        return [{'id': row[0], 'name': row[1]} for row in cursor.fetchall()]


def get_all_tarefas(apenas_ativas=False):
    with db_connection() as conn:
        cursor = conn.cursor()
        cond = "WHERE t.ativo = TRUE" if apenas_ativas else ""
        cursor.execute(f"""
            {_TAREFA_SELECT}
            {cond}
            ORDER BY t.tipo, t.frequencia NULLS LAST, t.nome
        """)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]


def get_tarefa_by_id(tarefa_id):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            {_TAREFA_SELECT}
            WHERE t.id = %s
        """, (tarefa_id,))
        row = cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))


def create_tarefa(nome, tipo, frequencia=None, dia_semana=None, dia_mes=None,
                  utilizador_id=None, loja_id=None, equipa=None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO tarefas
                (nome, tipo, frequencia, dia_semana, dia_mes, utilizador_id, loja_id, equipa, ativo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            RETURNING id
        """, (nome, tipo, frequencia or None, dia_semana, dia_mes,
              utilizador_id or None, loja_id or None, equipa or None))
        tarefa_id = cursor.fetchone()[0]
        conn.commit()
        return tarefa_id


def update_tarefa(tarefa_id, nome, tipo, frequencia=None, dia_semana=None, dia_mes=None,
                  utilizador_id=None, loja_id=None, equipa=None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tarefas
            SET nome = %s, tipo = %s, frequencia = %s,
                dia_semana = %s, dia_mes = %s, utilizador_id = %s,
                loja_id = %s, equipa = %s
            WHERE id = %s
        """, (nome, tipo, frequencia or None, dia_semana, dia_mes,
              utilizador_id or None, loja_id or None, equipa or None, tarefa_id))
        conn.commit()


def toggle_tarefa_ativa(tarefa_id):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tarefas SET ativo = NOT ativo WHERE id = %s RETURNING ativo
        """, (tarefa_id,))
        row = cursor.fetchone()
        conn.commit()
        return row[0] if row else None


_BULK_UPDATE_WHITELIST = frozenset({'nome', 'tipo', 'frequencia', 'utilizador_id', 'loja_id', 'equipa'})


def get_users_com_tarefas():
    """Return active users that have acesso_tarefas, for gestor filter dropdowns."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, username FROM users "
            "WHERE acesso_tarefas = TRUE AND ativo = TRUE ORDER BY username"
        )
        return [{'id': r[0], 'username': r[1], 'nome': r[1]} for r in cursor.fetchall()]


def bulk_delete_tarefas(ids):
    """Hard-delete tarefas by IDs (cascades to tarefas_registos). Returns count."""
    if not ids:
        return 0
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tarefas WHERE id = ANY(%s) RETURNING id", (list(ids),))
        deleted = cursor.rowcount
        conn.commit()
        return deleted


def bulk_update_tarefa_field(ids, field, value):
    """Update a single field on multiple tarefas. Raises ValueError for unknown fields."""
    if field not in _BULK_UPDATE_WHITELIST:
        raise ValueError(f"Campo '{field}' não permitido para edição em massa.")
    if not ids:
        return 0
    coerced = value or None
    if field in ('utilizador_id', 'loja_id'):
        try:
            coerced = int(value) if value else None
        except (ValueError, TypeError):
            coerced = None
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE tarefas SET {field} = %s WHERE id = ANY(%s) RETURNING id",
            (coerced, list(ids))
        )
        updated = cursor.rowcount
        conn.commit()
        return updated


def get_tarefas_do_dia(data=None, utilizador_id=None, loja_id=None, frequencia_filter=None):
    """Return active tasks for the given date, with today's registro if any.
    Tasks with NULL frequencia appear every day (no recurrence = always shown).
    Optional filters: utilizador_id, loja_id, frequencia_filter (key from FREQUENCIAS)."""
    if data is None:
        data = date.today()
    dia_semana = data.weekday()  # 0=Monday .. 6=Sunday
    dia_mes = data.day

    extra_conds = []
    extra_params_list = []
    if utilizador_id is not None:
        extra_conds.append("t.utilizador_id = %s")
        extra_params_list.append(utilizador_id)
    if loja_id is not None:
        extra_conds.append("t.loja_id = %s")
        extra_params_list.append(loja_id)
    if frequencia_filter and frequencia_filter in FREQUENCIAS:
        extra_conds.append("t.frequencia = %s")
        extra_params_list.append(frequencia_filter)

    extra_where = ('AND ' + ' AND '.join(extra_conds)) if extra_conds else ''

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            {_TAREFA_SELECT}
            WHERE t.ativo = TRUE
              AND (
                    t.frequencia IS NULL
                OR  t.frequencia = 'diaria'
                OR (t.frequencia = 'semanal' AND t.dia_semana = %s)
                OR (t.frequencia = 'mensal'  AND t.dia_mes   = %s)
              )
              {extra_where}
            ORDER BY t.tipo, t.nome
        """, [dia_semana, dia_mes] + extra_params_list)
        cols = [d[0] for d in cursor.description]
        tarefas = [dict(zip(cols, row)) for row in cursor.fetchall()]

    if not tarefas:
        return tarefas

    tarefa_ids = [t['id'] for t in tarefas]
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT tr.tarefa_id, tr.estado, tr.motivo, tr.utilizador_id, tr.created_at,
                   u.username AS utilizador_registo
            FROM tarefas_registos tr
            LEFT JOIN users u ON u.id = tr.utilizador_id
            WHERE tr.tarefa_id = ANY(%s) AND tr.data = %s
        """, (tarefa_ids, data))
        cols = [d[0] for d in cursor.description]
        registos_by_id = {
            r['tarefa_id']: r
            for r in [dict(zip(cols, row)) for row in cursor.fetchall()]
        }

    for t in tarefas:
        t['registo'] = registos_by_id.get(t['id'])

    return tarefas


def marcar_tarefa(tarefa_id, utilizador_id, estado, motivo=None, data=None):
    """Insert or replace a task registro for the given date (default today)."""
    if data is None:
        data = date.today()
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO tarefas_registos (tarefa_id, data, estado, motivo, utilizador_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (tarefa_id, data) DO UPDATE
            SET estado        = EXCLUDED.estado,
                motivo        = EXCLUDED.motivo,
                utilizador_id = EXCLUDED.utilizador_id,
                created_at    = NOW()
        """, (tarefa_id, data, estado, motivo or None, utilizador_id))
        conn.commit()


def delete_tarefa_registo(tarefa_id, data=None):
    """Remove the registro for this tarefa on the given date (reset to Pendente)."""
    if data is None:
        data = date.today()
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM tarefas_registos WHERE tarefa_id = %s AND data = %s",
            (tarefa_id, data)
        )
        conn.commit()


def get_historico_tarefas(page=1, per_page=30, data_inicio=None, data_fim=None, tipo=None, estado=None):
    """Paginated audit log of task registos."""
    offset = (page - 1) * per_page
    conditions = []
    params = []

    if data_inicio:
        conditions.append("tr.data >= %s")
        params.append(data_inicio)
    if data_fim:
        conditions.append("tr.data <= %s")
        params.append(data_fim)
    if tipo:
        conditions.append("t.tipo = %s")
        params.append(tipo)
    if estado:
        conditions.append("tr.estado = %s")
        params.append(estado)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT COUNT(*)
            FROM tarefas_registos tr
            JOIN tarefas t ON t.id = tr.tarefa_id
            {where}
        """, params)
        total = cursor.fetchone()[0]

        cursor.execute(f"""
            SELECT tr.id, tr.data, tr.estado, tr.motivo, tr.created_at,
                   t.nome, t.tipo, t.frequencia, t.equipa,
                   u.username AS utilizador_registo,
                   s.name     AS loja_nome
            FROM tarefas_registos tr
            JOIN tarefas  t ON t.id = tr.tarefa_id
            LEFT JOIN users   u ON u.id = tr.utilizador_id
            LEFT JOIN stores  s ON s.id = t.loja_id
            {where}
            ORDER BY tr.data DESC, tr.created_at DESC
            LIMIT %s OFFSET %s
        """, params + [per_page, offset])
        cols = [d[0] for d in cursor.description]
        registos = [dict(zip(cols, row)) for row in cursor.fetchall()]

    return {
        'registos': registos,
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': max(1, (total + per_page - 1) // per_page),
    }
