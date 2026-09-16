"""
Gestor Service — file-import business logic for production and sales data.

Routes parse the HTTP request, call these functions, then flash/redirect.
All pandas and DB-import logic lives here so it stays testable and off the route layer.
"""
import re as _re
import logging
import math
from datetime import datetime

import pandas as pd

logger = logging.getLogger(__name__)


# ── Value parsers ─────────────────────────────────────────────────────────────

def parse_euro_value(raw_val) -> float:
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


def parse_qty_value(raw_val) -> float:
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


def parse_weight_kg(raw_val, unit='kg'):
    """Return a signed kg value, or None when the source cell is empty."""
    if raw_val is None or pd.isna(raw_val) or str(raw_val).strip() == '':
        return None
    normalized_unit = str(unit or 'kg').strip().casefold().replace('.', '')
    if normalized_unit not in ('kg', 'quilo', 'quilos', 'kilogram', 'kilograms'):
        raise ValueError(
            f'Unidade de peso incompatível: {unit!s}. Use kg.'
        )
    if isinstance(raw_val, (int, float)):
        value = float(raw_val)
    else:
        normalized_value = str(raw_val).replace(' ', '').strip()
        if ',' in normalized_value and '.' in normalized_value:
            normalized_value = normalized_value.replace('.', '').replace(',', '.')
        elif ',' in normalized_value:
            normalized_value = normalized_value.replace(',', '.')
        try:
            value = float(normalized_value)
        except ValueError:
            raise ValueError(
                f'Peso vendido em kg inválido: {raw_val!s}.'
            ) from None
    if not math.isfinite(value):
        raise ValueError('O peso vendido em kg tem de ser um número finito.')
    if value == 0:
        raise ValueError('O peso vendido em kg não pode ser zero.')
    return value


def parse_sales_quantity(raw_val):
    value = parse_qty_value(raw_val)
    if not math.isfinite(value):
        raise ValueError('A quantidade vendida tem de ser um número finito.')
    return value


def _weight_unit(row, weight_column, unit_column):
    if unit_column:
        return row.get(unit_column, 'kg')
    header = str(weight_column or '').casefold()
    if '(g)' in header or 'grama' in header:
        return 'g'
    return 'kg'


# ── Internal helpers ──────────────────────────────────────────────────────────

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
    # Real IVA source: some Ravagnan exports (e.g. "Evolução de Vendas por
    # Produto") include a net-of-VAT column alongside the gross total, letting
    # us compute the exact IVA per line (valor - valor_sem_iva) instead of
    # assuming a rate. Confirmed with the user on 2026-07-05.
    valor_sem_iva_col = None
    for c in df_det.columns:
        if 'Valor' in c and 'S/IVA' in c.upper():
            valor_sem_iva_col = c
            break
    cat_col = None
    for c in df_det.columns:
        if 'Familia' in c or 'Categoria' in c or 'Família' in c:
            cat_col = c
            break
    peso_col = next((
        c for c in df_det.columns
        if (
            ('peso' in str(c).casefold() and 'vend' in str(c).casefold())
            or str(c).strip().casefold() in ('kg vendidos', 'kg vendido')
        )
    ), None)
    unidade_peso_col = next((
        c for c in df_det.columns
        if 'unidade' in str(c).casefold() and 'peso' in str(c).casefold()
    ), None)
    return (
        date_col, prod_col, qtd_col, valor_col, cat_col, valor_sem_iva_col,
        peso_col, unidade_peso_col,
    )


def _import_vendas_from_rows(df_det, date_col, prod_col, qtd_col, valor_col, cat_col,
                              pre_delete_pairs=None, valor_sem_iva_col=None,
                              peso_col=None, unidade_peso_col=None):
    import database as db
    skipped_no_loja = 0
    batch = []
    for _, row in df_det.iterrows():
        try:
            loja_row = row.get('_loja')
            if not loja_row:
                skipped_no_loja += 1
                continue
            date_str = str(row.get(date_col, ''))
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
                    continue
            produto = str(row.get(prod_col, '')) if prod_col else ''
            categoria = str(row.get(cat_col, '')).replace('/', '').strip() if cat_col else ''
            quantidade = (
                parse_sales_quantity(row.get(qtd_col, 0)) if qtd_col else 0
            )
            valor = parse_euro_value(row.get(valor_col, 0)) if valor_col else 0
            valor_sem_iva = (
                parse_euro_value(row.get(valor_sem_iva_col, 0))
                if valor_sem_iva_col else None
            )
            peso_vendido_kg = (
                parse_weight_kg(
                    row.get(peso_col),
                    _weight_unit(row, peso_col, unidade_peso_col),
                )
                if peso_col else None
            )
            if (
                peso_vendido_kg is not None
                and quantidade != 0
                and (peso_vendido_kg > 0) != (quantidade > 0)
            ):
                raise ValueError(
                    f'O peso vendido e a quantidade têm sinais incompatíveis '
                    f'para {produto}.'
                )
            if produto and quantidade != 0:
                batch.append({
                    'data': data_venda,
                    'loja': loja_row,
                    'produto': produto,
                    'quantidade': quantidade,
                    'categoria': categoria,
                    'valor_euros': valor,
                    'valor_sem_iva_euros': valor_sem_iva,
                    'peso_vendido_kg': peso_vendido_kg,
                })
        except ValueError:
            raise
        except Exception:
            continue
    batch = db.apply_vendas_weight_rules(batch)
    imported_count = db.add_venda_detalhe_batch(batch, pre_delete_pairs=pre_delete_pairs)
    return imported_count, skipped_no_loja


