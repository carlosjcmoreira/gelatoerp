import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, get_connection, release_connection, logger
from db.cache import ttl_cache, invalidate as _cache_invalidate
import json
import os

# NIFs da própria empresa — nunca devem identificar um fornecedor
OWN_COMPANY_NIFS: frozenset = frozenset({'516388819', 'PT516388819'})

PAYMENT_METHOD_LABELS = {
    'transferencia': 'Transferência',
    'debito_direto': 'Débito Direto',
    'confirming': 'Confirming',
    'numerario': 'Numerário',
}

PAYMENT_TERMS_LABELS = {
    'a_pronto': 'A pronto',
    '15_dias': '15 dias',
    '30_dias': '30 dias',
    '60_dias': '60 dias',
    'final_mes': 'Até final do mês',
}

MAX_SUPPLIER_COMMON_NAME_LENGTH = 120
_UNSET = object()


def normalize_supplier_common_name(value) -> str | None:
    """Normalize an optional supplier-facing name without touching legal identity."""
    if value is None:
        return None
    normalized = ' '.join(str(value).split())
    if len(normalized) > MAX_SUPPLIER_COMMON_NAME_LENGTH:
        raise ValueError(
            f'O nome comum não pode ter mais de {MAX_SUPPLIER_COMMON_NAME_LENGTH} caracteres.'
        )
    return normalized or None


def _supplier_display_expression(invoice_alias: str = 'i', supplier_alias: str = None) -> str:
    """Return the SQL expression for a supplier's human-facing invoice name."""
    if supplier_alias:
        return (
            f"COALESCE(NULLIF({supplier_alias}.common_name, ''), "
            f"{supplier_alias}.name, {invoice_alias}.supplier_name)"
        )
    return (
        f"COALESCE(NULLIF((SELECT s.common_name FROM suppliers s "
        f"WHERE s.id = {invoice_alias}.supplier_id), ''), "
        f"(SELECT s.name FROM suppliers s WHERE s.id = {invoice_alias}.supplier_id), "
        f"{invoice_alias}.supplier_name)"
    )


def calculate_due_date(issue_date, payment_terms: str):
    """
    Calculate invoice due date from issue date + supplier payment terms.
    Returns a date object or None if issue_date is None.
    """
    if not issue_date:
        return None
    if isinstance(issue_date, str):
        from datetime import datetime
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
            try:
                issue_date = datetime.strptime(issue_date.strip(), fmt).date()
                break
            except ValueError:
                continue
    if not payment_terms or payment_terms == 'a_pronto':
        return issue_date
    elif payment_terms == '15_dias':
        return issue_date + timedelta(days=15)
    elif payment_terms == '30_dias':
        return issue_date + timedelta(days=30)
    elif payment_terms == '60_dias':
        return issue_date + timedelta(days=60)
    elif payment_terms == 'final_mes':
        import calendar
        # last day of the month of issue_date
        last_day = calendar.monthrange(issue_date.year, issue_date.month)[1]
        return issue_date.replace(day=last_day)
    return None


def get_suppliers(only_active: bool = False) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.id, s.name, s.nif, s.store_id, s.notes,
                   st.name AS store_name, s.payment_method, s.payment_terms, s.iban,
                    s.centro_custo_id, cc.name AS centro_custo_name,
                    s.categoria_custo_id, ccat.name AS categoria_custo_name,
                    s.common_name
            FROM suppliers s
            LEFT JOIN stores st ON s.store_id = st.id
            LEFT JOIN cost_centers cc ON s.centro_custo_id = cc.id
            LEFT JOIN cost_categories ccat ON s.categoria_custo_id = ccat.id
            ORDER BY s.name
        """)
        rows = cursor.fetchall()
    return [{'id': r[0], 'name': r[1], 'nif': r[2],
             'store_id': r[3], 'notes': r[4], 'store_name': r[5],
             'payment_method': r[6], 'payment_terms': r[7], 'iban': r[8],
              'centro_custo_id': r[9], 'centro_custo_name': r[10],
              'categoria_custo_id': r[11], 'categoria_custo_name': r[12],
              'common_name': r[13]} for r in rows]


def _normalize_nif(nif) -> str:
    """Normalise a NIF/VAT string: strip surrounding whitespace, remove internal
    spaces and dashes, then uppercase.  Returns None for falsy input.

    Examples
    --------
    'PT 501234567' → 'PT501234567'
    'pt-501-234-567' → 'PT501234567'
    ' 501 234 567 ' → '501234567'
    """
    if not nif:
        return None
    cleaned = str(nif).strip()
    cleaned = cleaned.replace(' ', '').replace('-', '')
    return cleaned.upper() or None


def _nif_match_key(nif) -> str:
    """Return the comparable part of a Portuguese NIF.

    Historical imports contain both ``PT123...`` and ``123...`` forms.  The
    prefix distinguishes neither supplier identity nor a conflicting record.
    """
    normalized = _normalize_nif(nif)
    if normalized and normalized.startswith('PT'):
        return normalized[2:]
    return normalized


def _nifs_match(left, right) -> bool:
    """True only when both provided NIF values identify the same entity."""
    left_key = _nif_match_key(left)
    right_key = _nif_match_key(right)
    return bool(left_key and right_key and left_key == right_key)


def is_valid_portuguese_nif(nif) -> bool:
    """Validate the checksum of a Portuguese nine-digit NIF.

    Values with a non-PT country prefix are not Portuguese NIFs and therefore
    are left outside this validation rule.
    """
    normalized = _normalize_nif(nif)
    if not normalized:
        return False
    if normalized.startswith('PT'):
        normalized = normalized[2:]
        if len(normalized) != 9 or not normalized.isdigit():
            return False
    elif not normalized.isdigit():
        country_prefix = normalized[:2]
        return (
            len(normalized) > 2
            and country_prefix.isalpha()
            and country_prefix != 'PT'
        )
    if len(normalized) != 9 or not normalized.isdigit():
        return False
    total = sum(int(digit) * weight for digit, weight in zip(normalized[:8], range(9, 1, -1)))
    check = 11 - (total % 11)
    if check >= 10:
        check = 0
    return check == int(normalized[-1])


def can_auto_match_supplier_nif(nif) -> bool:
    """Return whether an OCR NIF is safe to use for automatic supplier matching."""
    normalized = _normalize_nif(nif)
    if not normalized or normalized in OWN_COMPANY_NIFS:
        return False
    return is_valid_portuguese_nif(normalized)


class SupplierIdentityConflict(ValueError):
    """Raised when submitted invoice supplier fields disagree with the linked supplier."""


def _confirmation_is_explicit(value) -> bool:
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _normalise_identity_name(value) -> str:
    return ' '.join(str(value or '').split()).casefold()


def _canonicalize_invoice_supplier_data(
        cursor, data: dict, effective_supplier_id, previous_supplier_id=None):
    """Return a write-safe copy whose supplier fields represent one entity."""
    prepared = dict(data)
    prepared.pop('supplier_conflict_confirmed', None)
    if not effective_supplier_id:
        return prepared

    cursor.execute(
        "SELECT id, name, nif FROM suppliers WHERE id = %s",
        (effective_supplier_id,),
    )
    supplier = cursor.fetchone()
    if not supplier:
        raise ValueError('O fornecedor selecionado já não existe.')

    canonical_id, canonical_name, canonical_nif = supplier
    conflicts = []
    if (previous_supplier_id is not None
            and canonical_id != previous_supplier_id):
        conflicts.append('fornecedor associado')
    if 'supplier_name' in data:
        submitted_name = data.get('supplier_name')
        if _normalise_identity_name(submitted_name) != _normalise_identity_name(canonical_name):
            conflicts.append('nome')
    if 'supplier_nif' in data:
        submitted_nif = data.get('supplier_nif')
        if submitted_nif or canonical_nif:
            if not _nifs_match(submitted_nif, canonical_nif):
                conflicts.append('NIF')

    if conflicts and not _confirmation_is_explicit(data.get('supplier_conflict_confirmed')):
        raise SupplierIdentityConflict(
            'Os dados introduzidos não correspondem ao fornecedor selecionado '
            f'({", ".join(conflicts)}). Confirma qual fornecedor deve ficar registado.'
        )

    prepared['supplier_id'] = canonical_id
    prepared['supplier_name'] = canonical_name
    prepared['supplier_nif'] = canonical_nif or None
    return prepared


def get_supplier_by_nif(nif: str) -> dict:
    nif = _normalize_nif(nif)
    if not nif:
        return None
    if nif in OWN_COMPANY_NIFS:
        return None  # never match a supplier using the company's own NIF
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.id, s.name, s.nif, s.store_id, s.notes,
                   st.name AS store_name, s.payment_method, s.payment_terms, s.iban,
                    s.centro_custo_id, cc.name AS centro_custo_name,
                    s.common_name
            FROM suppliers s
            LEFT JOIN stores st ON s.store_id = st.id
            LEFT JOIN cost_centers cc ON s.centro_custo_id = cc.id
            WHERE regexp_replace(upper(COALESCE(s.nif, '')), '^PT', '') = %s
        """, (_nif_match_key(nif),))
        row = cursor.fetchone()
    if row:
        return {'id': row[0], 'name': row[1], 'nif': row[2],
                'store_id': row[3], 'notes': row[4], 'store_name': row[5],
                'payment_method': row[6], 'payment_terms': row[7], 'iban': row[8],
                'centro_custo_id': row[9], 'centro_custo_name': row[10],
                'common_name': row[11]}
    return None


