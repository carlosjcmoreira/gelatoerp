import logging
from datetime import date, datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_app.auth import perm_required

logger = logging.getLogger(__name__)

tarefas_bp = Blueprint('tarefas', __name__)


@tarefas_bp.route('/', methods=['GET'])
@perm_required('acesso_tarefas')
def index():
    from db.tarefas import get_tarefas_do_dia, DIAS_SEMANA, FREQUENCIAS
    hoje = date.today()
    tarefas = get_tarefas_do_dia(hoje)

    abertura = [t for t in tarefas if t['tipo'] == 'abertura']
    fecho = [t for t in tarefas if t['tipo'] == 'fecho']

    user = session['user']
    return render_template(
        'tarefas/index.html',
        abertura=abertura,
        fecho=fecho,
        hoje=hoje,
        dias_semana=DIAS_SEMANA,
        frequencias=FREQUENCIAS,
        user=user,
    )


@tarefas_bp.route('/marcar', methods=['POST'])
@perm_required('acesso_tarefas')
def marcar():
    from db.tarefas import marcar_tarefa, get_tarefa_by_id
    user = session['user']
    tarefa_id = request.form.get('tarefa_id', type=int)
    estado = request.form.get('estado', '')
    motivo = request.form.get('motivo', '').strip() or None

    if not tarefa_id or estado not in ('feita', 'bloqueada'):
        flash('Pedido inválido.', 'warning')
        return redirect(url_for('tarefas.index'))

    tarefa = get_tarefa_by_id(tarefa_id)
    if not tarefa:
        flash('Tarefa não encontrada.', 'warning')
        return redirect(url_for('tarefas.index'))

    if tarefa.get('utilizador_id') != user['id'] and not user.get('acesso_gestor'):
        flash('Não tem permissão para marcar esta tarefa.', 'danger')
        return redirect(url_for('tarefas.index'))

    if estado == 'bloqueada' and not motivo:
        flash('Indique o motivo para marcar como bloqueada.', 'warning')
        return redirect(url_for('tarefas.index'))

    marcar_tarefa(tarefa_id, user['id'], estado, motivo)
    label = 'como feita' if estado == 'feita' else 'como bloqueada'
    flash(f"Tarefa marcada {label}.", 'success')
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
