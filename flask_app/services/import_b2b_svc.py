"""Service for importing B2B/Events invoices from an Excel file.

Expected Excel columns (listagemDocumentos format):
  Documento, Número, Data, Data Vencimento, Cliente, Nome, NIF,
  Armazém, Total Bruto, Total Líquido, Desconto Global, Total Imposto,
  Total, Observações, Anulado
"""
import logging
from datetime import date
import unicodedata
import openpyxl

from db.clientes_b2b import upsert_cliente
from db.faturas_clientes import upsert_fatura

logger = logging.getLogger(__name__)

_REQUIRED_COLS = {'Número', 'Data', 'Nome', 'NIF', 'Total'}


def normalize_document_type(value, numero: str = '') -> str:
    """Map the source document label to the stable B2B document type."""
    label = str(value or '').strip().casefold()
    number = str(numero or '').strip().casefold()
    compact = label.replace('-', ' ').replace('_', ' ')
    compact = ' '.join(compact.split())
    normalized = ''.join(
        char for char in unicodedata.normalize('NFKD', compact)
        if not unicodedata.combining(char)
    )
    if 'nota' in normalized and 'credito' in normalized:
        return 'nota_credito'
    if 'nota' in normalized and 'debito' in normalized:
        return 'nota_debito'
    if number.startswith('nc ') or number.startswith('nc/'):
        return 'nota_credito'
    if number.startswith('nd ') or number.startswith('nd/'):
        return 'nota_debito'
    return 'fatura'


def _to_date(val) -> date:
    if val is None:
        return None
    if isinstance(val, date):
        return val
    from datetime import datetime
    if isinstance(val, datetime):
        return val.date()
    s = str(val).strip()
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _to_float(val) -> float:
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def import_b2b_from_excel(file_obj) -> dict:
    """Parse an Excel file and upsert clients + invoices.

    Returns:
        {
            'faturas_importadas': int,
            'faturas_atualizadas': int,
            'faturas_anuladas_ignoradas': int,
            'clientes_novos': int,
            'clientes_existentes': int,
            'erros': list[str],
        }
    """
    result = {
        'faturas_importadas': 0,
        'faturas_duplicadas': 0,   # invoices that already existed and were updated
        'faturas_anuladas_ignoradas': 0,
        'clientes_novos': 0,
        'clientes_existentes': 0,
        'erros': [],
    }

    try:
        wb = openpyxl.load_workbook(file_obj, data_only=True)
    except Exception as exc:
        result['erros'].append(f'Erro ao abrir ficheiro Excel: {exc}')
        return result

    ws = wb.active
    headers = [str(c.value).strip() if c.value is not None else '' for c in ws[1]]

    missing = _REQUIRED_COLS - set(headers)
    if missing:
        result['erros'].append(f'Colunas obrigatórias em falta: {", ".join(sorted(missing))}')
        return result

    def col(row_cells, name: str):
        try:
            idx = headers.index(name)
            return row_cells[idx].value
        except (ValueError, IndexError):
            return None

    # Cache client upserts within this import to avoid repeated DB roundtrips
    _client_cache: dict[str, tuple[int, bool]] = {}  # nif -> (id, is_new)

    for row_num, row in enumerate(ws.iter_rows(min_row=2), start=2):
        if all(c.value is None for c in row):
            continue
        # PHC/Primavera exports append a human-readable summary block after
        # the documents. It has shifted headings in the normal columns and
        # must not be reported as a malformed customer document.
        if (
            not col(row, 'Nome')
            and not col(row, 'NIF')
            and not _to_date(col(row, 'Data'))
        ):
            continue

        numero = col(row, 'Número')
        if not numero:
            continue
        numero = str(numero).strip()
        document_type = normalize_document_type(col(row, 'Documento'), numero)

        anulado_raw = str(col(row, 'Anulado') or '').strip().upper()
        if anulado_raw == 'S':
            result['faturas_anuladas_ignoradas'] += 1
            continue

        nif = str(col(row, 'NIF') or '').strip()
        nome = str(col(row, 'Nome') or '').strip()
        codigo = str(col(row, 'Cliente') or '').strip()

        if not nif or not nome:
            result['erros'].append(f'Linha {row_num}: NIF ou Nome em falta, ignorada.')
            continue

        # Upsert client (deduplicated by NIF within this batch)
        if nif not in _client_cache:
            try:
                cliente_id, is_new = upsert_cliente(codigo, nome, nif)
                _client_cache[nif] = cliente_id
                if is_new:
                    result['clientes_novos'] += 1
                else:
                    result['clientes_existentes'] += 1
            except Exception as exc:
                result['erros'].append(f'Linha {row_num}: erro ao guardar cliente ({exc})')
                continue
        else:
            cliente_id = _client_cache[nif]
            result['clientes_existentes'] += 1

        data_fatura = _to_date(col(row, 'Data'))
        data_venc = _to_date(col(row, 'Data Vencimento'))

        if not data_fatura:
            result['erros'].append(f'Linha {row_num}: data inválida para fatura {numero}, ignorada.')
            continue

        try:
            fatura_id, is_new = upsert_fatura(
                cliente_id=cliente_id,
                numero=numero,
                data_fatura=data_fatura,
                data_vencimento=data_venc,
                documento=str(col(row, 'Documento') or '').strip() or None,
                document_type=document_type,
                armazem=str(col(row, 'Armazém') or '').strip() or None,
                total_bruto=_to_float(col(row, 'Total Bruto')),
                total_liquido=_to_float(col(row, 'Total Líquido')),
                desconto_global=_to_float(col(row, 'Desconto Global')),
                total_imposto=_to_float(col(row, 'Total Imposto')),
                total=_to_float(col(row, 'Total')),
                observacoes=str(col(row, 'Observações') or '').strip() or None,
                anulado=False,
            )
            if is_new:
                result['faturas_importadas'] += 1
            else:
                result['faturas_duplicadas'] += 1
        except Exception as exc:
            result['erros'].append(f'Linha {row_num}: erro ao guardar fatura {numero} ({exc})')

    return result