def get_supplier_by_id(supplier_id: int) -> dict:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.id, s.name, s.nif, s.store_id, s.notes,
                   st.name AS store_name, s.payment_method, s.payment_terms, s.iban,
                    s.centro_custo_id, cc.name AS centro_custo_name,
                    s.categoria_custo_id, ccat.name AS categoria_custo_name,
                    s.common_name
            FROM suppliers s
            LEFT JOIN stores st ON s.store_id = st.id
            LEFT JOIN cost_centers cc ON s.centro_custo_id = cc.id
            LEFT JOIN cost_categories ccat ON s.categoria_custo_id = ccat.id
            WHERE s.id = %s
        """, (supplier_id,))
        row = cursor.fetchone()
    if row:
        return {'id': row[0], 'name': row[1], 'nif': row[2],
                'store_id': row[3], 'notes': row[4], 'store_name': row[5],
                'payment_method': row[6], 'payment_terms': row[7], 'iban': row[8],
                'centro_custo_id': row[9], 'centro_custo_name': row[10],
                'categoria_custo_id': row[11], 'categoria_custo_name': row[12],
                'common_name': row[13]}
    return None


def update_supplier(supplier_id: int, name: str, nif: str = None,
                    store_id: int = None, notes: str = None,
                    payment_method: str = None, payment_terms: str = None,
                    iban: str = None, centro_custo_id: int = None,
                    categoria_custo_id: int = None,
                    common_name=_UNSET) -> bool:
    """Update an existing supplier by primary key. Returns True if a row was updated."""
    nif = _normalize_nif(nif)
    if common_name is not _UNSET:
        common_name = normalize_supplier_common_name(common_name)
    assignments = [
        'name = %s',
        'nif = %s',
        'store_id = %s',
        'notes = %s',
        'payment_method = %s',
        'payment_terms = %s',
        'iban = %s',
        'centro_custo_id = %s',
        'categoria_custo_id = %s',
    ]
    values = [
        name, nif or None, store_id, notes or None, payment_method or None,
        payment_terms or None, iban or None, centro_custo_id or None,
        categoria_custo_id or None,
    ]
    if common_name is not _UNSET:
        assignments.append('common_name = %s')
        values.append(common_name)
    assignments.append('updated_at = NOW()')
    values.append(supplier_id)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE suppliers SET {', '.join(assignments)} WHERE id = %s",
            values,
        )
        updated = cursor.rowcount > 0
        conn.commit()
    return updated


def patch_supplier(supplier_id: int, **fields) -> bool:
    """Patch a subset of supplier fields without overwriting other fields.

    Allowed field names: store_id, payment_method, payment_terms,
    centro_custo_id, categoria_custo_id.  Returns True if a row was updated.

    Note: ``category`` (legacy free-text column) is intentionally excluded —
    use ``categoria_custo_id`` (FK) instead.
    """
    _ALLOWED = {'store_id', 'payment_method', 'payment_terms', 'centro_custo_id', 'categoria_custo_id'}
    to_set = {k: v for k, v in fields.items() if k in _ALLOWED}
    if not to_set:
        return False
    set_clause = ', '.join(f'{col} = %s' for col in to_set)
    values = list(to_set.values()) + [supplier_id]
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE suppliers SET {set_clause}, updated_at = NOW() WHERE id = %s",
            values,
        )
        updated = cursor.rowcount > 0
        conn.commit()
    return updated


def get_supplier_name_mismatches() -> list:
    """Return invoices whose stored supplier identity differs from the linked supplier.

    Useful for diagnosing desynchronised denormalised data.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT i.id AS invoice_id,
                   i.supplier_id,
                   i.supplier_name AS stored_name,
                   s.name         AS canonical_name,
                   i.supplier_nif AS stored_nif,
                   s.nif          AS canonical_nif
            FROM invoices i
            JOIN suppliers s ON s.id = i.supplier_id
            WHERE i.supplier_id IS NOT NULL
              AND (
                    i.supplier_name IS DISTINCT FROM s.name
                 OR regexp_replace(upper(COALESCE(i.supplier_nif, '')), '^PT', '')
                    IS DISTINCT FROM
                    regexp_replace(upper(COALESCE(s.nif, '')), '^PT', '')
              )
            ORDER BY s.name, i.id
        """)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]


def get_supplier_identity_conflicts() -> list:
    """Return linked invoices needing human review, without changing any data."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT i.id, i.status, i.supplier_id, i.supplier_name, i.supplier_nif,
                   s.name, s.nif
            FROM invoices i
            JOIN suppliers s ON s.id = i.supplier_id
            WHERE i.supplier_id IS NOT NULL
            ORDER BY i.id
        """)
        conflicts = []
        for row in cursor.fetchall():
            (invoice_id, status, supplier_id, stored_name, stored_nif,
             canonical_name, canonical_nif) = row
            reasons = []
            if _normalise_identity_name(stored_name) != _normalise_identity_name(canonical_name):
                reasons.append('nome_divergente')
            if (stored_nif or canonical_nif) and not _nifs_match(stored_nif, canonical_nif):
                reasons.append('nif_divergente')
            if canonical_nif and not is_valid_portuguese_nif(canonical_nif):
                reasons.append('nif_portugues_invalido')
            if reasons:
                conflicts.append({
                    'invoice_id': invoice_id,
                    'status': status,
                    'supplier_id': supplier_id,
                    'stored_name': stored_name,
                    'stored_nif': stored_nif,
                    'canonical_name': canonical_name,
                    'canonical_nif': canonical_nif,
                    'reasons': reasons,
                })
    return conflicts


def upsert_supplier(name: str, nif: str = None,
                    store_id: int = None, notes: str = None,
                    payment_method: str = None, payment_terms: str = None,
                    iban: str = None, centro_custo_id: int = None,
                    categoria_custo_id: int = None, common_name: str = None) -> int:
    """Insert or update a supplier row."""
    nif = _normalize_nif(nif)
    common_name = normalize_supplier_common_name(common_name)
    with db_connection() as conn:
        cursor = conn.cursor()
        if nif:
            # Existing data contains both PT-prefixed and digits-only NIFs.
            # Resolve that identity before the unique-key upsert so imports
            # cannot create a second supplier merely because the prefix differs.
            # The transaction-scoped lock also serializes two concurrent
            # imports that represent the same NIF in different formats; the
            # database's raw-nif unique constraint cannot do that alone.
            nif_key = _nif_match_key(nif)
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f'supplier_nif:{nif_key}',),
            )
            cursor.execute("""
                SELECT id
                FROM suppliers
                WHERE regexp_replace(upper(COALESCE(nif, '')), '^PT', '') = %s
                LIMIT 1
            """, (nif_key,))
            existing = cursor.fetchone()
            if existing:
                supplier_id = existing[0]
                cursor.execute("""
                    UPDATE suppliers
                    SET store_id = %s,
                        notes = %s,
                        payment_method = COALESCE(%s, payment_method),
                        payment_terms = COALESCE(%s, payment_terms),
                        iban = COALESCE(%s, iban),
                        centro_custo_id = COALESCE(%s, centro_custo_id),
                        categoria_custo_id = COALESCE(%s, categoria_custo_id),
                        updated_at = NOW()
                    WHERE id = %s
                """, (store_id, notes, payment_method, payment_terms, iban,
                      centro_custo_id or None, categoria_custo_id or None, supplier_id))
            else:
                cursor.execute("""
                    INSERT INTO suppliers (name, common_name, nif, store_id, notes,
                                           payment_method, payment_terms, iban,
                                           centro_custo_id, categoria_custo_id, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (nif) WHERE nif IS NOT NULL DO UPDATE SET
                        -- An incoming OCR/display variant must not replace a
                        -- supplier's established canonical legal name. Renames
                        -- are handled through the explicit supplier editor.
                        store_id = EXCLUDED.store_id,
                        notes = EXCLUDED.notes,
                        payment_method = COALESCE(EXCLUDED.payment_method, suppliers.payment_method),
                        payment_terms = COALESCE(EXCLUDED.payment_terms, suppliers.payment_terms),
                        iban = COALESCE(EXCLUDED.iban, suppliers.iban),
                        centro_custo_id = COALESCE(EXCLUDED.centro_custo_id, suppliers.centro_custo_id),
                        categoria_custo_id = COALESCE(EXCLUDED.categoria_custo_id, suppliers.categoria_custo_id),
                        updated_at = NOW()
                    RETURNING id
                """, (name, common_name, nif, store_id, notes, payment_method, payment_terms, iban,
                      centro_custo_id or None, categoria_custo_id or None))
                supplier_id = cursor.fetchone()[0]
        else:
            # No NIF — look up by name first to avoid duplicates
            cursor.execute(
                "SELECT id FROM suppliers WHERE LOWER(name) = LOWER(%s) AND nif IS NULL LIMIT 1", (name,))
            row = cursor.fetchone()
            if row:
                supplier_id = row[0]
                # True upsert: update editable fields for existing null-NIF supplier
                cursor.execute("""
                    UPDATE suppliers SET
                        name = %s,
                        store_id = COALESCE(%s, store_id),
                        notes = COALESCE(%s, notes),
                        payment_method = COALESCE(%s, payment_method),
                        payment_terms = COALESCE(%s, payment_terms),
                        iban = COALESCE(%s, iban),
                        centro_custo_id = COALESCE(%s, centro_custo_id),
                        categoria_custo_id = COALESCE(%s, categoria_custo_id),
                        updated_at = NOW()
                    WHERE id = %s
                """, (name, store_id, notes,
                      payment_method, payment_terms, iban,
                      centro_custo_id or None, categoria_custo_id or None, supplier_id))
                # Keep the denormalized invoice identity canonical.
                cursor.execute(
                    "UPDATE invoices SET supplier_name = %s, supplier_nif = NULL "
                    "WHERE supplier_id = %s",
                    (name, supplier_id),
                )
            else:
                try:
                    cursor.execute("""
                        INSERT INTO suppliers (name, common_name, store_id, notes,
                                               payment_method, payment_terms, iban,
                                               centro_custo_id, categoria_custo_id, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        RETURNING id
                    """, (name, common_name, store_id, notes, payment_method, payment_terms, iban,
                          centro_custo_id or None, categoria_custo_id or None))
                    supplier_id = cursor.fetchone()[0]
                except psycopg2.errors.UniqueViolation:
                    # Race condition: another worker inserted the same name concurrently.
                    # Roll back the failed statement, then fetch the existing row and update it.
                    conn.rollback()
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT id FROM suppliers WHERE LOWER(name) = LOWER(%s) AND nif IS NULL LIMIT 1",
                        (name,))
                    row = cursor.fetchone()
                    if row is None:
                        raise
                    supplier_id = row[0]
                    cursor.execute("""
                        UPDATE suppliers SET
                            name = %s,
                            store_id = COALESCE(%s, store_id),
                            notes = COALESCE(%s, notes),
                            payment_method = COALESCE(%s, payment_method),
                            payment_terms = COALESCE(%s, payment_terms),
                            iban = COALESCE(%s, iban),
                            centro_custo_id = COALESCE(%s, centro_custo_id),
                            categoria_custo_id = COALESCE(%s, categoria_custo_id),
                            updated_at = NOW()
                        WHERE id = %s
                    """, (name, store_id, notes,
                          payment_method, payment_terms, iban,
                          centro_custo_id or None, categoria_custo_id or None, supplier_id))
                    # Keep the denormalized invoice identity canonical.
                    cursor.execute(
                        "UPDATE invoices SET supplier_name = %s, supplier_nif = NULL "
                        "WHERE supplier_id = %s",
                        (name, supplier_id),
                    )
                    logger.info("upsert_supplier: resolved race condition for null-NIF supplier '%s' (id=%s)", name, supplier_id)
            conn.commit()
            try:
                link_invoices_to_supplier_by_name(supplier_id)
            except Exception as _exc:
                logger.warning('link_invoices_to_supplier_by_name failed (no-nif path) for supplier %s: %s', supplier_id, _exc)
            return supplier_id
        cursor.execute("SELECT name, nif FROM suppliers WHERE id = %s", (supplier_id,))
        canonical_row = cursor.fetchone()
        canonical_name = canonical_row[0]
        canonical_nif = canonical_row[1] if len(canonical_row) > 1 else nif
        # Keep all already-linked invoice identity fields canonical.
        cursor.execute(
            "UPDATE invoices SET supplier_name = %s, supplier_nif = %s "
            "WHERE supplier_id = %s",
            (canonical_name, canonical_nif, supplier_id),
        )
        conn.commit()
    # Auto-link invoices whose supplier_name matches this supplier
    try:
        link_invoices_to_supplier_by_name(supplier_id)
    except Exception as exc:
        logger.warning('link_invoices_to_supplier_by_name failed for supplier %s: %s', supplier_id, exc)
    return supplier_id


def delete_supplier(supplier_id: int) -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM invoices WHERE supplier_id = %s", (supplier_id,))
        count = cursor.fetchone()[0]
        if count > 0:
            return False
        cursor.execute("DELETE FROM suppliers WHERE id = %s", (supplier_id,))
        conn.commit()
    return True


def merge_supplier(source_id: int, target_id: int) -> int:
    """Re-link all invoices from source supplier to target, then delete source.

    Also saves the source supplier's name (and NIF) as an alias of the target
    so that future OCR lookups for the old name resolve correctly.
    Existing aliases of the source are migrated to the target.

    Returns the number of invoices re-linked, or raises ValueError if the
    source or target does not exist or they are the same.
    """
    if source_id == target_id:
        raise ValueError('source_id e target_id devem ser diferentes.')
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, nif FROM suppliers WHERE id = %s", (source_id,))
        src_row = cursor.fetchone()
        if not src_row:
            raise ValueError(f'Fornecedor de origem {source_id} não encontrado.')
        _, source_name, source_nif = src_row

        cursor.execute("SELECT id, name, nif FROM suppliers WHERE id = %s", (target_id,))
        tgt_row = cursor.fetchone()
        if not tgt_row:
            raise ValueError(f'Fornecedor de destino {target_id} não encontrado.')
        _, target_name, target_nif = tgt_row

        # Migrate existing aliases from source → target
        try:
            cursor.execute(
                "UPDATE supplier_aliases SET supplier_id = %s WHERE supplier_id = %s",
                (target_id, source_id),
            )
            # Save source name as alias of target (ON CONFLICT = silently skip if name already aliased)
            cursor.execute("""
                INSERT INTO supplier_aliases (supplier_id, alias_name, alias_nif, source)
                VALUES (%s, %s, %s, 'merge')
                ON CONFLICT (alias_name) DO NOTHING
            """, (target_id, source_name, source_nif))
            # Guarantee the source NIF is always preserved even when the name-based INSERT
            # above was a no-op (conflict on alias_name with another supplier's alias).
            # Uses a synthetic alias_name that is guaranteed unique and will never collide
            # with a real supplier name (double-underscore prefix).
            if source_nif and source_nif != target_nif:
                nif_alias_key = f'__nif__{source_nif}__{source_id}'
                cursor.execute("""
                    INSERT INTO supplier_aliases (supplier_id, alias_name, alias_nif, source)
                    VALUES (%s, %s, %s, 'merge')
                    ON CONFLICT (alias_name) DO NOTHING
                """, (target_id, nif_alias_key, source_nif))
            # Clean up any ignored-pair records involving source
            cursor.execute(
                "DELETE FROM supplier_merge_ignored WHERE supplier_id_a = %s OR supplier_id_b = %s",
                (source_id, source_id),
            )
        except Exception as _alias_exc:
            logger.warning('merge_supplier: alias/ignored update skipped (table may not exist): %s', _alias_exc)

        cursor.execute("""
            UPDATE invoices
            SET supplier_id = %s,
                supplier_name = %s,
                supplier_nif = %s
            WHERE supplier_id = %s
        """, (target_id, target_name, target_nif, source_id))
        count = cursor.rowcount
        cursor.execute("DELETE FROM suppliers WHERE id = %s", (source_id,))
        conn.commit()
    logger.info('merge_supplier: %d→%d, %d invoice(s) re-linked', source_id, target_id, count)
    return count


def get_supplier_by_alias(name: str, nif: str = None) -> dict:
    """Look up a supplier via the alias table.

    Checks alias_name (case-insensitive) when no NIF is supplied. With a NIF,
    it only returns a supplier whose canonical or confirmed alias NIF agrees.
    Returns supplier dict or None. Safe to call before the migration has run.
    """
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            if nif:
                cursor.execute("""
                    SELECT s.id, s.name, s.nif, s.store_id, s.notes,
                           st.name AS store_name, s.payment_method, s.payment_terms, s.iban,
                           a.alias_nif
                    FROM supplier_aliases a
                    JOIN suppliers s ON s.id = a.supplier_id
                    LEFT JOIN stores st ON s.store_id = st.id
                    WHERE LOWER(a.alias_name) = LOWER(%s)
                       OR a.alias_nif IS NOT NULL
                    ORDER BY s.id, a.id
                """, (name,))
                rows = cursor.fetchall()
                row = next(
                    (candidate for candidate in rows
                     if _nifs_match(nif, candidate[2]) or _nifs_match(nif, candidate[8])),
                    None,
                )
            else:
                cursor.execute("""
                    SELECT s.id, s.name, s.nif, s.store_id, s.notes,
                           st.name AS store_name, s.payment_method, s.payment_terms, s.iban
                    FROM supplier_aliases a
                    JOIN suppliers s ON s.id = a.supplier_id
                    LEFT JOIN stores st ON s.store_id = st.id
                    WHERE LOWER(a.alias_name) = LOWER(%s)
                    LIMIT 1
                """, (name,))
                row = cursor.fetchone()
        if row:
            return {'id': row[0], 'name': row[1], 'nif': row[2],
                    'store_id': row[3], 'notes': row[4], 'store_name': row[5],
                    'payment_method': row[6], 'payment_terms': row[7], 'iban': row[8]}
    except Exception:
        pass  # alias table may not exist yet
    return None


def get_supplier_aliases(supplier_id: int) -> list:
    """Return all aliases for a given supplier_id. Safe to call before migration."""
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, alias_name, alias_nif, source, created_at
                FROM supplier_aliases
                WHERE supplier_id = %s
                ORDER BY alias_name
            """, (supplier_id,))
            rows = cursor.fetchall()
        return [
            {'id': r[0], 'alias_name': r[1], 'alias_nif': r[2],
             'source': r[3], 'created_at': r[4]}
            for r in rows
        ]
    except Exception:
        return []


def get_all_supplier_aliases() -> dict:
    """Return a dict of {supplier_id: [alias dicts]} for all suppliers. Safe to call before migration."""
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, supplier_id, alias_name, alias_nif, source, created_at
                FROM supplier_aliases
                ORDER BY supplier_id, alias_name
            """)
            rows = cursor.fetchall()
        result = {}
        for r in rows:
            sid = r[1]
            result.setdefault(sid, []).append(
                {'id': r[0], 'alias_name': r[2], 'alias_nif': r[3],
                 'source': r[4], 'created_at': r[5]}
            )
        return result
    except Exception:
        return {}


def delete_supplier_alias_with_stats(alias_id: int) -> dict:
    """Remove an alias and canonicalise matching historical invoice NIFs.

    The alias row and the invoice updates deliberately share one transaction.
    Only invoices already linked to the alias' supplier are touched; this must
    never become a supplier reassignment mechanism.
    """
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    SELECT supplier_id, alias_nif
                    FROM supplier_aliases
                    WHERE id = %s
                    FOR UPDATE
                """, (alias_id,))
                alias_row = cursor.fetchone()
                if not alias_row:
                    return {'deleted': False, 'invoices_updated': 0}

                supplier_id, alias_nif = alias_row
                cursor.execute("""
                    SELECT nif
                    FROM suppliers
                    WHERE id = %s
                    FOR SHARE
                """, (supplier_id,))
                supplier_row = cursor.fetchone()
                if not supplier_row or not supplier_row[0]:
                    raise ValueError(
                        f'Fornecedor {supplier_id} do alias {alias_id} não encontrado '
                        'ou sem NIF canónico.'
                    )

                canonical_nif = _normalize_nif(supplier_row[0])
                invoices_updated = 0
                normalized_alias_nif = _normalize_nif(alias_nif)
                if normalized_alias_nif:
                    # Strip formatting and the optional PT prefix on both sides.
                    # The supplier_id predicate prevents touching another
                    # supplier even if the same NIF appears in bad legacy data.
                    cursor.execute("""
                        UPDATE invoices
                        SET supplier_nif = %s
                        WHERE supplier_id = %s
                          AND NULLIF(
                              regexp_replace(
                                  regexp_replace(UPPER(COALESCE(supplier_nif, '')),
                                                 '[[:space:]-]', '', 'g'),
                                  '^PT', ''
                              ),
                              ''
                          ) = regexp_replace(%s, '^PT', '')
                    """, (canonical_nif, supplier_id, normalized_alias_nif))
                    invoices_updated = cursor.rowcount

                cursor.execute(
                    "DELETE FROM supplier_aliases WHERE id = %s",
                    (alias_id,),
                )
                if cursor.rowcount <= 0:
                    raise RuntimeError(f'Alias {alias_id} desapareceu durante a remoção.')

                conn.commit()
                return {
                    'deleted': True,
                    'invoices_updated': invoices_updated,
                }
            except Exception:
                conn.rollback()
                raise
    except Exception as exc:
        logger.warning('delete_supplier_alias(%s) failed: %s', alias_id, exc)
        return {'deleted': False, 'invoices_updated': 0}


