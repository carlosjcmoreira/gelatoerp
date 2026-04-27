"""Agente Scoopy — blueprint de rotas."""
from __future__ import annotations

import logging
from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify

from flask_app.auth import perm_required

logger = logging.getLogger(__name__)

agente_bp = Blueprint("agente", __name__)


def _uid() -> int:
    return session["user"]["id"]


# ── UI ────────────────────────────────────────────────────────────────────────

@agente_bp.route("/")
@perm_required("acesso_gestor")
def index():
    from db.agente import get_ultima_conversa, criar_conversa
    uid = _uid()
    cid = get_ultima_conversa(uid)
    if not cid:
        cid = criar_conversa(uid)
    return redirect(url_for("agente.conversa", conversa_id=cid))


@agente_bp.route("/<int:conversa_id>")
@perm_required("acesso_gestor")
def conversa(conversa_id: int):
    from db.agente import get_conversa, listar_conversas, get_mensagens
    uid = _uid()
    conv = get_conversa(conversa_id, uid)
    if not conv:
        return redirect(url_for("agente.index"))
    conversas = listar_conversas(uid)
    mensagens = get_mensagens(conversa_id, uid)
    return render_template(
        "agente/index.html",
        conversa=conv,
        conversas=conversas,
        mensagens=mensagens,
    )


# ── API JSON ──────────────────────────────────────────────────────────────────

@agente_bp.route("/nova", methods=["POST"])
@perm_required("acesso_gestor")
def nova_conversa():
    from db.agente import criar_conversa
    uid = _uid()
    cid = criar_conversa(uid)
    return jsonify({"conversa_id": cid, "url": url_for("agente.conversa", conversa_id=cid)})


@agente_bp.route("/<int:conversa_id>/mensagem", methods=["POST"])
@perm_required("acesso_gestor")
def enviar_mensagem(conversa_id: int):
    from db.agente import (
        get_conversa, guardar_mensagem, get_mensagens,
        actualizar_titulo_conversa, limpar_operacoes_expiradas,
    )
    from flask_app.services.agente_ia import processar_mensagem

    uid = _uid()
    conv = get_conversa(conversa_id, uid)
    if not conv:
        return jsonify({"erro": "Conversa não encontrada"}), 404

    data = request.get_json(silent=True) or {}
    mensagem = (data.get("mensagem") or "").strip()
    ficheiro_b64 = data.get("ficheiro_b64")
    ficheiro_nome = data.get("ficheiro_nome")
    is_first_of_day = bool(data.get("is_first_of_day"))

    if not mensagem and not ficheiro_b64:
        return jsonify({"erro": "Mensagem vazia"}), 400

    limpar_operacoes_expiradas()

    display_msg = mensagem
    if ficheiro_nome:
        display_msg = f"{mensagem}\n📎 {ficheiro_nome}".strip()
    guardar_mensagem(conversa_id, "user", display_msg)

    historico = get_mensagens(conversa_id, uid)[:-1]

    try:
        resultado = processar_mensagem(
            user_id=uid,
            conversa_id=conversa_id,
            mensagem=mensagem,
            ficheiro_b64=ficheiro_b64,
            ficheiro_nome=ficheiro_nome,
            is_first_of_day=is_first_of_day,
            historico_msgs=historico,
        )
    except Exception as exc:
        logger.error("processar_mensagem error: %s", exc)
        resultado = {"resposta": "Ocorreu um erro interno. Tenta novamente.", "charts": [], "operacao_pendente": None}

    resposta = resultado.get("resposta", "")
    guardar_mensagem(conversa_id, "assistant", resposta)

    if conv["titulo"] == "Nova conversa" and mensagem:
        titulo = mensagem[:80]
        actualizar_titulo_conversa(conversa_id, titulo)
        conv["titulo"] = titulo

    return jsonify({
        "resposta": resposta,
        "charts": resultado.get("charts", []),
        "operacao_pendente": resultado.get("operacao_pendente"),
        "conversa_id": conversa_id,
        "titulo": conv["titulo"],
    })


@agente_bp.route("/<int:conversa_id>/confirmar/<int:operacao_id>", methods=["POST"])
@perm_required("acesso_gestor")
def confirmar_operacao(conversa_id: int, operacao_id: int):
    from db.agente import (
        get_operacao_pendente, marcar_operacao_executada,
        guardar_mensagem,
    )
    uid = _uid()
    op = get_operacao_pendente(operacao_id, uid)
    if not op:
        return jsonify({"erro": "Operação não encontrada, expirada ou já executada."}), 404

    sql = op["sql_proposto"]
    try:
        from db.connection import db_connection
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql)
            linhas = cursor.rowcount
            conn.commit()
        marcar_operacao_executada(operacao_id, "executada")
        msg = f"✅ Operação executada com sucesso. {linhas} registo(s) afectado(s).\n\nSQL executado:\n```sql\n{sql}\n```"
        guardar_mensagem(conversa_id, "assistant", msg)
        return jsonify({"ok": True, "linhas_afectadas": linhas, "mensagem": msg})
    except Exception as exc:
        logger.error("confirmar_operacao SQL error: %s", exc)
        marcar_operacao_executada(operacao_id, "erro")
        msg = f"❌ Erro ao executar a operação: {exc}"
        guardar_mensagem(conversa_id, "assistant", msg)
        return jsonify({"erro": str(exc), "mensagem": msg}), 500


@agente_bp.route("/<int:conversa_id>/cancelar/<int:operacao_id>", methods=["POST"])
@perm_required("acesso_gestor")
def cancelar_operacao(conversa_id: int, operacao_id: int):
    from db.agente import get_operacao_pendente, marcar_operacao_executada, guardar_mensagem
    uid = _uid()
    op = get_operacao_pendente(operacao_id, uid)
    if op:
        marcar_operacao_executada(operacao_id, "cancelada")
    guardar_mensagem(conversa_id, "assistant", "Operação cancelada pelo utilizador.")
    return jsonify({"ok": True})


@agente_bp.route("/historico")
@perm_required("acesso_gestor")
def historico():
    from db.agente import listar_conversas
    uid = _uid()
    return jsonify(listar_conversas(uid))


@agente_bp.route("/<int:conversa_id>", methods=["DELETE"])
@perm_required("acesso_gestor")
def eliminar_conversa(conversa_id: int):
    from db.agente import eliminar_conversa as _del
    uid = _uid()
    ok = _del(conversa_id, uid)
    return jsonify({"ok": ok})
