import logging
import os
import sys
import pandas as pd
from datetime import datetime, date
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from flask_app.auth import perm_required
from db.cache import invalidate_prefix

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import database as db

import flask_app.services.gestor as gestor_svc
from flask_app.services import ServiceError
from db.credito import get_payment_methods_config, upsert_payment_method_config

gestor_bp = Blueprint('gestor', __name__)

TABS = [
    {'id': 'upload_producao', 'label': 'Upload Produção', 'icon': '📤', 'url_endpoint': 'gestor.upload_producao'},
    {'id': 'upload_pesagem', 'label': 'Upload Pesagem', 'icon': '⚖️', 'url_endpoint': 'gestor.upload_pesagem'},
    {'id': 'vendas_detalhe', 'label': 'Vendas Detalhe', 'icon': '🛒', 'url_endpoint': 'gestor.vendas_detalhe'},
    {'id': 'alocacao_produtos', 'label': 'Alocação de Produtos', 'icon': '📋', 'url_endpoint': 'gestor.regras_negocio'},
    {'id': 'ajustes_producao', 'label': 'Ajustes Produção', 'icon': '🔧', 'url_endpoint': 'gestor.ajustes_producao'},
    {'id': 'motivos_quebra', 'label': 'Motivos Quebra', 'icon': '⚠️', 'url_endpoint': 'gestor.motivos_quebra'},
    {'id': 'produtos_rececao', 'label': 'Produtos de Venda', 'icon': '📦', 'url_endpoint': 'gestor.produtos_rececao'},
    {'id': 'receitas_eurokg', 'label': 'Sabores Euro/kg', 'icon': '🍦', 'url_endpoint': 'gestor.receitas_eurokg'},
    {'id': 'gestao_utilizadores', 'label': 'Utilizadores', 'icon': '👥', 'url_endpoint': 'gestor.gestao_utilizadores'},
    {'id': 'premio_eurokg', 'label': 'Prémio Euro/kg', 'icon': '🏆', 'url_endpoint': 'gestor.premio_eurokg'},
    {'id': 'gestao_lojas', 'label': 'Lojas', 'icon': '🏪', 'url_endpoint': 'gestor.gestao_lojas'},
    {'id': 'metodos_pagamento', 'label': 'Métodos de Pagamento', 'icon': '💳', 'url_endpoint': 'gestor.metodos_pagamento'},
    {'id': 'materiais', 'label': 'Catálogo de Materiais', 'icon': '🗂️', 'url_endpoint': 'gestor.materiais'},
    {'id': 'configuracoes', 'label': 'Configurações', 'icon': '⚙️', 'url_endpoint': 'gestor.configuracoes'},
]

SECTION_ENDPOINT_MAP = {
    'alocacao_produtos': 'gestor.regras_negocio',
    'regras_negocio': 'gestor.regras_negocio',
    'ajustes_producao': 'gestor.ajustes_producao',
    'motivos_quebra': 'gestor.motivos_quebra',
    'coberturas': 'pastelaria.coberturas',
    'tipologias_pastelaria': 'pastelaria.tipologias',
    'produtos_pastelaria': 'pastelaria.produtos',
    'produtos_rececao': 'gestor.produtos_rececao',
    'receitas_eurokg': 'gestor.receitas_eurokg',
    'gestao_utilizadores': 'gestor.gestao_utilizadores',
    'materiais': 'gestor.materiais',
}

def get_tabs():
    return [{'id': t['id'], 'label': t['label'], 'icon': t['icon'], 'url': url_for(t['url_endpoint'])} for t in TABS]


def parse_decimal_input(val_str, default=0.0):
    if not val_str:
        return default
    s = str(val_str).replace(',', '.').strip()
    try:
        return float(s)
    except ValueError:
        return default


EUROKG_CHILD_IDS = {'premio_eurokg', 'receitas_eurokg', 'alocacao_produtos'}

@gestor_bp.route('/')
@perm_required('acesso_gestor')
def index():
    onedrive_configured = bool(db.get_system_config('onedrive_refresh_token'))
    items = []
    eurokg_added = False
    for t in TABS:
        if t['id'] in EUROKG_CHILD_IDS:
            if not eurokg_added:
                items.append({'icon': '💶', 'label': 'Euro/kg', 'url': url_for('gestor.eurokg_index')})
                eurokg_added = True
            continue
        item = {'icon': t['icon'], 'label': t['label'], 'url': url_for(t['url_endpoint'])}
        if t['id'] == 'configuracoes':
            if onedrive_configured:
                item['badge'] = {'text': 'Ligado', 'cls': 'bg-success'}
            else:
                item['badge'] = {'text': 'Por configurar', 'cls': 'bg-secondary'}
        items.append(item)
    return render_template('components/section_menu.html', items=items)


@gestor_bp.route('/eurokg/')
@perm_required('acesso_gestor')
def eurokg_index():
    items = [
        {'icon': '🏆', 'label': 'Prémio Euro/kg', 'url': url_for('gestor.premio_eurokg')},
        {'icon': '🍦', 'label': 'Sabores Euro/kg', 'url': url_for('gestor.receitas_eurokg')},
        {'icon': '📋', 'label': 'Alocação de Produtos', 'url': url_for('gestor.regras_negocio')},
    ]
    return render_template('gestor/eurokg_index.html', items=items)