def delete_supplier_alias(alias_id: int) -> bool:
    """Delete a single alias row by primary key. Returns True if deleted.

    Kept as a boolean wrapper for callers that predate the historical-NIF
    update. Use ``delete_supplier_alias_with_stats`` when the update count is
    needed for user feedback.
    """
    return delete_supplier_alias_with_stats(alias_id)['deleted']


def add_supplier_alias(supplier_id: int, alias_name: str, alias_nif: str = None) -> bool:
    """Insert a manual alias for a supplier. Returns True on success, False on duplicate/error."""
    if not alias_name or not alias_name.strip():
        return False
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO supplier_aliases (supplier_id, alias_name, alias_nif, source)
                VALUES (%s, %s, %s, 'manual')
                ON CONFLICT (alias_name) DO NOTHING
            """, (supplier_id, alias_name.strip(), alias_nif or None))
            inserted = cursor.rowcount > 0
            conn.commit()
        return inserted
    except Exception as exc:
        logger.warning('add_supplier_alias(%s, %r) failed: %s', supplier_id, alias_name, exc)
        return False


def get_duplicate_supplier_suggestions(suppliers: list = None, threshold: float = 0.85) -> list:
    """Return pairs of suppliers that may be duplicates.

    Criteria:
    - Critério A: same non-null NIF (should be impossible but may exist from direct inserts)
    - Critério B: SequenceMatcher similarity >= threshold on lowercased name
    - Critério C: one substantial normalized name is a prefix of the other
      (for example, "Grasumos" and "Grasumos Comércio de Bebidas"). This is
      a suggestion only; it must always be confirmed by a user.

    Pairs in supplier_merge_ignored are excluded.
    Returns list of dicts: {a: supplier, b: supplier, reason: str}.
    """
    from difflib import SequenceMatcher

    if suppliers is None:
        suppliers = get_suppliers_with_invoice_count()

    ignored = set()
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT LEAST(supplier_id_a,supplier_id_b), GREATEST(supplier_id_a,supplier_id_b) "
                "FROM supplier_merge_ignored"
            )
            for row in cursor.fetchall():
                ignored.add((row[0], row[1]))
    except Exception:
        pass  # table may not exist yet

    suggestions = []
    seen = set()
    for i, a in enumerate(suppliers):
        for b in suppliers[i + 1:]:
            pair_key = (min(a['id'], b['id']), max(a['id'], b['id']))
            if pair_key in seen or pair_key in ignored:
                continue
            seen.add(pair_key)

            # Critério A: same NIF
            if a['nif'] and b['nif'] and a['nif'] == b['nif']:
                suggestions.append({'a': a, 'b': b, 'reason': f'Mesmo NIF: {a["nif"]}'})
                continue

            # Critério B: name similarity
            ratio = SequenceMatcher(None, a['name'].lower(), b['name'].lower()).ratio()
            if ratio >= threshold:
                suggestions.append({
                    'a': a, 'b': b,
                    'reason': f'Nomes semelhantes ({int(ratio * 100)}%)',
                })
                continue

            # Critério C: an OCR/display name is often a short leading portion
            # of the legal supplier name. Do not merge automatically — the NIF
            # may be absent on the short record — but make the case visible in
            # the existing human-confirmed merge workflow.
            a_key = ''.join(ch for ch in a['name'].lower() if ch.isalnum())
            b_key = ''.join(ch for ch in b['name'].lower() if ch.isalnum())
            shorter, longer = sorted((a_key, b_key), key=len)
            if len(shorter) >= 6 and longer.startswith(shorter):
                suggestions.append({
                    'a': a, 'b': b,
                    'reason': 'Nome base coincidente — confirmar antes de fundir',
                })

    return suggestions


def find_similar_suppliers(name: str, threshold: float = 0.70) -> list:
    """Find registered suppliers whose name is similar to the given query.

    Returns up to 5 matches sorted by descending similarity, each enriched
    with a 'similarity' field (0-100 integer percentage).
    """
    from difflib import SequenceMatcher
    if not name:
        return []
    suppliers = get_suppliers_with_invoice_count()
    query = name.lower()
    results = []
    for s in suppliers:
        ratio = SequenceMatcher(None, query, s['name'].lower()).ratio()
        if ratio >= threshold:
            results.append({**s, 'similarity': round(ratio * 100)})
    results.sort(key=lambda x: -x['similarity'])
    return results[:5]


def ignore_supplier_pair(id_a: int, id_b: int) -> None:
    """Record that this pair should not appear in duplicate suggestions.

    Always stores the smaller ID as supplier_id_a to satisfy the CHECK constraint
    and ensure a consistent primary key regardless of argument order.
    """
    lo, hi = (min(id_a, id_b), max(id_a, id_b))
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO supplier_merge_ignored (supplier_id_a, supplier_id_b)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
            """, (lo, hi))
            conn.commit()
    except Exception as exc:
        logger.warning('ignore_supplier_pair failed: %s', exc)


def get_supplier_by_name(name: str) -> dict:
    """Look up a supplier by name (case-insensitive). Returns the first match or None."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.id, s.name, s.nif, s.store_id, s.notes,
                   st.name AS store_name, s.payment_method, s.payment_terms, s.iban
            FROM suppliers s
            LEFT JOIN stores st ON s.store_id = st.id
            WHERE LOWER(s.name) = LOWER(%s)
            LIMIT 1
        """, (name,))
        row = cursor.fetchone()
    if row:
        return {'id': row[0], 'name': row[1], 'nif': row[2],
                'store_id': row[3], 'notes': row[4], 'store_name': row[5],
                'payment_method': row[6], 'payment_terms': row[7], 'iban': row[8]}
    return None


def link_invoices_to_supplier_by_name(supplier_id: int) -> int:
    """Link all non-draft invoices whose supplier_name matches this supplier's name.

    Matching is case-insensitive.  Only invoices that currently have no
    supplier_id (or already point to this supplier) are updated.
    Returns the number of rows updated.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name, nif FROM suppliers WHERE id = %s", (supplier_id,))
        row = cursor.fetchone()
        if not row:
            return 0
        name, nif = row
        nif_key = _nif_match_key(nif)
        cursor.execute("""
            UPDATE invoices
            SET supplier_id = %s,
                supplier_name = %s,
                supplier_nif = %s
            WHERE LOWER(supplier_name) = LOWER(%s)
              AND status != 'draft'
              AND (supplier_id IS NULL OR supplier_id = %s)
              AND (
                    supplier_nif IS NULL
                 OR regexp_replace(upper(COALESCE(supplier_nif, '')), '^PT', '') = %s
              )
        """, (supplier_id, name, nif, name, supplier_id, nif_key))
        count = cursor.rowcount
        conn.commit()
    return count


def bulk_link_invoices_by_name(supplier_name: str, supplier_id: int) -> int:
    """Bulk-update invoices with the given supplier_name (exact text) to point to supplier_id.

    Also back-fills supplier_nif from the supplier record.
    Returns the number of rows updated.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name, nif FROM suppliers WHERE id = %s", (supplier_id,))
        row = cursor.fetchone()
        canonical_name = row[0] if row else supplier_name
        nif = row[1] if row else None
        cursor.execute("""
            UPDATE invoices
            SET supplier_id = %s,
                supplier_name = %s,
                supplier_nif = %s
            WHERE supplier_name = %s
              AND status != 'draft'
              AND (supplier_id IS NULL OR supplier_id = %s)
              AND (
                    supplier_nif IS NULL
                 OR regexp_replace(upper(COALESCE(supplier_nif, '')), '^PT', '') = %s
              )
        """, (
            supplier_id, canonical_name, nif, supplier_name, supplier_id,
            _nif_match_key(nif),
        ))
        count = cursor.rowcount
        conn.commit()
    return count


def get_unlinked_supplier_names() -> list:
    """Return distinct supplier_names from non-draft invoices with no supplier_id.

    Each entry: {'name': str, 'nif': str|None, 'invoice_count': int}
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT supplier_name,
                   MAX(supplier_nif) AS nif,
                   COUNT(*) AS invoice_count
            FROM invoices
            WHERE supplier_id IS NULL
              AND status != 'draft'
              AND supplier_name IS NOT NULL
              AND supplier_name != ''
            GROUP BY supplier_name
            ORDER BY supplier_name
        """)
        rows = cursor.fetchall()
    return [{'name': r[0], 'nif': r[1], 'invoice_count': int(r[2])} for r in rows]


def get_suppliers_with_invoice_count() -> list:
    """Return all suppliers with count and total spend of linked non-draft invoices."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT s.id, s.name, s.nif, s.store_id, s.notes,
                   st.name AS store_name, s.payment_method, s.payment_terms, s.iban,
                   COUNT(i.id) AS invoice_count,
                   COALESCE(SUM(i.amount_eur), 0) AS total_spend,
                   s.centro_custo_id, cc.name AS centro_custo_name,
                    s.categoria_custo_id, ccat.name AS categoria_custo_name,
                    s.common_name
            FROM suppliers s
            LEFT JOIN stores st ON s.store_id = st.id
            LEFT JOIN cost_centers cc ON s.centro_custo_id = cc.id
            LEFT JOIN cost_categories ccat ON s.categoria_custo_id = ccat.id
            LEFT JOIN invoices i ON i.supplier_id = s.id AND i.status != 'draft'
            GROUP BY s.id, s.name, s.nif, s.store_id, s.notes,
                     st.name, s.payment_method, s.payment_terms, s.iban,
                     s.centro_custo_id, cc.name,
                      s.categoria_custo_id, ccat.name, s.common_name
            ORDER BY COALESCE(NULLIF(s.common_name, ''), s.name)
        """)
        rows = cursor.fetchall()
    return [{'id': r[0], 'name': r[1], 'nif': r[2],
             'store_id': r[3], 'notes': r[4], 'store_name': r[5],
             'payment_method': r[6], 'payment_terms': r[7], 'iban': r[8],
             'invoice_count': int(r[9]),
             'total_spend': float(r[10]),
             'centro_custo_id': r[11], 'centro_custo_name': r[12],
              'categoria_custo_id': r[13], 'categoria_custo_name': r[14],
              'common_name': r[15]} for r in rows]


def _get_supplier_invoice_classification_config(cursor, supplier_id: int) -> dict:
    """Load a supplier's configured, active classification references."""
    cursor.execute("""
        SELECT s.id, s.name,
               s.centro_custo_id, cc.id, cc.name, cc.ativo,
               s.categoria_custo_id, cat.id, cat.name, cat.ativo
        FROM suppliers s
        LEFT JOIN cost_centers cc ON cc.id = s.centro_custo_id
        LEFT JOIN cost_categories cat ON cat.id = s.categoria_custo_id
        WHERE s.id = %s
    """, (supplier_id,))
    row = cursor.fetchone()
    if not row:
        raise ValueError('Fornecedor não encontrado.')

    supplier = {'id': row[0], 'name': row[1]}
    fields = {
        'centro_custo': {
            'label': 'Centro de Custo',
            'id': row[2],
            'name': row[4],
            'configured': bool(row[2]),
            'valid': bool(row[2] and row[3] and row[5]),
            'reason': None,
        },
        'categoria_custo': {
            'label': 'Categoria de Custo',
            'id': row[6],
            'name': row[8],
            'configured': bool(row[6]),
            'valid': bool(row[6] and row[7] and row[9]),
            'reason': None,
        },
    }
    for field in fields.values():
        if field['configured'] and not field['valid']:
            field['reason'] = f'{field["label"]} configurado já não existe ou está inativo.'
    return {'supplier': supplier, 'fields': fields}


