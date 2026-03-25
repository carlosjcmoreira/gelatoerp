import os
import sys
import re as _re
import unicodedata as _unicodedata
import pandas as pd
from datetime import datetime, date, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from flask_app.auth import perm_required

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import database as db
import weather_service as ws
import weather_scheduler as wsch

meteorologia_bp = Blueprint('meteorologia', __name__)


def parse_euro_value(raw_val):
    if raw_val is None:
        return 0.0
    if isinstance(raw_val, (int, float)):
        return float(raw_val)
    s = str(raw_val).replace('€', '').replace('\u20ac', '').replace(' ', '').strip()
    if not s:
        return 0.0
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_qty_value(raw_val):
    if raw_val is None:
        return 0.0
    if isinstance(raw_val, (int, float)):
        return float(raw_val)
    s = str(raw_val).replace(' ', '').strip()
    if not s:
        return 0.0
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return 0.0


def _detect_columns(df_det):
    date_col = next((c for c in df_det.columns if c in ['Data', 'Date']), None)
    prod_col = next((c for c in df_det.columns if 'Produto' in c or 'Product' in c or c == 'Descrição'), None)
    qtd_col = next((c for c in df_det.columns if 'Quantidade' in c or 'Qtd' in c), None)
    valor_col = None
    for c in df_det.columns:
        if 'Valor' in c and 'C/IVA' in c.upper():
            valor_col = c
            break
    if not valor_col:
        for c in df_det.columns:
            if 'Valor Total' in c and 'S/IVA' not in c.upper():
                valor_col = c
                break
    if not valor_col:
        for c in df_det.columns:
            if 'Valor' in c:
                valor_col = c
                break
    cat_col = None
    for c in df_det.columns:
        if 'Familia' in c or 'Categoria' in c or 'Família' in c:
            cat_col = c
            break
    return date_col, prod_col, qtd_col, valor_col, cat_col