# ── Producao CSV import ───────────────────────────────────────────────────────

def import_producao_csv(file_stream, loja: str, unit_is_grams: bool) -> dict:
    """
    Parse a CalybraBox production CSV and import into the database.

    Returns the result dict from ``db.import_producao_calybrabox``.
    Raises ``ServiceError`` on validation/parsing failure.
    """
    from flask_app.services import ServiceError
    import database as db

    try:
        try:
            df = pd.read_csv(file_stream, encoding='latin-1', sep=';', skiprows=1)
            if len(df.columns) < 2:
                file_stream.seek(0)
                df = pd.read_csv(file_stream, encoding='latin-1', sep=',')
        except Exception:
            file_stream.seek(0)
            df = pd.read_csv(file_stream, encoding='utf-8')

        df = df.dropna(axis=1, how='all')
        return db.import_producao_calybrabox(df, loja, unit_is_grams)
    except Exception as exc:
        raise ServiceError(f'Erro ao ler ficheiro: {exc}') from exc


# ── Vendas XLSX import ────────────────────────────────────────────────────────

def peek_dates_from_xlsx(file_stream, loja_map: dict) -> dict:
    """Read an XLSX file to extract the set of (loja, date) pairs it contains,
    WITHOUT writing anything to the database. Rewinds the stream afterwards.

    Returns a dict ``{loja_name: sorted_list_of_date_objects}`` for every loja
    identified in the file.  Used to warn the user about potential re-import
    duplicates before the actual import runs.
    """
    import io
    raw = file_stream.read()
    file_stream.seek(0)

    buf = io.BytesIO(raw)
    df_xlsx = pd.read_excel(buf, engine='openpyxl', header=None)

    current_loja_id = None
    headers = None
    pairs: dict = {}  # loja_name -> set of date objects

    for _, row_vals in df_xlsx.iterrows():
        row_str = ' '.join([str(v) for v in row_vals if pd.notna(v)])
        loja_match = _re.search(r'Loja[:\s]*(\d+)', row_str)
        if loja_match:
            current_loja_id = loja_match.group(1)

        cells = [str(v).strip() if pd.notna(v) else '' for v in row_vals]
        if 'Data' in cells or 'Date' in cells:
            headers = cells
            continue

        if headers and any(c != '' for c in cells):
            if cells[0].lower().startswith('totai') or cells[0].lower().startswith('total'):
                continue
            date_str = cells[0].strip()
            if not date_str:
                continue
            loja_resolved = loja_map.get(str(current_loja_id).strip(), None) if current_loja_id else None
            if not loja_resolved:
                continue
            parsed_date = None
            for fmt in ['%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d']:
                try:
                    parsed_date = datetime.strptime(date_str.split(' ')[0], fmt).date()
                    break
                except ValueError:
                    continue
            if not parsed_date:
                try:
                    parsed_date = pd.to_datetime(date_str).date()
                except Exception:
                    pass
            if parsed_date:
                pairs.setdefault(loja_resolved, set()).add(parsed_date)

    return {loja: sorted(dates) for loja, dates in pairs.items()}


def import_vendas_xlsx(file_stream, loja_map: dict,
                        pre_delete_pairs: list = None) -> tuple[int, int]:
    """Parse a multi-loja sales XLSX and import rows.

    ``loja_map`` maps string loja IDs to store names, e.g. ``{'1': 'Matosinhos'}``.

    If ``pre_delete_pairs`` is provided (list of ``(date, loja)`` tuples), those
    existing rows are deleted atomically with the insert (same DB transaction) so
    reimporting a corrected file is a safe replace with no duplication risk.

    Returns ``(imported, skipped)`` counts.
    Raises ``ServiceError`` on empty / malformed data.
    """
    from flask_app.services import ServiceError

    df_xlsx = pd.read_excel(file_stream, engine='openpyxl', header=None)
    all_data = []
    current_loja_id = None
    headers = None

    for _, row_vals in df_xlsx.iterrows():
        row_str = ' '.join([str(v) for v in row_vals if pd.notna(v)])
        loja_match = _re.search(r'Loja[:\s]*(\d+)', row_str)
        if loja_match:
            current_loja_id = loja_match.group(1)

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
            loja_resolved = loja_map.get(str(current_loja_id).strip(), None) if current_loja_id else None
            row_dict['_loja'] = loja_resolved
            if row_dict.get('Data', row_dict.get('Date', '')):
                all_data.append(row_dict)

    if not all_data:
        raise ServiceError('Não foram encontrados dados de vendas no ficheiro.')

    df_det = pd.DataFrame(all_data)
    (date_col, prod_col, qtd_col, valor_col, cat_col, valor_sem_iva_col,
     peso_col, unidade_peso_col) = _detect_columns(df_det)
    return _import_vendas_from_rows(df_det, date_col, prod_col, qtd_col, valor_col, cat_col,
                                    pre_delete_pairs=pre_delete_pairs,
                                    valor_sem_iva_col=valor_sem_iva_col,
                                    peso_col=peso_col,
                                    unidade_peso_col=unidade_peso_col)