def get_supplier_invoice_classification_preview(supplier_id: int) -> dict:
    """Return the safe, per-field propagation preview for one supplier.

    Only non-draft invoices linked to the supplier are considered. A split
    cost-centre allocation counts as an existing classification and is never
    eligible for a primary cost-centre assignment.
    """
    if not isinstance(supplier_id, int) or supplier_id <= 0:
        raise ValueError('Fornecedor inválido.')
    with db_connection() as conn:
        cursor = conn.cursor()
        preview = _get_supplier_invoice_classification_config(cursor, supplier_id)
        cursor.execute("""
            SELECT
                COUNT(*) FILTER (
                    WHERE i.centro_custo_id IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM invoice_centros_custo icc
                          WHERE icc.invoice_id = i.id
                      )
                ) AS centro_custo_count,
                COUNT(*) FILTER (
                    WHERE i.categoria_custo_id IS NULL
                ) AS categoria_custo_count
            FROM invoices i
            WHERE i.supplier_id = %s
              AND i.status != 'draft'
        """, (supplier_id,))
        counts = cursor.fetchone()

    preview['fields']['centro_custo']['eligible_count'] = int(counts[0] or 0) \
        if preview['fields']['centro_custo']['valid'] else 0
    preview['fields']['categoria_custo']['eligible_count'] = int(counts[1] or 0) \
        if preview['fields']['categoria_custo']['valid'] else 0
    return preview


def _get_supplier_classification_actor(changed_by: str = None) -> str:
    """Resolve the actor for supplier classification audit entries.

    The database helper is also called by scripts and tests outside Flask, so
    keep ``sistema`` as the non-request fallback.  During a web request, the
    session username is used when older callers do not pass ``changed_by``.
    """
    if changed_by:
        return str(changed_by)
    try:
        from flask import has_request_context, session
        if has_request_context():
            return session.get('user', {}).get('username', 'sistema') or 'sistema'
    except Exception:
        pass
    return 'sistema'


def _write_supplier_classification_audit(
    cursor,
    invoice_ids: list,
    field_name: str,
    classification_name: str,
    supplier_name: str,
    changed_by: str,
) -> None:
    """Record one source-aware audit entry for each changed invoice."""
    if not invoice_ids:
        return
    source = f'{classification_name} — configuração do fornecedor: {supplier_name}'
    for invoice_id in invoice_ids:
        cursor.execute(
            "INSERT INTO invoice_audit_log "
            "(invoice_id, campo_alterado, valor_anterior, valor_novo, alterado_por) "
            "VALUES (%s, %s, NULL, %s, %s)",
            (invoice_id, field_name, source, changed_by),
        )


def apply_supplier_invoice_classifications(supplier_id: int, changed_by: str = None) -> dict:
    """Fill a supplier's configured classifications into empty linked invoices.

    The conditions are repeated in the UPDATE statements rather than relying
    on a previous preview, so re-running the operation is idempotent and can
    never overwrite an existing primary or split classification.  Each invoice
    actually changed gets a source-aware audit entry in the same transaction.
    """
    if not isinstance(supplier_id, int) or supplier_id <= 0:
        raise ValueError('Fornecedor inválido.')
    with db_connection() as conn:
        cursor = conn.cursor()
        result = _get_supplier_invoice_classification_config(cursor, supplier_id)
        invalid = [field['reason'] for field in result['fields'].values() if field['reason']]
        if invalid:
            raise ValueError(' '.join(invalid))

        centro = result['fields']['centro_custo']
        categoria = result['fields']['categoria_custo']
        actor = _get_supplier_classification_actor(changed_by)
        centro_updated = 0
        categoria_updated = 0

        if centro['configured']:
            cursor.execute("""
                UPDATE invoices i
                SET centro_custo_id = %s
                WHERE i.supplier_id = %s
                  AND i.status != 'draft'
                  AND i.centro_custo_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM invoice_centros_custo icc
                      WHERE icc.invoice_id = i.id
                  )
                RETURNING i.id
            """, (centro['id'], supplier_id))
            centro_updated = cursor.rowcount
            fetch_changed = getattr(cursor, 'fetchall', None)
            centro_invoice_ids = [row[0] for row in fetch_changed()] if fetch_changed else []
            _write_supplier_classification_audit(
                cursor,
                centro_invoice_ids,
                'centro_custo_id',
                centro['name'],
                result['supplier']['name'],
                actor,
            )

        if categoria['configured']:
            cursor.execute("""
                UPDATE invoices i
                SET categoria_custo_id = %s
                WHERE i.supplier_id = %s
                  AND i.status != 'draft'
                  AND i.categoria_custo_id IS NULL
                RETURNING i.id
            """, (categoria['id'], supplier_id))
            categoria_updated = cursor.rowcount
            fetch_changed = getattr(cursor, 'fetchall', None)
            categoria_invoice_ids = [row[0] for row in fetch_changed()] if fetch_changed else []
            _write_supplier_classification_audit(
                cursor,
                categoria_invoice_ids,
                'categoria_custo_id',
                categoria['name'],
                result['supplier']['name'],
                actor,
            )

        conn.commit()

    return {
        'supplier': result['supplier'],
        'centro_custo': {
            'configured': centro['configured'],
            'name': centro['name'],
            'updated_count': centro_updated,
        },
        'categoria_custo': {
            'configured': categoria['configured'],
            'name': categoria['name'],
            'updated_count': categoria_updated,
        },
    }


def backfill_supplier_ids() -> dict:
    """One-time migration: create supplier records for distinct supplier names in invoices
    and back-fill supplier_id on all matching non-draft invoices.

    For each distinct supplier_name without a supplier_id:
    1. Check if a supplier with that name (case-insensitive) already exists.
    2. If not and there is a NIF on the invoice, try matching by NIF, then create.
    3. Update all invoices with that name to point to the supplier.

    Returns {'suppliers_created': int, 'invoices_linked': int}.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        # Advisory lock prevents concurrent backfill runs from creating duplicate suppliers
        cursor.execute("SELECT pg_try_advisory_xact_lock(hashtext('backfill_supplier_ids'))")
        if not cursor.fetchone()[0]:
            logger.warning('backfill_supplier_ids: another run is in progress, skipping')
            return {'suppliers_created': 0, 'invoices_linked': 0}
        cursor.execute("""
            SELECT supplier_name,
                   COALESCE(supplier_nif, '') AS nif
            FROM invoices
            WHERE supplier_id IS NULL
              AND status != 'draft'
              AND supplier_name IS NOT NULL
              AND supplier_name != ''
            GROUP BY supplier_name, COALESCE(supplier_nif, '')
            ORDER BY supplier_name, COALESCE(supplier_nif, '')
        """)
        unlinked = cursor.fetchall()
        # Aliases are created only after a human-confirmed merge or an explicit
        # manual choice. They are therefore safe for backfill matching, unlike
        # fuzzy name similarity. Use to_regclass so this remains compatible with
        # databases created before supplier_aliases was introduced.
        cursor.execute("SELECT to_regclass('public.supplier_aliases')")
        aliases_available = cursor.fetchone()[0] is not None

        suppliers_created = 0
        invoices_linked = 0

        for supplier_name, nif in unlinked:
            row = None
            if nif:
                # When a NIF exists it is the required proof of identity. Do
                # not link only because a display name happens to match.
                nif_key = _nif_match_key(nif)
                cursor.execute("""
                    SELECT id, name, nif
                    FROM suppliers
                    WHERE regexp_replace(upper(COALESCE(nif, '')), '^PT', '') = %s
                    LIMIT 1
                """, (nif_key,))
                row = cursor.fetchone()
                if not row and aliases_available:
                    cursor.execute("""
                        SELECT s.id, s.name, s.nif
                        FROM supplier_aliases a
                        JOIN suppliers s ON s.id = a.supplier_id
                        WHERE regexp_replace(upper(COALESCE(a.alias_nif, '')), '^PT', '') = %s
                        LIMIT 1
                    """, (nif_key,))
                    row = cursor.fetchone()
            else:
                # No NIF: exact canonical name or a human-confirmed alias is
                # safe. Similar-looking names remain for user confirmation.
                cursor.execute(
                    "SELECT id, name, nif FROM suppliers WHERE LOWER(name) = LOWER(%s)",
                    (supplier_name,),
                )
                row = cursor.fetchone()
                if not row and aliases_available:
                    cursor.execute("""
                        SELECT s.id, s.name, s.nif
                        FROM supplier_aliases a
                        JOIN suppliers s ON s.id = a.supplier_id
                        WHERE LOWER(a.alias_name) = LOWER(%s)
                        LIMIT 1
                    """, (supplier_name,))
                    row = cursor.fetchone()

            if row:
                supplier_id, canonical_name = row[0], row[1]
                canonical_nif = row[2] if len(row) > 2 else (nif or None)
            elif nif:
                # Create a separate record when no NIF-confirmed identity
                # exists. It will be visible for human duplicate review.
                cursor.execute("""
                    INSERT INTO suppliers (name, nif, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (nif) WHERE nif IS NOT NULL DO UPDATE SET
                        updated_at = NOW()
                    RETURNING id, name
                """, (supplier_name, nif))
                result = cursor.fetchone()
                supplier_id, canonical_name = result[0], result[1]
                canonical_nif = result[2] if len(result) > 2 else nif
                suppliers_created += 1
            else:
                # 3. Create without NIF
                cursor.execute("""
                    INSERT INTO suppliers (name, updated_at)
                    VALUES (%s, NOW())
                    RETURNING id, name
                """, (supplier_name,))
                result = cursor.fetchone()
                supplier_id, canonical_name = result[0], result[1]
                canonical_nif = result[2] if len(result) > 2 else None
                suppliers_created += 1

            cursor.execute("""
                UPDATE invoices
                SET supplier_id = %s,
                    supplier_name = %s,
                    supplier_nif = %s
                WHERE LOWER(supplier_name) = LOWER(%s)
                  AND COALESCE(supplier_nif, '') = %s
                  AND supplier_id IS NULL
                  AND status != 'draft'
            """, (supplier_id, canonical_name, canonical_nif, supplier_name, nif))
            invoices_linked += cursor.rowcount

        conn.commit()
    logger.info('backfill_supplier_ids: created=%d linked=%d', suppliers_created, invoices_linked)
    return {'suppliers_created': suppliers_created, 'invoices_linked': invoices_linked}


# ── Invoices ───────────────────────────────────────────────────────────────────

INVOICE_STATUS_LABELS = {
    'draft': 'Rascunho',
    'pending_review': 'Pendente revisão',
    'scheduled': 'Agendada',
    'paid': 'Paga',
    'overdue': 'Vencida',
    'cancelled': 'Cancelada',
}

_STATUS_COLORS_FALLBACK = {
    'draft':          'bg-secondary',
    'pending_review': 'bg-warning text-dark',
    'scheduled':      'bg-info text-dark',
    'paid':           'bg-success',
    'overdue':        'bg-danger',
    'cancelled':      'bg-secondary',
}


def get_invoice_status_configs():
    """Return all rows from invoice_status_config ordered by sort_order (no cache)."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT key, label, bg_class, sort_order, active "
            "FROM invoice_status_config ORDER BY sort_order, key"
        )
        return [dict(r) for r in cursor.fetchall()]


@ttl_cache('invoice_status_labels_map', ttl=120)
def get_invoice_status_labels_map() -> dict:
    """Cached {key: label} map for invoice status labels. Falls back to hardcoded."""
    try:
        rows = get_invoice_status_configs()
        if rows:
            return {r['key']: r['label'] for r in rows}
    except Exception:
        pass
    return dict(INVOICE_STATUS_LABELS)


@ttl_cache('invoice_status_colors_map', ttl=120)
def get_invoice_status_colors_map() -> dict:
    """Cached {key: bg_class} map for badge rendering. Falls back to hardcoded."""
    try:
        rows = get_invoice_status_configs()
        if rows:
            return {r['key']: r['bg_class'] for r in rows}
    except Exception:
        pass
    return dict(_STATUS_COLORS_FALLBACK)


_NON_BULK_STATUS_KEYS = frozenset({'draft', 'overdue'})


@ttl_cache('invoice_status_bulk_allowed', ttl=120)
def get_invoice_status_bulk_allowed() -> list:
    """Cached ordered list of active status keys valid for bulk/dropdown actions."""
    _FALLBACK = ['pending_review', 'scheduled', 'paid', 'cancelled']
    try:
        rows = get_invoice_status_configs()
        if rows:
            return [r['key'] for r in rows
                    if r['active'] and r['key'] not in _NON_BULK_STATUS_KEYS]
    except Exception:
        pass
    return _FALLBACK


def upsert_invoice_status_config(key: str, label: str, bg_class: str,
                                  sort_order: int, active: bool) -> None:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO invoice_status_config (key, label, bg_class, sort_order, active, updated_at)
               VALUES (%s, %s, %s, %s, %s, NOW())
               ON CONFLICT (key) DO UPDATE SET
                   label      = EXCLUDED.label,
                   bg_class   = EXCLUDED.bg_class,
                   sort_order = EXCLUDED.sort_order,
                   active     = EXCLUDED.active,
                   updated_at = NOW()""",
            (key, label, bg_class, sort_order, active),
        )
        conn.commit()
    _cache_invalidate('invoice_status_colors_map', 'invoice_status_labels_map',
                      'invoice_status_bulk_allowed')


def bulk_update_invoice_status_sort_order(ordered_keys: list) -> None:
    """Update sort_order for each key based on its position in ordered_keys list."""
    if not ordered_keys:
        return
    with db_connection() as conn:
        cursor = conn.cursor()
        for position, key in enumerate(ordered_keys):
            cursor.execute(
                "UPDATE invoice_status_config SET sort_order = %s, updated_at = NOW() WHERE key = %s",
                (position, key),
            )
        conn.commit()
    _cache_invalidate('invoice_status_colors_map', 'invoice_status_labels_map',
                      'invoice_status_bulk_allowed')


def delete_invoice_status_config(key: str) -> None:
    """Delete a status config row. Raises ValueError if invoices use this status."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM invoices WHERE status = %s", (key,))
        count = cursor.fetchone()[0]
        if count > 0:
            raise ValueError(
                f"Existem {count} fatura(s) com este estado. Não é possível eliminar."
            )
        cursor.execute("DELETE FROM invoice_status_config WHERE key = %s", (key,))
        conn.commit()
    _cache_invalidate('invoice_status_colors_map', 'invoice_status_labels_map',
                      'invoice_status_bulk_allowed')


