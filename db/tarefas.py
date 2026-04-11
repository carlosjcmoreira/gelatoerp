import logging
from datetime import date
from db.connection import db_connection, get_connection, release_connection

logger = logging.getLogger(__name__)

DIAS_SEMANA = ['Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado', 'Domingo']
FREQUENCIAS = {'diaria': 'Diária', 'semanal': 'Semanal', 'mensal': 'Mensal'}
TIPOS = {'abertura': 'Abertura', 'fecho': 'Fecho'}


def get_all_tarefas(apenas_ativas=False):
    with db_connection() as conn:
        cursor = conn.cursor()
        cond = "WHERE t.ativo = TRUE" if apenas_ativas else ""
        cursor.execute(f"""
            SELECT t.id, t.nome, t.tipo, t.frequencia, t.dia_semana, t.dia_mes,
                   t.utilizador_id, t.ativo, t.created_at,
                   u.username AS utilizador_nome
            FROM tarefas t
            LEFT JOIN users u ON u.id = t.utilizador_id
            {cond}
            ORDER BY t.tipo, t.frequencia, t.nome
        """)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]


def get_tarefa_by_id(tarefa_id):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT t.id, t.nome, t.tipo, t.frequencia, t.dia_semana, t.dia_mes,
                   t.utilizador_id, t.ativo, t.created_at,
                   u.username AS utilizador_nome
            FROM tarefas t
            LEFT JOIN users u ON u.id = t.utilizador_id
            WHERE t.id = %s
        """, (tarefa_id,))
        row = cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))


def create_tarefa(nome, tipo, frequencia, dia_semana=None, dia_mes=None, utilizador_id=None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO tarefas (nome, tipo, frequencia, dia_semana, dia_mes, utilizador_id, ativo)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            RETURNING id
        """, (nome, tipo, frequencia, dia_semana, dia_mes, utilizador_id or None))
        tarefa_id = cursor.fetchone()[0]
        conn.commit()
        return tarefa_id


def update_tarefa(tarefa_id, nome, tipo, frequencia, dia_semana=None, dia_mes=None, utilizador_id=None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tarefas
            SET nome = %s, tipo = %s, frequencia = %s,
                dia_semana = %s, dia_mes = %s, utilizador_id = %s
            WHERE id = %s
        """, (nome, tipo, frequencia, dia_semana, dia_mes, utilizador_id or None, tarefa_id))
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


def get_tarefas_do_dia(data=None):
    """Return active tasks for the given date, with today's registro if any."""
    if data is None:
        data = date.today()
    dia_semana = data.weekday()  # 0=Monday .. 6=Sunday
    dia_mes = data.day

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT t.id, t.nome, t.tipo, t.frequencia, t.dia_semana, t.dia_mes,
                   t.utilizador_id, u.username AS utilizador_nome
            FROM tarefas t
            LEFT JOIN users u ON u.id = t.utilizador_id
            WHERE t.ativo = TRUE
              AND (
                    t.frequencia = 'diaria'
                OR (t.frequencia = 'semanal' AND t.dia_semana = %s)
                OR (t.frequencia = 'mensal'  AND t.dia_mes   = %s)
              )
            ORDER BY t.tipo, t.nome
        """, (dia_semana, dia_mes))
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
            SET estado       = EXCLUDED.estado,
                motivo       = EXCLUDED.motivo,
                utilizador_id = EXCLUDED.utilizador_id,
                created_at   = NOW()
        """, (tarefa_id, data, estado, motivo or None, utilizador_id))
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
                   t.nome, t.tipo, t.frequencia,
                   u.username AS utilizador_registo
            FROM tarefas_registos tr
            JOIN tarefas t ON t.id = tr.tarefa_id
            LEFT JOIN users u ON u.id = tr.utilizador_id
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
