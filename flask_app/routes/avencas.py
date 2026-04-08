import sys
import os
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_app.auth import perm_required
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import (
    get_avencas, get_avenca, create_avenca, update_avenca, delete_avenca,
    next_due_date, get_cost_centers, get_cost_categories,
)

avencas_bp = Blueprint('avencas', __name__)

_PERIOD_MONTHS = {'mensal': 12, 'trimestral': 4, 'semestral': 2, 'anual': 1}
_PERIOD_LABEL = {
    'mensal': 'Mensal', 'trimestral': 'Trimestral',
    'semestral': 'Semestral', 'anual': 'Anual',
}


def _annual_value(avenca):
    factor = _PERIOD_MONTHS.get(avenca['periodicidade'], 1)
    return round(float(avenca['valor']) * factor, 2)


def _enrich(avencas_list):
    today = date.today()
    for a in avencas_list:
        a['next_due'] = next_due_date(a, today) if a['ativo'] else None
        a['annual_value'] = _annual_value(a)
    return avencas_list


@avencas_bp.route('/', methods=['GET'])
@perm_required('acesso_gestor')
def index():
    avencas = _enrich(list(get_avencas(ativo_only=False)))
    total_anual = sum(a['annual_value'] for a in avencas if a['ativo'])
    return render_template(
        'avencas/index.html',
        avencas=avencas,
        total_anual=round(total_anual, 2),
        period_label=_PERIOD_LABEL,
    )