ONEDRIVE_SUBFOLDERS = [
    'Bolhão (B)', 'Distribuição (D)', 'Eventos (E)', 'Faturas partilhadas (FP)',
    'Geral (G)', 'Matosinhos (M)', 'Produção (P)', 'Timeout (TOM)',
]

INVOICE_CATEGORIES = [
    'Matérias-primas', 'Embalagens', 'Serviços', 'Utilities', 'Rendas',
    'Equipamentos', 'Marketing', 'Transportes', 'Outros'
]


DOCUMENT_TYPE_LABELS = {
    'fatura':                 'Fatura',
    'nota_credito':           'Nota de Crédito',
    'nota_debito':            'Nota de Débito',
    'nota_pagamento_imposto': 'Nota de Pagamento de Imposto',
    'outro':                  'Outro Documento',
}

# Types that represent a supplier invoice (full supplier fields required)
DOCUMENT_TYPES_INVOICE = {'fatura', 'nota_credito', 'nota_debito'}


def _row_to_invoice(row) -> dict:
    document_type = row[21] if len(row) > 21 else 'fatura'
    if not document_type:
        document_type = 'fatura'
    return {
        'id': row[0],
        'supplier_id': row[1],
        'supplier_name': row[2],
        'supplier_display_name': row[2],
        'supplier_legal_name': row[2],
        'supplier_nif': row[3],
        'invoice_number': row[4],
        'amount_eur': float(row[5]) if row[5] is not None else None,
        'vat_amount_eur': float(row[6]) if row[6] is not None else None,
        'issue_date': row[7],
        'due_date': row[8],
        'category': row[9],
        'onedrive_subfolder': row[10],
        'onedrive_path': row[11],
        'pdf_filename': row[12],
        'status': row[13],
        'ocr_confidence': float(row[14]) if row[14] is not None else None,
        'created_by': row[15],
        'cfo_confirmed_date': row[16],
        'paid_date': row[17],
        'notes': row[18],
        'created_at': row[19],
        'onedrive_web_url': row[20] if len(row) > 20 else None,
        'document_type': document_type,
        'document_type_label': DOCUMENT_TYPE_LABELS.get(document_type, document_type),
        'status_label': get_invoice_status_labels_map().get(row[13], row[13]),
        'centro_custo_id': row[22] if len(row) > 22 else None,
        'categoria_custo_id': row[23] if len(row) > 23 else None,
        'centro_custo_name': row[29] if len(row) > 29 else None,
        'categoria_custo_name': row[30] if len(row) > 30 else None,
        'has_pdf': False,
        'pdf_is_image': (row[12] or '').lower().rsplit('.', 1)[-1] in ('jpg', 'jpeg', 'png', 'gif', 'webp') if row[12] else False,
        'has_duplicate': False,
    }


_ORDER_COL_MAP = {
    'due_date': 'i.due_date',
    'issue_date': 'i.issue_date',
    'paid_date': 'i.paid_date',
    'amount_eur': 'i.amount_eur',
    'supplier_name': "LOWER(COALESCE(NULLIF(s.common_name, ''), s.name, i.supplier_name))",
    'invoice_number': 'i.invoice_number',
    'category': 'i.category',
    'status': 'i.status',
    'cfo_confirmed_date': 'i.cfo_confirmed_date',
    # These names are selected by get_invoices below and can therefore be
    # referenced safely by the table's sortable column headers.
    # PostgreSQL accepts a SELECT alias directly in ORDER BY, but not when the
    # alias is nested inside an expression such as LOWER(centro_custo_name).
    # Keep this as the bare displayed-label alias.
    'centro_custo_name': 'centro_custo_name',
    'categoria_custo_name': 'LOWER(ccat.name)',
}


def get_invoice_suppliers() -> list:
    """Return suppliers that have at least one non-draft invoice, sorted by name.

    Each entry is a dict with 'id' (int) and 'name' (str).
    Only suppliers linked via supplier_id are returned; invoices without a
    supplier_id are not represented and remain accessible via the 'All' option.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT s.id, COALESCE(NULLIF(s.common_name, ''), s.name)
            FROM suppliers s
            JOIN invoices i ON i.supplier_id = s.id
            WHERE i.status != 'draft'
            ORDER BY COALESCE(NULLIF(s.common_name, ''), s.name)
        """)
        return [{'id': row[0], 'name': row[1]} for row in cursor.fetchall()]


def get_distinct_supplier_names() -> list:
    """Return distinct supplier names from non-draft invoices, sorted.

    For invoices linked to a supplier (supplier_id IS NOT NULL), the canonical
    name from the suppliers table is used.  For unlinked invoices the raw
    supplier_name is kept.  This eliminates OCR-variant duplicates from the
    filter dropdown once invoices are linked to their supplier record.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT COALESCE(NULLIF(s.common_name, ''), s.name, i.supplier_name) AS display_name
            FROM invoices i
            LEFT JOIN suppliers s ON s.id = i.supplier_id
            WHERE i.status != 'draft'
              AND COALESCE(NULLIF(s.common_name, ''), s.name, i.supplier_name) IS NOT NULL
              AND COALESCE(NULLIF(s.common_name, ''), s.name, i.supplier_name) != ''
            ORDER BY display_name
        """)
        return [row[0] for row in cursor.fetchall()]


def normalise_supplier_names() -> int:
    """Copy suppliers.name → invoices.supplier_name for every linked invoice
    where the stored text differs from the canonical name.

    Returns the number of rows updated.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE invoices
            SET supplier_name = s.name,
                supplier_nif = s.nif
            FROM suppliers s
            WHERE invoices.supplier_id = s.id
              AND (
                    invoices.supplier_name IS DISTINCT FROM s.name
                 OR regexp_replace(upper(COALESCE(invoices.supplier_nif, '')), '^PT', '')
                    IS DISTINCT FROM
                    regexp_replace(upper(COALESCE(s.nif, '')), '^PT', '')
              )
        """)
        count = cursor.rowcount
        conn.commit()
    logger.info('normalise_supplier_names: updated %d invoice(s)', count)
    return count


def rename_supplier_name_variant(old_name: str, new_name: str) -> int:
    """Rename all occurrences of old_name → new_name on invoices that have no
    supplier_id (unlinked), so OCR variants can be corrected without touching
    already-linked records.

    Returns the number of rows updated.
    """
    if not old_name or not new_name or old_name == new_name:
        return 0
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE invoices
            SET supplier_name = %s
            WHERE supplier_name = %s
              AND supplier_id IS NULL
        """, (new_name, old_name))
        count = cursor.rowcount
        conn.commit()
    logger.info('rename_supplier_name_variant: "%s" → "%s", %d row(s)', old_name, new_name, count)
    return count


_GOV_NIFS = ('500757155', '506826066')


def _build_invoice_where(status: str = None, statuses: list = None,
                         no_status_filter: bool = False,
                         search: str = None,
                         centro_custo_id: int = None,
                         categoria_custo_id: int = None,
                         supplier_name: str = None,
                         supplier_names: list = None,
                         supplier_id: int = None,
                         date_from=None, date_to=None,
                         date_field: str = 'issue_date',
                         document_type: str = None,
                         sem_evidencia: bool = None,
                         sem_cc: bool = None,
                         exclude_gov: bool = False):
    where = []
    params = []
    # 'overdue' is a virtual status: scheduled invoices with due_date in the past
    if no_status_filter:
        pass  # no status constraint — include all statuses including drafts
    elif statuses:
        where.append("i.status = ANY(%s)")
        params.append(statuses)
    elif status == 'overdue':
        where.append("i.status IN ('pending_review', 'scheduled') AND i.due_date < CURRENT_DATE")
    elif status:
        where.append("i.status = %s")
        params.append(status)
    else:
        # Exclude in-progress drafts from the default listing
        where.append("i.status != 'draft'")
    if centro_custo_id:
        where.append("""(i.centro_custo_id = %s OR EXISTS (
            SELECT 1 FROM invoice_centros_custo icc
            WHERE icc.invoice_id = i.id AND icc.centro_custo_id = %s
        ))""")
        params.extend([centro_custo_id, centro_custo_id])
    if categoria_custo_id:
        where.append("i.categoria_custo_id = %s")
        params.append(categoria_custo_id)
    if supplier_id:
        where.append("i.supplier_id = %s")
        params.append(supplier_id)
    if supplier_names:
        where.append(f"{_supplier_display_expression()} = ANY(%s)")
        params.append(supplier_names)
    elif supplier_name:
        where.append(f"LOWER({_supplier_display_expression()}) = LOWER(%s)")
        params.append(supplier_name)
    if document_type and document_type in DOCUMENT_TYPE_LABELS:
        where.append("i.document_type = %s")
        params.append(document_type)
    if date_field == 'paid_date':
        _date_col = 'i.paid_date'
    elif date_field == 'due_date':
        _date_col = 'i.due_date'
    else:
        _date_col = 'i.issue_date'
    if date_from:
        where.append(f"{_date_col} >= %s")
        params.append(date_from)
    if date_to:
        where.append(f"{_date_col} <= %s")
        params.append(date_to)
    if search:
        s = f'%{search.lower()}%'
        # Store-name match uses a correlated subquery so callers don't need to JOIN stores.
        _store_match = (
            "EXISTS (SELECT 1 FROM suppliers _sup"
            " LEFT JOIN stores _st ON _st.id = _sup.store_id"
            " WHERE _sup.id = i.supplier_id AND LOWER(COALESCE(_st.name,'')) LIKE %s)"
        )
        if supplier_name or supplier_names:
            # Supplier already pinned via filter — search invoice number, notes, document_type, store
            where.append(f"(LOWER(i.invoice_number) LIKE %s OR LOWER(COALESCE(i.notes,'')) LIKE %s"
                         f" OR LOWER(COALESCE(i.document_type,'')) LIKE %s OR {_store_match})")
            params.extend([s, s, s, s])
        else:
            where.append(f"(LOWER({_supplier_display_expression()}) LIKE %s OR LOWER(i.invoice_number) LIKE %s"
                         f" OR LOWER(COALESCE(i.notes,'')) LIKE %s OR LOWER(COALESCE(i.document_type,'')) LIKE %s"
                         f" OR {_store_match})")
            params.extend([s, s, s, s, s])
    if sem_evidencia:
        where.append("(i.pdf_data IS NULL OR octet_length(i.pdf_data) = 0)")
        # Alerts for missing evidence only apply to active invoices (never draft/cancelled)
        where.append("i.status NOT IN ('draft', 'cancelled')")
    if sem_cc:
        where.append("""(
            i.centro_custo_id IS NULL
            AND NOT EXISTS (
                SELECT 1 FROM invoice_centros_custo icc
                WHERE icc.invoice_id = i.id
            )
        )""")
    if exclude_gov:
        # Exclude invoices from government/tax entities (AT, SS, etc.)
        # Matched via the known NIF list or the entidade_governamental flag on the supplier record.
        where.append("""(
            (i.supplier_nif IS NULL OR i.supplier_nif NOT IN %s)
            AND NOT EXISTS (
                SELECT 1 FROM suppliers s
                WHERE s.id = i.supplier_id AND s.entidade_governamental = true
            )
        )""")
        params.append(_GOV_NIFS)
    where_clause = ('WHERE ' + ' AND '.join(where)) if where else ''
    return where_clause, params


def get_invoices(status: str = None, statuses: list = None,
                 no_status_filter: bool = False,
                 search: str = None, order_by: str = 'due_date',
                 order_dir: str = 'asc',
                 centro_custo_id: int = None,
                 categoria_custo_id: int = None,
                 supplier_name: str = None,
                 supplier_names: list = None,
                 supplier_id: int = None,
                 date_from=None, date_to=None,
                 date_field: str = 'issue_date',
                 document_type: str = None,
                 sem_evidencia: bool = None,
                 sem_cc: bool = None,
                 exclude_gov: bool = False,
                 limit: int = None, offset: int = 0) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        where_clause, params = _build_invoice_where(
            status=status, statuses=statuses, no_status_filter=no_status_filter,
            search=search,
            centro_custo_id=centro_custo_id, categoria_custo_id=categoria_custo_id,
            supplier_name=supplier_name, supplier_names=supplier_names,
            supplier_id=supplier_id, date_from=date_from, date_to=date_to,
            date_field=date_field, document_type=document_type,
            sem_evidencia=sem_evidencia, sem_cc=sem_cc, exclude_gov=exclude_gov,
        )
        order_col = _ORDER_COL_MAP.get(order_by, 'i.due_date')
        direction = 'DESC' if order_dir == 'desc' else 'ASC'
        if limit is not None:
            params.extend([int(limit), int(offset)])
            limit_sql = 'LIMIT %s OFFSET %s'
        else:
            limit_sql = ''
        cursor.execute(f"""
            SELECT i.id, i.supplier_id, i.supplier_name, i.supplier_nif,
                   i.invoice_number, i.amount_eur, i.vat_amount_eur,
                   i.issue_date, i.due_date, i.category,
                   i.onedrive_subfolder, i.onedrive_path, i.pdf_filename,
                   i.status, i.ocr_confidence, i.created_by,
                   i.cfo_confirmed_date, i.paid_date, i.notes, i.created_at,
                   i.onedrive_web_url,
                   i.document_type,
                   i.centro_custo_id,
                   i.categoria_custo_id,
                   ip.confirmed_date AS payment_confirmed_date,
                   i.payment_method,
                   COALESCE(i.installment_total, 0) AS installment_total,
                   COALESCE(ii_stats.paid_count, 0) AS installment_paid_count,
                   (i.pdf_data IS NOT NULL AND octet_length(i.pdf_data) > 0) AS has_pdf,
                   COALESCE(
                       (SELECT STRING_AGG(cc2.name || ' (' || ROUND(icc2.percentagem::numeric) || '%%)',
                                         ', ' ORDER BY icc2.percentagem DESC)
                        FROM invoice_centros_custo icc2
                        JOIN cost_centers cc2 ON cc2.id = icc2.centro_custo_id
                        WHERE icc2.invoice_id = i.id),
                       cc.name
                    ) AS centro_custo_name,
                     ccat.name AS categoria_custo_name,
                     s.name AS supplier_legal_name,
                     COALESCE(NULLIF(s.common_name, ''), s.name, i.supplier_name) AS supplier_display_name,
                     s.nif AS supplier_legal_nif
            FROM invoices i
            LEFT JOIN suppliers s ON s.id = i.supplier_id
            LEFT JOIN cost_centers cc ON cc.id = i.centro_custo_id
            LEFT JOIN cost_categories ccat ON ccat.id = i.categoria_custo_id
            LEFT JOIN invoice_payments ip ON ip.invoice_id = i.id
            LEFT JOIN (
                SELECT invoice_id,
                       COUNT(*) FILTER (WHERE status = 'paid') AS paid_count
                FROM invoice_installments
                GROUP BY invoice_id
            ) ii_stats ON ii_stats.invoice_id = i.id
            {where_clause}
            ORDER BY {order_col} {direction} NULLS LAST, i.created_at DESC
            {limit_sql}
        """, params)
        rows = cursor.fetchall()
    result = []
    for r in rows:
        inv = _row_to_invoice(r)
        inv['payment_confirmed_date'] = r[24] if len(r) > 24 else None
        inv['payment_method'] = r[25] if len(r) > 25 else None
        inv['installment_total'] = int(r[26]) if len(r) > 26 and r[26] else 0
        inv['installment_paid_count'] = int(r[27]) if len(r) > 27 and r[27] else 0
        inv['has_pdf'] = bool(r[28]) if len(r) > 28 else False
        inv['supplier_legal_name'] = r[31] if len(r) > 31 else inv['supplier_name']
        inv['supplier_display_name'] = r[32] if len(r) > 32 else inv['supplier_name']
        inv['supplier_legal_nif'] = r[33] if len(r) > 33 else inv['supplier_nif']
        result.append(inv)
    return result


def count_invoices(status: str = None, statuses: list = None,
                   no_status_filter: bool = False,
                   search: str = None,
                   centro_custo_id: int = None,
                   categoria_custo_id: int = None,
                   supplier_name: str = None,
                   supplier_names: list = None,
                   supplier_id: int = None,
                   date_from=None, date_to=None,
                   date_field: str = 'issue_date',
                   document_type: str = None,
                   sem_evidencia: bool = None,
                    sem_cc: bool = None,
                   exclude_gov: bool = False) -> int:
    where_clause, params = _build_invoice_where(
        status=status, statuses=statuses, no_status_filter=no_status_filter,
        search=search,
        centro_custo_id=centro_custo_id, categoria_custo_id=categoria_custo_id,
        supplier_name=supplier_name, supplier_names=supplier_names,
        supplier_id=supplier_id, date_from=date_from, date_to=date_to,
        date_field=date_field, document_type=document_type,
        sem_evidencia=sem_evidencia, sem_cc=sem_cc, exclude_gov=exclude_gov,
    )
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT COUNT(*)
            FROM invoices i
            {where_clause}
        """, params)
        return int(cursor.fetchone()[0])