@meteorologia_bp.route('/', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def index():
    stores = db.get_all_stores()
    active_stores = [s for s in stores if s.get('is_active')]

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'trigger_update':
            ok = wsch.trigger_weather_update_now()
            if ok:
                flash('Actualização meteorológica iniciada em segundo plano!', 'success')
            else:
                flash('Erro ao iniciar actualização.', 'error')
            return redirect(url_for('meteorologia.index'))

        elif action == 'delete_weather':
            store_id = request.form.get('del_store_id') or None
            fonte = request.form.get('del_fonte') or None
            data_inicio_str = request.form.get('del_data_inicio') or None
            data_fim_str = request.form.get('del_data_fim') or None
            try:
                sid = int(store_id) if store_id else None
                di = datetime.strptime(data_inicio_str, '%Y-%m-%d').date() if data_inicio_str else None
                df = datetime.strptime(data_fim_str, '%Y-%m-%d').date() if data_fim_str else None
                deleted = db.delete_weather_data_by_store_fonte(sid, fonte, di, df) if sid else 0
                flash(f'{deleted} registos meteorológicos eliminados.', 'success' if deleted > 0 else 'warning')
            except Exception as e:
                flash(f'Erro ao eliminar: {str(e)}', 'error')
            return redirect(url_for('meteorologia.index'))

    sel_store_id = request.args.get('store_id')
    try:
        sel_store_id = int(sel_store_id) if sel_store_id else (active_stores[0]['id'] if active_stores else None)
    except (ValueError, TypeError):
        sel_store_id = active_stores[0]['id'] if active_stores else None

    today = date.today()
    data_inicio = request.args.get('data_inicio', (today - timedelta(days=3)).isoformat())
    data_fim = request.args.get('data_fim', (today + timedelta(days=16)).isoformat())

    weather_composite = []
    weather_detail = []
    sel_store = None

    if sel_store_id:
        try:
            di = datetime.strptime(data_inicio, '%Y-%m-%d').date()
            df = datetime.strptime(data_fim, '%Y-%m-%d').date()
            weather_composite = db.get_weather_composite_by_store(sel_store_id, di, df)
            weather_detail = db.get_weather_data_for_store(sel_store_id, di, df)
            sel_store = next((s for s in stores if s['id'] == sel_store_id), None)
        except Exception:
            pass

    scheduler_status = wsch.get_scheduler_status()

    return render_template(
        'meteorologia/index.html',
        stores=active_stores,
        sel_store_id=sel_store_id,
        sel_store=sel_store,
        weather_composite=weather_composite,
        weather_detail=weather_detail,
        data_inicio=data_inicio,
        data_fim=data_fim,
        scheduler_status=scheduler_status,
        today=today.isoformat(),
    )


@meteorologia_bp.route('/historico', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def historico_vendas():
    stores = db.get_all_stores()
    active_stores = [s for s in stores if s.get('is_active')]

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'upload_historico':
            return _handle_upload_historico(active_stores)

        elif action == 'delete_historico':
            return _handle_delete_historico()

    anos = db.get_sales_historico_anos()
    sel_ano = request.args.get('ano')
    try:
        sel_ano = int(sel_ano) if sel_ano else (anos[0] if anos else None)
    except (ValueError, TypeError):
        sel_ano = anos[0] if anos else None

    resumo = db.get_sales_historico_summary(sel_ano)
    import_logs = db.get_sales_historico_import_logs(10)

    return render_template(
        'meteorologia/historico_vendas.html',
        stores=active_stores,
        anos=anos,
        sel_ano=sel_ano,
        resumo=resumo,
        import_logs=import_logs,
    )


def _resolve_store(loja_code, loja_name, active_stores, loja_manual, aliases=None):
    """Resolve a store using a single priority chain:
    1. Match loja_code against each store's pos_store_code (Zonesoft ID)
    2. Match loja_name (accent-normalised, lowercase, partial) against store names
    3. Match loja_code or loja_name against store_aliases table
    4. Fall back to loja_manual if provided
    Returns the matched store dict or None.
    """
    def _norm(t):
        return _unicodedata.normalize('NFD', t).encode('ascii', 'ignore').decode().lower().strip()

    store_by_id = {s['id']: s for s in active_stores}

    if loja_code:
        code_str = str(loja_code).strip()
        for s in active_stores:
            if s.get('pos_store_code') and str(s['pos_store_code']).strip() == code_str:
                return s

    if loja_name:
        name_norm = _norm(loja_name)
        for s in active_stores:
            if _norm(s['name']) == name_norm:
                return s
        for s in active_stores:
            if name_norm in _norm(s['name']) or _norm(s['name']) in name_norm:
                return s

    if aliases:
        if loja_code:
            code_str = str(loja_code).strip()
            for a in aliases:
                if a.get('alias_code') and str(a['alias_code']).strip() == code_str:
                    return store_by_id.get(a['store_id'])

        if loja_name:
            name_norm = _norm(loja_name)
            for a in aliases:
                if a.get('alias_name') and _norm(a['alias_name']) == name_norm:
                    return store_by_id.get(a['store_id'])
            for a in aliases:
                if a.get('alias_name'):
                    an = _norm(a['alias_name'])
                    if name_norm in an or an in name_norm:
                        return store_by_id.get(a['store_id'])

    if loja_manual:
        manual_norm = _norm(loja_manual)
        for s in active_stores:
            if _norm(s['name']) == manual_norm:
                return s

    return None


def _import_evolucao_vendas(df_xlsx, active_stores, loja_manual, ano_ref, aliases=None):
    """Parse 'Evolução de Vendas por Data/Hora' XLSX format.

    Scans rows for 'Loja: X - Name' section headers, then reads Data,
    Docs Emitidos, Valor Total columns by header position.
    Store resolution uses a single priority chain via _resolve_store.
    Each valid row is imported as produto='Total Diário', categoria=''.
    If ano_ref is None, the year is auto-detected from dates in the file.
    Returns (imported, skipped, erros_loja, ano_detetado).
    """
    from collections import Counter as _Counter
    records = []
    skipped_no_store = 0
    skipped_other = 0
    unresolved_pairs = {}
    all_years = []

    current_loja_code = None
    current_loja_name = None
    in_data_section = False
    col_idx_data = 0
    col_idx_docs = 1
    col_idx_valor = 5

    for _, row_vals in df_xlsx.iterrows():
        cells = [str(v).strip() if pd.notna(v) else '' for v in row_vals]
        row_str = ' '.join(c for c in cells if c)

        loja_match = _re.search(r'Loja[:\s]*(\d+)(?:\s*[-\u2013]\s*(.+))?', row_str)
        if loja_match:
            current_loja_code = loja_match.group(1).strip()
            current_loja_name = loja_match.group(2).strip() if loja_match.group(2) else None
            in_data_section = False
            continue

        if 'Docs Emitidos' in cells or ('Data' in cells and 'Valor Total' in row_str):
            in_data_section = True
            if 'Data' in cells:
                col_idx_data = cells.index('Data')
            if 'Docs Emitidos' in cells:
                col_idx_docs = cells.index('Docs Emitidos')
            if 'Valor Total' in cells:
                col_idx_valor = cells.index('Valor Total')
            continue

        if not in_data_section:
            continue

        data_cell = cells[col_idx_data] if len(cells) > col_idx_data else ''
        if not data_cell or data_cell.lower().startswith('total'):
            continue

        try:
            data_venda = None
            for fmt in ['%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d']:
                try:
                    data_venda = datetime.strptime(data_cell.split(' ')[0], fmt).date()
                    break
                except ValueError:
                    continue
            if not data_venda:
                try:
                    data_venda = pd.to_datetime(data_cell).date()
                except Exception:
                    skipped_other += 1
                    continue

            all_years.append(data_venda.year)

            docs_emitidos = parse_qty_value(cells[col_idx_docs]) if len(cells) > col_idx_docs else 0
            if docs_emitidos <= 0:
                skipped_other += 1
                continue

            store = _resolve_store(current_loja_code, current_loja_name, active_stores, loja_manual, aliases)
            if store is None:
                skipped_no_store += 1
                key = (current_loja_code or '', current_loja_name or '')
                unresolved_pairs[key] = True
                continue

            valor_total = parse_euro_value(cells[col_idx_valor]) if len(cells) > col_idx_valor else 0

            records.append({
                'store_id': store['id'],
                'pos_store_code': store.get('pos_store_code'),
                'data': data_venda,
                'loja': store['name'],
                'produto': 'Total Diário',
                'categoria': '',
                'quantidade': int(docs_emitidos),
                'valor_euros': valor_total,
                'ano_referencia': ano_ref,
            })
        except Exception:
            skipped_other += 1
            continue

    if not records:
        raise ValueError("Não foram encontrados dados no ficheiro.")

    ano_detetado = None
    if all_years:
        ano_detetado = _Counter(all_years).most_common(1)[0][0]

    if ano_ref is None and ano_detetado:
        for r in records:
            r['ano_referencia'] = ano_detetado
    elif ano_ref is None:
        for r in records:
            r['ano_referencia'] = date.today().year

    lojas_importadas = sorted({r['loja'] for r in records if r.get('loja')})
    imported = db.add_sales_historico_batch(records)
    erros_loja = [{'code': k[0], 'name': k[1]} for k in unresolved_pairs]
    return imported, (skipped_no_store + skipped_other), erros_loja, ano_detetado, lojas_importadas


def _import_xlsx_generic(df_xlsx, active_stores, loja_manual, ano_ref, aliases=None):
    """Parse a generic multi-loja XLSX historical sales format.

    Expects rows with 'Loja: X' section headers followed by a header row
    containing 'Data' and product/quantity/value columns.
    Uses _resolve_store for store resolution.
    Returns (imported, skipped, erros_loja, ano_detetado).
    """
    all_data = []
    current_loja_code = None
    headers = None

    for _, row_vals in df_xlsx.iterrows():
        row_str = ' '.join([str(v) for v in row_vals if pd.notna(v)])
        loja_match = _re.search(r'Loja[:\s]*(\d+)', row_str)
        if loja_match:
            current_loja_code = loja_match.group(1).strip()

        cells = [str(v).strip() if pd.notna(v) else '' for v in row_vals]
        if 'Data' in cells or 'Date' in cells:
            headers = cells
            continue
        if headers and any(c != '' for c in cells):
            if cells[0].lower().startswith('totai') or cells[0].lower().startswith('total'):
                continue
            while len(cells) < len(headers):
                cells.append('')
            if len(cells) > len(headers):
                cells = cells[:len(headers)]
            row_dict = dict(zip(headers, cells))
            row_dict['_loja_code'] = current_loja_code
            if row_dict.get('Data', row_dict.get('Date', '')):
                all_data.append(row_dict)

    if not all_data:
        raise ValueError("Não foram encontrados dados no ficheiro.")

    df_det = pd.DataFrame(all_data)
    date_col, prod_col, qtd_col, valor_col, cat_col = _detect_columns(df_det)
    records = []
    skipped_no_store = 0
    skipped_other = 0
    unresolved_pairs = {}
    all_years = []
    for _, row in df_det.iterrows():
        try:
            loja_code = row.get('_loja_code')
            store = _resolve_store(loja_code, None, active_stores, loja_manual, aliases)
            if store is None:
                skipped_no_store += 1
                unresolved_pairs[(loja_code or '', '')] = True
                continue
            date_str = str(row.get(date_col, '')) if date_col else ''
            data_venda = None
            for fmt in ['%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d']:
                try:
                    data_venda = datetime.strptime(date_str.split(' ')[0], fmt).date()
                    break
                except ValueError:
                    continue
            if not data_venda:
                try:
                    data_venda = pd.to_datetime(date_str).date()
                except Exception:
                    skipped_other += 1
                    continue
            all_years.append(data_venda.year)
            produto = str(row.get(prod_col, '')) if prod_col else ''
            categoria = str(row.get(cat_col, '')).replace('/', '').strip() if cat_col else ''
            quantidade = parse_qty_value(row.get(qtd_col, 0)) if qtd_col else 0
            valor = parse_euro_value(row.get(valor_col, 0)) if valor_col else 0
            if not produto or quantidade <= 0:
                skipped_other += 1
                continue
            records.append({
                'store_id': store['id'],
                'pos_store_code': store.get('pos_store_code'),
                'data': data_venda,
                'loja': store['name'],
                'produto': produto,
                'categoria': categoria,
                'quantidade': int(quantidade),
                'valor_euros': valor,
                'ano_referencia': ano_ref,
            })
        except Exception:
            skipped_other += 1
            continue
    ano_detetado = None
    if all_years:
        from collections import Counter as _Counter2
        ano_detetado = _Counter2(all_years).most_common(1)[0][0]
    if ano_ref is None and ano_detetado:
        for r in records:
            r['ano_referencia'] = ano_detetado
    elif ano_ref is None:
        for r in records:
            r['ano_referencia'] = date.today().year
    lojas_importadas_xlsx = sorted({r['loja'] for r in records if r.get('loja')})
    erros_loja = [{'code': k[0], 'name': k[1]} for k in unresolved_pairs]
    return db.add_sales_historico_batch(records), (skipped_no_store + skipped_other), erros_loja, ano_detetado, lojas_importadas_xlsx


def _handle_upload_historico(active_stores):
    from collections import Counter
    uploaded_file = request.files.get('historico_file')
    loja_manual = request.form.get('loja_manual', '').strip()
    ano_str = request.form.get('ano_referencia', '').strip()

    if not uploaded_file or uploaded_file.filename == '':
        flash('Por favor, selecione um ficheiro.', 'error')
        return redirect(url_for('meteorologia.historico_vendas'))

    ano_form = None
    if ano_str:
        try:
            ano_form = int(ano_str)
        except ValueError:
            flash('Ano de referência inválido.', 'error')
            return redirect(url_for('meteorologia.historico_vendas'))

    aliases = db.get_all_store_aliases()
    file_name = uploaded_file.filename
    file_name_lower = file_name.lower()
    user_id = session.get('user_id')

    erros_loja = []
    ano_detetado = None
    imported = 0
    skipped = 0
    lojas_importadas = []

    try:
        if file_name_lower.endswith('.xlsx'):
            df_xlsx = pd.read_excel(uploaded_file, engine='openpyxl', header=None)
            is_evolucao = False
            for _, row_vals in df_xlsx.iterrows():
                cells = [str(v).strip() if pd.notna(v) else '' for v in row_vals]
                if 'Docs Emitidos' in cells:
                    is_evolucao = True
                    break
                if any('Evolução de Vendas por Data/Hora' in c for c in cells):
                    is_evolucao = True
                    break
            if is_evolucao:
                imported, skipped, erros_loja, ano_detetado, lojas_importadas = _import_evolucao_vendas(
                    df_xlsx, active_stores, loja_manual, None, aliases)
            else:
                imported, skipped, erros_loja, ano_detetado, lojas_importadas = _import_xlsx_generic(
                    df_xlsx, active_stores, loja_manual, None, aliases)

        elif file_name_lower.endswith('.csv'):
            if not ano_form:
                flash('Para ficheiros CSV, indique o Ano de Referência.', 'error')
                return redirect(url_for('meteorologia.historico_vendas'))
            ano_ref = ano_form
            df = pd.read_csv(uploaded_file, encoding='utf-8')
            records = []
            skipped_no_store = 0
            skipped_other = 0
            unresolved_pairs = {}
            all_years_csv = []
            for _, row in df.iterrows():
                try:
                    pos_code = str(row.get('pos_store_code', row.get('loja_codigo', ''))).strip()
                    row_name = str(row.get('loja', '')).strip()
                    store = _resolve_store(pos_code or None, row_name or None, active_stores, loja_manual, aliases)
                    if store is None:
                        skipped_no_store += 1
                        unresolved_pairs[(pos_code, row_name)] = True
                        continue
                    data_str = str(row.get('Data', row.get('data', '')))
                    data_venda = None
                    for fmt in ['%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d']:
                        try:
                            data_venda = datetime.strptime(data_str, fmt).date()
                            break
                        except ValueError:
                            continue
                    if not data_venda:
                        skipped_other += 1
                        continue
                    all_years_csv.append(data_venda.year)
                    produto = str(row.get('Produto', row.get('produto', '')))
                    categoria = str(row.get('Categoria', row.get('categoria', '')))
                    quantidade = int(float(row.get('Quantidade', row.get('quantidade', 0))))
                    valor = parse_euro_value(row.get('Valor', row.get('valor', 0)))
                    if not produto or quantidade <= 0:
                        skipped_other += 1
                        continue
                    records.append({
                        'store_id': store['id'],
                        'pos_store_code': store.get('pos_store_code'),
                        'data': data_venda,
                        'loja': store['name'],
                        'produto': produto,
                        'categoria': categoria,
                        'quantidade': quantidade,
                        'valor_euros': valor,
                        'ano_referencia': ano_ref,
                    })
                except Exception:
                    skipped_other += 1
                    continue
            lojas_importadas = sorted({r['loja'] for r in records if r.get('loja')})
            imported = db.add_sales_historico_batch(records)
            skipped = skipped_no_store + skipped_other
            erros_loja = [{'code': k[0], 'name': k[1]} for k in unresolved_pairs]
            if all_years_csv:
                ano_detetado = Counter(all_years_csv).most_common(1)[0][0]

        else:
            if not ano_form:
                flash('Para ficheiros HTML/XLS, indique o Ano de Referência.', 'error')
                return redirect(url_for('meteorologia.historico_vendas'))
            ano_ref = ano_form
            from bs4 import BeautifulSoup
            raw_bytes = uploaded_file.read()
            try:
                content = raw_bytes.decode('utf-8')
            except UnicodeDecodeError:
                content = raw_bytes.decode('latin-1')

            if 'Excel Workbook Frameset' in content or ('frameset' in content.lower() and 'sheet001.htm' in content):
                raise ValueError("Este ficheiro é um índice Excel. Abra no Excel e guarde como .xlsx")

            soup = BeautifulSoup(content, 'html.parser')
            tables = soup.find_all('table')
            all_data = []
            current_loja_code = None

            for table in tables:
                rows = table.find_all('tr')
                if not rows:
                    continue
                headers = None
                data_start_idx = 0
                for ridx, row in enumerate(rows):
                    row_text = row.get_text(strip=True)
                    loja_match = _re.search(r'Loja[:\s]*(\d+)', row_text)
                    if loja_match:
                        current_loja_code = loja_match.group(1).strip()
                    cells = [td.get_text(strip=True) for td in row.find_all(['th', 'td'])]
                    if cells and ('Data' in cells or 'Date' in cells):
                        headers = cells
                        data_start_idx = ridx + 1
                        break
                if not headers:
                    continue
                for row in rows[data_start_idx:]:
                    cols = [td.get_text(strip=True) for td in row.find_all(['td', 'th'])]
                    if not cols or all(c == '' for c in cols):
                        continue
                    if cols[0].lower().startswith('totai') or cols[0].lower().startswith('total'):
                        continue
                    while len(cols) < len(headers):
                        cols.append('')
                    if len(cols) > len(headers):
                        cols = cols[:len(headers)]
                    row_dict = dict(zip(headers, cols))
                    row_dict['_loja_code'] = current_loja_code
                    all_data.append(row_dict)

            if not all_data:
                raise ValueError("Não foram encontrados dados no ficheiro.")

            df_det = pd.DataFrame(all_data)
            date_col, prod_col, qtd_col, valor_col, cat_col = _detect_columns(df_det)
            records = []
            skipped_no_store = 0
            skipped_other = 0
            unresolved_pairs = {}
            all_years_html = []
            for _, row in df_det.iterrows():
                try:
                    loja_code = row.get('_loja_code')
                    store = _resolve_store(loja_code, None, active_stores, loja_manual, aliases)
                    if store is None:
                        skipped_no_store += 1
                        unresolved_pairs[(loja_code or '', '')] = True
                        continue
                    date_str = str(row.get(date_col, '')) if date_col else ''
                    data_venda = None
                    for fmt in ['%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d']:
                        try:
                            data_venda = datetime.strptime(date_str.split(' ')[0], fmt).date()
                            break
                        except ValueError:
                            continue
                    if not data_venda:
                        try:
                            data_venda = pd.to_datetime(date_str).date()
                        except Exception:
                            skipped_other += 1
                            continue
                    all_years_html.append(data_venda.year)
                    produto = str(row.get(prod_col, '')) if prod_col else ''
                    categoria = str(row.get(cat_col, '')).replace('/', '').strip() if cat_col else ''
                    quantidade = parse_qty_value(row.get(qtd_col, 0)) if qtd_col else 0
                    valor = parse_euro_value(row.get(valor_col, 0)) if valor_col else 0
                    if not produto or quantidade <= 0:
                        skipped_other += 1
                        continue
                    records.append({
                        'store_id': store['id'],
                        'pos_store_code': store.get('pos_store_code'),
                        'data': data_venda,
                        'loja': store['name'],
                        'produto': produto,
                        'categoria': categoria,
                        'quantidade': int(quantidade),
                        'valor_euros': valor,
                        'ano_referencia': ano_ref,
                    })
                except Exception:
                    skipped_other += 1
                    continue
            lojas_importadas = sorted({r['loja'] for r in records if r.get('loja')})
            imported = db.add_sales_historico_batch(records)
            skipped = skipped_no_store + skipped_other
            erros_loja = [{'code': k[0], 'name': k[1]} for k in unresolved_pairs]
            if all_years_html:
                ano_detetado = Counter(all_years_html).most_common(1)[0][0]

        ano_ref_final = ano_detetado or ano_form or date.today().year
        aviso_ano = None
        if ano_detetado and ano_form and ano_detetado != ano_form:
            if file_name_lower.endswith('.xlsx'):
                aviso_ano = (f'⚠️ O ano detetado no ficheiro ({ano_detetado}) diverge do ano indicado '
                             f'no formulário ({ano_form}). Os dados foram importados com o ano do ficheiro ({ano_detetado}).')
            else:
                aviso_ano = (f'⚠️ O ano detetado no ficheiro ({ano_detetado}) diverge do ano indicado '
                             f'no formulário ({ano_form}). Os dados foram importados com o ano do formulário ({ano_form}).')

        try:
            notas_parts = []
            if aviso_ano:
                notas_parts.append(aviso_ano)
            if erros_loja:
                pairs_str = '; '.join(
                    f"Código {e['code']!r} — {e['name']!r}" if e.get('name') else f"Código {e['code']!r}"
                    for e in erros_loja
                )
                notas_parts.append(f'Lojas não mapeadas: {pairs_str}')
            db.log_sales_historico_import(
                user_id=user_id,
                filename=file_name,
                ano_detetado=ano_detetado,
                ano_formulario=ano_form,
                registos_importados=imported,
                linhas_ignoradas=skipped,
                lojas_importadas=lojas_importadas,
                erros_loja=erros_loja,
                notas='\n'.join(notas_parts) if notas_parts else None,
            )
        except Exception as log_err:
            import logging as _log
            _log.getLogger(__name__).warning("Failed to write import log: %s", log_err)

        msg = f'{imported} registos históricos importados para {ano_ref_final}!'
        if skipped > 0:
            msg += f' ({skipped} linha(s) ignoradas: sem loja mapeada ou dados inválidos)'
        flash(msg, 'success')

        if aviso_ano:
            flash(aviso_ano, 'warning')

        if erros_loja:
            pairs_txt = ', '.join(
                f"Loja {e['code']} — '{e['name']}'" if e.get('name') else f"Código {e['code']}"
                for e in erros_loja
            )
            flash(
                f'Lojas não mapeadas no ficheiro: {pairs_txt}. '
                f'Adicione um alias ou configure o ID Zonesoft em '
                f'<a href="{url_for("gestor.gestao_lojas")}">Gestor → Lojas</a>.',
                'warning'
            )

        if imported > 0:
            resumo = db.get_sales_historico_summary(ano_ref_final)
            for r in resumo:
                if r['data_inicio'] and r['data_fim']:
                    dias = (r['data_fim'] - r['data_inicio']).days + 1
                    if dias < 300:
                        flash(
                            f'⚠️ Atenção: o histórico de {r["loja"]} cobre apenas {dias} dias '
                            f'({r["data_inicio"]} → {r["data_fim"]}). Para previsão YoY fiável, '
                            f'recomenda-se pelo menos 300 dias (ano completo = 365 dias).',
                            'warning'
                        )

    except Exception as e:
        flash(f'Erro ao processar ficheiro: {str(e)}', 'error')

    return redirect(url_for('meteorologia.historico_vendas'))


def _handle_delete_historico():
    ano_str = request.form.get('del_ano', '').strip()
    store_id_str = request.form.get('del_store_id', '').strip() or None
    confirm = request.form.get('confirm_delete', '').strip().upper()

    if confirm != 'ELIMINAR':
        flash('Escreva ELIMINAR para confirmar.', 'warning')
        return redirect(url_for('meteorologia.historico_vendas'))

    try:
        ano = int(ano_str) if ano_str else None
        sid = int(store_id_str) if store_id_str else None
        if not ano:
            flash('Indique o ano a eliminar.', 'warning')
            return redirect(url_for('meteorologia.historico_vendas'))
        deleted = db.delete_sales_historico(ano, sid)
        flash(f'{deleted} registos históricos eliminados ({ano})!', 'success' if deleted > 0 else 'warning')
    except Exception as e:
        flash(f'Erro: {str(e)}', 'error')

    return redirect(url_for('meteorologia.historico_vendas'))


@meteorologia_bp.route('/historico-meteo', methods=['GET', 'POST'])
@perm_required('acesso_gestor')
def historico_meteo():
    """Historical weather backfill from Open-Meteo (free, data since 1940)."""
    stores = db.get_all_stores()
    active_stores = [s for s in stores if s.get('is_active')]

    if request.method == 'POST':
        store_id_str = request.form.get('store_id', '').strip()
        data_inicio_str = request.form.get('data_inicio', '').strip()
        data_fim_str = request.form.get('data_fim', '').strip()
        try:
            sid = int(store_id_str)
            di = datetime.strptime(data_inicio_str, '%Y-%m-%d').date()
            df = datetime.strptime(data_fim_str, '%Y-%m-%d').date()
            store = next((s for s in active_stores if s['id'] == sid), None)
            if not store:
                flash('Loja não encontrada.', 'error')
                return redirect(url_for('meteorologia.historico_meteo'))
            lat = store.get('latitude')
            lon = store.get('longitude')
            if lat is None or lon is None:
                flash('Esta loja não tem coordenadas GPS configuradas. Configure em Gestão de Lojas.', 'warning')
                return redirect(url_for('meteorologia.historico_meteo'))

            records = ws.fetch_openmeteo_historical(lat, lon, di, df)
            for rec in records:
                rec['store_id'] = sid
            db.upsert_weather_data_batch(records)
            flash(f'{len(records)} dias históricos Open-Meteo importados para {store["name"]} ({di} → {df}).', 'success')
        except Exception as e:
            flash(f'Erro: {str(e)}', 'error')
        return redirect(url_for('meteorologia.historico_meteo'))

    hoje = date.today()
    return render_template(
        'meteorologia/historico_meteo.html',
        stores=active_stores,
        hoje=hoje.isoformat(),
        default_inicio=(hoje.replace(year=hoje.year - 1, month=1, day=1)).isoformat(),
        default_fim=(hoje.replace(year=hoje.year - 1, month=12, day=31)).isoformat(),
    )


@meteorologia_bp.route('/api/weather-now')
@perm_required('acesso_gestor')
def api_weather_now():
    today_data = db.get_weather_data_all_stores_today()
    return jsonify(today_data)


@meteorologia_bp.route('/api/composite/<int:store_id>')
@perm_required('acesso_gestor')
def api_composite(store_id):
    today = date.today()
    data_inicio = today - timedelta(days=1)
    data_fim = today + timedelta(days=16)
    composite = db.get_weather_composite_by_store(store_id, data_inicio, data_fim)
    for r in composite:
        r['data'] = r['data'].isoformat() if hasattr(r['data'], 'isoformat') else str(r['data'])
    return jsonify(composite)