# ── Vendas CSV import ─────────────────────────────────────────────────────────

def import_vendas_csv(file_stream, loja: str) -> int:
    """
    Parse a simple sales CSV. Optional weight columns are Peso Vendido and
    Unidade Peso; weights must be expressed in kg.

    Returns the number of imported records.
    """
    import database as db

    df = pd.read_csv(file_stream, encoding='utf-8')
    batch = []
    for _, row in df.iterrows():
        try:
            data_str = str(row.get('Data', row.get('data', '')))
            if not data_str:
                continue
            data_venda = None
            for fmt in ['%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d']:
                try:
                    data_venda = datetime.strptime(data_str, fmt).date()
                    break
                except ValueError:
                    continue
            if not data_venda:
                continue
            produto = str(row.get('Produto', row.get('produto', '')))
            categoria = str(row.get('Categoria', row.get('categoria', '')))
            quantidade = parse_sales_quantity(
                row.get('Quantidade', row.get('quantidade', 0))
            )
            valor = parse_euro_value(row.get('Valor', row.get('valor', 0)))
            raw_weight = row.get(
                'Peso Vendido',
                row.get(
                    'Peso Vendido (kg)',
                    row.get('peso_vendido_kg', row.get('Kg Vendidos')),
                ),
            )
            weight_column = next((
                column for column in (
                    'Peso Vendido', 'Peso Vendido (kg)', 'peso_vendido_kg',
                    'Kg Vendidos', 'Peso Vendido (g)',
                )
                if column in row.index
            ), None)
            if weight_column is not None:
                raw_weight = row.get(weight_column)
            unit_column = next((
                column for column in ('Unidade Peso', 'unidade_peso')
                if column in row.index
            ), None)
            unit = _weight_unit(row, weight_column, unit_column)
            peso_vendido_kg = parse_weight_kg(raw_weight, unit)
            if (
                peso_vendido_kg is not None
                and quantidade != 0
                and (peso_vendido_kg > 0) != (quantidade > 0)
            ):
                raise ValueError(
                    f'O peso vendido e a quantidade têm sinais incompatíveis '
                    f'para {produto}.'
                )
            if produto and quantidade != 0:
                batch.append({
                    'data': data_venda, 'loja': loja, 'produto': produto,
                    'quantidade': quantidade, 'categoria': categoria,
                    'valor_euros': valor, 'peso_vendido_kg': peso_vendido_kg,
                })
        except ValueError:
            raise
        except Exception:
            continue
    batch = db.apply_vendas_weight_rules(batch)
    return db.add_venda_detalhe_batch(batch)


# ── Vendas HTML import ────────────────────────────────────────────────────────

def import_vendas_html(file_stream, loja_map: dict) -> tuple[int, int]:
    """
    Parse a multi-loja sales HTML export and import rows.

    Returns ``(imported, skipped)`` counts.
    Raises ``ServiceError`` on index-only Excel framesets.
    """
    from bs4 import BeautifulSoup
    from flask_app.services import ServiceError

    raw_bytes = file_stream.read()
    try:
        content = raw_bytes.decode('utf-8')
    except UnicodeDecodeError:
        content = raw_bytes.decode('latin-1')

    if 'Excel Workbook Frameset' in content or (
        'frameset' in content.lower() and 'sheet001.htm' in content
    ):
        raise ServiceError('Este ficheiro é um índice Excel. Abra no Excel e guarde como .xlsx')

    soup = BeautifulSoup(content, 'html.parser')
    tables = soup.find_all('table')
    all_data = []
    current_loja_id = None

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
                current_loja_id = loja_match.group(1)
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
            loja_resolved = loja_map.get(str(current_loja_id).strip(), None) if current_loja_id else None
            row_dict['_loja'] = loja_resolved
            all_data.append(row_dict)

    if not all_data:
        raise ServiceError('Não foram encontrados dados de vendas no ficheiro HTML.')

    df_det = pd.DataFrame(all_data)
    (date_col, prod_col, qtd_col, valor_col, cat_col, valor_sem_iva_col,
     peso_col, unidade_peso_col) = _detect_columns(df_det)
    return _import_vendas_from_rows(df_det, date_col, prod_col, qtd_col, valor_col, cat_col,
                                    valor_sem_iva_col=valor_sem_iva_col,
                                    peso_col=peso_col,
                                    unidade_peso_col=unidade_peso_col)