def count_invoices_sem_evidencia() -> int:
    """Return the number of invoices (excluding draft/cancelled) without any PDF or image attached."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*)
            FROM invoices
            WHERE (pdf_data IS NULL OR octet_length(pdf_data) = 0)
              AND status NOT IN ('draft', 'cancelled')
        """)
        return int(cursor.fetchone()[0])


def count_invoices_sem_categoria() -> int:
    """Return the number of active invoices (pending_review or scheduled) missing a categoria_custo_id."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*)
            FROM invoices i
            WHERE i.categoria_custo_id IS NULL
              AND i.status IN ('pending_review', 'scheduled')
        """)
        return int(cursor.fetchone()[0])


def get_invoices_sem_categoria_summary() -> dict:
    """Return {count, amount} for classifiable (pending_review/scheduled) invoices
    without a categoria_custo_id, excluding government/tax entities (AT, SS).

    Uses the same eligibility contract as the categoria_custo view with
    sem_categoria=1 (exclude_gov=True, status in pending_review/scheduled,
    no store filter), so the banner count/amount matches exactly what the
    'Ir classificar' link destination shows.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*), COALESCE(SUM(i.amount_eur), 0)
            FROM invoices i
            WHERE i.categoria_custo_id IS NULL
              AND i.status IN ('pending_review', 'scheduled')
              AND (i.supplier_nif IS NULL OR i.supplier_nif NOT IN %s)
              AND NOT EXISTS (
                  SELECT 1 FROM suppliers s
                  WHERE s.id = i.supplier_id AND s.entidade_governamental = true
              )
        """, (_GOV_NIFS,))
        row = cursor.fetchone()
    return {'count': int(row[0]), 'amount': float(row[1])}


def get_invoices_type_totals() -> dict:
    """Return a {document_type: {count, total}} dict for all non-draft invoices.

    Uses a single GROUP BY query rather than fetching all rows.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(document_type, 'fatura') AS dt,
                   COUNT(*) AS n,
                   COALESCE(SUM(amount_eur), 0) AS total
            FROM invoices
            WHERE status != 'draft'
            GROUP BY dt
            ORDER BY dt
        """)
        rows = cursor.fetchall()
    return {r[0]: {'count': int(r[1]), 'total': float(r[2])} for r in rows}


def get_invoice(invoice_id: int) -> dict:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT i.id, i.supplier_id, i.supplier_name, i.supplier_nif,
                   i.invoice_number, i.amount_eur, i.vat_amount_eur,
                   i.issue_date, i.due_date, i.category,
                   i.onedrive_subfolder, i.onedrive_path, i.pdf_filename,
                   i.status, i.ocr_confidence, i.created_by,
                   i.cfo_confirmed_date, i.paid_date, i.notes, i.created_at,
                   i.onedrive_web_url,
                   i.document_type,
                   i.centro_custo_id,
                   i.categoria_custo_id,
                   i.ocr_raw,
                   ip.confirmed_date AS payment_confirmed_date,
                   i.stock_registado_at,
                   i.stock_registado_por,
                   i.payment_method,
                   i.onedrive_failed,
                   i.onedrive_retry_at,
                   COALESCE(i.installment_total, 0) AS installment_total,
                   (SELECT COUNT(*) FROM invoice_installments
                    WHERE invoice_id = i.id AND status = 'paid') AS installment_paid_count,
                   (i.pdf_data IS NOT NULL AND octet_length(i.pdf_data) > 0) AS has_pdf,
                   COALESCE(i.accounting_status, 'por_contabilizar') AS accounting_status,
                   i.accounting_notes,
                   i.accounting_updated_by,
                    i.accounting_updated_at,
                    s.name AS supplier_legal_name,
                     COALESCE(NULLIF(s.common_name, ''), s.name, i.supplier_name) AS supplier_display_name,
                     s.nif AS supplier_legal_nif
            FROM invoices i
            LEFT JOIN suppliers s ON s.id = i.supplier_id
            LEFT JOIN invoice_payments ip ON ip.invoice_id = i.id
            WHERE i.id = %s
        """, (invoice_id,))
        row = cursor.fetchone()
    if not row:
        return None
    inv = _row_to_invoice(row)
    inv['ocr_raw'] = row[24]
    inv['payment_confirmed_date'] = row[25] if len(row) > 25 else None
    inv['stock_registado_at'] = row[26] if len(row) > 26 else None
    inv['stock_registado_por'] = row[27] if len(row) > 27 else None
    inv['payment_method'] = row[28] if len(row) > 28 else None
    inv['onedrive_failed'] = row[29] if len(row) > 29 else False
    inv['onedrive_retry_at'] = row[30] if len(row) > 30 else None
    inv['installment_total'] = int(row[31]) if len(row) > 31 and row[31] else 0
    inv['installment_paid_count'] = int(row[32]) if len(row) > 32 and row[32] else 0
    inv['has_pdf'] = bool(row[33]) if len(row) > 33 else inv['has_pdf']
    inv['accounting_status'] = row[34] if len(row) > 34 else 'por_contabilizar'
    inv['accounting_notes'] = row[35] if len(row) > 35 else None
    inv['accounting_updated_by'] = row[36] if len(row) > 36 else None
    inv['accounting_updated_at'] = row[37] if len(row) > 37 else None
    inv['supplier_legal_name'] = row[38] if len(row) > 38 else inv['supplier_name']
    inv['supplier_display_name'] = row[39] if len(row) > 39 else inv['supplier_name']
    inv['supplier_legal_nif'] = row[40] if len(row) > 40 else inv['supplier_nif']
    inv['supplier_identity_conflict'] = bool(
        inv.get('supplier_id')
        and (
            _normalise_identity_name(inv.get('supplier_name'))
            != _normalise_identity_name(inv.get('supplier_legal_name'))
            or (
                bool(inv.get('supplier_nif') or inv.get('supplier_legal_nif'))
                and not _nifs_match(inv.get('supplier_nif'), inv.get('supplier_legal_nif'))
            )
            or (
                bool(inv.get('supplier_legal_nif'))
                and not is_valid_portuguese_nif(inv.get('supplier_legal_nif'))
            )
        )
    )
    return inv


def get_invoices_missing_onedrive() -> list:
    """Return invoices with a PDF but no OneDrive path and not permanently failed.

    Used by the OneDrive retry scheduler to find uploads that need re-attempting.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, onedrive_subfolder, issue_date, onedrive_retry_at
            FROM invoices
            WHERE pdf_filename IS NOT NULL
              AND onedrive_path IS NULL
              AND (onedrive_failed IS NULL OR onedrive_failed = FALSE)
              AND status != 'draft'
            ORDER BY created_at
        """)
        rows = cursor.fetchall()
    return [{'id': r[0], 'onedrive_subfolder': r[1], 'issue_date': r[2],
             'onedrive_retry_at': r[3]} for r in rows]


def mark_onedrive_retry(invoice_id: int):
    """Record that a first upload attempt failed (sets onedrive_retry_at = NOW())."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE invoices SET onedrive_retry_at = NOW(), updated_at = NOW() WHERE id = %s",
            (invoice_id,),
        )
        conn.commit()


def mark_onedrive_failed(invoice_id: int):
    """Mark an invoice as permanently failed for OneDrive upload."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE invoices SET onedrive_failed = TRUE, updated_at = NOW() WHERE id = %s",
            (invoice_id,),
        )
        conn.commit()


def count_onedrive_failed() -> int:
    """Return the number of invoices with onedrive_failed = TRUE."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM invoices WHERE onedrive_failed = TRUE"
        )
        return int(cursor.fetchone()[0])


