import logging
from datetime import date, datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_app.auth import perm_required

logger = logging.getLogger(__name__)

tarefas_bp = Blueprint('tarefas', __name__)


@tarefas_bp.route('/', methods=['GET'])
@perm_required('acesso_tarefas')
def index():
    from db.tarefas import (
        get_tarefas_do_dia, DIAS_SEMANA, FREQUENCIAS,
        get_all_active_stores, get_users_com_tarefas,
    )
    hoje = date.today()
    user = session['user']
    is_gestor = bool(user.get('acesso_gestor'))

    filter_loja_id = None
    filter_utilizador_id = None
    filter_frequencia = None
    stores = []
    all_users = []

    if is_gestor:
        loja_str = request.args.get('loja_id', '').strip()
        uid_str = request.args.get('utilizador_id', '').strip()
        filter_frequencia = request.args.get('frequencia', '').strip() or None
        try:
            filter_loja_id = int(loja_str) if loja_str else None
        except (ValueError, TypeError):
            filter_loja_id = None
        try:
            filter_utilizador_id = int(uid_str) if uid_str else None
        except (ValueError, TypeError):
            filter_utilizador_id = None
        stores = get_all_active_stores()
        all_users = get_users_com_tarefas()
    else:
        filter_utilizador_id = user['id']

    tarefas = get_tarefas_do_dia(
        hoje,
        utilizador_id=filter_utilizador_id,
        loja_id=filter_loja_id,
        frequencia_filter=filter_frequencia,
    )

    abertura = [t for t in tarefas if t['tipo'] == 'abertura']
    fecho = [t for t in tarefas if t['tipo'] == 'fecho']

    return render_template(
        'tarefas/index.html',
        abertura=abertura,
        fecho=fecho,
        hoje=hoje,
        dias_semana=DIAS_SEMANA,
        frequencias=FREQUENCIAS,
        user=user,
        is_gestor=is_gestor,
        stores=stores,
        all_users=all_users,
        filter_loja_id=filter_loja_id,
        filter_utilizador_id=filter_utilizador_id,
        filter_frequencia=filter_frequencia or '',
    )


@tarefas_bp.route('/marcar', methods=['POST'])
@perm_required('acesso_tarefas')
def marcar():
    from db.tarefas import marcar_tarefa, delete_tarefa_registo, get_tarefa_by_id
    user = session['user']
    tarefa_id = request.form.get('tarefa_id', type=int)
    estado = request.form.get('estado', '')
    motivo = request.form.get('motivo', '').strip() or None

    if not tarefa_id or estado not in ('feita', 'bloqueada', 'em_curso', 'pendente'):
        flash('Pedido inválido.', 'warning')
        return redirect(url_for('tarefas.index'))

    tarefa = get_tarefa_by_id(tarefa_id)
    if not tarefa:
        flash('Tarefa não encontrada.', 'warning')
        return redirect(url_for('tarefas.index'))

    if tarefa.get('utilizador_id') != user['id'] and not user.get('acesso_gestor'):
        flash('Não tem permissão para marcar esta tarefa.', 'danger')
        return redirect(url_for('tarefas.index'))

    if not tarefa.get('ativo'):
        flash('Esta tarefa está inativa e não pode ser marcada.', 'warning')
        return redirect(url_for('tarefas.index'))

    hoje = date.today()
    freq = tarefa.get('frequencia')
    due = (
        freq is None
        or freq == 'diaria'
        or (freq == 'semanal' and tarefa.get('dia_semana') == hoje.weekday())
        or (freq == 'mensal' and tarefa.get('dia_mes') == hoje.day)
    )
    if not due:
        flash('Esta tarefa não é devida hoje.', 'warning')
        return redirect(url_for('tarefas.index'))

    if estado == 'bloqueada' and not motivo:
        flash('Indique o motivo para marcar como bloqueada.', 'warning')
        return redirect(url_for('tarefas.index'))

    if estado == 'pendente':
        delete_tarefa_registo(tarefa_id)
        flash('Tarefa reposta como pendente.', 'success')
    else:
        marcar_tarefa(tarefa_id, user['id'], estado, motivo)
        labels = {'feita': 'como feita', 'bloqueada': 'como bloqueada', 'em_curso': 'em curso'}
        flash(f"Tarefa marcada {labels[estado]}.", 'success')
    return redirect(url_for('tarefas.index'))


@tarefas_bp.route('/historico', methods=['GET'])
@perm_required('acesso_tarefas')
def historico():
    from db.tarefas import get_historico_tarefas, FREQUENCIAS

    page = request.args.get('page', 1, type=int)
    data_inicio_str = request.args.get('data_inicio', '')
    data_fim_str = request.args.get('data_fim', '')
    tipo = request.args.get('tipo', '')
    estado = request.args.get('estado', '')

    data_inicio = None
    data_fim = None
    try:
        if data_inicio_str:
            data_inicio = datetime.strptime(data_inicio_str, '%Y-%m-%d').date()
    except ValueError:
        pass
    try:
        if data_fim_str:
            data_fim = datetime.strptime(data_fim_str, '%Y-%m-%d').date()
    except ValueError:
        pass

    resultado = get_historico_tarefas(
        page=page,
        per_page=30,
        data_inicio=data_inicio,
        data_fim=data_fim,
        tipo=tipo or None,
        estado=estado or None,
    )

    return render_template(
        'tarefas/historico.html',
        registos=resultado['registos'],
        total=resultado['total'],
        page=resultado['page'],
        per_page=resultado['per_page'],
        total_pages=resultado['total_pages'],
        data_inicio=data_inicio_str,
        data_fim=data_fim_str,
        tipo=tipo,
        estado=estado,
        frequencias=FREQUENCIAS,
    )