@gestor_bp.route('/upload-producao', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def upload_producao():
    tabs = get_tabs()
    loja_upload = request.args.get('loja', 'Matosinhos')
    prod_totals_list = db.get_producao_total_by_date(loja_upload)

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'upload_csv':
            return _handle_upload_producao_csv(loja_upload)
        elif action == 'delete_producao':
            return _handle_delete_producao()
        elif action == 'manual_producao':
            return _handle_manual_producao()

    return render_template('gestor/upload_producao.html',
                           active_tab='upload_producao', tabs=tabs,
                           loja_upload=loja_upload,
                           prod_totals=prod_totals_list,
                           today=date.today().isoformat())


def _handle_upload_producao_csv(loja_upload):
    uploaded_file = request.files.get('csv_file')
    unit_is_grams = request.form.get('unit_is_grams') == 'on'
    loja = request.form.get('loja', loja_upload)

    if not uploaded_file or uploaded_file.filename == '':
        flash('Por favor, selecione um ficheiro CSV.', 'error')
        return redirect(url_for('gestor.upload_producao', loja=loja))

    try:
        result = gestor_svc.import_producao_csv(uploaded_file, loja, unit_is_grams)
        imported = result['imported']
        updated = result.get('updated', 0)
        skipped = result.get('skipped', 0)
        novas = result['novas_receitas']
        msg = f'{imported} registo(s) inserido(s), {updated} atualizado(s) e {skipped} ignorado(s) para {loja}.'
        if novas > 0:
            nomes = ', '.join(result['receitas_novas_nomes'])
            msg += f' {novas} receita(s) nova(s) adicionada(s) automaticamente: {nomes}'
        flash(msg, 'success')
    except ServiceError as e:
        flash(str(e), 'error')

    return redirect(url_for('gestor.upload_producao', loja=loja))


def _handle_delete_producao():
    data_inicio_str = request.form.get('data_del_inicio')
    data_fim_str = request.form.get('data_del_fim')
    loja = request.form.get('loja_del')
    try:
        data_inicio = datetime.strptime(data_inicio_str, '%Y-%m-%d').date()
        data_fim = datetime.strptime(data_fim_str, '%Y-%m-%d').date()
        if data_inicio > data_fim:
            flash('Data de início não pode ser posterior à data de fim.', 'warning')
        else:
            deleted = db.delete_producao_by_date_range(data_inicio, data_fim, loja)
            if deleted > 0:
                if data_inicio == data_fim:
                    flash(f'{deleted} registo(s) de produção eliminados para {loja} em {data_inicio.strftime("%d/%m/%Y")}!', 'success')
                else:
                    flash(f'{deleted} registo(s) de produção eliminados para {loja} entre {data_inicio.strftime("%d/%m/%Y")} e {data_fim.strftime("%d/%m/%Y")}!', 'success')
            else:
                flash('Nenhum registo encontrado para esse período e loja.', 'warning')
    except Exception as e:
        flash(f'Erro: {str(e)}', 'error')
    return redirect(url_for('gestor.upload_producao', loja=request.form.get('loja', 'Matosinhos')))


def _handle_manual_producao():
    data_str = request.form.get('data_prod')
    loja = request.form.get('loja_prod')
    qtd = parse_decimal_input(request.form.get('qtd_prod'))
    try:
        data_prod = datetime.strptime(data_str, '%Y-%m-%d').date()
        if qtd > 0:
            db.add_producao(data_prod, loja, qtd)
            flash(f'Produção de {qtd:.2f}kg registada para {loja}!', 'success')
        else:
            flash('Por favor, insira uma quantidade válida.', 'warning')
    except Exception as e:
        flash(f'Erro: {str(e)}', 'error')
    return redirect(url_for('gestor.upload_producao', loja=loja))


@gestor_bp.route('/upload-pesagem', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def upload_pesagem():
    tabs = get_tabs()
    loja_pesagem = request.args.get('loja', 'Matosinhos')

    if request.method == 'POST':
        return _handle_upload_pesagem_post(loja_pesagem)

    tipo_pesagem = 'fim' if loja_pesagem == 'Bolhão' else 'inicio'
    last_date, anterior, _ = db.get_pesagem_comparison(loja_pesagem, tipo_pesagem)
    last_pesagem = []
    if last_date and anterior:
        mapping = db.get_sabores_mapping()
        rev_map = {}
        for nr, nc in mapping.items():
            rev_map[nr] = nc
            rev_map[nc] = nc
        for s in sorted(anterior.keys()):
            last_pesagem.append({'sabor': rev_map.get(s, s), 'pesagem_kg': round(anterior[s], 3)})
        total_pesagem = round(sum(anterior.values()), 3)
    else:
        last_date = None
        total_pesagem = 0

    return render_template('gestor/upload_pesagem.html',
                           active_tab='upload_pesagem', tabs=tabs,
                           loja_pesagem=loja_pesagem,
                           last_date=last_date,
                           last_pesagem=last_pesagem,
                           total_pesagem=total_pesagem)


def _handle_upload_pesagem_post(loja_pesagem):
    uploaded_file = request.files.get('pesagem_file')
    loja = request.form.get('loja', loja_pesagem)

    if not uploaded_file or uploaded_file.filename == '':
        flash('Por favor, selecione um ficheiro Excel.', 'error')
        return redirect(url_for('gestor.upload_pesagem', loja=loja))

    try:
        df_pesagem = pd.read_excel(uploaded_file)
        sabor_col = df_pesagem.columns[0]
        date_cols = [c for c in df_pesagem.columns[1:] if isinstance(c, (pd.Timestamp, datetime))]
        if not date_cols:
            for c in df_pesagem.columns[1:]:
                try:
                    pd.to_datetime(c)
                    date_cols.append(c)
                except:
                    pass

        sabor_mapping_upload = {
            'Ricota, Noz/Mel': 'Ricota, Noz e Mel',
            'Extra Noir': 'Extra noir',
            'Doce de Leite': 'Doce de leite',
        }

        tipo_pesagem = 'fim' if loja == 'Bolhão' else 'inicio'
        imported = 0
        skipped = 0

        all_dates_pesagem = set()
        for col in date_cols:
            try:
                all_dates_pesagem.add(pd.to_datetime(col).date())
            except Exception:
                continue

        existing_set = set()
        if all_dates_pesagem:
            min_d = min(all_dates_pesagem)
            max_d = max(all_dates_pesagem)
            existing_records = db.get_stock_gelado_df(loja=loja, tipo=tipo_pesagem, data_inicio=min_d, data_fim=max_d)
            for r in existing_records:
                existing_set.add((r['data'], r['sabor']))

        for _, row in df_pesagem.iterrows():
            sabor_raw = str(row[sabor_col]).strip()
            sabor = sabor_mapping_upload.get(sabor_raw, sabor_raw)
            for col in date_cols:
                val = row[col]
                if pd.isna(val) or str(val).strip() == '-':
                    continue
                try:
                    quantidade = float(val)
                    if quantidade <= 0:
                        continue
                    if quantidade > 50:
                        quantidade = quantidade / 1000.0
                    data_val = pd.to_datetime(col).date()
                    if (data_val, sabor) in existing_set:
                        skipped += 1
                        continue
                    db.add_stock_gelado(data_val, loja, sabor, round(quantidade, 3), tipo_pesagem)
                    existing_set.add((data_val, sabor))
                    imported += 1
                except:
                    continue

        flash(f'{imported} registos importados com sucesso! ({skipped} já existentes, ignorados)', 'success')
        if imported > 0:
            invalidate_prefix('kpi_annual')
            invalidate_prefix('kpi_monthly')
            invalidate_prefix('kpi_by_day')
    except Exception as e:
        flash(f'Erro ao processar ficheiro: {str(e)}', 'error')

    return redirect(url_for('gestor.upload_pesagem', loja=loja))


@gestor_bp.route('/vendas-detalhe', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def vendas_detalhe():
    tabs = get_tabs()
    loja_map_vendas = {'1': 'Matosinhos', '3': 'Bolhão'}

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'upload_vendas':
            return _handle_upload_vendas(loja_map_vendas)
        elif action == 'delete_vendas':
            return _handle_delete_vendas()

    loja_hist = request.args.get('loja_hist', 'Todas')
    loja_query = None if loja_hist == 'Todas' else loja_hist
    export_all = request.args.get('export_all') == '1'
    query_limit = None if export_all else 500
    _cols = {'id', 'data', 'loja', 'produto', 'categoria', 'quantidade', 'valor_euros'}
    vendas_list = [{k: v for k, v in r.items() if k in _cols}
                   for r in db.get_vendas_detalhe_df(loja_query, limit=query_limit)]

    return render_template('gestor/vendas_detalhe.html',
                           active_tab='vendas_detalhe', tabs=tabs,
                           vendas_list=vendas_list,
                           loja_hist=loja_hist,
                           export_all=export_all,
                           default_limit=500)


def _handle_upload_vendas(loja_map_vendas):
    uploaded_file = request.files.get('vendas_file')
    if not uploaded_file or uploaded_file.filename == '':
        flash('Por favor, selecione um ficheiro.', 'error')
        return redirect(url_for('gestor.vendas_detalhe'))

    file_name = uploaded_file.filename.lower()
    try:
        if file_name.endswith('.xlsx'):
            imported, skipped = gestor_svc.import_vendas_xlsx(uploaded_file, loja_map_vendas)
            db.sync_produtos_vendas_config()
            msg = f'{imported} registos importados com sucesso!'
            if skipped > 0:
                msg += f' ({skipped} ignorados sem loja identificada)'
            flash(msg, 'success')

        elif file_name.endswith('.csv'):
            loja_csv = request.form.get('loja_csv', 'Matosinhos')
            imported = gestor_svc.import_vendas_csv(uploaded_file, loja_csv)
            db.sync_produtos_vendas_config()
            flash(f'{imported} registos de vendas detalhadas importados com sucesso!', 'success')

        else:
            imported, skipped = gestor_svc.import_vendas_html(uploaded_file, loja_map_vendas)
            db.sync_produtos_vendas_config()
            msg = f'{imported} registos importados com sucesso!'
            if skipped > 0:
                msg += f' ({skipped} ignorados sem loja identificada)'
            flash(msg, 'success')

        invalidate_prefix('kpi_annual')
        invalidate_prefix('kpi_monthly')
        invalidate_prefix('kpi_by_day')

    except ServiceError as e:
        flash(str(e), 'error')
    except Exception as e:
        flash(f'Erro ao ler ficheiro: {str(e)}', 'error')

    return redirect(url_for('gestor.vendas_detalhe'))


def _handle_delete_vendas():
    data_inicio_str = request.form.get('del_data_inicio')
    data_fim_str = request.form.get('del_data_fim')
    loja = request.form.get('del_loja')
    confirm = request.form.get('confirm_delete', '').strip().upper()

    if confirm != 'ELIMINAR':
        flash('Por favor, escreva ELIMINAR na caixa de confirmação.', 'warning')
        return redirect(url_for('gestor.vendas_detalhe'))

    try:
        data_inicio = datetime.strptime(data_inicio_str, '%Y-%m-%d').date()
        data_fim = datetime.strptime(data_fim_str, '%Y-%m-%d').date()
        loja_del = None if loja == 'Todas' else loja
        deleted = db.delete_vendas_detalhe_by_dates(data_inicio, data_fim, loja_del)
        if deleted > 0:
            flash(f'{deleted} registos eliminados com sucesso!', 'success')
        else:
            flash('Nenhum registo encontrado para eliminar.', 'warning')
    except Exception as e:
        flash(f'Erro: {str(e)}', 'error')

    return redirect(url_for('gestor.vendas_detalhe'))


@gestor_bp.route('/regras-negocio', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def regras_negocio():
    if request.method == 'POST':
        return _handle_config_post(request.form.get('action', ''), 'regras_negocio')
    db.sync_produtos_vendas_config()
    return render_template('gestor/regras_negocio.html',
                           produtos_config=db.get_produtos_vendas_config())


@gestor_bp.route('/ajustes-producao', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def ajustes_producao():
    if request.method == 'POST':
        return _handle_config_post(request.form.get('action', ''), 'ajustes_producao')
    current_year = date.today().year
    meses_nomes = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
                   'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']
    return render_template('gestor/ajustes_producao.html',
                           ajustes=db.get_ajustes_producao(current_year),
                           current_year=current_year, meses_nomes=meses_nomes)


@gestor_bp.route('/motivos-quebra', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def motivos_quebra():
    if request.method == 'POST':
        return _handle_config_post(request.form.get('action', ''), 'motivos_quebra')
    return render_template('gestor/motivos_quebra.html',
                           motivos=db.get_all_motivos_quebra())


@gestor_bp.route('/coberturas')
@perm_required('acesso_gestor')
def coberturas():
    return redirect(url_for('pastelaria.coberturas'))


@gestor_bp.route('/tipologias-pastelaria')
@perm_required('acesso_gestor')
def tipologias_pastelaria():
    return redirect(url_for('pastelaria.tipologias'))


@gestor_bp.route('/produtos-pastelaria')
@perm_required('acesso_gestor')
def produtos_pastelaria():
    return redirect(url_for('pastelaria.produtos'))


@gestor_bp.route('/produtos-confeitaria')
@perm_required('acesso_gestor')
def produtos_confeitaria():
    return redirect(url_for('confeitaria.produtos'))


@gestor_bp.route('/produtos-rececao', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def produtos_rececao():
    if request.method == 'POST':
        return _handle_config_post(request.form.get('action', ''), 'produtos_rececao')
    return render_template('gestor/produtos_rececao.html',
                           produtos_rec=db.get_all_produtos_rececao())


@gestor_bp.route('/receitas-eurokg', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def receitas_eurokg():
    if request.method == 'POST':
        return _handle_config_post(request.form.get('action', ''), 'receitas_eurokg')
    receitas = db.get_all_receitas_gelado()
    com_sabor = [r for r in receitas if r.get('nome_corrente')]
    sem_sabor = [r for r in receitas if not r.get('nome_corrente')]
    return render_template('gestor/receitas_eurokg.html',
                           receitas_com_sabor=com_sabor,
                           receitas_sem_sabor=sem_sabor)


@gestor_bp.route('/gestao-utilizadores', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def gestao_utilizadores():
    if request.method == 'POST':
        return _handle_config_post(request.form.get('action', ''), 'gestao_utilizadores')
    return render_template('gestor/gestao_utilizadores.html',
                           users=db.get_all_users(),
                           lojas_venda=db.get_active_venda_stores())


def _handle_config_post(action, config_option):
    section = request.form.get('section', config_option)

    if action == 'save_regras':
        produtos_config = db.get_produtos_vendas_config()
        updates = []
        for p in produtos_config:
            pid = p['id']
            updates.append({
                'id': pid,
                'gelado_kpi': request.form.get(f'gelado_kpi_{pid}') == 'on',
                'pastelaria': request.form.get(f'pastelaria_{pid}') == 'on',
                'confeitaria': request.form.get(f'confeitaria_{pid}') == 'on',
            })
        db.update_produtos_vendas_config_batch(updates)
        flash('Alocação de Produtos atualizada com sucesso!', 'success')

    elif action == 'add_ajuste':
        mes = int(request.form.get('ajuste_mes', 1))
        kg = parse_decimal_input(request.form.get('ajuste_kg'))
        desc = request.form.get('ajuste_desc', '').strip() or None
        if kg > 0:
            current_year = date.today().year
            db.add_ajuste_producao(current_year, mes, kg, desc)
            meses = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
                     'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']
            flash(f'Ajuste de -{kg:.2f} kg adicionado a {meses[mes - 1]}!', 'success')
        else:
            flash('Insira uma quantidade válida.', 'warning')

    elif action == 'delete_ajuste':
        ajuste_id = int(request.form.get('ajuste_id'))
        db.delete_ajuste_producao(ajuste_id)
        flash('Ajuste eliminado!', 'success')

    elif action == 'add_motivo':
        nome = request.form.get('novo_motivo', '').strip()
        if nome:
            success = db.add_motivo_quebra(nome)
            flash(f"Motivo '{nome}' adicionado!" if success else 'Motivo já existe.', 'success' if success else 'warning')
        else:
            flash('Por favor, insira um nome.', 'warning')

    elif action == 'delete_motivo':
        mid = int(request.form.get('motivo_id'))
        db.delete_motivo_quebra(mid)
        flash('Motivo eliminado!', 'success')

    elif action == 'add_cobertura':
        nome = request.form.get('nova_cobertura', '').strip()
        if nome:
            success = db.add_cobertura(nome)
            flash(f"Cobertura '{nome}' adicionada!" if success else 'Cobertura já existe.', 'success' if success else 'warning')
        else:
            flash('Por favor, insira um nome.', 'warning')

    elif action == 'delete_cobertura':
        cid = int(request.form.get('cobertura_id'))
        db.delete_cobertura(cid)
        flash('Cobertura eliminada!', 'success')

    elif action == 'add_tipologia':
        nome = request.form.get('nova_tipologia', '').strip()
        if nome:
            success = db.add_tipologia_pastelaria(nome)
            flash(f"Tipologia '{nome}' adicionada!" if success else 'Tipologia já existe.', 'success' if success else 'warning')
        else:
            flash('Por favor, insira um nome.', 'warning')

    elif action == 'delete_tipologia':
        tid = int(request.form.get('tipologia_id'))
        db.delete_tipologia_pastelaria(tid)
        flash('Tipologia eliminada!', 'success')

    elif action == 'save_produtos_past':
        produtos_past = db.get_all_produtos_pastelaria()
        for p in produtos_past:
            pid = p['id']
            tip_new = request.form.get(f'tipologia_{pid}', '')
            sabor_new = request.form.get(f'sabor_{pid}', '')
            cob_new = request.form.get(f'cobertura_{pid}', '')
            tip_orig = p.get('tipologia', '')
            sabor_orig = p.get('sabor', '') or ''
            cob_orig = p.get('cobertura', '') or ''
            if tip_new != tip_orig or sabor_new != sabor_orig or cob_new != cob_orig:
                db.update_produto_pastelaria(pid, tip_new, sabor_new, cob_new)
        flash('Alterações guardadas!', 'success')

    elif action == 'add_produto_past':
        tip = request.form.get('novo_tipologia', '')
        sabor = request.form.get('novo_sabor_past', '')
        cob = request.form.get('novo_cob_past', '')
        if tip:
            success = db.add_produto_pastelaria(tip, sabor, cob)
            flash('Produto adicionado!' if success else 'Produto já existe.', 'success' if success else 'warning')
        else:
            flash('Por favor, selecione uma tipologia.', 'warning')

    elif action == 'delete_produto_past':
        pid = int(request.form.get('produto_past_id'))
        db.delete_produto_pastelaria(pid)
        flash('Produto eliminado!', 'success')

    elif action == 'save_gelado_tip':
        gelado_tip = db.get_gelado_por_tipologia()
        for item in gelado_tip:
            gid = item['id']
            tip_new = request.form.get(f'gelado_tip_nome_{gid}', '')
            qtd_new = parse_decimal_input(request.form.get(f'gelado_tip_qtd_{gid}'))
            if tip_new != item['tipologia'] or qtd_new != item['quantidade_gelado_g']:
                db.update_gelado_por_tipologia(gid, tip_new, qtd_new)
        flash('Alterações guardadas!', 'success')

    elif action == 'add_gelado_tip':
        tip = request.form.get('nova_tip_gelado', '').strip()
        qtd = parse_decimal_input(request.form.get('nova_qtd_gelado'))
        if tip:
            success = db.add_gelado_por_tipologia(tip, qtd)
            flash(f"Tipologia '{tip}' adicionada!" if success else 'Tipologia já existe.', 'success' if success else 'warning')
        else:
            flash('Por favor, insira uma tipologia.', 'warning')

    elif action == 'delete_gelado_tip':
        gid = int(request.form.get('gelado_tip_id'))
        db.delete_gelado_por_tipologia(gid)
        flash('Tipologia eliminada!', 'success')

    elif action == 'add_produto_conf':
        nome = request.form.get('novo_prod_conf', '').strip()
        if nome:
            success = db.add_produto_confeitaria(nome)
            flash(f"Produto '{nome}' adicionado!" if success else 'Produto já existe.', 'success' if success else 'warning')
        else:
            flash('Por favor, insira um nome.', 'warning')

    elif action == 'delete_produto_conf':
        pid = int(request.form.get('produto_conf_id'))
        db.delete_produto_confeitaria(pid)
        flash('Produto eliminado!', 'success')

    elif action == 'add_produto_rec':
        nome = request.form.get('novo_prod_rec', '').strip()
        tipo = request.form.get('tipo_prod_rec', 'embalagem')
        if nome:
            success = db.add_produto_rececao(nome, tipo)
            flash(f"Produto '{nome}' adicionado!" if success else 'Produto já existe.', 'success' if success else 'warning')
        else:
            flash('Por favor, insira um nome.', 'warning')

    elif action == 'delete_produto_rec':
        pid = int(request.form.get('produto_rec_id'))
        db.delete_produto_rececao(pid)
        flash('Produto eliminado!', 'success')

    elif action == 'save_receitas':
        receitas = db.get_all_receitas_gelado()
        for r in receitas:
            rid = r['id']
            new_nome_corrente = request.form.get(f'sabor_{rid}', '').strip() or None
            new_ativo = request.form.get(f'ativo_{rid}') == 'on'
            new_conta_eurokg = request.form.get(f'conta_eurokg_{rid}') == 'on'
            old_nome_corrente = r.get('nome_corrente') or None
            old_ativo = r.get('ativo', True)
            old_conta_eurokg = r.get('conta_eurokg', True)
            if new_nome_corrente != old_nome_corrente:
                db.update_receita_gelado(rid, r['nome'], new_nome_corrente)
            if new_ativo != old_ativo:
                db.update_receita_gelado_ativo(rid, new_ativo)
            if new_conta_eurokg != old_conta_eurokg:
                db.update_receita_gelado_conta_eurokg(rid, new_conta_eurokg)
        flash('Receitas atualizadas com sucesso!', 'success')

    elif action == 'add_receita':
        nome = request.form.get('nova_receita', '').strip()
        nome_corrente = request.form.get('novo_sabor', '').strip() or None
        if nome:
            db.add_receita_gelado(nome, nome_corrente)
            flash(f"Receita '{nome}' adicionada!", 'success')
        else:
            flash('Por favor, insira o nome da receita.', 'warning')

    elif action == 'delete_receita':
        rid = int(request.form.get('receita_id'))
        db.delete_receita_gelado(rid)
        flash('Receita eliminada!', 'success')

    elif action == 'save_users_perms':
        users = db.get_all_users()
        lojas_venda = db.get_active_venda_stores()
        loja_ids = [loja['id'] for loja in lojas_venda]
        updates = []
        for u in users:
            uid = u['id']
            vendas_store_ids = [
                sid for sid in loja_ids
                if request.form.get(f'venda_loja_{sid}_{uid}') == 'on'
            ]
            updates.append({
                'id': uid,
                'acesso_eurokg': request.form.get(f'acesso_eurokg_{uid}') == 'on',
                'acesso_producao': request.form.get(f'acesso_producao_{uid}') == 'on',
                'acesso_pastelaria': request.form.get(f'acesso_pastelaria_{uid}') == 'on',
                'acesso_confeitaria': request.form.get(f'acesso_confeitaria_{uid}') == 'on',
                'acesso_administrativo': request.form.get(f'acesso_administrativo_{uid}') == 'on',
                'acesso_gestor': request.form.get(f'acesso_gestor_{uid}') == 'on',
                'acesso_financeiro': request.form.get(f'acesso_financeiro_{uid}') == 'on',
                'acesso_eventos': request.form.get(f'acesso_eventos_{uid}') == 'on',
                'ativo': request.form.get(f'ativo_{uid}') == 'on',
                'vendas_store_ids': vendas_store_ids,
            })
        db.update_user_permissoes_batch(updates)
        flash('Permissões atualizadas com sucesso!', 'success')

    elif action == 'add_user':
        username = request.form.get('novo_username', '').strip()
        password = request.form.get('nova_password', '')
        if username and password:
            success = db.add_user(username, password)
            if success:
                flash(f"Utilizador '{username}' criado! Configure os acessos na tabela acima.", 'success')
            else:
                flash('Utilizador já existe.', 'warning')
        else:
            flash('Preencha o utilizador e a password.', 'warning')

    elif action == 'change_password':
        user_id = int(request.form.get('user_id'))
        new_pass = request.form.get('new_password', '')
        if new_pass:
            db.update_user_password(user_id, new_pass)
            flash('Password alterada com sucesso!', 'success')
        else:
            flash('Introduza a nova password.', 'warning')

    endpoint = SECTION_ENDPOINT_MAP.get(section, 'gestor.index')
    return redirect(url_for(endpoint))


@gestor_bp.route('/premio-eurokg')
@perm_required('acesso_gestor')
def premio_eurokg():
    from calendar import monthrange
    from datetime import date as _date

    today = _date.today()
    ano = int(request.args.get('ano', today.year))
    loja = request.args.get('loja', 'Porto (Global)')

    meses_nomes = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
                   'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']

    dados_meses = []
    erro = None

    try:
        for mes in range(1, 13):
            kpi = db.calculate_kpi_monthly(ano, mes, loja)
            target = db.get_target_by_month(mes)

            consumo = kpi['consumo']
            vendas = kpi['vendas']
            vendas_teoricas = round(consumo * target, 2) if consumo > 0 else 0.0
            ganho_extra = round(vendas - vendas_teoricas, 2)
            premio = round(max(0.0, ganho_extra * 0.10), 2)

            is_future = (ano > today.year) or (ano == today.year and mes >= today.month)

            dados_meses.append({
                'mes': mes,
                'mes_nome': meses_nomes[mes - 1],
                'consumo': round(consumo, 3),
                'vendas': round(vendas, 2),
                'target': round(target, 2),
                'vendas_teoricas': round(vendas_teoricas, 2),
                'ganho_extra': round(ganho_extra, 2),
                'premio': round(premio, 2),
                'is_future': is_future,
                'kpi_real': round(kpi['kpi'], 2),
                'colaboradores_elegiveis': 0,
                'premio_por_colaborador': 0.0,
            })
    except Exception as e:
        erro = str(e)

    return render_template('gestor/premio_eurokg.html',
                           dados_meses=dados_meses,
                           erro=erro,
                           ano_sel=ano,
                           loja_sel=loja,
                           meses_nomes=meses_nomes,
                           back_url=url_for('gestor.eurokg_index'))


@gestor_bp.route('/gestao-lojas', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def gestao_lojas():
    tabs = get_tabs()
    msg = None
    erro = None
    edit_store = None

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'upsert_store':
            store_id = request.form.get('store_id') or None
            if store_id:
                store_id = int(store_id)
            name = request.form.get('name', '').strip()
            address = request.form.get('address', '').strip() or None
            latitude = request.form.get('latitude', '').strip() or None
            longitude = request.form.get('longitude', '').strip() or None
            store_type = request.form.get('store_type', 'loja').strip()
            is_active = request.form.get('is_active') == '1'
            receives_transfers = request.form.get('receives_transfers') == '1'
            requires_eod_weighing = request.form.get('requires_eod_weighing') == '1'
            shows_on_landing = request.form.get('shows_on_landing') == '1'
            pos_store_code = request.form.get('pos_store_code', '').strip() or None
            opened_at = request.form.get('opened_at', '').strip() or None

            if latitude:
                try:
                    latitude = float(latitude)
                except ValueError:
                    latitude = None
            if longitude:
                try:
                    longitude = float(longitude)
                except ValueError:
                    longitude = None

            if not name:
                erro = 'O nome da loja é obrigatório.'
            else:
                ok = db.upsert_store(store_id, name, address, latitude, longitude,
                                     store_type, is_active, receives_transfers,
                                     requires_eod_weighing, pos_store_code, opened_at,
                                     shows_on_landing)
                if ok:
                    msg = 'Loja guardada com sucesso.'
                else:
                    erro = 'Erro ao guardar loja. O nome pode já existir.'

        elif action == 'toggle_active':
            store_id = int(request.form.get('store_id'))
            is_active = request.form.get('is_active') == '1'
            db.toggle_store_active(store_id, is_active)
            msg = 'Estado da loja actualizado.'

        elif action == 'delete_store':
            store_id = int(request.form.get('store_id'))
            db.delete_store(store_id)
            msg = 'Loja eliminada.'

        return redirect(url_for('gestor.gestao_lojas', msg=msg, erro=erro))

    msg = request.args.get('msg')
    erro = request.args.get('erro')
    edit_id = request.args.get('edit')
    edit_store = None
    edit_store_aliases = []
    if edit_id:
        edit_store = db.get_store(int(edit_id))
        if edit_store:
            edit_store_aliases = db.get_store_aliases(int(edit_id))

    stores = db.get_all_stores()
    return render_template('gestor/gestao_lojas.html',
                           tabs=tabs,
                           stores=stores,
                           edit_store=edit_store,
                           edit_store_aliases=edit_store_aliases,
                           msg=msg,
                           erro=erro,
                           active_tab='gestao_lojas',
                           back_url=url_for('gestor.index'))


@gestor_bp.route('/gestao-lojas/<int:store_id>/aliases', methods=['POST'])
@perm_required('acesso_gestor')
def add_store_alias(store_id):
    alias_name = request.form.get('alias_name', '').strip() or None
    alias_code = request.form.get('alias_code', '').strip() or None
    if alias_name or alias_code:
        db.add_store_alias(store_id, alias_name, alias_code)
        flash('Alias adicionado com sucesso.', 'success')
    else:
        flash('Indique pelo menos um nome alternativo ou código alternativo.', 'warning')
    return redirect(url_for('gestor.gestao_lojas', edit=store_id))


@gestor_bp.route('/gestao-lojas/<int:store_id>/aliases/<int:alias_id>/delete', methods=['POST', 'DELETE'])
@perm_required('acesso_gestor')
def delete_store_alias(store_id, alias_id):
    db.delete_store_alias(alias_id, store_id=store_id)
    flash('Alias removido.', 'success')
    return redirect(url_for('gestor.gestao_lojas', edit=store_id))


@gestor_bp.route('/metodos-pagamento', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def metodos_pagamento():
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'save':
            metodo = request.form.get('metodo', '').strip()
            label = request.form.get('label', '').strip()
            ativo = request.form.get('ativo') == 'on'
            try:
                taxa_raw = request.form.get('taxa_percentagem', '').strip()
                taxa = float(taxa_raw.replace(',', '.')) if taxa_raw else None
            except ValueError:
                taxa = None
            try:
                prazo_raw = request.form.get('prazo_dias', '').strip()
                prazo = int(prazo_raw) if prazo_raw else None
            except ValueError:
                prazo = None
            notas = request.form.get('notas', '').strip() or None
            if not metodo or not label:
                flash('Método e label são obrigatórios.', 'warning')
            else:
                try:
                    upsert_payment_method_config(metodo, label, ativo, taxa, prazo, notas)
                    flash(f'Método «{label}» guardado.', 'success')
                except Exception as e:
                    logger.error('Erro ao guardar método pagamento: %s', e)
                    flash('Não foi possível guardar. Tente novamente.', 'error')
        return redirect(url_for('gestor.metodos_pagamento'))

    metodos = get_payment_methods_config()
    return render_template('gestor/metodos_pagamento.html', metodos=metodos)


@gestor_bp.route('/configuracoes', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def configuracoes():
    _ONEDRIVE_DEFAULT_FOLDER = 'Niva Porto/2. Contabilidade/Registo de Faturas'
    _ONEDRIVE_OLD_DEFAULT = 'NivaPorto/Faturas'

    if request.method == 'POST':
        folder = request.form.get('onedrive_folder', '').strip()
        db.set_system_config('onedrive_folder', folder or _ONEDRIVE_DEFAULT_FOLDER)
        flash('Configuração guardada com sucesso.', 'success')
        return redirect(url_for('gestor.configuracoes'))

    onedrive_user = db.get_system_config('onedrive_user_email') or ''
    onedrive_token = db.get_system_config('onedrive_refresh_token') or ''
    _stored_folder = (db.get_system_config('onedrive_folder') or '').strip('/')
    if not _stored_folder or _stored_folder == _ONEDRIVE_OLD_DEFAULT:
        onedrive_folder = _ONEDRIVE_DEFAULT_FOLDER
        try:
            db.set_system_config('onedrive_folder', _ONEDRIVE_DEFAULT_FOLDER)
        except Exception:
            pass
    else:
        onedrive_folder = _stored_folder
    onedrive_connected = bool(onedrive_token)

    redirect_uri = url_for('gestor.onedrive_callback', _external=True)

    return render_template('gestor/configuracoes.html',
                           active_tab='configuracoes',
                           onedrive_connected=onedrive_connected,
                           onedrive_user=onedrive_user,
                           onedrive_folder=onedrive_folder,
                           redirect_uri=redirect_uri,
                           back_url=url_for('gestor.index'))


@gestor_bp.route('/configuracoes/onedrive-autorizar')
@perm_required('acesso_gestor')
def onedrive_autorizar():
    import secrets as _secrets
    from urllib.parse import urlencode

    client_id = os.environ.get('AZURE_CLIENT_ID', '')
    if not client_id:
        flash('AZURE_CLIENT_ID não configurado nos secrets do Replit.', 'danger')
        return redirect(url_for('gestor.configuracoes'))

    state = _secrets.token_urlsafe(24)
    session['_onedrive_state'] = state

    redirect_uri = url_for('gestor.onedrive_callback', _external=True)

    params = urlencode({
        'client_id': client_id,
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'scope': 'Files.ReadWrite offline_access User.Read',
        'state': state,
        'prompt': 'select_account',
    })
    auth_url = f'https://login.microsoftonline.com/common/oauth2/v2.0/authorize?{params}'
    return redirect(auth_url)


@gestor_bp.route('/onedrive/callback')
@perm_required('acesso_gestor')
def onedrive_callback():
    import requests as _requests

    error = request.args.get('error')
    if error:
        desc = request.args.get('error_description', error)
        flash(f'Autorização recusada: {desc}', 'danger')
        return redirect(url_for('gestor.configuracoes'))

    state = request.args.get('state', '')
    if state != session.pop('_onedrive_state', None):
        flash('Estado inválido — tente novamente.', 'danger')
        return redirect(url_for('gestor.configuracoes'))

    code = request.args.get('code', '')
    if not code:
        flash('Código de autorização em falta.', 'danger')
        return redirect(url_for('gestor.configuracoes'))

    redirect_uri = url_for('gestor.onedrive_callback', _external=True)
    client_id = os.environ.get('AZURE_CLIENT_ID', '')
    client_secret = os.environ.get('AZURE_CLIENT_SECRET', '')

    token_resp = _requests.post(
        'https://login.microsoftonline.com/common/oauth2/v2.0/token',
        data={
            'grant_type': 'authorization_code',
            'client_id': client_id,
            'client_secret': client_secret,
            'code': code,
            'redirect_uri': redirect_uri,
            'scope': 'Files.ReadWrite offline_access User.Read',
        },
        timeout=20,
    )

    if not token_resp.ok:
        flash(f'Erro ao obter tokens: {token_resp.status_code} {token_resp.text[:200]}', 'danger')
        return redirect(url_for('gestor.configuracoes'))

    token_data = token_resp.json()
    refresh_token = token_data.get('refresh_token', '')
    access_token = token_data.get('access_token', '')

    if not refresh_token:
        flash('Refresh token não recebido. Certifique-se que o scope "offline_access" está autorizado.', 'danger')
        return redirect(url_for('gestor.configuracoes'))

    user_email = ''
    if access_token:
        try:
            me_resp = _requests.get(
                'https://graph.microsoft.com/v1.0/me',
                headers={'Authorization': f'Bearer {access_token}'},
                timeout=10,
            )
            if me_resp.ok:
                user_email = me_resp.json().get('userPrincipalName') or me_resp.json().get('mail', '')
        except Exception as e:
            logger.warning("Could not fetch user email after OAuth: %s", e)

    db.set_system_config('onedrive_refresh_token', refresh_token)
    db.set_system_config('onedrive_user_email', user_email)

    flash(f'OneDrive autorizado com sucesso! Conta: {user_email or "desconhecida"}', 'success')
    return redirect(url_for('gestor.configuracoes'))


@gestor_bp.route('/configuracoes/onedrive-desligar', methods=['POST'])
@perm_required('acesso_gestor')
def onedrive_desligar():
    refresh_token = db.get_system_config('onedrive_refresh_token') or ''

    if refresh_token:
        try:
            import requests as _requests
            _requests.post(
                'https://login.microsoftonline.com/common/oauth2/v2.0/logout',
                data={
                    'client_id': os.environ.get('AZURE_CLIENT_ID', ''),
                    'client_secret': os.environ.get('AZURE_CLIENT_SECRET', ''),
                    'token': refresh_token,
                    'token_type_hint': 'refresh_token',
                },
                timeout=10,
            )
        except Exception as e:
            logger.warning("Best-effort OneDrive token revocation failed (ignoring): %s", e)

    db.set_system_config('onedrive_refresh_token', None)
    db.set_system_config('onedrive_user_email', None)
    flash('Ligação OneDrive removida.', 'warning')
    return redirect(url_for('gestor.configuracoes'))


@gestor_bp.route('/configuracoes/testar-onedrive', methods=['POST'])
@perm_required('acesso_gestor')
def testar_onedrive():
    from flask_app.onedrive_archive import test_connection
    result = test_connection()
    return jsonify(result)


# ── Catálogo de Materiais ──────────────────────────────────────────────────────

@gestor_bp.route('/materiais', methods=['GET'])
@perm_required('acesso_gestor')
def materiais():
    from db.materiais import list_materiais, CATEGORIAS_MATERIAIS, UNIDADES_MATERIAIS
    lista = list_materiais()
    por_categoria = {}
    for m in lista:
        cat = m['categoria']
        por_categoria.setdefault(cat, []).append(m)
    return render_template(
        'gestor/materiais.html',
        tabs=get_tabs(),
        active_tab='materiais',
        materiais=lista,
        por_categoria=por_categoria,
        categorias=CATEGORIAS_MATERIAIS,
        unidades=UNIDADES_MATERIAIS,
    )


@gestor_bp.route('/materiais', methods=['POST'])
@perm_required('acesso_gestor')
def materiais_post():
    from db.materiais import (upsert_material, toggle_material_ativo,
                               delete_material, CATEGORIAS_MATERIAIS, UNIDADES_MATERIAIS)
    action = request.form.get('action', '')

    if action == 'add_material':
        nome = request.form.get('nome', '').strip()
        unidade = request.form.get('unidade', 'un')
        categoria = request.form.get('categoria', 'Outro')
        fornecedor = request.form.get('fornecedor', '').strip() or None
        if not nome:
            flash('O nome do material é obrigatório.', 'warning')
        elif unidade not in UNIDADES_MATERIAIS:
            flash('Unidade inválida.', 'warning')
        elif categoria not in CATEGORIAS_MATERIAIS:
            flash('Categoria inválida.', 'warning')
        else:
            upsert_material(nome, unidade, categoria, fornecedor)
            flash(f"Material '{nome}' adicionado com sucesso!", 'success')

    elif action == 'edit_material':
        mid = int(request.form.get('material_id', 0))
        nome = request.form.get('nome', '').strip()
        unidade = request.form.get('unidade', 'un')
        categoria = request.form.get('categoria', 'Outro')
        fornecedor = request.form.get('fornecedor', '').strip() or None
        if not nome:
            flash('O nome do material é obrigatório.', 'warning')
        else:
            upsert_material(nome, unidade, categoria, fornecedor, material_id=mid)
            flash(f"Material '{nome}' atualizado!", 'success')

    elif action == 'toggle_ativo':
        mid = int(request.form.get('material_id', 0))
        ativo = request.form.get('ativo') == 'true'
        toggle_material_ativo(mid, ativo)
        estado = 'ativado' if ativo else 'desativado'
        flash(f'Material {estado}.', 'success')

    elif action == 'delete_material':
        mid = int(request.form.get('material_id', 0))
        ok = delete_material(mid)
        if ok:
            flash('Material eliminado.', 'success')
        else:
            flash('Não foi possível eliminar (material em uso?).', 'danger')

    return redirect(url_for('gestor.materiais'))