def get_invoice_pdf(invoice_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pdf_data, pdf_filename FROM invoices WHERE id = %s", (invoice_id,))
        row = cursor.fetchone()
    if row:
        return row[0], row[1]
    return None, None


def save_invoice_pdf(invoice_id: int, pdf_data: bytes, pdf_filename: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE invoices SET pdf_data = %s, pdf_filename = %s, updated_at = NOW() WHERE id = %s",
            (psycopg2.Binary(pdf_data), pdf_filename, invoice_id),
        )
        conn.commit()


def create_invoice(data: dict) -> int:
    doc_type = data.get('document_type', 'fatura')
    status = data.get('status', 'pending_review')
    if (doc_type in _DOCUMENT_TYPES_INVOICE
            and status in _STATUSES_REQUIRING_SUPPLIER
            and not data.get('supplier_id')):
        raise ValueError(
            f'Não é possível criar documento tipo "{doc_type}" com estado "{status}" '
            f'sem fornecedor ligado.'
        )
    with db_connection() as conn:
        cursor = conn.cursor()
        if data.get('supplier_id'):
            data = _canonicalize_invoice_supplier_data(
                cursor, data, data.get('supplier_id')
            )
        cursor.execute("""
            INSERT INTO invoices (
                supplier_id, supplier_name, supplier_nif, invoice_number,
                amount_eur, vat_amount_eur, issue_date, due_date,
                category, onedrive_subfolder, onedrive_path,
                onedrive_web_url, pdf_filename, pdf_data, status,
                ocr_confidence, ocr_raw, created_by, notes, document_type, source,
                centro_custo_id, categoria_custo_id, updated_at
            ) VALUES (
                %(supplier_id)s, %(supplier_name)s, %(supplier_nif)s, %(invoice_number)s,
                %(amount_eur)s, %(vat_amount_eur)s, %(issue_date)s, %(due_date)s,
                %(category)s, %(onedrive_subfolder)s, %(onedrive_path)s,
                %(onedrive_web_url)s, %(pdf_filename)s, %(pdf_data)s, %(status)s,
                %(ocr_confidence)s, %(ocr_raw)s, %(created_by)s, %(notes)s,
                %(document_type)s, %(source)s,
                %(centro_custo_id)s, %(categoria_custo_id)s, NOW()
            ) RETURNING id
        """, {**data,
              'source': data.get('source', 'email_upload'),
              'centro_custo_id': data.get('centro_custo_id'),
              'categoria_custo_id': data.get('categoria_custo_id')})
        invoice_id = cursor.fetchone()[0]
        # ── Audit: record creation event ──
        try:
            doc_type_label = DOCUMENT_TYPE_LABELS.get(doc_type, doc_type)
            parts = [doc_type_label]
            if data.get('supplier_name'):
                parts.append(data['supplier_name'])
            if data.get('amount_eur') is not None:
                parts.append(f"{data['amount_eur']} €")
            cursor.execute(
                "INSERT INTO invoice_audit_log "
                "(invoice_id, campo_alterado, valor_anterior, valor_novo, alterado_por) "
                "VALUES (%s, 'criação', NULL, %s, %s)",
                (invoice_id, ' · '.join(str(p) for p in parts),
                 data.get('created_by') or 'sistema'),
            )
        except Exception as _audit_exc:
            logger.warning('create_invoice: audit log insert failed for inv=%s: %s', invoice_id, _audit_exc)
        conn.commit()
    return invoice_id


_STATUSES_REQUIRING_SUPPLIER = frozenset({'pending_review', 'scheduled', 'paid', 'overdue'})
_DOCUMENT_TYPES_INVOICE = frozenset({'fatura', 'nota_credito', 'nota_debito'})


def get_invoice_audit_log(invoice_id: int) -> list:
    """Return all audit log entries for an invoice, newest first."""
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, campo_alterado, valor_anterior, valor_novo, alterado_por, alterado_em
                FROM invoice_audit_log
                WHERE invoice_id = %s
                ORDER BY alterado_em ASC
            """, (invoice_id,))
            rows = cursor.fetchall()
        return [
            {
                'id': r[0],
                'campo_alterado': r[1],
                'valor_anterior': r[2],
                'valor_novo': r[3],
                'alterado_por': r[4],
                'alterado_em': r[5],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning('get_invoice_audit_log(%s) failed: %s', invoice_id, exc)
        return []


def update_invoice(invoice_id: int, data: dict, changed_by: str = 'sistema'):
    allowed = [
        'supplier_name', 'supplier_nif', 'invoice_number', 'amount_eur', 'vat_amount_eur',
        'issue_date', 'due_date', 'category', 'onedrive_subfolder',
        'onedrive_path', 'status', 'cfo_confirmed_date', 'paid_date', 'notes', 'supplier_id',
        'document_type', 'centro_custo_id', 'categoria_custo_id', 'payment_method',
    ]
    # Fields tracked in audit log (human-editable, meaningful to audit)
    _AUDIT_TRACKED = [
        'supplier_name', 'supplier_nif', 'supplier_id',
        'invoice_number', 'amount_eur', 'vat_amount_eur',
        'issue_date', 'due_date', 'category', 'document_type',
        'notes', 'status', 'payment_method',
        'centro_custo_id', 'categoria_custo_id', 'paid_date', 'cfo_confirmed_date',
    ]
    with db_connection() as conn:
        cursor = conn.cursor()
        identity_fields = {'supplier_id', 'supplier_name', 'supplier_nif'}
        if identity_fields.intersection(data):
            cursor.execute(
                "SELECT supplier_id FROM invoices WHERE id = %s",
                (invoice_id,),
            )
            identity_row = cursor.fetchone()
            existing_supplier_id = identity_row[0] if identity_row else None
            effective_supplier_id = (
                data.get('supplier_id')
                if 'supplier_id' in data
                else existing_supplier_id
            )
            data = _canonicalize_invoice_supplier_data(
                cursor, data, effective_supplier_id,
                previous_supplier_id=existing_supplier_id,
            )

        fields = []
        params = []
        for key in allowed:
            if key in data:
                fields.append(f"{key} = %s")
                params.append(data[key])
        if not fields:
            return
        fields.append("updated_at = NOW()")
        params.append(invoice_id)

        fields_to_audit = [f for f in _AUDIT_TRACKED if f in data]

        # Central guard: invoice-type documents cannot enter post-draft states without supplier_id
        new_status = data.get('status')
        if new_status in _STATUSES_REQUIRING_SUPPLIER:
            cursor.execute(
                "SELECT supplier_id, document_type FROM invoices WHERE id = %s", (invoice_id,)
            )
            row = cursor.fetchone()
            if row:
                existing_supplier_id, existing_doc_type = row
                effective_supplier_id = data.get('supplier_id') if 'supplier_id' in data else existing_supplier_id
                effective_doc_type = data.get('document_type', existing_doc_type)
                if effective_doc_type in _DOCUMENT_TYPES_INVOICE and not effective_supplier_id:
                    raise ValueError(
                        f'Não é possível mover para estado "{new_status}" sem fornecedor ligado '
                        f'(documento tipo {effective_doc_type}).'
                    )

        # ── Capture old values for all auditable fields before writing ──
        old_values = {}
        if fields_to_audit:
            # Also fetch store name when store_id is being changed
            extra_cols = ''
            if 'store_id' in fields_to_audit:
                extra_cols = ', (SELECT name FROM stores WHERE id = invoices.store_id)'
            cursor.execute(
                f"SELECT {', '.join(fields_to_audit)}{extra_cols} FROM invoices WHERE id = %s",
                (invoice_id,)
            )
            _row = cursor.fetchone()
            if _row:
                for i, fname in enumerate(fields_to_audit):
                    old_values[fname] = _row[i]
                if 'store_id' in fields_to_audit:
                    old_values['_store_name_old'] = _row[len(fields_to_audit)]

        # Resolve new store name if store_id is changing
        if 'store_id' in data and data['store_id']:
            cursor.execute("SELECT name FROM stores WHERE id = %s", (data['store_id'],))
            _sr = cursor.fetchone()
            old_values['_store_name_new'] = _sr[0] if _sr else str(data['store_id'])
        elif 'store_id' in data:
            old_values['_store_name_new'] = None

        # Resolve supplier names for supplier_id changes (display name instead of numeric ID)
        if 'supplier_id' in fields_to_audit and old_values.get('supplier_id'):
            cursor.execute("SELECT name FROM suppliers WHERE id = %s", (old_values['supplier_id'],))
            _ss = cursor.fetchone()
            old_values['_supplier_name_old'] = _ss[0] if _ss else str(old_values['supplier_id'])
        if 'supplier_id' in data and data['supplier_id']:
            cursor.execute("SELECT name FROM suppliers WHERE id = %s", (data['supplier_id'],))
            _ss = cursor.fetchone()
            old_values['_supplier_name_new'] = _ss[0] if _ss else str(data['supplier_id'])
        elif 'supplier_id' in data:
            old_values['_supplier_name_new'] = None

        cursor.execute(
            f"UPDATE invoices SET {', '.join(fields)} WHERE id = %s",
            params
        )

        # ── Audit log: record each changed field ──
        def _str(v):
            """Normalise a DB value to a comparable/storable string."""
            if v is None:
                return None
            if isinstance(v, float):
                return f'{v:.2f}'
            return str(v)

        try:
            for fname in fields_to_audit:
                new_raw = data[fname]
                old_raw = old_values.get(fname)

                # For store_id / supplier_id use human-readable names instead of numeric IDs
                if fname == 'store_id':
                    old_display = old_values.get('_store_name_old') or (_str(old_raw) if old_raw else None)
                    new_display = old_values.get('_store_name_new') or (_str(new_raw) if new_raw else None)
                elif fname == 'supplier_id':
                    old_display = old_values.get('_supplier_name_old') or (_str(old_raw) if old_raw else None)
                    new_display = old_values.get('_supplier_name_new') or (_str(new_raw) if new_raw else None)
                else:
                    old_display = _str(old_raw)
                    new_display = _str(new_raw)

                if old_display != new_display:
                    cursor.execute(
                        "INSERT INTO invoice_audit_log "
                        "(invoice_id, campo_alterado, valor_anterior, valor_novo, alterado_por) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (invoice_id, fname, old_display, new_display, changed_by),
                    )
        except Exception as _audit_exc:
            logger.warning('update_invoice: audit log insert failed for inv=%s: %s', invoice_id, _audit_exc)

        # ── Audit trail: auto-create invoice_payments record when paid via update_invoice ──
        if new_status == 'paid':
            cursor.execute(
                "SELECT 1 FROM invoice_payments WHERE invoice_id = %s", (invoice_id,)
            )
            if not cursor.fetchone():
                logger.warning(
                    'update_invoice: inv=%s set to paid without prior invoice_payments record — '
                    'creating automatic audit entry (confirmed_by="sistema:update_invoice"). '
                    'Prefer mark_payment_executed for deliberate payment actions.',
                    invoice_id,
                )
                effective_paid_date = data.get('paid_date')
                cursor.execute(
                    "INSERT INTO invoice_payments "
                    "(invoice_id, paid_date, status, confirmed_by, notes, updated_at) "
                    "VALUES (%s, "
                    "  COALESCE(%s::date, (SELECT paid_date FROM invoices WHERE id = %s), NOW()::date), "
                    "  'paid', 'sistema:update_invoice', "
                    "  'Registo criado automaticamente — chamada directa a update_invoice', NOW()) "
                    "ON CONFLICT (invoice_id) DO NOTHING",
                    (invoice_id, effective_paid_date, invoice_id),
                )
        conn.commit()


def update_invoice_onedrive(invoice_id: int, onedrive_path: str, onedrive_subfolder: str,
                            onedrive_web_url: str = None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE invoices
            SET onedrive_path = %s, onedrive_subfolder = %s,
                onedrive_web_url = %s, updated_at = NOW()
            WHERE id = %s
        """, (onedrive_path, onedrive_subfolder, onedrive_web_url, invoice_id))
        conn.commit()


def delete_invoice(invoice_id: int, deleted_by: str = 'sistema'):
    with db_connection() as conn:
        cursor = conn.cursor()
        # ── Tombstone: capture snapshot + FULL audit history before CASCADE destroys both ──
        try:
            cursor.execute(
                "SELECT invoice_number, supplier_name, amount_eur, document_type, status "
                "FROM invoices WHERE id = %s",
                (invoice_id,)
            )
            _snap = cursor.fetchone()

            # Fetch prior audit log *before* DELETE triggers the cascade
            _prior_audit = []
            try:
                cursor.execute(
                    "SELECT campo_alterado, valor_anterior, valor_novo, alterado_por, alterado_em "
                    "FROM invoice_audit_log WHERE invoice_id = %s ORDER BY alterado_em ASC",
                    (invoice_id,)
                )
                for _row in cursor.fetchall():
                    _prior_audit.append({
                        'campo_alterado': _row[0],
                        'valor_anterior': _row[1],
                        'valor_novo': _row[2],
                        'alterado_por': _row[3],
                        'alterado_em': _row[4].isoformat() if _row[4] else None,
                    })
            except Exception:
                pass

            # Append the deletion event itself so it appears last in the timeline
            from datetime import datetime as _dt_now
            _prior_audit.append({
                'campo_alterado': 'eliminação',
                'valor_anterior': _snap[4] if _snap else None,
                'valor_novo': None,
                'alterado_por': deleted_by,
                'alterado_em': _dt_now.utcnow().isoformat(),
            })

            if _snap:
                cursor.execute(
                    "INSERT INTO invoice_deletion_log "
                    "(invoice_id, invoice_number, supplier_name, amount_eur, "
                    " document_type, status_at_deletion, deleted_by, prior_audit_json) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (invoice_id, _snap[0], _snap[1], _snap[2], _snap[3], _snap[4],
                     deleted_by, json.dumps(_prior_audit)),
                )
        except Exception as _exc:
            logger.warning('delete_invoice: tombstone write failed for inv=%s: %s', invoice_id, _exc)
        cursor.execute("DELETE FROM invoices WHERE id = %s", (invoice_id,))
        conn.commit()


def mark_invoice_confirmed(invoice_id: int, confirmed_date) -> None:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE invoices
            SET status = 'scheduled', cfo_confirmed_date = %s, updated_at = NOW()
            WHERE id = %s AND status = 'pending_review'
        """, (confirmed_date, invoice_id))
        conn.commit()


def mark_invoice_paid(invoice_id: int, paid_date) -> None:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE invoices
            SET status = 'paid', paid_date = %s, updated_at = NOW()
            WHERE id = %s
        """, (paid_date, invoice_id))
        conn.commit()


def get_stores_list() -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM stores WHERE is_active = TRUE ORDER BY name")
        rows = cursor.fetchall()
    return [{'id': r[0], 'name': r[1]} for r in rows]


# Subfolder suggestion rules: store name → OneDrive subfolder
_STORE_SUBFOLDER_MAP = {
    'matosinhos': 'Matosinhos (M)',
    'bolhão': 'Bolhão (B)',
    'bolhao': 'Bolhão (B)',
    'fp': 'Faturas partilhadas (FP)',
    'faturas partilhadas': 'Faturas partilhadas (FP)',
    'fábrica': 'Produção (P)',
    'fabrica': 'Produção (P)',
    'produção': 'Produção (P)',
    'producao': 'Produção (P)',
    'distribuição': 'Distribuição (D)',
    'distribuicao': 'Distribuição (D)',
    'eventos': 'Eventos (E)',
    'gestão': 'Geral (G)',
    'gestao': 'Geral (G)',
    'geral': 'Geral (G)',
    'timeout': 'Timeout (TOM)',
    'tom': 'Timeout (TOM)',
}

_CATEGORY_SUBFOLDER_MAP = {
    'Matérias-primas': 'Produção (P)',
    'Embalagens': 'Produção (P)',
    'Serviços': 'Geral (G)',
    'Utilities': 'Geral (G)',
    'Rendas': 'Geral (G)',
    'Equipamentos': 'Geral (G)',
    'Marketing': 'Geral (G)',
    'Transportes': 'Distribuição (D)',
    'Outros': 'Geral (G)',
}


def suggest_onedrive_subfolder(supplier_nif: str = None,
                                category: str = None) -> str:
    """
    Suggest OneDrive subfolder based on supplier NIF + category.
    Rules (priority order):
    1. If supplier has a known store association → use store-based mapping
    2. If category provided → use category mapping
    3. Default → 'Geral (G)'
    """
    # Rule 1: look up supplier's associated store
    if supplier_nif:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT st.name FROM suppliers s
                LEFT JOIN stores st ON s.store_id = st.id
                WHERE s.nif = %s
            """, (supplier_nif,))
            row = cursor.fetchone()
            if row and row[0]:
                store_name = row[0].lower()
                if store_name in _STORE_SUBFOLDER_MAP:
                    return _STORE_SUBFOLDER_MAP[store_name]

    # Rule 2: category-based fallback
    if category and category in _CATEGORY_SUBFOLDER_MAP:
        return _CATEGORY_SUBFOLDER_MAP[category]

    return 'Geral (G)'


def get_invoice_installments(invoice_id: int) -> list:
    """Returns all installments for an invoice, ordered by installment_number."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT id, invoice_id, installment_number, total_installments,
                   amount_eur, due_date, paid_date, status, paid_by, notes, created_at
            FROM invoice_installments
            WHERE invoice_id = %s
            ORDER BY installment_number ASC
        """, (invoice_id,))
        return cursor.fetchall()


def get_pending_installments() -> list:
    """Returns all pending/scheduled installments joined with parent invoice data."""
    with db_connection() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT ii.id, ii.invoice_id, ii.installment_number, ii.total_installments,
                   ii.amount_eur, ii.due_date, ii.paid_date, ii.status,
                   i.supplier_name, i.supplier_nif,
                   s.name AS supplier_legal_name,
                   COALESCE(NULLIF(s.common_name, ''), s.name, i.supplier_name) AS supplier_display_name,
                   i.invoice_number AS parent_invoice_number,
                   i.document_type
            FROM invoice_installments ii
            JOIN invoices i ON i.id = ii.invoice_id
            LEFT JOIN suppliers s ON s.id = i.supplier_id
            WHERE ii.status IN ('pending_review', 'scheduled')
            ORDER BY ii.due_date ASC NULLS LAST, ii.invoice_id, ii.installment_number
        """)
        return cursor.fetchall()