@avencas_bp.route('/nova', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def nova():
    centros = get_cost_centers(ativo_only=False)
    categorias = get_cost_categories(ativo_only=False)
    if request.method == 'POST':
        errors = []
        nome = request.form.get('nome', '').strip()
        if not nome:
            errors.append('Nome é obrigatório.')
        try:
            valor = float(request.form.get('valor', '0').replace(',', '.'))
            if valor <= 0:
                raise ValueError
        except ValueError:
            errors.append('Valor deve ser um número positivo.')
            valor = 0.0
        periodicidade = request.form.get('periodicidade', '')
        if periodicidade not in ('mensal', 'trimestral', 'semestral', 'anual'):
            errors.append('Periodicidade inválida.')
        try:
            dia = int(request.form.get('dia_vencimento', 1))
            if not (1 <= dia <= 28):
                raise ValueError
        except ValueError:
            errors.append('Dia de vencimento deve ser entre 1 e 28.')
            dia = 1
        data_inicio_str = request.form.get('data_inicio', '')
        try:
            data_inicio = date.fromisoformat(data_inicio_str) if data_inicio_str else date.today()
        except ValueError:
            errors.append('Data de início inválida.')
            data_inicio = date.today()
        data_fim_str = request.form.get('data_fim', '').strip()
        data_fim = None
        if data_fim_str:
            try:
                data_fim = date.fromisoformat(data_fim_str)
            except ValueError:
                errors.append('Data de fim inválida.')
        centro_id_raw = request.form.get('centro_custo_id', '').strip()
        centro_id = int(centro_id_raw) if centro_id_raw else None
        cat_id_raw = request.form.get('categoria_custo_id', '').strip()
        cat_id = int(cat_id_raw) if cat_id_raw else None
        descricao = request.form.get('descricao', '').strip() or None
        observacoes = request.form.get('observacoes', '').strip() or None

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('avencas/form.html', avenca=request.form,
                                   centros=centros, categorias=categorias,
                                   titulo='Nova Avença', action=url_for('avencas.nova'))

        try:
            create_avenca(nome, valor, periodicidade, dia, data_inicio, data_fim,
                          centro_id, cat_id, descricao, observacoes)
            flash(f'Avença "{nome}" criada com sucesso.', 'success')
        except Exception as exc:
            flash(f'Erro ao criar avença: {exc}', 'danger')
        return redirect(url_for('avencas.index'))

    return render_template('avencas/form.html', avenca=None,
                           centros=centros, categorias=categorias,
                           titulo='Nova Avença', action=url_for('avencas.nova'))


@avencas_bp.route('/<int:avenca_id>/editar', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def editar(avenca_id):
    avenca = get_avenca(avenca_id)
    if avenca is None:
        flash('Avença não encontrada.', 'danger')
        return redirect(url_for('avencas.index'))
    centros = get_cost_centers(ativo_only=False)
    categorias = get_cost_categories(ativo_only=False)

    if request.method == 'POST':
        errors = []
        nome = request.form.get('nome', '').strip()
        if not nome:
            errors.append('Nome é obrigatório.')
        try:
            valor = float(request.form.get('valor', '0').replace(',', '.'))
            if valor <= 0:
                raise ValueError
        except ValueError:
            errors.append('Valor deve ser um número positivo.')
            valor = 0.0
        periodicidade = request.form.get('periodicidade', '')
        if periodicidade not in ('mensal', 'trimestral', 'semestral', 'anual'):
            errors.append('Periodicidade inválida.')
        try:
            dia = int(request.form.get('dia_vencimento', 1))
            if not (1 <= dia <= 28):
                raise ValueError
        except ValueError:
            errors.append('Dia de vencimento deve ser entre 1 e 28.')
            dia = 1
        data_inicio_str = request.form.get('data_inicio', '')
        try:
            data_inicio = date.fromisoformat(data_inicio_str) if data_inicio_str else avenca['data_inicio']
        except ValueError:
            errors.append('Data de início inválida.')
            data_inicio = avenca['data_inicio']
        data_fim_str = request.form.get('data_fim', '').strip()
        data_fim = None
        if data_fim_str:
            try:
                data_fim = date.fromisoformat(data_fim_str)
            except ValueError:
                errors.append('Data de fim inválida.')
        centro_id_raw = request.form.get('centro_custo_id', '').strip()
        centro_id = int(centro_id_raw) if centro_id_raw else None
        cat_id_raw = request.form.get('categoria_custo_id', '').strip()
        cat_id = int(cat_id_raw) if cat_id_raw else None
        descricao = request.form.get('descricao', '').strip() or None
        observacoes = request.form.get('observacoes', '').strip() or None
        ativo = request.form.get('ativo') == '1'

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('avencas/form.html', avenca=request.form,
                                   centros=centros, categorias=categorias,
                                   titulo='Editar Avença',
                                   action=url_for('avencas.editar', avenca_id=avenca_id),
                                   is_edit=True)

        try:
            update_avenca(avenca_id,
                          nome=nome, valor=valor, periodicidade=periodicidade,
                          dia_vencimento=dia, data_inicio=data_inicio, data_fim=data_fim,
                          centro_custo_id=centro_id, categoria_custo_id=cat_id,
                          descricao=descricao, observacoes=observacoes, ativo=ativo)
            flash(f'Avença "{nome}" actualizada.', 'success')
        except Exception as exc:
            flash(f'Erro ao actualizar avença: {exc}', 'danger')
        return redirect(url_for('avencas.index'))

    return render_template('avencas/form.html', avenca=avenca,
                           centros=centros, categorias=categorias,
                           titulo='Editar Avença',
                           action=url_for('avencas.editar', avenca_id=avenca_id),
                           is_edit=True)


@avencas_bp.route('/<int:avenca_id>/eliminar', methods=['POST'])
@perm_required('acesso_gestor')
def eliminar(avenca_id):
    avenca = get_avenca(avenca_id)
    if avenca is None:
        flash('Avença não encontrada.', 'danger')
        return redirect(url_for('avencas.index'))
    confirmacao = request.form.get('confirmacao', '').strip().upper()
    if confirmacao != 'ELIMINAR':
        flash('Confirmação incorrecta. Escreva "ELIMINAR" para confirmar.', 'warning')
        return redirect(url_for('avencas.index'))
    try:
        delete_avenca(avenca_id)
        flash(f'Avença "{avenca["nome"]}" eliminada.', 'success')
    except Exception as exc:
        flash(f'Erro ao eliminar: {exc}', 'danger')
    return redirect(url_for('avencas.index'))


@avencas_bp.route('/<int:avenca_id>/toggle-ativo', methods=['POST'])
@perm_required('acesso_gestor')
def toggle_ativo(avenca_id):
    avenca = get_avenca(avenca_id)
    if avenca is None:
        flash('Avença não encontrada.', 'danger')
        return redirect(url_for('avencas.index'))
    novo_estado = not avenca['ativo']
    update_avenca(avenca_id, ativo=novo_estado)
    estado_txt = 'activada' if novo_estado else 'desactivada'
    flash(f'Avença "{avenca["nome"]}" {estado_txt}.', 'success')
    return redirect(url_for('avencas.index'))
