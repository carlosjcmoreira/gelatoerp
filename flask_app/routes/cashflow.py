"""M3: Cash Flow 13 Semanas — Flask blueprint."""
import json
import logging
from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from flask_app.auth import perm_required
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from db.cashflow import build_cashflow_13weeks, get_cashflow_config, set_cashflow_config
from db.faturas import ONEDRIVE_SUBFOLDERS

logger = logging.getLogger(__name__)

cashflow_bp = Blueprint('cashflow', __name__)


@cashflow_bp.route('/')
@perm_required('acesso_gestor')
def index():
    data = build_cashflow_13weeks()
    return render_template('financeiro/cashflow/index.html',
                           weeks=data['weeks'],
                           alerts=data['alerts'],
                           config=data['config'],
                           threshold=data['threshold'],
                           overdraft_total_plafond=data['overdraft_total_plafond'],
                           overdraft_utilizado=data['overdraft_utilizado'],
                           overdraft_disponivel=data['overdraft_disponivel'],
                           today=data['today'])


@cashflow_bp.route('/semana/<int:week_num>')
@perm_required('acesso_gestor')
def semana(week_num):
    data = build_cashflow_13weeks()
    weeks = data['weeks']
    if week_num < 1 or week_num > len(weeks):
        flash('Semana inválida.', 'warning')
        return redirect(url_for('cashflow.index'))
    week = weeks[week_num - 1]
    return render_template('financeiro/cashflow/semana.html',
                           week=week,
                           week_num=week_num,
                           alerts=[a for a in data['alerts'] if a.get('semana') == week_num],
                           threshold=data['threshold'],
                           overdraft_disponivel=data['overdraft_disponivel'],
                           today=data['today'])