def create_invoice_installments(invoice_id: int, installments: list, created_by: str) -> list:
    """Creates installment records for an invoice.

    installments: list of dicts with keys 'amount_eur' (float) and 'due_date' (date).
    The first installment is marked as 'paid' immediately; the rest as 'pending_review'.
    The parent invoice status is set to 'scheduled' and installment_total is updated.

    Returns list of created installment IDs.
    """
    n = len(installments)
    if n < 2:
        raise ValueError("Pagamento parcelado requer pelo menos 2 parcelas.")

    ids = []
    with db_connection() as conn:
        cursor = conn.cursor()
        for idx, inst in enumerate(installments, start=1):
            is_first = idx == 1
            status = 'paid' if is_first else 'pending_review'
            paid_date = inst['due_date'] if is_first else None
            paid_by_val = created_by if is_first else None
            cursor.execute("""
                INSERT INTO invoice_installments
                    (invoice_id, installment_number, total_installments, amount_eur,
                     due_date, paid_date, status, paid_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (invoice_id, idx, n, inst['amount_eur'], inst['due_date'],
                  paid_date, status, paid_by_val))
            ids.append(cursor.fetchone()[0])

        cursor.execute("""
            UPDATE invoices
            SET installment_total = %s, status = 'scheduled', updated_at = NOW()
            WHERE id = %s
        """, (n, invoice_id))
        conn.commit()
    return ids


def mark_installment_paid(installment_id: int, invoice_id: int, paid_date, confirmed_by: str) -> bool:
    """Marks one installment as paid.

    invoice_id is required and must match the installment's invoice_id (ownership check).
    If all installments for the parent invoice are now paid, also marks the parent
    invoice as paid and returns True; otherwise returns False.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE invoice_installments
            SET status = 'paid', paid_date = %s, paid_by = %s
            WHERE id = %s AND invoice_id = %s AND status != 'paid'
            RETURNING invoice_id
        """, (paid_date, confirmed_by, installment_id, invoice_id))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return False
        invoice_id = row[0]

        cursor.execute("""
            SELECT COUNT(*) FROM invoice_installments
            WHERE invoice_id = %s AND status != 'paid'
        """, (invoice_id,))
        remaining = cursor.fetchone()[0]

        parent_paid = False
        if remaining == 0:
            cursor.execute("""
                UPDATE invoices
                SET status = 'paid', paid_date = %s, updated_at = NOW()
                WHERE id = %s
            """, (paid_date, invoice_id))
            # Audit trail: write invoice_payments record so the paid-by user is captured.
            # Done inline (same transaction) to avoid circular import of db.pagamentos.
            cursor.execute(
                "INSERT INTO invoice_payments "
                "(invoice_id, paid_date, status, confirmed_by, notes, updated_at) "
                "VALUES (%s, %s, 'paid', %s, 'Pago pela última parcela da fatura', NOW()) "
                "ON CONFLICT (invoice_id) DO UPDATE SET "
                "    paid_date = EXCLUDED.paid_date, "
                "    status = 'paid', "
                "    confirmed_by = EXCLUDED.confirmed_by, "
                "    notes = COALESCE(EXCLUDED.notes, invoice_payments.notes), "
                "    updated_at = NOW()",
                (invoice_id, paid_date, confirmed_by),
            )
            parent_paid = True

        conn.commit()
        return parent_paid


def get_paid_counts_by_supplier(supplier_ids: list,
                                unlinked_supplier_names: list = None) -> dict:
    """Return {stable_supplier_key: paid_count} for supplier groups.

    Used to show a "X pagas" badge when the default filter hides paid invoices.
    Linked suppliers are always counted by supplier_id, never by their
    presentation name. Unlinked invoices retain a separate raw-name key.
    """
    supplier_ids = [int(sid) for sid in (supplier_ids or []) if sid]
    unlinked_supplier_names = [
        name for name in (unlinked_supplier_names or []) if name is not None
    ]
    if not supplier_ids and not unlinked_supplier_names:
        return {}
    filters = []
    params = []
    if supplier_ids:
        filters.append("i.supplier_id = ANY(%s)")
        params.append(supplier_ids)
    if unlinked_supplier_names:
        filters.append("(i.supplier_id IS NULL AND COALESCE(i.supplier_name, '') = ANY(%s))")
        params.append(unlinked_supplier_names)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT CASE WHEN i.supplier_id IS NOT NULL "
            "THEN 'supplier:' || i.supplier_id::text "
            "ELSE 'unlinked:' || COALESCE(i.supplier_name, '') END AS supplier_key, "
            "COUNT(*) AS n "
            "FROM invoices i "
            "WHERE i.status = 'paid' "
            f"AND ({' OR '.join(filters)}) "
            "GROUP BY supplier_key",
            params,
        )
        return {row[0]: row[1] for row in cursor.fetchall()}


def get_contas_por_fornecedor(status_filter: str = None,
                               status_filters: list = None,
                               exclude_gov: bool = False) -> list:
    """
    Returns a list of all non-draft invoices and credit notes, grouped by supplier.
    Each entry contains:
      - supplier_name, supplier_nif
      - n_docs: total number of documents
      - total_faturas: sum of invoice amounts (positive)
      - total_nc: sum of credit note amounts (positive)
      - saldo_liquido: total_nc - total_faturas (negative means owed)
      - invoices: list of individual invoice dicts

    status_filters: list of statuses to include (e.g. ['pending_review', 'scheduled']).
                    Takes precedence over status_filter when provided.
    status_filter:  single status string for backwards-compatible callers.
                    Ignored when status_filters is set.
                    Defaults to all non-draft invoices when both are None.
    exclude_gov:    when True, exclude invoices from government/tax entities (AT, SS)
                    identified by their NIF or the entidade_governamental flag.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        params = []
        if status_filters is not None:
            extra_where = "AND i.status = ANY(%s)"
            params.append(status_filters)
        elif status_filter == 'overdue':
            extra_where = "AND i.status IN ('pending_review', 'scheduled') AND i.due_date < CURRENT_DATE"
        elif status_filter:
            extra_where = "AND i.status = %s"
            params.append(status_filter)
        else:
            extra_where = ""
        if exclude_gov:
            extra_where += """
            AND (
                (i.supplier_nif IS NULL OR i.supplier_nif NOT IN %s)
                AND NOT EXISTS (
                    SELECT 1 FROM suppliers s
                    WHERE s.id = i.supplier_id AND s.entidade_governamental = true
                )
            )"""
            params.append(_GOV_NIFS)
        cursor.execute(f"""
            SELECT i.id, i.supplier_id, i.supplier_name, i.supplier_nif,
                   i.invoice_number, i.amount_eur, i.vat_amount_eur,
                   i.issue_date, i.due_date, i.category,
                   i.onedrive_subfolder, i.onedrive_path, i.pdf_filename,
                   i.status, i.ocr_confidence, i.created_by,
                   i.cfo_confirmed_date, i.paid_date, i.notes, i.created_at,
                   i.onedrive_web_url,
                   i.document_type,
                   i.centro_custo_id,
                   i.categoria_custo_id,
                    (i.pdf_data IS NOT NULL AND octet_length(i.pdf_data) > 0) AS has_pdf,
                    s.name AS supplier_legal_name,
                    COALESCE(NULLIF(s.common_name, ''), s.name, i.supplier_name) AS supplier_display_name
            FROM invoices i
            LEFT JOIN suppliers s ON s.id = i.supplier_id
            WHERE i.status != 'draft' {extra_where}
            ORDER BY LOWER(COALESCE(NULLIF(s.common_name, ''), s.name, i.supplier_name)), i.due_date ASC NULLS LAST
        """, params)
        rows = cursor.fetchall()

    def _to_inv(r):
        inv = _row_to_invoice(r)
        inv['has_pdf'] = bool(r[24]) if len(r) > 24 else False
        inv['supplier_legal_name'] = r[25] if len(r) > 25 else inv['supplier_name']
        inv['supplier_display_name'] = r[26] if len(r) > 26 else inv['supplier_name']
        return inv

    invoices = [_to_inv(r) for r in rows]

    from collections import defaultdict
    groups = defaultdict(lambda: {
        'group_key': None,
        'supplier_id': None,
        'supplier_raw_name': None,
        'supplier_name': None,
        'supplier_legal_name': None,
        'supplier_nif': None,
        'invoices': [],
        'total_faturas': 0.0,
        'total_nc': 0.0,
    })

    for inv in invoices:
        if inv['supplier_id'] is not None:
            key = f"supplier:{inv['supplier_id']}"
        else:
            key = f"unlinked:{inv['supplier_name'] or ''}"
        g = groups[key]
        g['group_key'] = key
        g['supplier_id'] = inv['supplier_id']
        g['supplier_raw_name'] = inv['supplier_name']
        g['supplier_name'] = inv['supplier_display_name'] or '(sem fornecedor)'
        if g['supplier_legal_name'] is None:
            g['supplier_legal_name'] = inv['supplier_legal_name']
        if g['supplier_nif'] is None:
            g['supplier_nif'] = inv['supplier_nif']
        g['invoices'].append(inv)
        amt = abs(float(inv['amount_eur'] or 0))
        if inv['document_type'] == 'nota_credito':
            g['total_nc'] += amt
        else:
            g['total_faturas'] += amt

    result = []
    for g in groups.values():
        g['n_docs'] = len(g['invoices'])
        g['saldo_liquido'] = round(g['total_nc'] - g['total_faturas'], 2)
        g['total_faturas'] = round(g['total_faturas'], 2)
        g['total_nc'] = round(g['total_nc'], 2)
        result.append(g)

    result.sort(key=lambda x: (x['supplier_name'] or '').lower())
    return result


# ── Saved invoice views ────────────────────────────────────────────────────

_LOCK_SAVED_VIEWS = 590590  # advisory lock — unique per migration


def run_migrations_saved_invoice_views():
    """Create saved_invoice_views table if not already present.

    Uses an advisory lock so concurrent gunicorn workers don't race on the
    SERIAL sequence creation (identical pattern to run_migrations_tile_config).
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_SAVED_VIEWS,))
        if not cursor.fetchone()[0]:
            logger.info('run_migrations_saved_invoice_views: lock held by another worker, skipping')
            return
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS saved_invoice_views (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(100) NOT NULL,
                name VARCHAR(200) NOT NULL,
                filters_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    logger.info('run_migrations_saved_invoice_views: ready')


def get_saved_views(user_id: str) -> list:
    """Return all saved views for a user, newest first."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, filters_json, created_at
            FROM saved_invoice_views
            WHERE user_id = %s
            ORDER BY created_at DESC
        """, (user_id,))
        rows = cursor.fetchall()
    result = []
    for r in rows:
        try:
            filters = json.loads(r[2]) if r[2] else {}
        except Exception:
            filters = {}
        result.append({'id': r[0], 'name': r[1], 'filters': filters, 'created_at': r[3]})
    return result


def save_view(user_id: str, name: str, filters_dict: dict) -> int:
    """Persist a named saved view. Returns the new row id."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO saved_invoice_views (user_id, name, filters_json)
            VALUES (%s, %s, %s)
            RETURNING id
        """, (user_id, name.strip(), json.dumps(filters_dict)))
        view_id = cursor.fetchone()[0]
        conn.commit()
    return view_id


def delete_saved_view(view_id: int, user_id: str) -> bool:
    """Delete a saved view owned by user_id. Returns True if deleted."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM saved_invoice_views WHERE id = %s AND user_id = %s",
            (view_id, user_id),
        )
        deleted = cursor.rowcount > 0
        conn.commit()
    return deleted


def get_invoice_deletion_log(invoice_id: int) -> dict | None:
    """Return the tombstone record for a deleted invoice, or None if not found.

    The returned dict includes ``prior_audit`` (list of dicts) reconstructed from
    the JSON snapshot taken just before the DELETE.
    """
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, invoice_number, supplier_name, amount_eur, document_type, "
                "status_at_deletion, deleted_by, deleted_at, prior_audit_json "
                "FROM invoice_deletion_log WHERE invoice_id = %s ORDER BY deleted_at DESC LIMIT 1",
                (invoice_id,)
            )
            row = cursor.fetchone()
        if not row:
            return None
        prior_audit = []
        if row[8]:
            try:
                prior_audit = json.loads(row[8])
            except Exception:
                pass
        return {
            'id': row[0],
            'invoice_id': invoice_id,
            'invoice_number': row[1],
            'supplier_name': row[2],
            'amount_eur': row[3],
            'document_type': row[4],
            'status_at_deletion': row[5],
            'deleted_by': row[6],
            'deleted_at': row[7],
            'prior_audit': prior_audit,
        }
    except Exception as exc:
        logger.warning('get_invoice_deletion_log(%s) failed: %s', invoice_id, exc)
        return None


def run_migrations_invoice_audit_complete():
    """Idempotent: create invoice_deletion_log for tombstone records that survive CASCADE deletes.

    Uses advisory lock 593593 to serialise concurrent worker executions.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(593593)")
            if not cursor.fetchone()[0]:
                return
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS invoice_deletion_log (
                    id SERIAL PRIMARY KEY,
                    invoice_id INTEGER NOT NULL,
                    invoice_number VARCHAR(100),
                    supplier_name VARCHAR(255),
                    amount_eur NUMERIC(12,2),
                    document_type VARCHAR(50),
                    status_at_deletion VARCHAR(50),
                    deleted_by VARCHAR(100),
                    deleted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    prior_audit_json TEXT
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_invoice_deletion_log_invoice "
                "ON invoice_deletion_log(invoice_id)"
            )
            # Idempotent: add prior_audit_json if table was created by an earlier version
            cursor.execute(
                "ALTER TABLE invoice_deletion_log "
                "ADD COLUMN IF NOT EXISTS prior_audit_json TEXT"
            )
            conn.commit()
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(593593)")
                conn.commit()
            except Exception:
                pass
