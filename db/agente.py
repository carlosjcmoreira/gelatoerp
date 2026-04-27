"""Database layer for the Agente Scoopy module.

Tables managed:
  - agente_conversas      — conversation sessions (30-day TTL)
  - agente_mensagens      — messages within a conversation
  - agente_memoria        — persistent business memory / facts
  - agente_operacoes_pendentes — proposed write operations awaiting confirmation
"""
from __future__ import annotations

import logging
from datetime import datetime, date

from db.connection import db_connection

logger = logging.getLogger(__name__)


# ── Conversas ────────────────────────────────────────────────────────────────

def criar_conversa(user_id: int, titulo: str = "Nova conversa") -> int:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO agente_conversas (user_id, titulo) VALUES (%s, %s) RETURNING id",
            (user_id, titulo),
        )
        cid = cursor.fetchone()[0]
        conn.commit()
        return cid


def actualizar_titulo_conversa(conversa_id: int, titulo: str) -> None:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE agente_conversas SET titulo = %s, updated_at = NOW() WHERE id = %s",
            (titulo, conversa_id),
        )
        conn.commit()


def get_conversa(conversa_id: int, user_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, user_id, titulo, created_at, updated_at FROM agente_conversas WHERE id = %s AND user_id = %s",
            (conversa_id, user_id),
        )
        row = cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))


def listar_conversas(user_id: int) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT id, titulo, created_at, updated_at
               FROM agente_conversas
               WHERE user_id = %s
                 AND created_at >= NOW() - INTERVAL '30 days'
               ORDER BY updated_at DESC""",
            (user_id,),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]


def get_ultima_conversa(user_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT id FROM agente_conversas
               WHERE user_id = %s
               ORDER BY updated_at DESC LIMIT 1""",
            (user_id,),
        )
        row = cursor.fetchone()
        return row[0] if row else None


def eliminar_conversa(conversa_id: int, user_id: int) -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM agente_conversas WHERE id = %s AND user_id = %s",
            (conversa_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def limpar_conversas_antigas() -> int:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM agente_conversas WHERE created_at < NOW() - INTERVAL '30 days'"
        )
        deleted = cursor.rowcount
        conn.commit()
        return deleted


# ── Mensagens ────────────────────────────────────────────────────────────────

def guardar_mensagem(conversa_id: int, role: str, content: str, tool_name: str | None = None) -> int:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO agente_mensagens (conversa_id, role, content, tool_name) VALUES (%s, %s, %s, %s) RETURNING id",
            (conversa_id, role, content, tool_name),
        )
        mid = cursor.fetchone()[0]
        cursor.execute(
            "UPDATE agente_conversas SET updated_at = NOW() WHERE id = %s",
            (conversa_id,),
        )
        conn.commit()
        return mid


def get_mensagens(conversa_id: int, user_id: int) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT m.id, m.role, m.content, m.tool_name, m.created_at
               FROM agente_mensagens m
               JOIN agente_conversas c ON c.id = m.conversa_id
               WHERE m.conversa_id = %s AND c.user_id = %s
               ORDER BY m.created_at ASC""",
            (conversa_id, user_id),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]


# ── Memória ───────────────────────────────────────────────────────────────────

def guardar_memoria(user_id: int, chave: str, valor: str, categoria: str) -> None:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO agente_memoria (user_id, chave, valor, categoria)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (user_id, chave)
               DO UPDATE SET valor = EXCLUDED.valor, categoria = EXCLUDED.categoria, updated_at = NOW()""",
            (user_id, chave, valor, categoria),
        )
        conn.commit()


def get_memoria(user_id: int) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT chave, valor, categoria, updated_at
               FROM agente_memoria
               WHERE user_id = %s
               ORDER BY updated_at DESC""",
            (user_id,),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]


# ── Operações Pendentes ───────────────────────────────────────────────────────

def criar_operacao_pendente(conversa_id: int, user_id: int, sql: str, descricao: str, impacto: str) -> int:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO agente_operacoes_pendentes
               (conversa_id, user_id, sql_proposto, descricao, impacto_estimado, estado)
               VALUES (%s, %s, %s, %s, %s, 'pendente')
               RETURNING id""",
            (conversa_id, user_id, sql, descricao, impacto),
        )
        oid = cursor.fetchone()[0]
        conn.commit()
        return oid


def get_operacao_pendente(operacao_id: int, user_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT id, conversa_id, user_id, sql_proposto, descricao, impacto_estimado,
                      estado, created_at
               FROM agente_operacoes_pendentes
               WHERE id = %s AND user_id = %s AND estado = 'pendente'
                 AND created_at >= NOW() - INTERVAL '15 minutes'""",
            (operacao_id, user_id),
        )
        row = cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))


def marcar_operacao_executada(operacao_id: int, resultado: str) -> None:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE agente_operacoes_pendentes SET estado = %s, executada_at = NOW() WHERE id = %s",
            (resultado, operacao_id),
        )
        conn.commit()


def limpar_operacoes_expiradas() -> int:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM agente_operacoes_pendentes WHERE created_at < NOW() - INTERVAL '15 minutes' AND estado = 'pendente'"
        )
        deleted = cursor.rowcount
        conn.commit()
        return deleted