@cashflow_bp.route('/configuracoes', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def configuracoes():
    if request.method == 'POST':
        threshold_str = request.form.get('alert_threshold_eur', '2000').replace(',', '.')
        try:
            threshold = float(threshold_str)
            set_cashflow_config('alert_threshold_eur', str(threshold))
            flash(f'Threshold de alerta actualizado: {threshold:,.0f}€', 'success')
        except (ValueError, TypeError):
            flash('Valor inválido para threshold.', 'danger')
        return redirect(url_for('cashflow.configuracoes'))

    cfg = get_cashflow_config()
    return render_template('financeiro/cashflow/configuracoes.html', cfg=cfg)


@cashflow_bp.route('/salarios', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def salarios():
    SS_TRAB   = 0.11
    SS_PATR   = 0.2375

    if request.method == 'POST':
        sal_imp_dia = request.form.get('salarios_impostos_dia', '15')
        sal_liq_dia = request.form.get('salarios_liquido_dia', '28')
        nomes        = request.form.getlist('colab_nome[]')
        brutos_lst   = request.form.getlist('colab_bruto[]')
        premios_lst  = request.form.getlist('colab_premio_bruto[]')
        irs_lst      = request.form.getlist('colab_irs_taxa[]')
        try:
            colaboradores = []
            total_liq     = 0.0
            total_imp     = 0.0
            for nome, bruto_s, premio_s, irs_s in zip(nomes, brutos_lst, premios_lst, irs_lst):
                nome = nome.strip()
                if not nome:
                    continue
                bruto      = float(bruto_s.replace(',', '.')  or 0)
                premio     = float(premio_s.replace(',', '.') or 0)
                irs_taxa   = min(100.0, max(0.0, float(irs_s.replace(',', '.') or 0)))
                total_bruto    = bruto + premio
                irs_frac       = irs_taxa / 100.0
                ss_trab        = round(total_bruto * SS_TRAB, 2)
                ss_patr        = round(total_bruto * SS_PATR, 2)
                irs_retido     = round(total_bruto * irs_frac, 2)
                liq            = round(total_bruto * (1 - SS_TRAB - irs_frac), 2)
                impostos_d15   = round(ss_trab + ss_patr + irs_retido, 2)
                custo_empresa  = round(total_bruto * (1 + SS_PATR), 2)
                colaboradores.append({
                    'nome': nome,
                    'salario_bruto':   bruto,
                    'premio_bruto':    premio,
                    'irs_taxa':        irs_taxa,
                    'salario_liq':     liq,
                    'ss_patronal':     ss_patr,
                    'ss_trabalhador':  ss_trab,
                    'irs_retido':      irs_retido,
                    'impostos_dia15':  impostos_d15,
                    'custo_empresa':   custo_empresa,
                })
                total_liq += liq
                total_imp += impostos_d15
            set_cashflow_config('salarios_colaboradores', json.dumps(colaboradores, ensure_ascii=False))
            set_cashflow_config('salarios_liquido_eur',  str(round(total_liq, 2)))
            set_cashflow_config('salarios_impostos_eur', str(round(total_imp, 2)))
            set_cashflow_config('salarios_impostos_dia', str(int(sal_imp_dia)))
            set_cashflow_config('salarios_liquido_dia',  str(int(sal_liq_dia)))
            flash('Configuração de salários actualizada.', 'success')
        except (ValueError, TypeError):
            flash('Valores inválidos.', 'danger')
        return redirect(url_for('cashflow.salarios'))

    try:
        cfg = get_cashflow_config()
    except Exception:
        flash('Erro ao carregar configuração de salários. Por favor tente novamente.', 'danger')
        cfg = {}
    try:
        colaboradores = json.loads(cfg.get('salarios_colaboradores', '[]'))
    except Exception:
        colaboradores = []

    # Backfill missing fields for collaborators saved before task #47
    backfilled = []
    for c in colaboradores:
        c = dict(c)
        bruto    = float(c.get('salario_bruto', 0) or 0)
        premio   = float(c.get('premio_bruto', 0) or 0)
        irs_taxa = float(c.get('irs_taxa', 0) or 0)
        irs_frac = irs_taxa / 100.0
        total_b  = bruto + premio
        ss_trab  = round(total_b * SS_TRAB, 2)
        ss_patr  = round(total_b * SS_PATR, 2)
        irs_ret  = round(total_b * irs_frac, 2)
        c.setdefault('salario_bruto',  bruto)
        c.setdefault('premio_bruto',   premio)
        c.setdefault('irs_taxa',       irs_taxa)
        c.setdefault('ss_trabalhador', ss_trab)
        c.setdefault('ss_patronal',    ss_patr)
        c.setdefault('irs_retido',     irs_ret)
        c.setdefault('salario_liq',    round(total_b * (1 - SS_TRAB - irs_frac), 2))
        c.setdefault('impostos_dia15', round(ss_trab + ss_patr + irs_ret, 2))
        c.setdefault('custo_empresa',  round(total_b * (1 + SS_PATR), 2))
        backfilled.append(c)
    colaboradores = backfilled

    return render_template('financeiro/cashflow/salarios.html',
                           cfg=cfg,
                           colaboradores=colaboradores,
                           SS_TRAB=SS_TRAB,
                           SS_PATR=SS_PATR)


@cashflow_bp.route('/debitos', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def debitos():
    if request.method == 'POST':
        action = request.form.get('action', 'save_debitos')

        if action == 'save_debitos':
            labels = request.form.getlist('dd_label')
            valores = request.form.getlist('dd_valor')
            dias = request.form.getlist('dd_dia')
            centros = request.form.getlist('dd_centro_custo')
            debitos_list = []
            for lbl, val, dia, cc in zip(labels, valores, dias, centros):
                lbl = lbl.strip()
                cc = cc.strip() or None
                val_str = val.replace(',', '.')
                try:
                    valor = float(val_str)
                    dia_int = int(dia)
                    if lbl and valor > 0 and 1 <= dia_int <= 31:
                        debitos_list.append({
                            'label': lbl,
                            'valor_eur': valor,
                            'dia_debito': dia_int,
                            'centro_custo': cc,
                        })
                except (ValueError, TypeError):
                    pass
            set_cashflow_config('debitos_directos', json.dumps(debitos_list))
            flash(f'{len(debitos_list)} débito(s) directo(s) guardado(s).', 'success')
            return redirect(url_for('cashflow.debitos'))

        elif action == 'upload_invoice':
            pdf_file = request.files.get('invoice_pdf')
            if not pdf_file or not pdf_file.filename:
                return jsonify({'error': 'Nenhum ficheiro enviado.'}), 400
            if not pdf_file.filename.lower().endswith('.pdf'):
                return jsonify({'error': 'Apenas ficheiros PDF são suportados.'}), 400
            try:
                pdf_bytes = pdf_file.read()
                from flask_app.ocr_invoice import _get_anthropic_client
                import base64
                client = _get_anthropic_client()
                if not client:
                    return jsonify({'error': 'Serviço OCR não disponível.'}), 503

                pdf_b64 = base64.standard_b64encode(pdf_bytes).decode('utf-8')

                prompt = """Analisa este documento PDF que é uma factura de serviço público (electricidade, água, gás, telecomunicações, etc.).
Extrai os seguintes campos e devolve APENAS um objeto JSON válido (sem markdown, sem texto extra):

{
  "fornecedor": "nome do fornecedor/empresa",
  "valor_facturado_eur": 0.00,
  "periodo_inicio": "YYYY-MM-DD",
  "periodo_fim": "YYYY-MM-DD",
  "dias_periodo": 30,
  "estimativa_mensal_eur": 0.00
}

Regras:
- valor_facturado_eur: valor total a pagar nesta factura (com IVA se aplicável).
- periodo_inicio e periodo_fim: período de consumo/serviço cobrado nesta factura (não a data de emissão).
- dias_periodo: número de dias cobertos pelo período (calcula a partir das datas se possível).
- estimativa_mensal_eur: valor_facturado_eur dividido por dias_periodo e multiplicado por 30 (estimativa de custo mensal).
- Se um campo não for encontrado, usa null.
- Devolve APENAS o JSON, sem qualquer texto adicional."""

                message = client.messages.create(
                    model="claude-haiku-4-5",
                    max_tokens=512,
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": pdf_b64,
                                },
                            },
                            {
                                "type": "text",
                                "text": prompt
                            }
                        ]
                    }]
                )

                raw_text = message.content[0].text.strip()
                if raw_text.startswith("```"):
                    raw_text = raw_text.split("```")[1]
                    if raw_text.startswith("json"):
                        raw_text = raw_text[4:]
                raw_text = raw_text.strip()

                parsed = json.loads(raw_text)

                def _safe_float(val):
                    if val is None:
                        return None
                    try:
                        return float(str(val).replace(',', '.'))
                    except (ValueError, TypeError):
                        return None

                def _safe_int(val):
                    if val is None:
                        return None
                    try:
                        return int(val)
                    except (ValueError, TypeError):
                        return None

                suggestion = {
                    'fornecedor': parsed.get('fornecedor') or None,
                    'valor_facturado_eur': _safe_float(parsed.get('valor_facturado_eur')),
                    'periodo_inicio': parsed.get('periodo_inicio') or None,
                    'periodo_fim': parsed.get('periodo_fim') or None,
                    'dias_periodo': _safe_int(parsed.get('dias_periodo')),
                    'estimativa_mensal_eur': _safe_float(parsed.get('estimativa_mensal_eur')),
                    'error': None,
                }
                return jsonify(suggestion)

            except json.JSONDecodeError as e:
                logger.error("OCR JSON parse error: %s", e)
                return jsonify({'error': 'Não foi possível interpretar a resposta do OCR.'}), 500
            except Exception as e:
                logger.error("Utility invoice OCR failed: %s", e)
                return jsonify({'error': 'Ocorreu um erro ao processar o ficheiro. Tente novamente.'}), 500

    cfg = get_cashflow_config()
    try:
        debitos_directos = json.loads(cfg.get('debitos_directos', '[]'))
    except Exception:
        debitos_directos = []

    return render_template('financeiro/cashflow/debitos.html',
                           cfg=cfg,
                           debitos_directos=debitos_directos,
                           onedrive_subfolders=ONEDRIVE_SUBFOLDERS)
