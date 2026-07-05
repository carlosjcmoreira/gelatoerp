"""Shared parsing/normalisation utilities for the financial module
(Faturas, Compras, Pagamentos).

These functions were previously duplicated (with minor drift) across
``flask_app/routes/faturas.py`` and ``flask_app/routes/compras.py``.
Centralising them here means a fix or behaviour change only needs to
happen in one place.

Note: this module intentionally contains only pure, stateless helpers.
Compras and Faturas/Pagamentos remain separate routes/UIs for separate
user populations — this module does not couple them together.
"""
import re
from collections import defaultdict
from datetime import datetime

_DATE_FORMATS = ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y')

_INVOICE_PREFIX_RE = re.compile(r'^(FT|NC|FR|RB|VD|FS|FC|FA|RC)\s+')
_INVOICE_NON_ALNUM_RE = re.compile(r'[^A-Z0-9]')


def parse_date(val: str):
    """Parse a date string trying, in order, ISO (YYYY-MM-DD) and the two
    common Portuguese slash/dash formats (DD/MM/YYYY, DD-MM-YYYY).

    Returns a ``date`` object, or ``None`` if ``val`` is falsy or does not
    match any of the accepted formats.
    """
    if not val:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(val.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


def parse_float(val):
    """Parse a numeric string that may use a comma as decimal separator
    (common in Portuguese forms/spreadsheets).

    Returns a ``float``, or ``None`` if ``val`` is falsy or not parseable.
    """
    if not val:
        return None
    try:
        return float(str(val).replace(',', '.').strip())
    except ValueError:
        return None


def normalize_invoice_number(number: str) -> str:
    """Normalise an invoice/document number for duplicate comparison:
    strip surrounding whitespace, uppercase, drop a leading document-type
    prefix (FT/NC/FR/RB/VD/FS/FC/FA/RC), then strip all non-alphanumeric
    characters.

    Returns '' for falsy input (never None), so it is always safe to use
    as a dict/group key.
    """
    if not number:
        return ''
    n = number.strip().upper()
    n = _INVOICE_PREFIX_RE.sub('', n)
    return _INVOICE_NON_ALNUM_RE.sub('', n)


def find_duplicate_invoice_ids(invoices) -> set:
    """Given a list of invoice dicts (each with at least ``id``,
    ``invoice_number`` and ``supplier_id``), return the set of invoice
    ``id``s that share the same (supplier_id, normalised invoice_number)
    with at least one other invoice in the list.

    Invoices missing ``invoice_number`` or ``supplier_id`` are ignored for
    grouping purposes (never flagged as duplicates on that basis alone).
    """
    groups = defaultdict(list)
    for inv in invoices:
        if inv.get('invoice_number') and inv.get('supplier_id'):
            key = (inv['supplier_id'], normalize_invoice_number(inv['invoice_number']))
            groups[key].append(inv['id'])
    return {inv_id for ids in groups.values() if len(ids) > 1 for inv_id in ids}
