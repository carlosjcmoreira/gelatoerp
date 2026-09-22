import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, logger, hash_password
from db.dose_associations import get_product_family_ids
import json
import os
from werkzeug.security import generate_password_hash


_LOCK_STOCK_PRODUCAO_LOJAS = 202612
_LOCK_EVENTOS_V2_FOUNDATION = 2026821
_LOCK_EVENTOS_CUSTOMER_PORTAL = 2026822
_LOCK_COMPRAS_ORIGENS = 202711
_LOCK_COMPRAS_CATALOGO = 202712
_LOCK_COMPRAS_ENCOMENDAS_SEMANAIS = 202713
_LOCK_COMPRAS_PEDIDOS_URGENTES = 202714


def run_migrations_compras_origens():
    """Create typed purchasing origins and classify legacy article labels.

    Spreadsheet labels are not trusted as supplier identities.  Known labels
    are mapped to the canonical Matosinhos store or to an operational category;
    every other legacy label remains explicitly unresolved until a human
    confirms a supplier.
    """
    from db.artigos import classify_compras_origin_label

    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_COMPRAS_ORIGENS,))
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_compras_origens: lock held, skipping")
                return

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS compras_origens (
                    id SERIAL PRIMARY KEY,
                    chave VARCHAR(100) NOT NULL UNIQUE,
                    tipo VARCHAR(30) NOT NULL CHECK (
                        tipo IN (
                            'fornecedor_externo', 'centro_interno',
                            'categoria_operacional', 'por_resolver'
                        )
                    ),
                    nome VARCHAR(255) NOT NULL,
                    rotulo_original VARCHAR(255),
                    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
                    store_id INTEGER REFERENCES stores(id) ON DELETE SET NULL,
                    ativo BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK (tipo <> 'centro_interno' OR store_id IS NOT NULL),
                    CHECK (tipo <> 'fornecedor_externo' OR supplier_id IS NOT NULL)
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_compras_origens_supplier
                    ON compras_origens(supplier_id)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_compras_origens_store
                    ON compras_origens(store_id)
                """
            )
            cursor.execute(
                """
                ALTER TABLE artigos_administrativos
                    ADD COLUMN IF NOT EXISTS origem_id INTEGER
                        REFERENCES compras_origens(id) ON DELETE SET NULL,
                    ADD COLUMN IF NOT EXISTS origem_original VARCHAR(255)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS artigos_administrativos_origem_audit (
                    id BIGSERIAL PRIMARY KEY,
                    artigo_id INTEGER REFERENCES artigos_administrativos(id)
                        ON DELETE SET NULL,
                    origem_anterior_id INTEGER REFERENCES compras_origens(id)
                        ON DELETE SET NULL,
                    origem_nova_id INTEGER REFERENCES compras_origens(id)
                        ON DELETE SET NULL,
                    rotulo_original VARCHAR(255),
                    actor VARCHAR(255) NOT NULL DEFAULT 'sistema',
                    reason TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_artigos_origem_audit_artigo
                    ON artigos_administrativos_origem_audit(artigo_id, created_at DESC)
                """
            )

            cursor.execute(
                """
                INSERT INTO compras_origens
                    (chave, tipo, nome, rotulo_original, store_id)
                SELECT 'centro:matosinhos', 'centro_interno', 'Matosinhos',
                       'MATOSINHOS', s.id
                FROM stores s
                WHERE LOWER(s.name) = LOWER('Matosinhos')
                ON CONFLICT (chave) DO NOTHING
                """
            )
            cursor.execute(
                """
                INSERT INTO compras_origens
                    (chave, tipo, nome, rotulo_original, store_id)
                SELECT 'categoria:moedas', 'categoria_operacional', 'Moedas',
                       'MOEDAS', s.id
                FROM stores s
                WHERE LOWER(s.name) = LOWER('Matosinhos')
                ON CONFLICT (chave) DO NOTHING
                """
            )
            cursor.execute(
                """
                INSERT INTO compras_origens
                    (chave, tipo, nome, rotulo_original)
                VALUES ('categoria:grafica', 'categoria_operacional',
                        'Gráfica', 'GRÁFICA')
                ON CONFLICT (chave) DO NOTHING
                """
            )

            cursor.execute(
                "SELECT DISTINCT fornecedor FROM artigos_administrativos "
                "WHERE fornecedor IS NOT NULL AND BTRIM(fornecedor) <> ''"
            )
            legacy_labels = [row[0] for row in cursor.fetchall()]
            for label in legacy_labels:
                origin = classify_compras_origin_label(label)
                store_name = origin.get('store_name')
                if store_name:
                    cursor.execute(
                        """
                        INSERT INTO compras_origens
                            (chave, tipo, nome, rotulo_original, store_id)
                        SELECT %s, %s, %s, %s, s.id
                        FROM stores s
                        WHERE LOWER(s.name) = LOWER(%s)
                        ON CONFLICT (chave) DO NOTHING
                        """,
                        (
                            origin['key'], origin['tipo'], origin['nome'],
                            origin['rotulo_original'], store_name,
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO compras_origens
                            (chave, tipo, nome, rotulo_original)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (chave) DO NOTHING
                        """,
                        (
                            origin['key'], origin['tipo'], origin['nome'],
                            origin['rotulo_original'],
                        ),
                    )
                cursor.execute(
                    """
                    UPDATE artigos_administrativos a
                       SET origem_id = o.id,
                           origem_original = COALESCE(a.origem_original, a.fornecedor)
                      FROM compras_origens o
                     WHERE a.origem_id IS NULL
                       AND a.fornecedor = %s
                       AND o.chave = %s
                    """,
                    (label, origin['key']),
                )

            conn.commit()
            logger.info(
                "run_migrations_compras_origens: origins and legacy article links ready"
            )
        except Exception as exc:
            logger.error("run_migrations_compras_origens failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


def run_migrations_compras_catalogo():
    """Evolve and seed the purchasing catalogue from the versioned dataset."""
    from db.artigos import (
        catalog_key_for,
        classify_compras_origin_label,
        infer_artigo_unidade,
    )
    from db.compras_catalog_seed import (
        CATALOG_ROWS,
        CATALOG_SOURCE,
        CATALOG_VERSION,
    )

    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s)", (_LOCK_COMPRAS_CATALOGO,)
            )
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_compras_catalogo: lock held, skipping")
                return {'created': 0, 'existing': 0, 'updated': 0, 'rejected': 0}

            cursor.execute(
                """
                ALTER TABLE artigos_administrativos
                    ADD COLUMN IF NOT EXISTS catalog_key VARCHAR(255),
                    ADD COLUMN IF NOT EXISTS marca VARCHAR(255),
                    ADD COLUMN IF NOT EXISTS unidade VARCHAR(40),
                    ADD COLUMN IF NOT EXISTS source_dataset VARCHAR(100),
                    ADD COLUMN IF NOT EXISTS source_version VARCHAR(100),
                    ADD COLUMN IF NOT EXISTS source_row INTEGER,
                    ADD COLUMN IF NOT EXISTS imported_at TIMESTAMP,
                    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP
                        NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    ADD COLUMN IF NOT EXISTS human_modified_at TIMESTAMP
                """
            )
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_artigos_catalog_key
                    ON artigos_administrativos(catalog_key)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_artigos_catalog_active_origin
                    ON artigos_administrativos(origem_id, ativo)
                """
            )
            # Invoice lines keep their OCR/accounting snapshot and material link;
            # the purchasing catalogue association is an independent, nullable
            # link with its own append-only decision history.
            cursor.execute(
                """
                ALTER TABLE invoice_linhas
                    ADD COLUMN IF NOT EXISTS artigo_id INTEGER
                        REFERENCES artigos_administrativos(id) ON DELETE SET NULL
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_invoice_linhas_artigo
                    ON invoice_linhas(artigo_id)
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS invoice_linha_artigo_audit (
                    id SERIAL PRIMARY KEY,
                    invoice_linha_id INTEGER NOT NULL
                        REFERENCES invoice_linhas(id) ON DELETE CASCADE,
                    invoice_id INTEGER NOT NULL
                        REFERENCES invoices(id) ON DELETE CASCADE,
                    artigo_anterior_id INTEGER
                        REFERENCES artigos_administrativos(id) ON DELETE SET NULL,
                    artigo_novo_id INTEGER
                        REFERENCES artigos_administrativos(id) ON DELETE SET NULL,
                    decisao VARCHAR(40) NOT NULL,
                    motivo TEXT,
                    alterado_por VARCHAR(100) NOT NULL,
                    alterado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_invoice_linha_artigo_audit_line
                    ON invoice_linha_artigo_audit(invoice_linha_id, alterado_em DESC)
                """
            )

            seen_keys = set()
            stats = {'created': 0, 'existing': 0, 'updated': 0, 'rejected': 0}
            for source_row, origin_label, product, brand in CATALOG_ROWS:
                origin_label = str(origin_label or '').strip()
                product = str(product or '').strip()
                if not origin_label or not product:
                    stats['rejected'] += 1
                    continue
                catalog_key = catalog_key_for(origin_label, product)
                if catalog_key in seen_keys:
                    stats['rejected'] += 1
                    continue
                seen_keys.add(catalog_key)

                origin = classify_compras_origin_label(origin_label)
                if origin.get('store_name'):
                    cursor.execute(
                        """
                        INSERT INTO compras_origens
                            (chave, tipo, nome, rotulo_original, store_id)
                        SELECT %s, %s, %s, %s, s.id
                        FROM stores s
                        WHERE LOWER(s.name) = LOWER(%s)
                        ON CONFLICT (chave) DO NOTHING
                        """,
                        (
                            origin['key'], origin['tipo'], origin['nome'],
                            origin['rotulo_original'], origin['store_name'],
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO compras_origens
                            (chave, tipo, nome, rotulo_original)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (chave) DO NOTHING
                        """,
                        (
                            origin['key'], origin['tipo'], origin['nome'],
                            origin['rotulo_original'],
                        ),
                    )
                cursor.execute(
                    "SELECT id FROM compras_origens WHERE chave = %s",
                    (origin['key'],),
                )
                origin_row = cursor.fetchone()
                if not origin_row:
                    stats['rejected'] += 1
                    continue
                origin_id = origin_row[0]
                unidade = infer_artigo_unidade(product)

                # Adopt an exact legacy row without changing its human data.
                cursor.execute(
                    """
                    UPDATE artigos_administrativos
                       SET catalog_key = %s,
                           origem_id = COALESCE(origem_id, %s),
                           origem_original = COALESCE(origem_original, fornecedor),
                           source_dataset = COALESCE(source_dataset, %s),
                           source_version = COALESCE(source_version, %s),
                           source_row = COALESCE(source_row, %s),
                           imported_at = COALESCE(imported_at, NOW()),
                           updated_at = NOW()
                     WHERE catalog_key IS NULL
                       AND fornecedor = %s
                       AND produto = %s
                    """,
                    (
                        catalog_key, origin_id, CATALOG_SOURCE, CATALOG_VERSION,
                        source_row, origin_label, product,
                    ),
                )

                cursor.execute(
                    "SELECT id, marca, unidade, ativo, human_modified_at "
                    "FROM artigos_administrativos WHERE catalog_key = %s",
                    (catalog_key,),
                )
                existing = cursor.fetchone()
                if existing:
                    stats['existing'] += 1
                cursor.execute(
                    """
                    INSERT INTO artigos_administrativos
                        (fornecedor, produto, ativo, origem_id, origem_original,
                         catalog_key, marca, unidade, source_dataset,
                         source_version, source_row, imported_at, updated_at)
                    VALUES (%s, %s, TRUE, %s, %s, %s, %s, %s, %s, %s, %s,
                            NOW(), NOW())
                    ON CONFLICT (catalog_key) DO UPDATE
                       SET marca = CASE
                               WHEN artigos_administrativos.human_modified_at IS NULL
                               THEN COALESCE(artigos_administrativos.marca, EXCLUDED.marca)
                               ELSE artigos_administrativos.marca
                           END,
                           unidade = CASE
                               WHEN artigos_administrativos.human_modified_at IS NULL
                               THEN COALESCE(artigos_administrativos.unidade, EXCLUDED.unidade)
                               ELSE artigos_administrativos.unidade
                           END,
                           source_dataset = COALESCE(
                               artigos_administrativos.source_dataset,
                               EXCLUDED.source_dataset
                           ),
                           source_version = COALESCE(
                               artigos_administrativos.source_version,
                               EXCLUDED.source_version
                           ),
                           source_row = COALESCE(
                               artigos_administrativos.source_row,
                               EXCLUDED.source_row
                           ),
                           imported_at = COALESCE(
                               artigos_administrativos.imported_at,
                               EXCLUDED.imported_at
                           ),
                           updated_at = NOW()
                    """,
                    (
                        origin_label, product, origin_id, origin_label,
                        catalog_key, brand, unidade, CATALOG_SOURCE,
                        CATALOG_VERSION, source_row,
                    ),
                )
                if existing:
                    stats['updated'] += 1
                else:
                    stats['created'] += 1

            conn.commit()
            logger.info(
                "run_migrations_compras_catalogo: %s", stats
            )
            return stats
        except Exception as exc:
            logger.error("run_migrations_compras_catalogo failed: %s", exc)
            conn.rollback()
            raise


def run_migrations_compras_encomendas_semanais():
    """Create the isolated weekly store-purchase request model.

    These rows are planning documents, not stock movements.  Submitted
    snapshots and audit events are kept separately so catalogue deactivation
    or later amendments cannot rewrite what Compras originally received.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT pg_try_advisory_xact_lock(%s)",
                (_LOCK_COMPRAS_ENCOMENDAS_SEMANAIS,),
            )
            if not cursor.fetchone()[0]:
                logger.info(
                    "run_migrations_compras_encomendas_semanais: lock held, skipping"
                )
                return
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS compras_encomendas_semanais (
                    id BIGSERIAL PRIMARY KEY,
                    store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE RESTRICT,
                    ciclo_domingo DATE NOT NULL,
                    entrega_prevista DATE NOT NULL,
                    status VARCHAR(30) NOT NULL DEFAULT 'rascunho'
                        CHECK (status IN (
                            'rascunho', 'submetida', 'em_preparacao',
                            'concluida', 'cancelada'
                        )),
                    observacoes TEXT NOT NULL DEFAULT '',
                    versao_actual INTEGER NOT NULL DEFAULT 0,
                    versao_submetida INTEGER,
                    created_by VARCHAR(100),
                    updated_by VARCHAR(100),
                    submitted_by VARCHAR(100),
                    submitted_at TIMESTAMP,
                    prepared_by VARCHAR(100),
                    prepared_at TIMESTAMP,
                    completed_by VARCHAR(100),
                    completed_at TIMESTAMP,
                    cancelled_by VARCHAR(100),
                    cancelled_at TIMESTAMP,
                    cancel_reason TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (store_id, ciclo_domingo),
                    CHECK (EXTRACT(ISODOW FROM ciclo_domingo) = 7),
                    CHECK (entrega_prevista = ciclo_domingo + 1)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS compras_encomendas_semanais_linhas (
                    id BIGSERIAL PRIMARY KEY,
                    encomenda_id BIGINT NOT NULL
                        REFERENCES compras_encomendas_semanais(id) ON DELETE CASCADE,
                    artigo_id INTEGER REFERENCES artigos_administrativos(id)
                        ON DELETE SET NULL,
                    produto_snapshot VARCHAR(255) NOT NULL,
                    unidade_snapshot VARCHAR(50) NOT NULL,
                    origem_id_snapshot INTEGER REFERENCES compras_origens(id)
                        ON DELETE SET NULL,
                    origem_tipo_snapshot VARCHAR(30) NOT NULL,
                    origem_nome_snapshot VARCHAR(255) NOT NULL,
                    origem_supplier_id_snapshot INTEGER REFERENCES suppliers(id)
                        ON DELETE SET NULL,
                    origem_supplier_nome_snapshot VARCHAR(255),
                    quantidade NUMERIC(12, 3) NOT NULL CHECK (quantidade > 0),
                    observacoes TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (encomenda_id, artigo_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS compras_encomendas_semanais_versoes (
                    id BIGSERIAL PRIMARY KEY,
                    encomenda_id BIGINT NOT NULL
                        REFERENCES compras_encomendas_semanais(id) ON DELETE CASCADE,
                    versao INTEGER NOT NULL,
                    tipo VARCHAR(30) NOT NULL
                        CHECK (tipo IN ('submetida', 'alteracao')),
                    snapshot JSONB NOT NULL,
                    actor VARCHAR(100) NOT NULL,
                    reason TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (encomenda_id, versao)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS compras_encomendas_semanais_audit (
                    id BIGSERIAL PRIMARY KEY,
                    encomenda_id BIGINT NOT NULL
                        REFERENCES compras_encomendas_semanais(id) ON DELETE CASCADE,
                    event_type VARCHAR(50) NOT NULL,
                    actor VARCHAR(100) NOT NULL,
                    reason TEXT,
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    previous_status VARCHAR(30),
                    new_status VARCHAR(30),
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_compras_encomendas_semanais_cycle
                    ON compras_encomendas_semanais(ciclo_domingo, status)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_compras_encomendas_semanais_lines_article
                    ON compras_encomendas_semanais_linhas(artigo_id)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_compras_encomendas_semanais_audit_order
                    ON compras_encomendas_semanais_audit(encomenda_id, created_at, id)
                """
            )
            conn.commit()
            logger.info("run_migrations_compras_encomendas_semanais: schema ready")
        except Exception as exc:
            logger.error(
                "run_migrations_compras_encomendas_semanais failed: %s", exc
            )
            conn.rollback()
            raise


def run_migrations_compras_pedidos_urgentes():
    """Create the isolated, auditable urgent store-purchase request model."""
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT pg_try_advisory_xact_lock(%s)",
                (_LOCK_COMPRAS_PEDIDOS_URGENTES,),
            )
            if not cursor.fetchone()[0]:
                logger.info(
                    "run_migrations_compras_pedidos_urgentes: lock held, skipping"
                )
                return
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS compras_pedidos_urgentes (
                    id BIGSERIAL PRIMARY KEY,
                    store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE RESTRICT,
                    data_pretendida DATE NOT NULL,
                    encomenda_semanal_id BIGINT
                        REFERENCES compras_encomendas_semanais(id) ON DELETE SET NULL,
                    ciclo_domingo DATE,
                    motivo VARCHAR(40) NOT NULL CHECK (
                        motivo IN (
                            'falta_planeamento',
                            'procura_acima_previsto',
                            'outro'
                        )
                    ),
                    motivo_detalhe VARCHAR(1000) NOT NULL DEFAULT '',
                    observacoes TEXT NOT NULL,
                    status VARCHAR(30) NOT NULL DEFAULT 'submetida'
                        CHECK (status IN (
                            'submetida', 'em_preparacao',
                            'concluida', 'cancelada'
                        )),
                    prioridade SMALLINT NOT NULL DEFAULT 2
                        CHECK (prioridade BETWEEN 1 AND 9),
                    dedupe_key VARCHAR(64) NOT NULL,
                    submitted_by VARCHAR(100) NOT NULL,
                    submitted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_by VARCHAR(100),
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    prepared_by VARCHAR(100),
                    prepared_at TIMESTAMP,
                    completed_by VARCHAR(100),
                    completed_at TIMESTAMP,
                    cancelled_by VARCHAR(100),
                    cancelled_at TIMESTAMP,
                    cancel_reason TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK (ciclo_domingo IS NULL OR EXTRACT(ISODOW FROM ciclo_domingo) = 7),
                    CHECK (motivo <> 'outro' OR BTRIM(motivo_detalhe) <> ''),
                    CHECK (BTRIM(observacoes) <> ''),
                    UNIQUE (store_id, dedupe_key)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS compras_pedidos_urgentes_linhas (
                    id BIGSERIAL PRIMARY KEY,
                    pedido_id BIGINT NOT NULL
                        REFERENCES compras_pedidos_urgentes(id) ON DELETE CASCADE,
                    artigo_id INTEGER REFERENCES artigos_administrativos(id)
                        ON DELETE SET NULL,
                    produto_snapshot VARCHAR(255) NOT NULL,
                    unidade_snapshot VARCHAR(50) NOT NULL,
                    origem_id_snapshot INTEGER REFERENCES compras_origens(id)
                        ON DELETE SET NULL,
                    origem_tipo_snapshot VARCHAR(30) NOT NULL,
                    origem_nome_snapshot VARCHAR(255) NOT NULL,
                    origem_store_id_snapshot INTEGER REFERENCES stores(id)
                        ON DELETE SET NULL,
                    origem_supplier_id_snapshot INTEGER REFERENCES suppliers(id)
                        ON DELETE SET NULL,
                    origem_supplier_nome_snapshot VARCHAR(255),
                    quantidade NUMERIC(12, 3) NOT NULL CHECK (quantidade > 0),
                    observacoes TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (pedido_id, artigo_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS compras_pedidos_urgentes_audit (
                    id BIGSERIAL PRIMARY KEY,
                    pedido_id BIGINT NOT NULL
                        REFERENCES compras_pedidos_urgentes(id) ON DELETE CASCADE,
                    event_type VARCHAR(50) NOT NULL,
                    actor VARCHAR(100) NOT NULL,
                    reason TEXT,
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    previous_status VARCHAR(30),
                    new_status VARCHAR(30),
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_compras_pedidos_urgentes_queue
                    ON compras_pedidos_urgentes(status, prioridade, data_pretendida)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_compras_pedidos_urgentes_store
                    ON compras_pedidos_urgentes(store_id, data_pretendida)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_compras_pedidos_urgentes_lines_article
                    ON compras_pedidos_urgentes_linhas(artigo_id)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_compras_pedidos_urgentes_audit
                    ON compras_pedidos_urgentes_audit(pedido_id, created_at, id)
                """
            )
            conn.commit()
            logger.info("run_migrations_compras_pedidos_urgentes: schema ready")
        except Exception as exc:
            logger.error(
                "run_migrations_compras_pedidos_urgentes failed: %s", exc
            )
            conn.rollback()
            raise


def run_migrations_stock_producao_lojas():
    """Ensure stock_producao supports Mouzinho and B2B as loja values.

    Drops any CHECK constraint on stock_producao.loja that would block
    arbitrary store names (Mouzinho, B2B, etc.).  Uses advisory lock 202612
    for idempotency across workers.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_STOCK_PRODUCAO_LOJAS,))
        if not cursor.fetchone()[0]:
            logger.info("run_migrations_stock_producao_lojas: lock held by another worker, skipping")
            return

        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'stock_producao' AND column_name = 'loja'
        """)
        if cursor.fetchone()[0] == 0:
            logger.warning("run_migrations_stock_producao_lojas: stock_producao.loja column not found")
            conn.commit()
            return

        cursor.execute("""
            SELECT con.conname
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_attribute att ON att.attrelid = rel.oid
                AND att.attnum = ANY(con.conkey)
            WHERE con.contype = 'c'
              AND rel.relname = 'stock_producao'
              AND att.attname = 'loja'
        """)
        check_constraints = [row[0] for row in cursor.fetchall()]
        for conname in check_constraints:
            cursor.execute(
                f"ALTER TABLE stock_producao DROP CONSTRAINT IF EXISTS {conname}"
            )
            logger.info(
                "run_migrations_stock_producao_lojas: dropped CHECK constraint %s on loja",
                conname,
            )

        if not check_constraints:
            logger.info(
                "run_migrations_stock_producao_lojas: no CHECK constraints on loja — already clean"
            )

        conn.commit()


def run_migrations_eventos_v2_foundation():
    """Create the v2 events foundation without discarding historical CRM data.

    The existing ``events`` table remains the canonical commercial event record.
    Older ``eventos`` / ``evento_items`` records are intentionally left intact:
    they are still consumed by the already-shipped production integration.  This
    migration adds the v2 structures alongside both models and backfills only a
    primary occurrence for compatible ``events`` rows.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (_LOCK_EVENTOS_V2_FOUNDATION,),
            )
            if not cursor.fetchone()[0]:
                logger.info(
                    "run_migrations_eventos_v2_foundation: lock held by another worker, skipping"
                )
                return

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS event_occurrences (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    occurrence_number INTEGER NOT NULL DEFAULT 1,
                    event_date DATE,
                    venue VARCHAR(255),
                    venue_address TEXT,
                    latitude NUMERIC(10,7),
                    longitude NUMERIC(10,7),
                    estimated_km NUMERIC(10,2),
                    service_start_time TIME,
                    service_end_time TIME,
                    expected_duration_minutes INTEGER,
                    logistics_notes TEXT,
                    access_instructions TEXT,
                    venue_contact_is_client BOOLEAN,
                    venue_contact_name VARCHAR(255),
                    venue_contact_phone VARCHAR(32),
                    service_mode VARCHAR(30) NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(event_id, occurrence_number)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS event_resources (
                    id SERIAL PRIMARY KEY,
                    code VARCHAR(80) NOT NULL UNIQUE,
                    name VARCHAR(255) NOT NULL,
                    resource_type VARCHAR(80) NOT NULL DEFAULT 'equipment',
                    capacity_carapinas INTEGER,
                    capacity_flavors INTEGER,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CHECK (capacity_carapinas IS NULL OR capacity_carapinas >= 0),
                    CHECK (capacity_flavors IS NULL OR capacity_flavors >= 0)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS event_resource_reservations (
                    id SERIAL PRIMARY KEY,
                    occurrence_id INTEGER NOT NULL REFERENCES event_occurrences(id) ON DELETE CASCADE,
                    resource_id INTEGER NOT NULL REFERENCES event_resources(id) ON DELETE RESTRICT,
                    status VARCHAR(30) NOT NULL DEFAULT 'requested',
                    risk_acknowledged BOOLEAN NOT NULL DEFAULT FALSE,
                    notes TEXT,
                    reserved_by VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(occurrence_id, resource_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS event_pricing_settings (
                    key VARCHAR(100) PRIMARY KEY,
                    label VARCHAR(255) NOT NULL,
                    setting_type VARCHAR(30) NOT NULL DEFAULT 'money',
                    value_gross NUMERIC(12,2) NOT NULL DEFAULT 0,
                    taxa_iva NUMERIC(5,4),
                    requires_tax_review BOOLEAN NOT NULL DEFAULT TRUE,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    updated_by VARCHAR(255),
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CHECK (value_gross >= 0),
                    CHECK (taxa_iva IS NULL OR (taxa_iva >= 0 AND taxa_iva <= 1))
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS event_history (
                    id BIGSERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
                    event_type VARCHAR(50) NOT NULL,
                    old_status VARCHAR(50),
                    new_status VARCHAR(50),
                    actor VARCHAR(255),
                    reason TEXT,
                    details JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS event_quote_versions (
                    id BIGSERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
                    version_number INTEGER NOT NULL,
                    reason TEXT,
                    created_by VARCHAR(255),
                    snapshot JSONB NOT NULL,
                    total_net NUMERIC(12,2),
                    total_vat NUMERIC(12,2),
                    total_gross NUMERIC(12,2) NOT NULL DEFAULT 0,
                    quote_revision VARCHAR(128),
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(event_id, version_number)
                )
                """
            )
            cursor.execute(
                "ALTER TABLE event_quote_versions ADD COLUMN IF NOT EXISTS quote_revision VARCHAR(128)"
            )
            cursor.execute(
                "ALTER TABLE event_quote_versions ADD COLUMN IF NOT EXISTS proposal_snapshot JSONB"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_occurrences_event_date "
                "ON event_occurrences(event_date)"
            )
            cursor.execute(
                "ALTER TABLE event_occurrences ADD COLUMN IF NOT EXISTS access_instructions TEXT"
            )
            cursor.execute(
                "ALTER TABLE event_occurrences "
                "ADD COLUMN IF NOT EXISTS venue_contact_is_client BOOLEAN"
            )
            cursor.execute(
                "ALTER TABLE event_occurrences "
                "ADD COLUMN IF NOT EXISTS venue_contact_name VARCHAR(255)"
            )
            cursor.execute(
                "ALTER TABLE event_occurrences "
                "ADD COLUMN IF NOT EXISTS venue_contact_phone VARCHAR(32)"
            )
            cursor.execute(
                "ALTER TABLE event_occurrences "
                "ADD COLUMN IF NOT EXISTS venue_review_dismissed_at TIMESTAMP"
            )
            cursor.execute(
                "ALTER TABLE event_occurrences "
                "ADD COLUMN IF NOT EXISTS venue_review_dismissed_by VARCHAR(255)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_resource_reservations_resource "
                "ON event_resource_reservations(resource_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_history_event_created "
                "ON event_history(event_id, created_at DESC)"
            )
            # Durable coordination for the Sheets importer.  The partial unique
            # index is important: it closes the race between two web workers
            # both deciding that no import is currently queued.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS event_sheet_sync_runs (
                    id BIGSERIAL PRIMARY KEY,
                    status VARCHAR(20) NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
                    trigger_source VARCHAR(80) NOT NULL DEFAULT 'scheduler',
                    requester VARCHAR(255),
                    phase VARCHAR(40),
                    total_rows INTEGER NOT NULL DEFAULT 0,
                    processed_rows INTEGER NOT NULL DEFAULT 0,
                    inserted INTEGER NOT NULL DEFAULT 0,
                    updated INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0,
                    error_summary TEXT,
                    timings JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    heartbeat_at TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                ALTER TABLE event_sheet_sync_runs
                    ADD COLUMN IF NOT EXISTS worker_id VARCHAR(120),
                    ADD COLUMN IF NOT EXISTS lease_token VARCHAR(80),
                    ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(120)
                """
            )
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_event_sheet_sync_active
                ON event_sheet_sync_runs ((1))
                WHERE status IN ('queued', 'running')
                """
            )
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_event_sheet_sync_idempotency
                ON event_sheet_sync_runs (idempotency_key)
                WHERE idempotency_key IS NOT NULL
                """
            )

            # Keep the previous date/location fields for established screens, while
            # adding the financial and operational snapshots used by v2.
            cursor.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMP"
            )
            cursor.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS deposit_amount_eur NUMERIC(12,2)"
            )
            cursor.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS deposit_received_at TIMESTAMP"
            )
            cursor.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS deposit_validated_at TIMESTAMP"
            )
            cursor.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS deposit_verified_by VARCHAR(255)"
            )
            cursor.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS deposit_proof_reference TEXT"
            )
            cursor.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS deposit_non_refundable "
                "BOOLEAN NOT NULL DEFAULT TRUE"
            )
            cursor.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS reserved_at TIMESTAMP"
            )
            cursor.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS invoice_reference VARCHAR(255)"
            )
            cursor.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS invoice_sent_at TIMESTAMP"
            )
            cursor.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS payment_amount_eur NUMERIC(12,2) DEFAULT 0"
            )
            cursor.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS payment_method VARCHAR(80)"
            )
            cursor.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS payment_reference TEXT"
            )
            cursor.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS payment_received_by VARCHAR(255)"
            )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS event_production_requirements (
                    id BIGSERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
                    occurrence_id BIGINT NOT NULL REFERENCES event_occurrences(id) ON DELETE RESTRICT,
                    sabor VARCHAR(255) NOT NULL,
                    required_kg NUMERIC(12,2) NOT NULL CHECK (required_kg > 0),
                    source VARCHAR(50) NOT NULL DEFAULT 'portal_acceptance',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(occurrence_id, sabor)
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_production_requirements_date "
                "ON event_production_requirements(occurrence_id)"
            )
            cursor.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP"
            )
            cursor.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS archived_by VARCHAR(255)"
            )
            # Existing databases created the first version with CASCADE.  Audit
            # records must survive an archive attempt, so make the relationship
            # restrictive too; this is safe to repeat on every startup.
            cursor.execute(
                "ALTER TABLE event_history DROP CONSTRAINT IF EXISTS event_history_event_id_fkey"
            )
            cursor.execute(
                "ALTER TABLE event_history ADD CONSTRAINT event_history_event_id_fkey "
                "FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE RESTRICT"
            )
            cursor.execute(
                "ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS unit_price_gross NUMERIC(10,2)"
            )
            cursor.execute(
                "ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS total_net NUMERIC(10,2)"
            )
            cursor.execute(
                "ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS total_vat NUMERIC(10,2)"
            )
            cursor.execute(
                "ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS total_gross NUMERIC(10,2)"
            )
            cursor.execute(
                "ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS pricing_setting_key VARCHAR(100)"
            )
            cursor.execute(
                "ALTER TABLE artigos_evento ADD COLUMN IF NOT EXISTS taxa_iva NUMERIC(5,4)"
            )
            cursor.execute(
                "ALTER TABLE artigos_evento ADD COLUMN IF NOT EXISTS pricing_setting_key VARCHAR(100)"
            )

            # The defaults are editable starting points, not accounting truth.
            # Historical quote lines with no explicit IVA remain untouched below.
            pricing_defaults = [
                ('gelado_kg', 'Gelado por kg', 'money', 31.80, 0.13),
                ('servico_fixo', 'Serviço por ocorrência', 'money', 120.00, 0.23),
                ('deslocacao_km', 'Deslocação por km', 'money', 0.00, 0.23),
                ('carrinha_fixa', 'Carrinha por ocorrência', 'money', 35.00, 0.23),
                ('carrinho_fixo', 'Carrinho por ocorrência', 'money', 40.00, 0.23),
                ('arca_fixa', 'Arca por ocorrência', 'money', 20.00, 0.23),
                ('sinal_percentagem', 'Sinal de reserva', 'percentage', 15.00, None),
            ]
            for key, label, setting_type, value_gross, taxa_iva in pricing_defaults:
                cursor.execute(
                    """
                    INSERT INTO event_pricing_settings
                        (key, label, setting_type, value_gross, taxa_iva, requires_tax_review)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    ON CONFLICT (key) DO NOTHING
                    """,
                    (key, label, setting_type, value_gross, taxa_iva),
                )

            for code, name in (
                ('carrinha', 'Carrinha de eventos'),
                ('carrinho', 'Carrinho de gelado'),
                ('arca', 'Arca de gelado'),
            ):
                cursor.execute(
                    """
                    INSERT INTO event_resources (code, name, resource_type)
                    VALUES (%s, %s, 'equipment')
                    ON CONFLICT (code) DO NOTHING
                    """,
                    (code, name),
                )

            # The Pipeline is canonical, so every historical lead must have an
            # event row before the legacy lead screens redirect into it.
            cursor.execute(
                "ALTER TABLE events ADD COLUMN IF NOT EXISTS lead_id "
                "INTEGER REFERENCES lead_requests(id) ON DELETE SET NULL"
            )
            cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS google_sheet_row_id VARCHAR(100)")
            cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS source VARCHAR(50) DEFAULT 'manual'")
            cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS event_end_time VARCHAR(20)")
            cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS customer_type VARCHAR(20)")
            cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS company_name VARCHAR(255)")
            cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS nif VARCHAR(9)")
            cursor.execute("ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS event_end_time VARCHAR(20)")
            cursor.execute("ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS customer_type VARCHAR(20)")
            cursor.execute("ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS company_name VARCHAR(255)")
            cursor.execute("ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS nif VARCHAR(9)")
            cursor.execute("""
                UPDATE events e
                   SET lead_id = l.id
                  FROM lead_requests l
                 WHERE e.lead_id IS NULL
                   AND e.google_sheet_row_id IS NOT NULL
                   AND l.google_sheet_row_id = e.google_sheet_row_id
            """)
            cursor.execute("""
                INSERT INTO events (
                    lead_id, event_name, event_type, event_date, event_time,
                    event_end_time, estimated_guests, venue, venue_address,
                    client_name, client_email, client_phone, customer_type,
                    company_name, nif, status, internal_notes, google_sheet_row_id,
                    source, created_at, updated_at
                )
                SELECT l.id, COALESCE(l.event_type, l.client_name), l.event_type,
                       l.event_date, l.event_time, l.event_end_time,
                       l.estimated_guests, l.venue, l.venue_address, l.client_name,
                       l.client_email, l.client_phone, l.customer_type, l.company_name,
                       l.nif,
                       CASE l.status
                           WHEN 'proposal' THEN 'orcamentado'
                           WHEN 'proposal_sent' THEN 'enviado'
                           WHEN 'won' THEN 'adjudicado'
                           WHEN 'lost' THEN 'rejeitado'
                           WHEN 'cancelled' THEN 'cancelado'
                           ELSE 'novos'
                       END,
                       COALESCE(l.internal_notes, l.notes),
                       CASE WHEN l.google_sheet_row_id IS NOT NULL
                                  AND NOT EXISTS (
                                      SELECT 1 FROM events existing
                                      WHERE existing.google_sheet_row_id = l.google_sheet_row_id
                                  )
                            THEN l.google_sheet_row_id END,
                       l.source, COALESCE(l.created_at, CURRENT_TIMESTAMP),
                       COALESCE(l.updated_at, CURRENT_TIMESTAMP)
                  FROM lead_requests l
                 WHERE NOT EXISTS (
                     SELECT 1 FROM events e WHERE e.lead_id = l.id
                 )
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS events_google_sheet_row_id_idx
                ON events(google_sheet_row_id) WHERE google_sheet_row_id IS NOT NULL
            """)

            # Move the active CRM table to the agreed pipeline.  The legacy
            # production table named ``eventos`` remains untouched by design.
            cursor.execute(
                """
                UPDATE events
                SET status = CASE status
                    WHEN 'lead' THEN 'novos'
                    WHEN 'contacted' THEN 'orcamentado'
                    WHEN 'negotiating' THEN 'orcamentado'
                    WHEN 'proposal_sent' THEN 'enviado'
                    WHEN 'won' THEN 'adjudicado'
                    WHEN 'lost' THEN 'rejeitado'
                    WHEN 'cancelled' THEN 'cancelado'
                    ELSE status
                END,
                status_changed_at = COALESCE(status_changed_at, updated_at, created_at)
                WHERE status IN ('lead', 'contacted', 'negotiating', 'proposal_sent',
                                 'won', 'lost', 'cancelled')
                """
            )

            # Backfill a single primary occurrence for the existing CRM data.
            cursor.execute(
                """
                INSERT INTO event_occurrences (
                    event_id, occurrence_number, event_date, venue, venue_address,
                    service_start_time, service_end_time, logistics_notes
                )
                SELECT
                    e.id, 1, e.event_date, e.venue, e.venue_address,
                    CASE
                        WHEN e.event_time ~ '^\\d{1,2}:\\d{2}(:\\d{2})?$'
                        THEN e.event_time::time
                        ELSE NULL
                    END,
                    CASE
                        WHEN e.event_end_time ~ '^\\d{1,2}:\\d{2}(:\\d{2})?$'
                        THEN e.event_end_time::time
                        ELSE NULL
                    END,
                    e.internal_notes
                FROM events e
                WHERE NOT EXISTS (
                    SELECT 1 FROM event_occurrences eo
                    WHERE eo.event_id = e.id
                )
                ON CONFLICT (event_id, occurrence_number) DO NOTHING
                """
            )

            # Preserve historical totals without inventing IVA.  New or edited
            # lines are snapshotted by db.eventos with their configured rate.
            cursor.execute(
                """
                UPDATE quote_items
                SET unit_price_gross = COALESCE(unit_price_gross, preco_unitario),
                    total_gross = COALESCE(total_gross, total)
                WHERE unit_price_gross IS NULL OR total_gross IS NULL
                """
            )
            cursor.execute(
                """
                UPDATE quote_items
                SET total_net = ROUND(total / (1 + taxa_iva), 2),
                    total_vat = total - ROUND(total / (1 + taxa_iva), 2),
                    total_gross = total,
                    unit_price_gross = COALESCE(unit_price_gross, preco_unitario)
                WHERE taxa_iva IS NOT NULL
                  AND (total_net IS NULL OR total_vat IS NULL)
                """
            )
            cursor.execute(
                """
                UPDATE artigos_evento
                SET taxa_iva = CASE
                    WHEN codigo IN ('gelado_kg', 'gelado_sabor') THEN 0.13
                    ELSE 0.23
                END
                WHERE taxa_iva IS NULL
                """
            )
            # Locations are independent operational records.  Occurrence text is
            # deliberately retained as historical evidence; the optional link is
            # only made for an exact name + address match.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS event_venues (
                    id BIGSERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    address TEXT NOT NULL,
                    name_key VARCHAR(255) NOT NULL,
                    address_key TEXT NOT NULL,
                    contact_name VARCHAR(255),
                    contact_phone VARCHAR(80),
                    contact_email VARCHAR(255),
                    logistics_notes TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(name_key, address_key)
                )
            """)
            cursor.execute(
                "ALTER TABLE event_venues ADD COLUMN IF NOT EXISTS latitude NUMERIC(10,7)"
            )
            cursor.execute(
                "ALTER TABLE event_venues ADD COLUMN IF NOT EXISTS longitude NUMERIC(10,7)"
            )
            cursor.execute(
                "ALTER TABLE event_venues ADD COLUMN IF NOT EXISTS geocode_provider VARCHAR(80)"
            )
            cursor.execute(
                "ALTER TABLE event_venues "
                "ADD COLUMN IF NOT EXISTS geocode_failed BOOLEAN NOT NULL DEFAULT FALSE"
            )
            cursor.execute(
                "ALTER TABLE event_clients ADD COLUMN IF NOT EXISTS email_key VARCHAR(255)"
            )
            cursor.execute(
                "ALTER TABLE event_clients ADD COLUMN IF NOT EXISTS phone_key VARCHAR(32)"
            )
            cursor.execute("""
                UPDATE event_clients
                SET email_key=LOWER(BTRIM(email))
                WHERE email_key IS NULL AND NULLIF(BTRIM(COALESCE(email,'')), '') IS NOT NULL
            """)
            cursor.execute("""
                UPDATE event_clients
                SET phone_key=REGEXP_REPLACE(COALESCE(phone,''), '[^0-9]', '', 'g')
                WHERE phone_key IS NULL
                  AND NULLIF(REGEXP_REPLACE(COALESCE(phone,''), '[^0-9]', '', 'g'), '') IS NOT NULL
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_clients_email_key ON event_clients(email_key)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_clients_phone_key ON event_clients(phone_key)"
            )
            # Populate the CRM from historical events using deterministic identifiers
            # only. Existing duplicate client identities remain unlinked for review.
            cursor.execute("""
                WITH historical AS (
                    SELECT DISTINCT ON (identity_key)
                           client_name, client_email, client_phone, email_key, phone_key
                    FROM (
                        SELECT client_name, NULLIF(BTRIM(client_email),'') AS client_email,
                               NULLIF(BTRIM(client_phone),'') AS client_phone,
                               NULLIF(LOWER(BTRIM(client_email)),'') AS email_key,
                               NULLIF(REGEXP_REPLACE(COALESCE(client_phone,''), '[^0-9]', '', 'g'),'') AS phone_key,
                               CASE
                                 WHEN NULLIF(LOWER(BTRIM(client_email)),'') IS NOT NULL
                                   THEN 'email:' || LOWER(BTRIM(client_email))
                                 WHEN NULLIF(REGEXP_REPLACE(COALESCE(client_phone,''), '[^0-9]', '', 'g'),'') IS NOT NULL
                                   THEN 'phone:' || REGEXP_REPLACE(COALESCE(client_phone,''), '[^0-9]', '', 'g')
                               END AS identity_key,
                               COALESCE(updated_at, created_at) AS seen_at
                        FROM events
                        WHERE NULLIF(BTRIM(COALESCE(client_name,'')), '') IS NOT NULL
                    ) source
                    WHERE identity_key IS NOT NULL
                    ORDER BY identity_key, seen_at DESC NULLS LAST
                )
                INSERT INTO event_clients
                    (name,email,phone,marketing_consent,email_key,phone_key)
                SELECT client_name,client_email,client_phone,FALSE,email_key,phone_key
                FROM historical h
                WHERE NOT EXISTS (
                    SELECT 1 FROM event_clients ec
                    WHERE (h.email_key IS NOT NULL AND ec.email_key=h.email_key)
                       OR (h.email_key IS NULL AND h.phone_key IS NOT NULL
                           AND ec.email_key IS NULL AND ec.phone_key=h.phone_key)
                )
            """)
            cursor.execute("""
                WITH candidates AS (
                    SELECT e.id AS event_id, ec.id AS client_id,
                           COUNT(*) OVER (PARTITION BY e.id) AS candidate_count
                    FROM events e
                    JOIN event_clients ec ON
                      (NULLIF(LOWER(BTRIM(e.client_email)),'') IS NOT NULL
                       AND ec.email_key=LOWER(BTRIM(e.client_email)))
                      OR
                      (NULLIF(LOWER(BTRIM(e.client_email)),'') IS NULL
                       AND NULLIF(REGEXP_REPLACE(COALESCE(e.client_phone,''), '[^0-9]', '', 'g'),'') IS NOT NULL
                       AND ec.email_key IS NULL
                       AND ec.phone_key=REGEXP_REPLACE(COALESCE(e.client_phone,''), '[^0-9]', '', 'g'))
                    WHERE e.client_id IS NULL
                )
                UPDATE events e SET client_id=c.client_id
                FROM candidates c
                WHERE e.id=c.event_id AND c.candidate_count=1
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS event_venue_history (
                    id BIGSERIAL PRIMARY KEY,
                    venue_id BIGINT NOT NULL REFERENCES event_venues(id) ON DELETE RESTRICT,
                    actor VARCHAR(255),
                    action VARCHAR(60) NOT NULL,
                    details JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute(
                "ALTER TABLE event_occurrences ADD COLUMN IF NOT EXISTS venue_id "
                "BIGINT REFERENCES event_venues(id) ON DELETE SET NULL"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_occurrences_venue_id "
                "ON event_occurrences(venue_id)"
            )
            cursor.execute("""
                INSERT INTO event_venues (name, address, name_key, address_key, logistics_notes)
                SELECT MIN(BTRIM(venue)), MIN(BTRIM(venue_address)),
                       LOWER(REGEXP_REPLACE(BTRIM(venue), '\s+', ' ', 'g')),
                       LOWER(REGEXP_REPLACE(BTRIM(venue_address), '\s+', ' ', 'g')),
                       MIN(NULLIF(BTRIM(logistics_notes), ''))
                FROM event_occurrences
                WHERE NULLIF(BTRIM(venue), '') IS NOT NULL
                  AND NULLIF(BTRIM(venue_address), '') IS NOT NULL
                GROUP BY LOWER(REGEXP_REPLACE(BTRIM(venue), '\s+', ' ', 'g')),
                         LOWER(REGEXP_REPLACE(BTRIM(venue_address), '\s+', ' ', 'g'))
                ON CONFLICT (name_key, address_key) DO NOTHING
            """)
            cursor.execute("""
                UPDATE event_occurrences eo
                SET venue_id = ev.id
                FROM event_venues ev
                WHERE eo.venue_id IS NULL
                  AND LOWER(REGEXP_REPLACE(BTRIM(COALESCE(eo.venue, '')), '\s+', ' ', 'g')) = ev.name_key
                  AND LOWER(REGEXP_REPLACE(BTRIM(COALESCE(eo.venue_address, '')), '\s+', ' ', 'g')) = ev.address_key
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS event_pipeline_view_preferences (
                    user_key VARCHAR(255) PRIMARY KEY,
                    columns JSONB NOT NULL DEFAULT '[]'::jsonb,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.commit()
            logger.info("run_migrations_eventos_v2_foundation: complete")
        except Exception:
            conn.rollback()
            logger.exception("run_migrations_eventos_v2_foundation failed")
            raise
        finally:
            try:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (_LOCK_EVENTOS_V2_FOUNDATION,),
                )
                conn.commit()
            except Exception:
                logger.warning(
                    "run_migrations_eventos_v2_foundation: could not release advisory lock"
                )


def run_migrations_eventos_customer_portal():
    """Additive storage for the public customer event portal.

    Portal data is deliberately separate from internal event notes and legacy
    Google Sheets fields so public reads can be restricted by normalized email.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (_LOCK_EVENTOS_CUSTOMER_PORTAL,),
            )
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_eventos_customer_portal: lock held, skipping")
                return
            cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS source VARCHAR(50)")
            cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS customer_type VARCHAR(20)")
            cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS company_name VARCHAR(255)")
            cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS nif VARCHAR(9)")
            cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS event_end_time VARCHAR(20)")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS event_portal_requests (
                    id BIGSERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL UNIQUE REFERENCES events(id) ON DELETE RESTRICT,
                    email_normalized VARCHAR(255) NOT NULL,
                    access_code_hash VARCHAR(128),
                    privacy_version VARCHAR(40) NOT NULL DEFAULT 'portal-v1',
                    privacy_accepted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    marketing_consent BOOLEAN NOT NULL DEFAULT FALSE,
                    referral_source VARCHAR(255),
                    servings_per_guest INTEGER NOT NULL DEFAULT 1,
                    flavours JSONB NOT NULL DEFAULT '[]'::jsonb,
                    resource_preferences JSONB NOT NULL DEFAULT '[]'::jsonb,
                    catering_requested BOOLEAN NOT NULL DEFAULT FALSE,
                    estimate_eligible BOOLEAN NOT NULL DEFAULT FALSE,
                    estimated_base_eur NUMERIC(12,2),
                    estimated_vat_eur NUMERIC(12,2),
                    estimated_total_eur NUMERIC(12,2),
                    public_message TEXT,
                    manual_review_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
                     short_notice_warning TEXT,
                    logistics_message TEXT,
                    sent_quote_version_id BIGINT REFERENCES event_quote_versions(id) ON DELETE RESTRICT,
                    accepted_quote_revision VARCHAR(128),
                    quote_accepted_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                ALTER TABLE event_portal_requests
                ADD COLUMN IF NOT EXISTS submission_identifier VARCHAR(255)
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_event_portal_requests_submission_identifier
                ON event_portal_requests (submission_identifier)
                WHERE submission_identifier IS NOT NULL
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS event_portal_brand_configs (
                    id BIGSERIAL PRIMARY KEY,
                    store_id INTEGER NOT NULL UNIQUE REFERENCES stores(id) ON DELETE RESTRICT,
                    brand_name VARCHAR(120) NOT NULL DEFAULT 'Scoopy',
                    logo_filename VARCHAR(255),
                    logo_data BYTEA,
                    logo_content_type VARCHAR(100),
                    primary_color VARCHAR(7) NOT NULL DEFAULT '#167C70',
                    accent_color VARCHAR(7) NOT NULL DEFAULT '#35A394',
                    background_color VARCHAR(7) NOT NULL DEFAULT '#FFF8F2',
                    text_color VARCHAR(7) NOT NULL DEFAULT '#173B38',
                    button_color VARCHAR(7) NOT NULL DEFAULT '#167C70',
                    button_text_color VARCHAR(7) NOT NULL DEFAULT '#FFFFFF',
                    form_title VARCHAR(160) NOT NULL DEFAULT 'Peça o seu evento',
                    form_intro TEXT NOT NULL DEFAULT '',
                    confirmation_message TEXT NOT NULL DEFAULT '',
                    contact_text TEXT NOT NULL DEFAULT '',
                    support_phone VARCHAR(32),
                    portal_access_code_hash TEXT,
                     min_advance_days INTEGER NOT NULL DEFAULT 0,
                     short_notice_warning TEXT NOT NULL DEFAULT 'Atenção: esta data está próxima e poderá não ser possível garantir a disponibilidade.',
                    field_labels JSONB NOT NULL DEFAULT '{}'::jsonb,
                    visible_fields JSONB NOT NULL DEFAULT '{}'::jsonb,
                    is_default BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_event_portal_brand_default
                ON event_portal_brand_configs (is_default)
                WHERE is_default = TRUE
            """)
            cursor.execute("""
                ALTER TABLE event_portal_requests
                ADD COLUMN IF NOT EXISTS brand_store_id INTEGER
                REFERENCES stores(id) ON DELETE RESTRICT
            """)
            cursor.execute("ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS customer_type VARCHAR(20)")
            cursor.execute("ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS company_name VARCHAR(255)")
            cursor.execute("ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS nif VARCHAR(9)")
            cursor.execute("""
                ALTER TABLE event_portal_requests
                ADD COLUMN IF NOT EXISTS access_code_hash VARCHAR(128)
            """)
            cursor.execute("ALTER TABLE event_portal_requests ADD COLUMN IF NOT EXISTS short_notice_warning TEXT")
            cursor.execute(
                "ALTER TABLE event_portal_requests "
                "ADD COLUMN IF NOT EXISTS manual_review_reasons JSONB NOT NULL DEFAULT '[]'::jsonb"
            )
            cursor.execute("ALTER TABLE event_portal_brand_configs ADD COLUMN IF NOT EXISTS min_advance_days INTEGER NOT NULL DEFAULT 0")
            cursor.execute("ALTER TABLE event_portal_brand_configs ADD COLUMN IF NOT EXISTS short_notice_warning TEXT NOT NULL DEFAULT 'Atenção: esta data está próxima e poderá não ser possível garantir a disponibilidade.'")
            cursor.execute("ALTER TABLE event_portal_brand_configs ADD COLUMN IF NOT EXISTS logo_data BYTEA")
            cursor.execute("ALTER TABLE event_portal_brand_configs ADD COLUMN IF NOT EXISTS logo_content_type VARCHAR(100)")
            cursor.execute(
                "ALTER TABLE event_portal_brand_configs "
                "ADD COLUMN IF NOT EXISTS support_phone VARCHAR(32)"
            )
            cursor.execute(
                "ALTER TABLE event_portal_brand_configs "
                "ADD COLUMN IF NOT EXISTS portal_access_code_hash TEXT"
            )
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM event_portal_brand_configs
                    WHERE portal_access_code_hash IS NULL
                       OR BTRIM(portal_access_code_hash) = ''
                )
            """)
            if (cursor.fetchone() or (False,))[0]:
                cursor.execute("""
                    UPDATE event_portal_brand_configs
                    SET portal_access_code_hash = %s
                    WHERE portal_access_code_hash IS NULL
                       OR BTRIM(portal_access_code_hash) = ''
                """, (generate_password_hash('080522'),))
            cursor.execute("""
                ALTER TABLE event_portal_requests
                ADD COLUMN IF NOT EXISTS sent_quote_version_id BIGINT
            """)
            # Public catalogue metadata is additive; historical requests retain
            # their original snapshots.
            cursor.execute("ALTER TABLE event_resources ADD COLUMN IF NOT EXISTS image_url TEXT")
            cursor.execute("ALTER TABLE event_resources ADD COLUMN IF NOT EXISTS image_data BYTEA")
            cursor.execute("ALTER TABLE event_resources ADD COLUMN IF NOT EXISTS image_content_type VARCHAR(100)")
            cursor.execute("ALTER TABLE event_resources ADD COLUMN IF NOT EXISTS public_description TEXT")
            cursor.execute("ALTER TABLE event_resources ADD COLUMN IF NOT EXISTS public_capacity_flavors INTEGER")
            cursor.execute("ALTER TABLE event_resources ADD COLUMN IF NOT EXISTS width_cm NUMERIC(10,2)")
            cursor.execute("ALTER TABLE event_resources ADD COLUMN IF NOT EXISTS height_cm NUMERIC(10,2)")
            cursor.execute("ALTER TABLE event_resources ADD COLUMN IF NOT EXISTS length_cm NUMERIC(10,2)")
            cursor.execute("ALTER TABLE event_resources ADD COLUMN IF NOT EXISTS weight_kg NUMERIC(10,2)")
            cursor.execute("ALTER TABLE event_resources ADD COLUMN IF NOT EXISTS public_customer_requirements TEXT")
            for name, expression in (
                ("event_resources_width_cm_check", "width_cm IS NULL OR (width_cm >= 0 AND width_cm <= 100000)"),
                ("event_resources_height_cm_check", "height_cm IS NULL OR (height_cm >= 0 AND height_cm <= 100000)"),
                ("event_resources_length_cm_check", "length_cm IS NULL OR (length_cm >= 0 AND length_cm <= 100000)"),
                ("event_resources_weight_kg_check", "weight_kg IS NULL OR (weight_kg >= 0 AND weight_kg <= 100000)"),
            ):
                cursor.execute(f"""DO $$ BEGIN
                    ALTER TABLE event_resources ADD CONSTRAINT {name} CHECK ({expression});
                EXCEPTION WHEN duplicate_object THEN NULL; END $$;""")
            cursor.execute("ALTER TABLE event_portal_requests ADD COLUMN IF NOT EXISTS resource_requirements_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb")
            cursor.execute("""
                DO $$ BEGIN
                    ALTER TABLE event_resources
                    ADD CONSTRAINT event_resources_public_capacity_flavors_check
                    CHECK (public_capacity_flavors IS NULL OR
                           public_capacity_flavors BETWEEN 1 AND 6);
                EXCEPTION WHEN duplicate_object THEN NULL;
                END $$;
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS event_portal_flavours (
                    id BIGSERIAL PRIMARY KEY,
                    receita_id INTEGER NOT NULL REFERENCES receitas_gelado(id) ON DELETE RESTRICT,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    UNIQUE(receita_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS event_portal_rate_limits (
                    id BIGSERIAL PRIMARY KEY,
                    ip_fingerprint VARCHAR(128) NOT NULL,
                    action VARCHAR(40) NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_event_portal_rate_limits_lookup
                ON event_portal_rate_limits (ip_fingerprint, action, created_at DESC)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS event_portal_access_log (
                    id BIGSERIAL PRIMARY KEY,
                    email_normalized VARCHAR(255) NOT NULL,
                    event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
                    action VARCHAR(80) NOT NULL,
                    ip_fingerprint VARCHAR(128),
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS event_portal_files (
                    id BIGSERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
                    storage_name VARCHAR(255) NOT NULL UNIQUE,
                    original_filename VARCHAR(255) NOT NULL,
                    content_type VARCHAR(100) NOT NULL,
                    byte_size INTEGER NOT NULL,
                    purpose VARCHAR(50) NOT NULL DEFAULT 'deposit_proof',
                    expires_at TIMESTAMP NOT NULL,
                    deleted_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS event_portal_geocode_cache (
                    address_key VARCHAR(500) PRIMARY KEY,
                    latitude NUMERIC(10,7),
                    longitude NUMERIC(10,7),
                    round_trip_km NUMERIC(10,2),
                    provider VARCHAR(50),
                    failed BOOLEAN NOT NULL DEFAULT FALSE,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_portal_requests_email "
                "ON event_portal_requests(email_normalized, created_at DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_portal_files_expiry "
                "ON event_portal_files(expires_at) WHERE deleted_at IS NULL"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_portal_access_email "
                "ON event_portal_access_log(email_normalized, created_at DESC)"
            )
            conn.commit()
            logger.info("run_migrations_eventos_customer_portal: complete")
        except Exception:
            conn.rollback()
            logger.exception("run_migrations_eventos_customer_portal failed")
            raise
        finally:
            try:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (_LOCK_EVENTOS_CUSTOMER_PORTAL,),
                )
                conn.commit()
            except Exception:
                logger.warning("run_migrations_eventos_customer_portal: could not release lock")

SCHEMA_VERSION = 15

def init_database():
    with db_connection() as conn:
        cursor = conn.cursor()

        # Criar tabela de versão se não existir
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS db_schema_version (
                version INTEGER NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

        cursor.execute("SELECT version FROM db_schema_version LIMIT 1")
        row = cursor.fetchone()
        current_version = row[0] if row else 0

        if current_version >= SCHEMA_VERSION:
            return
    
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.tables 
            WHERE table_schema = 'public' AND table_name = 'gramas_gelado'
        """)
        gramas_exist = cursor.fetchone()[0] > 0
    
        if not gramas_exist:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS gramas_gelado (
                    id SERIAL PRIMARY KEY,
                    artigo VARCHAR(255) NOT NULL UNIQUE,
                    gramas REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
        
            artigos_iniciais = [
                ('Copo Pequeno ( 2 Sabores)', 150),
                ('Copo Medio (3 Sabores)', 180),
                ('Cone Pequeno (2 Sabores)', 150),
                ('Copo Mini', 110),
                ('Cone Medio (3 Sabores)', 180),
                ('Copo Grande (4 Sabores)', 230),
                ('Copo Maxi', 350),
            ]
            for artigo, gramas in artigos_iniciais:
                cursor.execute("INSERT INTO gramas_gelado (artigo, gramas) VALUES (%s, %s) ON CONFLICT DO NOTHING", (artigo, gramas))
            conn.commit()
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) NOT NULL UNIQUE,
                password VARCHAR(255) NOT NULL,
                role VARCHAR(50) NOT NULL,
                nome VARCHAR(255),
                ativo BOOLEAN DEFAULT TRUE,
                acesso_eurokg BOOLEAN DEFAULT FALSE,
                acesso_producao BOOLEAN DEFAULT FALSE,
                acesso_vendas BOOLEAN DEFAULT FALSE,
                acesso_pastelaria BOOLEAN DEFAULT FALSE,
                acesso_confeitaria BOOLEAN DEFAULT FALSE,
                acesso_gestor BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS acesso_eurokg BOOLEAN DEFAULT FALSE")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS acesso_producao BOOLEAN DEFAULT FALSE")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS acesso_vendas BOOLEAN DEFAULT FALSE")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS acesso_pastelaria BOOLEAN DEFAULT FALSE")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS acesso_confeitaria BOOLEAN DEFAULT FALSE")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS acesso_gestor BOOLEAN DEFAULT FALSE")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS acesso_administrativo BOOLEAN DEFAULT FALSE")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS acesso_financeiro BOOLEAN DEFAULT FALSE")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS acesso_eventos BOOLEAN DEFAULT TRUE")

        ROLE_PERMS = {
            'gestao': {'acesso_eurokg': True, 'acesso_producao': True, 'acesso_vendas': True, 'acesso_pastelaria': True, 'acesso_confeitaria': True, 'acesso_gestor': True, 'acesso_administrativo': True},
            'producao': {'acesso_eurokg': True, 'acesso_producao': True, 'acesso_pastelaria': True, 'acesso_confeitaria': True},
            'vendas': {'acesso_eurokg': True, 'acesso_vendas': True},
            'vendasmat': {'acesso_eurokg': True, 'acesso_pastelaria': True, 'acesso_confeitaria': True},
        }
    
        users_iniciais = [
            ('carlosjcmoreira', os.environ.get('SEED_PASSWORD_GESTAO'), 'gestao'),
            ('producao', os.environ.get('SEED_PASSWORD_PRODUCAO'), 'producao'),
            ('vendas', os.environ.get('SEED_PASSWORD_VENDAS'), 'vendas'),
            ('vendasmat', os.environ.get('SEED_PASSWORD_VENDASMAT'), 'vendasmat'),
        ]
        for username, password, role in users_iniciais:
            if not password:
                continue
            cursor.execute("SELECT COUNT(*) FROM users WHERE username = %s", (username,))
            if cursor.fetchone()[0] == 0:
                hashed_pw = hash_password(password)
                perms = ROLE_PERMS.get(role, {})
                cursor.execute(
                    """INSERT INTO users (username, password, role, nome, acesso_eurokg, acesso_producao, acesso_vendas, acesso_pastelaria, acesso_confeitaria, acesso_gestor)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (username, hashed_pw, role, username,
                     perms.get('acesso_eurokg', False), perms.get('acesso_producao', False),
                     perms.get('acesso_vendas', False), perms.get('acesso_pastelaria', False),
                     perms.get('acesso_confeitaria', False), perms.get('acesso_gestor', False))
                )
    
        cursor.execute("UPDATE users SET acesso_eurokg = TRUE, acesso_producao = TRUE, acesso_vendas = TRUE, acesso_pastelaria = TRUE, acesso_confeitaria = TRUE, acesso_gestor = TRUE, acesso_administrativo = TRUE WHERE role = 'gestao' AND acesso_gestor = FALSE")
        cursor.execute("UPDATE users SET acesso_administrativo = TRUE WHERE role = 'gestao' AND acesso_administrativo = FALSE")
        cursor.execute("UPDATE users SET acesso_eurokg = TRUE, acesso_producao = TRUE, acesso_pastelaria = TRUE, acesso_confeitaria = TRUE WHERE role = 'producao' AND acesso_eurokg = FALSE AND acesso_producao = FALSE")
        cursor.execute("UPDATE users SET acesso_eurokg = TRUE, acesso_vendas = TRUE WHERE role = 'vendas' AND acesso_eurokg = FALSE AND acesso_vendas = FALSE")
        cursor.execute("UPDATE users SET acesso_eurokg = TRUE, acesso_pastelaria = TRUE, acesso_confeitaria = TRUE WHERE role = 'vendasmat' AND acesso_eurokg = FALSE AND acesso_pastelaria = FALSE")
    
        cursor.execute("UPDATE users SET acesso_eurokg = FALSE WHERE acesso_eurokg IS NULL")
        cursor.execute("UPDATE users SET acesso_producao = FALSE WHERE acesso_producao IS NULL")
        cursor.execute("UPDATE users SET acesso_vendas = FALSE WHERE acesso_vendas IS NULL")
        cursor.execute("UPDATE users SET acesso_pastelaria = FALSE WHERE acesso_pastelaria IS NULL")
        cursor.execute("UPDATE users SET acesso_confeitaria = FALSE WHERE acesso_confeitaria IS NULL")
        cursor.execute("UPDATE users SET acesso_gestor = FALSE WHERE acesso_gestor IS NULL")
        cursor.execute("UPDATE users SET acesso_administrativo = FALSE WHERE acesso_administrativo IS NULL")
        cursor.execute("UPDATE users SET acesso_financeiro = TRUE WHERE acesso_gestor = TRUE AND (acesso_financeiro IS NULL OR acesso_financeiro = FALSE)")
        cursor.execute("UPDATE users SET acesso_financeiro = FALSE WHERE acesso_financeiro IS NULL")
        cursor.execute("UPDATE users SET acesso_eventos = TRUE WHERE acesso_eventos IS NULL")
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS producao (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                loja VARCHAR(100) NOT NULL,
                quantidade_kg REAL NOT NULL,
                tipo VARCHAR(50) DEFAULT 'producao',
                sabor VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS quebras (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                loja VARCHAR(100) NOT NULL,
                quantidade_kg REAL NOT NULL,
                motivo TEXT,
                sabor VARCHAR(255),
                lote VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vendas (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                loja VARCHAR(100) NOT NULL,
                valor_euros REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_inicial (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                loja VARCHAR(100) NOT NULL,
                quantidade_kg REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS rececao_stock (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                loja VARCHAR(100) NOT NULL,
                quantidade_kg REAL NOT NULL,
                origem VARCHAR(255),
                sabor VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS producao_pastelaria (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                loja VARCHAR(100) NOT NULL,
                produto VARCHAR(255) NOT NULL,
                quantidade INTEGER NOT NULL,
                lote VARCHAR(100) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS producao_confeitaria (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                loja VARCHAR(100) NOT NULL,
                produto VARCHAR(255) NOT NULL,
                quantidade INTEGER NOT NULL,
                lote VARCHAR(100) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS produtos_confeitaria (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(255) NOT NULL UNIQUE,
                ativo BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute("SELECT COUNT(*) FROM produtos_confeitaria")
        if cursor.fetchone()[0] == 0:
            produtos_iniciais = ['Cookie Avelã', 'Cookie Chocolate', 'Cookie Pistachio', 'Brownie']
            for produto in produtos_iniciais:
                cursor.execute("INSERT INTO produtos_confeitaria (nome) VALUES (%s) ON CONFLICT DO NOTHING", (produto,))
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS motivos_quebra (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(255) NOT NULL UNIQUE,
                ativo BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute("SELECT COUNT(*) FROM motivos_quebra")
        if cursor.fetchone()[0] == 0:
            motivos_iniciais = ['Produto depreciado', 'Produto defeituoso', 'Validade excedida']
            for motivo in motivos_iniciais:
                cursor.execute("INSERT INTO motivos_quebra (nome) VALUES (%s) ON CONFLICT DO NOTHING", (motivo,))
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS produtos_pastelaria (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(255) NOT NULL UNIQUE,
                ativo BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'produtos_pastelaria' AND column_name = 'nome'
        """)
        if cursor.fetchone():
            cursor.execute("ALTER TABLE produtos_pastelaria RENAME COLUMN nome TO tipologia")
    
        try:
            cursor.execute("ALTER TABLE produtos_pastelaria DROP CONSTRAINT IF EXISTS produtos_pastelaria_nome_key")
        except:
            conn.rollback()
    
        cursor.execute("ALTER TABLE produtos_pastelaria ADD COLUMN IF NOT EXISTS cobertura VARCHAR(255)")
        cursor.execute("ALTER TABLE produtos_pastelaria ADD COLUMN IF NOT EXISTS sabor VARCHAR(255)")
    
        cursor.execute("""
            SELECT 1 FROM pg_indexes 
            WHERE tablename = 'produtos_pastelaria' AND indexname = 'uq_produtos_past_tip_sab_cob'
        """)
        if not cursor.fetchone():
            try:
                cursor.execute("CREATE UNIQUE INDEX uq_produtos_past_tip_sab_cob ON produtos_pastelaria (tipologia, COALESCE(sabor, ''), COALESCE(cobertura, ''))")
            except:
                conn.rollback()
    
        cursor.execute("SELECT COUNT(*) FROM produtos_pastelaria")
        if cursor.fetchone()[0] == 0:
            produtos_past_iniciais = ['Palitos', 'Nivotos', 'Palitos Pequenos', 'Bolo', 'Bolo Individual']
            for produto in produtos_past_iniciais:
                cursor.execute("INSERT INTO produtos_pastelaria (tipologia) VALUES (%s) ON CONFLICT DO NOTHING", (produto,))
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS gelado_por_tipologia (
                id SERIAL PRIMARY KEY,
                tipologia VARCHAR(255) NOT NULL UNIQUE,
                quantidade_gelado_g REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tipologias_pastelaria (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(255) NOT NULL UNIQUE,
                ativo BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute("SELECT COUNT(*) FROM tipologias_pastelaria")
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                INSERT INTO tipologias_pastelaria (nome)
                SELECT DISTINCT tipologia FROM produtos_pastelaria WHERE tipologia IS NOT NULL AND tipologia != ''
                ON CONFLICT DO NOTHING
            """)

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS coberturas (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(255) NOT NULL UNIQUE,
                ativo BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS produtos_rececao (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(255) NOT NULL UNIQUE,
                tipo VARCHAR(100) NOT NULL,
                ativo BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute("SELECT COUNT(*) FROM produtos_rececao")
        if cursor.fetchone()[0] == 0:
            produtos_rec_iniciais = [
                ('Copos', 'embalagem'),
                ('Cones', 'embalagem'),
                ('Colheres', 'embalagem'),
                ('Gelado', 'gelado'),
                ('Pastelaria', 'pastelaria'),
                ('Confeitaria', 'confeitaria')
            ]
            for nome, tipo in produtos_rec_iniciais:
                cursor.execute("INSERT INTO produtos_rececao (nome, tipo) VALUES (%s, %s) ON CONFLICT DO NOTHING", (nome, tipo))
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS receitas_gelado (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(255) NOT NULL UNIQUE,
                nome_corrente VARCHAR(255),
                ativo BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'receitas_gelado' AND column_name = 'nome_corrente'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE receitas_gelado ADD COLUMN nome_corrente VARCHAR(255)")

        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'receitas_gelado' AND column_name = 'conta_eurokg'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE receitas_gelado ADD COLUMN conta_eurokg BOOLEAN DEFAULT TRUE")
    
        receitas_mapeamento = [
            ('GANACHE FONDENTE', 'Ganache'),
            ('STRACCIATELLA', 'Stracciatella'),
            ('RICOTTA E FICHI', 'Ricota e Figo'),
            ('CHEESECAKE 2024', 'Cheesecake'),
            ('PANNA MONTATA', 'Pana'),
            ('PISTACCHIO', 'Pistacchio'),
            ('YOGURT', 'Iogurte'),
            ('NOZES RICOTA E MEL', 'Ricota, Noz e Mel'),
            ('ALL NATURAL ACAI SORBETTO', 'Açaí'),
            ('CREMINO', 'Cremino'),
            ('ALL NATURAL FRAMBOESA', 'Framboesa'),
            ('Base Chocolate Nivà', 'Base chocolate niva'),
            ('ALL NATURAL PASSION FRUIT', 'Maracujá'),
            ('CAFFE ILLY', 'Café'),
            ('ALL NATURAL MANGO', 'Manga'),
            ('VANIGLIA AGRIMONTANA', 'Baunilha'),
            ('PISTACCHIO VEGAN', 'Pistacchio V.'),
            ('GANACHE BUNET', 'Bonet'),
            ('ARACHIDE TORINO', 'Amendoim'),
            ('CARAMELLO SALATO NEW', 'Caramelo'),
            ('COCCO LATTE LIOFILIZZATO NEW', 'Coco'),
            ('CARAMELLO DULCE DE LECHE FRANCISCO', 'Doce de leite'),
            ('ALL NATURAL CHOCOLATE SORBETTO', 'Extra noir'),
            ('ALL NATURAL MANDARINO', 'Tangerina'),
            ('GIANDUJA MARCHISIO MODIFICATA PER VARIEGATURA', 'Gianduia'),
            ('CREMA MELA CARAMELLATA', 'Maça Caramelizada'),
            ('CHOCOLATE COM LARANJA', 'Chocolate com laranja'),
            ('NOCCIOLA', 'Avelã'),
            ('ALL NATURAL BERGAMOTTO', 'Bergamota'),
            ('FIORDILATTE', 'Flor de leite'),
            ('EUSKALDUNA', 'Queijo da serra'),
            ('CREMA DI NATALE', 'Creme de natal'),
            ('CREMA', 'Crema'),
        ]
    
        cursor.execute("SELECT COUNT(*) FROM receitas_gelado")
        if cursor.fetchone()[0] == 0:
            for nome_receita, nome_corrente in receitas_mapeamento:
                cursor.execute("INSERT INTO receitas_gelado (nome, nome_corrente) VALUES (%s, %s) ON CONFLICT DO NOTHING", (nome_receita, nome_corrente))
        else:
            for nome_receita, nome_corrente in receitas_mapeamento:
                cursor.execute("INSERT INTO receitas_gelado (nome, nome_corrente) VALUES (%s, %s) ON CONFLICT (nome) DO UPDATE SET nome_corrente = EXCLUDED.nome_corrente WHERE receitas_gelado.nome_corrente IS NULL"
                              , (nome_receita, nome_corrente))
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS rececao_mercadoria (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                loja VARCHAR(100) NOT NULL,
                tipo_produto VARCHAR(100) NOT NULL,
                produto VARCHAR(255),
                sabor VARCHAR(255),
                lote VARCHAR(100),
                quantidade REAL NOT NULL,
                unidade VARCHAR(50) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vendas_detalhe (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                loja VARCHAR(100) NOT NULL,
                produto VARCHAR(255) NOT NULL,
                categoria VARCHAR(100),
                quantidade NUMERIC(12,3) NOT NULL,
                valor_euros REAL,
                peso_vendido_kg NUMERIC(12,4),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_gelado (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                loja VARCHAR(100) NOT NULL,
                sabor VARCHAR(255) NOT NULL,
                quantidade_kg NUMERIC(10,4) NOT NULL,
                tipo VARCHAR(50) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS regras_negocio (
                id SERIAL PRIMARY KEY,
                area VARCHAR(50) NOT NULL,
                palavra_chave VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(area, palavra_chave)
            )
        ''')
    
        cursor.execute("SELECT COUNT(*) FROM regras_negocio")
        if cursor.fetchone()[0] == 0:
            regras_iniciais = [
                ('gelado_kpi', 'Copo'),
                ('gelado_kpi', 'Cone'),
                ('gelado_kpi', 'Caixa'),
                ('gelado_kpi', 'Nivotto'),
                ('gelado_kpi', 'Palito'),
                ('gelado_kpi', 'Biscoito'),
                ('gelado_kpi', 'Brioche'),
                ('gelado_kpi', 'Milk Shake'),
                ('gelado_kpi', 'Batido'),
                ('gelado_kpi', 'Extra Gelado'),
                ('gelado_kpi', 'Sorbet Shake'),
                ('gelado_kpi', 'Afogado'),
                ('gelado_kpi', 'Afogato'),
                ('gelado_kpi', 'Bolo'),
                ('gelado_kpi', 'Açai Bowl'),
                ('gelado_kpi', 'Acai Bowl'),
                ('gelado_kpi', 'Ghiacciolo'),
                ('pastelaria', 'Palito'),
                ('pastelaria', 'Ghiacciolo'),
                ('pastelaria', 'Nivotto'),
                ('pastelaria', 'Bolo'),
                ('pastelaria', 'Biscoito'),
                ('confeitaria', 'Cookie'),
                ('confeitaria', 'Brownie'),
            ]
            for area, palavra in regras_iniciais:
                cursor.execute("INSERT INTO regras_negocio (area, palavra_chave) VALUES (%s, %s) ON CONFLICT DO NOTHING", (area, palavra))
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS produtos_vendas_config (
                id SERIAL PRIMARY KEY,
                produto VARCHAR(255) NOT NULL UNIQUE,
                gelado_kpi BOOLEAN NOT NULL DEFAULT FALSE,
                pastelaria BOOLEAN NOT NULL DEFAULT FALSE,
                confeitaria BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS gramas_gelado (
                id SERIAL PRIMARY KEY,
                artigo VARCHAR(255) NOT NULL UNIQUE,
                gramas REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contagem_stock (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                loja VARCHAR(50) NOT NULL,
                produto VARCHAR(255) NOT NULL,
                quantidade INTEGER NOT NULL,
                tipo VARCHAR(50) NOT NULL,
                origem VARCHAR(30) NOT NULL DEFAULT 'contagem',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute("""
            ALTER TABLE contagem_stock
            ADD COLUMN IF NOT EXISTS origem VARCHAR(30) NOT NULL DEFAULT 'contagem'
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_contagem_pastelaria_intelligence
            ON contagem_stock (data, loja, produto, id)
            WHERE tipo='pastelaria' AND origem='contagem'
        """)

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ajustes_producao (
                id SERIAL PRIMARY KEY,
                ano INTEGER NOT NULL,
                mes INTEGER NOT NULL,
                quantidade_kg REAL NOT NULL,
                descricao VARCHAR(500),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute("SELECT COUNT(*) FROM gramas_gelado")
        if cursor.fetchone()[0] == 0:
            artigos_iniciais = [
                ('Copo Pequeno ( 2 Sabores)', 150),
                ('Copo Medio (3 Sabores)', 180),
                ('Cone Pequeno (2 Sabores)', 150),
                ('Copo Mini', 110),
                ('Cone Medio (3 Sabores)', 180),
                ('Copo Grande (4 Sabores)', 230),
                ('Copo Maxi', 350),
            ]
            for artigo, gramas in artigos_iniciais:
                cursor.execute("INSERT INTO gramas_gelado (artigo, gramas) VALUES (%s, %s) ON CONFLICT DO NOTHING", (artigo, gramas))
    
        cursor.execute("""
            UPDATE stock_gelado SET tipo = 'fim'
            WHERE loja = 'Bolhão' AND tipo = 'inicio'
              AND data IN ('2025-12-21', '2026-01-02', '2026-01-31')
        """)
    
        cursor.execute("""
            UPDATE stock_gelado SET data = '2025-12-31'
            WHERE loja = 'Bolhão' AND data = '2025-12-21'
        """)
    
        # Adicionar coluna local a stock_gelado (locais de armazenamento)
        cursor.execute("ALTER TABLE stock_gelado ADD COLUMN IF NOT EXISTS local VARCHAR(100)")
        cursor.execute("""
            UPDATE stock_gelado SET local = 'Stock Balcão Matosinhos'
            WHERE loja = 'Matosinhos' AND local IS NULL
        """)
        cursor.execute("""
            UPDATE stock_gelado SET local = 'Stock Balcão Bolhão'
            WHERE loja = 'Bolhão' AND local IS NULL
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stock_gelado_loja_tipo_data ON stock_gelado(loja, tipo, data)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stock_gelado_sabor ON stock_gelado(sabor)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_producao_data_loja ON producao(data, loja)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_producao_sabor ON producao(sabor)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_quebras_data_loja ON quebras(data, loja)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_quebras_sabor ON quebras(sabor)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vendas_data_loja ON vendas(data, loja)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rececao_stock_data_loja ON rececao_stock(data, loja)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vendas_detalhe_data_loja ON vendas_detalhe(data, loja)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vendas_detalhe_produto ON vendas_detalhe(produto)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rececao_mercadoria_data_loja ON rececao_mercadoria(data, loja)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_producao_pastelaria_data ON producao_pastelaria(data, loja)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_producao_confeitaria_data ON producao_confeitaria(data, loja)")
    
        cursor.execute("UPDATE stock_gelado SET quantidade_kg = quantidade_kg / 1000.0 WHERE quantidade_kg > 50")
    
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                token VARCHAR(64) PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS plano_producao (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                sabor VARCHAR(255) NOT NULL,
                pesagem_matosinhos REAL DEFAULT 0,
                producao_estimada_bolhao REAL DEFAULT 0,
                producao_estimada_matosinhos REAL DEFAULT 0,
                no_plano BOOLEAN DEFAULT FALSE,
                producao_real_bolhao REAL,
                producao_real_matosinhos REAL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(data, sabor)
            )
        ''')

        # Registar versão do schema
        cursor.execute("DELETE FROM db_schema_version")
        cursor.execute("INSERT INTO db_schema_version (version) VALUES (%s)", (SCHEMA_VERSION,))

        conn.commit()

def run_migrations():
    """Runs idempotent DDL migrations on every startup. Safe to call repeatedly."""
    with db_connection() as conn:
        cursor = conn.cursor()
        # New tables added after initial schema freeze
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS plano_producao (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                sabor VARCHAR(255) NOT NULL,
                pesagem_matosinhos REAL DEFAULT 0,
                producao_estimada_bolhao REAL DEFAULT 0,
                producao_estimada_matosinhos REAL DEFAULT 0,
                no_plano BOOLEAN DEFAULT FALSE,
                producao_real_bolhao REAL,
                producao_real_matosinhos REAL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(data, sabor)
            )
        ''')
        # New columns added after initial schema freeze
        cursor.execute("ALTER TABLE stock_gelado ADD COLUMN IF NOT EXISTS local VARCHAR(100)")
        cursor.execute("ALTER TABLE plano_producao ADD COLUMN IF NOT EXISTS no_plano BOOLEAN DEFAULT FALSE")
        cursor.execute("ALTER TABLE plano_producao ADD COLUMN IF NOT EXISTS producao_real_bolhao REAL")
        cursor.execute("ALTER TABLE plano_producao ADD COLUMN IF NOT EXISTS producao_real_matosinhos REAL")
        cursor.execute("ALTER TABLE plano_producao ADD COLUMN IF NOT EXISTS producao_estimada_outros REAL DEFAULT 0")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ordem_producao (
                id SERIAL PRIMARY KEY,
                sabor VARCHAR(255) NOT NULL UNIQUE,
                posicao INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ordens_transferencia (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                area_origem VARCHAR(100) NOT NULL,
                produto VARCHAR(255) NOT NULL,
                sabor VARCHAR(255),
                quantidade REAL NOT NULL,
                unidade VARCHAR(50) NOT NULL DEFAULT 'kg',
                loja_destino VARCHAR(100) NOT NULL DEFAULT 'Bolhão',
                status VARCHAR(50) NOT NULL DEFAULT 'pendente',
                criado_por VARCHAR(100),
                confirmado_por VARCHAR(100),
                confirmado_em TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                destino_tipo VARCHAR(20) NOT NULL DEFAULT 'loja',
                destino_nome VARCHAR(255)
            )
        ''')
        cursor.execute("""
            ALTER TABLE contagem_stock
            ADD COLUMN IF NOT EXISTS origem VARCHAR(30) NOT NULL DEFAULT 'contagem'
        """)
        cursor.execute("""
            WITH exact_candidates AS (
                SELECT cs.id AS count_id, ot.id AS order_id,
                       COUNT(*) OVER (PARTITION BY ot.id) AS counts_per_order,
                       COUNT(*) OVER (PARTITION BY cs.id) AS orders_per_count
                FROM contagem_stock cs
                JOIN ordens_transferencia ot
                  ON ot.status='confirmada'
                 AND LOWER(ot.area_origem)=cs.tipo
                 AND ot.loja_destino=cs.loja
                 AND ot.produto=cs.produto
                 AND ot.quantidade::integer=cs.quantidade
                 AND ot.confirmado_em IS NOT NULL
                 AND cs.data=ot.confirmado_em::date
                 AND cs.created_at=ot.confirmado_em
                WHERE cs.origem='contagem'
                  AND cs.tipo IN ('pastelaria', 'confeitaria')
            )
            UPDATE contagem_stock cs
            SET origem='transferencia'
            FROM exact_candidates candidate
            WHERE cs.id=candidate.count_id
              AND candidate.counts_per_order=1
              AND candidate.orders_per_count=1
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_transferencias_pastelaria_intelligence
            ON ordens_transferencia
                (confirmado_em, loja_destino, produto)
            WHERE status='confirmada'
              AND LOWER(area_origem)='pastelaria'
        """)

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS artigos_administrativos (
                id SERIAL PRIMARY KEY,
                fornecedor VARCHAR(255) NOT NULL,
                produto VARCHAR(255) NOT NULL,
                ativo BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(fornecedor, produto)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_producao (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                sabor VARCHAR(255) NOT NULL,
                loja VARCHAR(100) NOT NULL,
                quantidade_kg NUMERIC(10,4) NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(data, sabor, loja)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transferencias (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                sabor VARCHAR(255) NOT NULL,
                loja_destino VARCHAR(100) NOT NULL,
                quantidade_kg NUMERIC(10,4) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS plano_producao_pastelaria (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                produto VARCHAR(255) NOT NULL,
                producao_estimada INTEGER DEFAULT 0,
                producao_real INTEGER,
                no_plano BOOLEAN DEFAULT FALSE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status VARCHAR(20) DEFAULT 'pendente',
                nota TEXT DEFAULT '',
                producao_estimada_bolhao INTEGER DEFAULT 0,
                producao_estimada_matosinhos INTEGER DEFAULT 0,
                produto_pastelaria_id INTEGER
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS plano_producao_confeitaria (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                produto VARCHAR(255) NOT NULL,
                producao_estimada INTEGER DEFAULT 0,
                producao_real INTEGER,
                no_plano BOOLEAN DEFAULT FALSE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(data, produto)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_producao_pastelaria (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                produto VARCHAR(255) NOT NULL,
                quantidade INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                produto_pastelaria_id INTEGER
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_producao_confeitaria (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                produto VARCHAR(255) NOT NULL,
                quantidade INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(data, produto)
            )
        ''')

        cursor.execute("ALTER TABLE ordens_transferencia ADD COLUMN IF NOT EXISTS data_prevista DATE")

        cursor.execute("SELECT COUNT(*) FROM ordem_producao")
        if cursor.fetchone()[0] == 0:
            sabores_iniciais = [
                "Coco", "Stracciatella", "Baunilha", "Caramelo", "Doce de leite",
                "Ganache", "Amendoim", "Cremino", "Pistacchio", "Manga",
                "Maracujá", "Framboesa", "Açaí", "Extra noir", "Iogurte",
                "Pistacchio V.", "Ricota, Noz e Mel", "Cheesecake", "Café",
                "Bonet", "Chocolate Branco",
            ]
            for i, s in enumerate(sabores_iniciais):
                cursor.execute(
                    "INSERT INTO ordem_producao (sabor, posicao) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (s, i)
                )

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stores (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL UNIQUE,
                address TEXT,
                latitude REAL,
                longitude REAL,
                store_type VARCHAR(100) DEFAULT 'loja',
                is_active BOOLEAN DEFAULT TRUE,
                receives_transfers BOOLEAN DEFAULT FALSE,
                requires_eod_weighing BOOLEAN DEFAULT FALSE,
                pos_store_code VARCHAR(100),
                opened_at DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute("SELECT COUNT(*) FROM stores")
        if cursor.fetchone()[0] == 0:
            lojas_iniciais = [
                ('Matosinhos', None, None, None, 'producao', True, False, False, None, None),
                ('Bolhão', None, None, None, 'loja', True, True, True, None, None),
            ]
            for name, address, lat, lng, stype, is_active, recv_tr, req_eod, pos_code, opened_at in lojas_iniciais:
                cursor.execute(
                    """INSERT INTO stores (name, address, latitude, longitude, store_type, is_active,
                       receives_transfers, requires_eod_weighing, pos_store_code, opened_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                    (name, address, lat, lng, stype, is_active, recv_tr, req_eod, pos_code, opened_at)
                )

        # store_id FK columns on operational tables (nullable for backwards compat)
        for tbl in ('producao', 'vendas', 'quebras', 'stock_inicial', 'rececao_stock',
                    'producao_pastelaria', 'producao_confeitaria', 'transferencias', 'stock_gelado',
                    'rececao_mercadoria', 'vendas_detalhe', 'contagem_stock', 'stock_producao'):
            cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS store_id INTEGER REFERENCES stores(id)")

        # shows_on_landing flag — controls whether a store tile appears on the landing page
        cursor.execute("ALTER TABLE stores ADD COLUMN IF NOT EXISTS shows_on_landing BOOLEAN DEFAULT FALSE")

        # supports_vendas flag — store participates in the vendas module (quebras + fecho de caixa)
        # independent of store_type so a 'producao' store (Matosinhos) can also have vendas access
        cursor.execute("ALTER TABLE stores ADD COLUMN IF NOT EXISTS supports_vendas BOOLEAN DEFAULT FALSE")

        # Idempotent backfill: map existing loja text to store_id where the name matches
        for tbl in ('producao', 'vendas', 'quebras', 'stock_inicial', 'rececao_stock',
                    'producao_pastelaria', 'producao_confeitaria', 'stock_gelado',
                    'rececao_mercadoria', 'vendas_detalhe', 'contagem_stock', 'stock_producao'):
            cursor.execute(f"""
                UPDATE {tbl} t
                SET store_id = s.id
                FROM stores s
                WHERE LOWER(t.loja) = LOWER(s.name) AND t.store_id IS NULL
            """)
        # transferencias uses loja_destino instead of loja
        cursor.execute("""
            UPDATE transferencias t
            SET store_id = s.id
            FROM stores s
            WHERE LOWER(t.loja_destino) = LOWER(s.name) AND t.store_id IS NULL
        """)

        # Correct EOD weighing config: Bolhão does EOD (fim de dia), Matosinhos does not.
        # shows_on_landing: Matosinhos and Bolhão have dedicated module tiles, no landing tile needed.
        cursor.execute("UPDATE stores SET requires_eod_weighing = TRUE,  shows_on_landing = FALSE WHERE name ILIKE '%Bolhão%'")
        cursor.execute("UPDATE stores SET requires_eod_weighing = FALSE, shows_on_landing = FALSE WHERE name ILIKE '%Matosinhos%'")

        # Both Bolhão and Matosinhos participate in the vendas module (quebras + fecho de caixa).
        # Use ILIKE with wildcards to match regardless of prefix (e.g. "Niva Bolhão", "Niva Matosinhos").
        cursor.execute("UPDATE stores SET supports_vendas = TRUE WHERE name ILIKE '%Bolhão%' OR name ILIKE '%Matosinhos%'")

        # loja_id FK on users — associates a vendas user with a specific store (deprecated, kept for rollback)
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS loja_id INTEGER REFERENCES stores(id)")
        # Backfill: associate existing 'vendas'-like users with Bolhão
        cursor.execute("""
            UPDATE users SET loja_id = (SELECT id FROM stores WHERE name ILIKE '%Bolhão%' LIMIT 1)
            WHERE acesso_vendas = TRUE AND loja_id IS NULL
        """)

        # Many-to-many vendas store permissions
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_store_vendas (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
                PRIMARY KEY (user_id, store_id)
            )
        ''')
        # Migrate existing single-store vendas permissions into junction table
        cursor.execute("""
            INSERT INTO user_store_vendas (user_id, store_id)
            SELECT id, loja_id FROM users
            WHERE acesso_vendas = TRUE AND loja_id IS NOT NULL
            ON CONFLICT DO NOTHING
        """)

        # Mouzinho columns for plano_producao
        cursor.execute("ALTER TABLE plano_producao ADD COLUMN IF NOT EXISTS producao_estimada_mouzinho REAL DEFAULT 0")
        cursor.execute("ALTER TABLE plano_producao ADD COLUMN IF NOT EXISTS producao_real_mouzinho REAL")

        # Eventos/B2B real production for plano_producao
        cursor.execute("ALTER TABLE plano_producao ADD COLUMN IF NOT EXISTS producao_real_outros REAL")

        # credit_contracts — Fase 3 M1
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS credit_contracts (
                id SERIAL PRIMARY KEY,
                tipo VARCHAR(50) NOT NULL,
                label VARCHAR(255) NOT NULL,
                banco VARCHAR(255),
                loja_associada VARCHAR(100),
                capital_inicial NUMERIC(15,2),
                saldo_divida NUMERIC(15,2),
                tan NUMERIC(8,4),
                prestacao_mensal NUMERIC(15,2),
                dia_debito INTEGER,
                data_inicio DATE,
                data_fim DATE,
                plafond NUMERIC(15,2),
                estado VARCHAR(50) NOT NULL DEFAULT 'ativo',
                notas TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # cost_categories is created by run_migrations_centros_custo(), which
        # runs after this legacy migration in the application bootstrap. On a
        # brand-new database the referenced table is not available yet; the
        # centros de custo migration adds this column once its dependency
        # exists.
        cursor.execute(
            "SELECT to_regclass('public.cost_categories')"
        )
        if cursor.fetchone()[0] is not None:
            cursor.execute(
                "ALTER TABLE credit_contracts ADD COLUMN IF NOT EXISTS "
                "categoria_custo_id INTEGER REFERENCES cost_categories(id)"
            )

        # ── Eventos / CRM ──────────────────────────────────────────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS lead_requests (
                id SERIAL PRIMARY KEY,
                submitted_at TIMESTAMP,
                source VARCHAR(50) DEFAULT 'manual',
                google_sheet_row_id VARCHAR(100),
                event_type VARCHAR(100),
                event_date DATE,
                event_time VARCHAR(20),
                estimated_guests_raw VARCHAR(50),
                estimated_guests INTEGER,
                venue VARCHAR(255),
                venue_address TEXT,
                client_name VARCHAR(255),
                client_email VARCHAR(255),
                client_phone VARCHAR(50),
                marketing_consent BOOLEAN DEFAULT FALSE,
                referral_source VARCHAR(100),
                notes TEXT,
                internal_notes TEXT,
                status VARCHAR(50) DEFAULT 'lead',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(google_sheet_row_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id SERIAL PRIMARY KEY,
                lead_id INTEGER REFERENCES lead_requests(id) ON DELETE SET NULL,
                event_name VARCHAR(255),
                event_type VARCHAR(100),
                event_date DATE,
                event_time VARCHAR(20),
                estimated_guests INTEGER,
                venue VARCHAR(255),
                venue_address TEXT,
                client_name VARCHAR(255),
                client_email VARCHAR(255),
                client_phone VARCHAR(50),
                status VARCHAR(50) DEFAULT 'lead',
                loss_reason TEXT,
                internal_notes TEXT,
                invoice_amount_eur NUMERIC(10,2),
                expected_payment_date DATE,
                payment_status VARCHAR(50) DEFAULT 'pending',
                payment_received_at DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bank_balance_entries (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                loja VARCHAR(100),
                saldo NUMERIC(15,2) NOT NULL,
                contrato_id INTEGER REFERENCES credit_contracts(id) ON DELETE SET NULL,
                utilizacao_calculada NUMERIC(15,2),
                custo_juros_estimado NUMERIC(15,2),
                notas TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS contract_payment_revisions (
                id SERIAL PRIMARY KEY,
                contract_id INTEGER NOT NULL REFERENCES credit_contracts(id) ON DELETE CASCADE,
                data_inicio DATE NOT NULL,
                prestacao NUMERIC(15,2) NOT NULL,
                tan NUMERIC(8,4),
                euribor NUMERIC(8,4),
                spread NUMERIC(8,4),
                notas TEXT,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_cpr_contract_data ON contract_payment_revisions(contract_id, data_inicio DESC)'
        )

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS artigos_evento (
                id SERIAL PRIMARY KEY,
                codigo VARCHAR(50) NOT NULL UNIQUE,
                nome VARCHAR(255) NOT NULL,
                unidade VARCHAR(50) DEFAULT 'un',
                preco_base NUMERIC(10,2) DEFAULT 0,
                cost_tier VARCHAR(20) DEFAULT 'medium',
                ativo BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_credit_contracts_estado ON credit_contracts(estado)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bank_balance_entries_data ON bank_balance_entries(data)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bank_balance_entries_contrato ON bank_balance_entries(contrato_id)")
        cursor.execute("ALTER TABLE artigos_evento ADD COLUMN IF NOT EXISTS cost_tier VARCHAR(20) DEFAULT 'medium'")
        cursor.execute("UPDATE artigos_evento SET cost_tier='high' WHERE codigo IN ('gelado_kg','gelado_sabor') AND cost_tier='medium'")
        cursor.execute("UPDATE artigos_evento SET cost_tier='low' WHERE codigo IN ('horas_servico','deslocacao_km','copos','colheres') AND cost_tier='medium'")

        cursor.execute("SELECT COUNT(*) FROM artigos_evento")
        if cursor.fetchone()[0] == 0:
            artigos_padrao = [
                ('gelado_kg',    'Gelado (kg)',          'kg',  0, 'high'),
                ('gelado_sabor', 'Gelado por sabor',     'un',  0, 'high'),
                ('pastelaria',   'Pastelaria',           'un',  0, 'medium'),
                ('confeitaria',  'Confeitaria',          'un',  0, 'medium'),
                ('horas_servico','Horas de serviço',     'h',   0, 'low'),
                ('deslocacao_km','Deslocação (km)',      'km',  0, 'low'),
                ('copos',        'Copos',                'un',  0, 'low'),
                ('colheres',     'Colheres',             'un',  0, 'low'),
                ('outro',        'Outro',                'un',  0, 'medium'),
            ]
            for codigo, nome, unidade, preco, tier in artigos_padrao:
                cursor.execute(
                    "INSERT INTO artigos_evento (codigo, nome, unidade, preco_base, cost_tier) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (codigo, nome, unidade, preco, tier)
                )

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS quote_items (
                id SERIAL PRIMARY KEY,
                event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                artigo_codigo VARCHAR(50),
                descricao VARCHAR(255) NOT NULL,
                quantidade NUMERIC(10,3) NOT NULL DEFAULT 1,
                preco_unitario NUMERIC(10,2) NOT NULL DEFAULT 0,
                total NUMERIC(10,2) GENERATED ALWAYS AS (quantidade * preco_unitario) STORED,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS loss_reason TEXT")
        cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS payment_received_at DATE")
        cursor.execute("ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS loss_reason TEXT")
        cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS event_end_time VARCHAR(20)")
        cursor.execute("ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS event_end_time VARCHAR(20)")
        cursor.execute("ALTER TABLE quote_items ADD COLUMN IF NOT EXISTS taxa_iva NUMERIC(5,4)")
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS event_clients (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                email VARCHAR(255),
                phone VARCHAR(50),
                marketing_consent BOOLEAN DEFAULT FALSE,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS client_id INTEGER REFERENCES event_clients(id) ON DELETE SET NULL")
        cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS google_sheet_row_id VARCHAR(100)")
        cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS source VARCHAR(50) DEFAULT 'manual'")
        # Keep the additive lead conversion self-contained: older installations
        # may not yet have the customer metadata columns when this migration runs.
        cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS customer_type VARCHAR(20)")
        cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS company_name VARCHAR(255)")
        cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS nif VARCHAR(9)")
        cursor.execute("ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS customer_type VARCHAR(20)")
        cursor.execute("ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS company_name VARCHAR(255)")
        cursor.execute("ALTER TABLE lead_requests ADD COLUMN IF NOT EXISTS nif VARCHAR(9)")
        # Imported leads and events share the immutable Sheet row identity.
        # Backfill only unlinked rows; do not overwrite an operational link.
        cursor.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS lead_id INTEGER REFERENCES lead_requests(id) ON DELETE SET NULL")
        cursor.execute("""
            UPDATE events e
               SET lead_id = l.id
              FROM lead_requests l
             WHERE e.lead_id IS NULL
               AND e.google_sheet_row_id IS NOT NULL
               AND l.google_sheet_row_id = e.google_sheet_row_id
        """)
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS events_google_sheet_row_id_idx
            ON events(google_sheet_row_id) WHERE google_sheet_row_id IS NOT NULL
        """)
        # ───────────────────────────────────────────────────────────────────────────

        # Fecho de Caixa — daily POS reconciliation
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS fecho_caixa (
                id SERIAL PRIMARY KEY,
                data DATE NOT NULL,
                loja_id INTEGER NOT NULL REFERENCES stores(id),
                total_moedas NUMERIC(10,2),
                valor_notas NUMERIC(10,2),
                dinheiro_pos NUMERIC(10,2),
                cartao_pos NUMERIC(10,2),
                tpa_getnet NUMERIC(10,2),
                desvio_numerario NUMERIC(10,2) GENERATED ALWAYS AS (
                    CASE WHEN dinheiro_pos IS NOT NULL AND total_moedas IS NOT NULL AND valor_notas IS NOT NULL
                    THEN dinheiro_pos - (total_moedas + valor_notas) ELSE NULL END
                ) STORED,
                desvio_tpa NUMERIC(10,2) GENERATED ALWAYS AS (
                    CASE WHEN cartao_pos IS NOT NULL AND tpa_getnet IS NOT NULL
                    THEN cartao_pos - tpa_getnet ELSE NULL END
                ) STORED,
                justificacao_desvio TEXT,
                imagem_caixa_path TEXT,
                ocr_raw JSONB,
                ocr_confianca NUMERIC(5,3),
                registado_por VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(data, loja_id)
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fecho_caixa_data ON fecho_caixa(data DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fecho_caixa_data_loja ON fecho_caixa(data, loja_id)")

        # Add columns introduced after initial fecho_caixa table creation
        cursor.execute("ALTER TABLE fecho_caixa ADD COLUMN IF NOT EXISTS colaborador VARCHAR(100)")
        cursor.execute("ALTER TABLE fecho_caixa ADD COLUMN IF NOT EXISTS moedas_json JSONB")
        cursor.execute("ALTER TABLE fecho_caixa ADD COLUMN IF NOT EXISTS total_caixa NUMERIC(10,2)")
        cursor.execute("ALTER TABLE fecho_caixa ADD COLUMN IF NOT EXISTS envelope_sobra NUMERIC(10,2)")
        cursor.execute("ALTER TABLE fecho_caixa ADD COLUMN IF NOT EXISTS total_vendas_pos NUMERIC(10,2)")
        cursor.execute("ALTER TABLE fecho_caixa ADD COLUMN IF NOT EXISTS ubereats_pos NUMERIC(10,2)")
        cursor.execute("ALTER TABLE fecho_caixa ADD COLUMN IF NOT EXISTS loja VARCHAR(100)")
        # Backfill loja name from stores join
        cursor.execute("""
            UPDATE fecho_caixa fc SET loja = s.name
            FROM stores s WHERE fc.loja_id = s.id AND fc.loja IS NULL
        """)

        # ── Materiais — stock de materiais / consumíveis ───────────────────────
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS materiais (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(255) NOT NULL,
                unidade VARCHAR(20) NOT NULL DEFAULT 'un',
                categoria VARCHAR(100) NOT NULL DEFAULT 'Outro',
                fornecedor VARCHAR(255),
                ativo BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(nome)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_materiais (
                id SERIAL PRIMARY KEY,
                material_id INTEGER NOT NULL REFERENCES materiais(id) ON DELETE CASCADE,
                local VARCHAR(50) NOT NULL,
                quantidade NUMERIC(12,3) NOT NULL DEFAULT 0,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(material_id, local)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS movimentos_stock_materiais (
                id SERIAL PRIMARY KEY,
                material_id INTEGER NOT NULL REFERENCES materiais(id) ON DELETE CASCADE,
                local VARCHAR(50) NOT NULL,
                tipo VARCHAR(20) NOT NULL,
                quantidade NUMERIC(12,3) NOT NULL,
                data DATE NOT NULL DEFAULT CURRENT_DATE,
                invoice_id INTEGER,
                notas TEXT,
                utilizador VARCHAR(100),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_movimentos_stock_material_id ON movimentos_stock_materiais(material_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_movimentos_stock_local ON movimentos_stock_materiais(local)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_movimentos_stock_data ON movimentos_stock_materiais(data)")

        cursor.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.table_constraints
                    WHERE constraint_name = 'chk_stock_materiais_local'
                      AND table_name = 'stock_materiais'
                ) THEN
                    ALTER TABLE stock_materiais
                        ADD CONSTRAINT chk_stock_materiais_local
                        CHECK (local IN ('Bolhão', 'Matosinhos', 'Garagem'));
                END IF;
            END $$;
        """)

        cursor.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.table_constraints
                    WHERE constraint_name = 'chk_movimentos_stock_local'
                      AND table_name = 'movimentos_stock_materiais'
                ) THEN
                    ALTER TABLE movimentos_stock_materiais
                        ADD CONSTRAINT chk_movimentos_stock_local
                        CHECK (local IN ('Bolhão', 'Matosinhos', 'Garagem'));
                END IF;
            END $$;
        """)

        conn.commit()


def run_faturas_migrations():
    """Idempotent migrations for the faturas module."""
    with db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS suppliers (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                common_name VARCHAR(120),
                nif VARCHAR(20) NOT NULL UNIQUE,
                store_id INTEGER REFERENCES stores(id),
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invoices (
                id SERIAL PRIMARY KEY,
                supplier_id INTEGER REFERENCES suppliers(id),
                supplier_name VARCHAR(255),
                supplier_nif VARCHAR(20),
                invoice_number VARCHAR(100),
                amount_eur NUMERIC(12,2),
                vat_amount_eur NUMERIC(12,2),
                issue_date DATE,
                due_date DATE,
                category VARCHAR(100),
                onedrive_subfolder VARCHAR(255),
                onedrive_path TEXT,
                pdf_filename VARCHAR(500),
                pdf_data BYTEA,
                status VARCHAR(50) NOT NULL DEFAULT 'pending_review',
                ocr_confidence REAL,
                ocr_raw JSONB,
                created_by VARCHAR(100),
                cfo_confirmed_date DATE,
                paid_date DATE,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS supplier_id INTEGER REFERENCES suppliers(id)")
        cursor.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS store_id")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS cfo_confirmed_date DATE")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS paid_date DATE")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS ocr_raw JSONB")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS pdf_data BYTEA")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS onedrive_subfolder VARCHAR(255)")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS onedrive_path TEXT")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS pdf_filename VARCHAR(500)")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS onedrive_web_url TEXT")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS categoria VARCHAR(100)")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS document_type VARCHAR(20) DEFAULT 'fatura'")
        cursor.execute("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS notes TEXT")
        cursor.execute("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS common_name VARCHAR(120)")
        cursor.execute("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        cursor.execute("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS payment_method VARCHAR(50)")
        cursor.execute("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS payment_terms VARCHAR(50)")
        cursor.execute("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS iban VARCHAR(50)")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS source VARCHAR(50) DEFAULT 'email_upload'")
        # Widen document_type column to accommodate longer type keys (e.g. nota_pagamento_imposto = 22 chars)
        cursor.execute("ALTER TABLE invoices ALTER COLUMN document_type TYPE VARCHAR(30)")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS payment_method VARCHAR(50)")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_config (
                key VARCHAR(100) PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS eventos (
                id SERIAL PRIMARY KEY,
                cliente VARCHAR(255) NOT NULL,
                descricao TEXT,
                event_date DATE NOT NULL,
                local VARCHAR(255),
                status VARCHAR(50) NOT NULL DEFAULT 'proposta',
                invoice_amount_eur NUMERIC(15,2),
                expected_payment_date DATE,
                payment_date DATE,
                payment_status VARCHAR(50) NOT NULL DEFAULT 'pending',
                payment_amount_eur NUMERIC(15,2),
                production_alert_sent BOOLEAN DEFAULT FALSE,
                notas TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS evento_items (
                id SERIAL PRIMARY KEY,
                evento_id INTEGER NOT NULL REFERENCES eventos(id) ON DELETE CASCADE,
                descricao VARCHAR(255) NOT NULL,
                quantidade REAL NOT NULL DEFAULT 1,
                is_production_item BOOLEAN DEFAULT FALSE,
                unidade VARCHAR(50) DEFAULT 'un',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_eventos_status ON eventos(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_eventos_event_date ON eventos(event_date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_eventos_payment_status ON eventos(payment_status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_evento_items_evento ON evento_items(evento_id)")

        # --- Fase 6: Meteorologia e Histórico de Vendas ---
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS weather_data (
                id SERIAL PRIMARY KEY,
                store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                fonte VARCHAR(50) NOT NULL,
                data DATE NOT NULL,
                temperatura_max REAL,
                temperatura_min REAL,
                precipitacao_mm REAL,
                vento_kmh REAL,
                uv_index REAL,
                condicao VARCHAR(255),
                score REAL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(store_id, fonte, data)
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_weather_data_store_data ON weather_data(store_id, data)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_weather_data_fonte ON weather_data(fonte)")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sales_historico (
                id SERIAL PRIMARY KEY,
                store_id INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                pos_store_code VARCHAR(100),
                data DATE NOT NULL,
                loja VARCHAR(100) NOT NULL,
                produto VARCHAR(255) NOT NULL,
                categoria VARCHAR(100),
                quantidade INTEGER NOT NULL,
                valor_euros REAL,
                ano_referencia INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sales_hist_store_data ON sales_historico(store_id, data)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sales_hist_ano ON sales_historico(ano_referencia)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sales_hist_pos_code ON sales_historico(pos_store_code)")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS store_aliases (
                id SERIAL PRIMARY KEY,
                store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
                alias_name VARCHAR(255),
                alias_code VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_store_aliases_store ON store_aliases(store_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_store_aliases_code ON store_aliases(alias_code)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_store_aliases_name ON store_aliases(alias_name)")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sales_historico_import_log (
                id SERIAL PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                filename VARCHAR(500),
                ano_detetado INTEGER,
                ano_formulario INTEGER,
                registos_importados INTEGER NOT NULL DEFAULT 0,
                linhas_ignoradas INTEGER NOT NULL DEFAULT 0,
                lojas_importadas JSONB DEFAULT '[]',
                erros_loja JSONB DEFAULT '[]',
                notas TEXT
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sales_imp_log_created ON sales_historico_import_log(created_at DESC)")

        # Confirming bancário: payment_method + confirming_contract_id on invoice_payments
        cursor.execute("ALTER TABLE invoice_payments ADD COLUMN IF NOT EXISTS payment_method VARCHAR(50)")
        cursor.execute("ALTER TABLE invoice_payments ADD COLUMN IF NOT EXISTS confirming_contract_id INTEGER REFERENCES credit_contracts(id) ON DELETE SET NULL")

        # payment_methods_config — configurable payment method options
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payment_methods_config (
                id SERIAL PRIMARY KEY,
                metodo VARCHAR(50) NOT NULL UNIQUE,
                label VARCHAR(100) NOT NULL,
                ativo BOOLEAN NOT NULL DEFAULT TRUE,
                taxa_percentagem NUMERIC(6,4),
                prazo_dias INTEGER,
                notas TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute("""
            INSERT INTO payment_methods_config (metodo, label, ativo) VALUES
                ('transferencia', 'Transferência Bancária', TRUE),
                ('debito_direto', 'Débito Direto', TRUE),
                ('confirming', 'Confirming', TRUE),
                ('numerario', 'Numerário', TRUE),
                ('cheque', 'Cheque', TRUE)
            ON CONFLICT (metodo) DO NOTHING
        """)

        # invoice_audit_log — full history of status changes per invoice
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invoice_audit_log (
                id SERIAL PRIMARY KEY,
                invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
                campo_alterado VARCHAR(100) NOT NULL,
                valor_anterior TEXT,
                valor_novo TEXT,
                alterado_por VARCHAR(100),
                alterado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_invoice_audit_log_invoice ON invoice_audit_log(invoice_id, alterado_em DESC)")

        # Wire FK from movimentos_stock_materiais -> invoices now that invoices exists
        cursor.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.table_constraints
                    WHERE constraint_name = 'fk_movimentos_stock_invoice'
                      AND table_name = 'movimentos_stock_materiais'
                ) THEN
                    ALTER TABLE movimentos_stock_materiais
                        ADD CONSTRAINT fk_movimentos_stock_invoice
                        FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE SET NULL;
                END IF;
            END $$;
        """)

        # Task #66: invoice line items linked to materiais
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invoice_linhas (
                id SERIAL PRIMARY KEY,
                invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
                material_id INTEGER REFERENCES materiais(id) ON DELETE SET NULL,
                descricao VARCHAR(500) NOT NULL,
                quantidade NUMERIC(12,3) NOT NULL,
                unidade VARCHAR(20) NOT NULL DEFAULT 'un',
                preco_unitario NUMERIC(12,4),
                stock_registado BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_invoice_linhas_invoice ON invoice_linhas(invoice_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_invoice_linhas_material ON invoice_linhas(material_id)")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS stock_registado_at TIMESTAMP")
        cursor.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS stock_registado_por VARCHAR(100)")

        conn.commit()


def run_migrations_credito():
    """Idempotent migrations for the credit/credito domain.
    Uses an advisory lock to serialise concurrent worker executions."""
    with db_connection() as conn:
        cursor = conn.cursor()
        acquired = False
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202605)")
            acquired = cursor.fetchone()[0]
            if not acquired:
                return
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS contract_payment_revisions (
                    id SERIAL PRIMARY KEY,
                    contract_id INTEGER NOT NULL REFERENCES credit_contracts(id) ON DELETE CASCADE,
                    data_inicio DATE NOT NULL,
                    prestacao NUMERIC(15,2) NOT NULL,
                    tan NUMERIC(8,4),
                    euribor NUMERIC(8,4),
                    spread NUMERIC(8,4),
                    notas TEXT,
                    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_cpr_contract_data ON contract_payment_revisions(contract_id, data_inicio DESC)'
            )

            # confirming_parcelas — parcelas de confirming bancário ligadas a faturas
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS confirming_parcelas (
                    id SERIAL PRIMARY KEY,
                    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
                    confirming_contract_id INTEGER NOT NULL REFERENCES credit_contracts(id) ON DELETE CASCADE,
                    montante NUMERIC(12,2) NOT NULL,
                    data_pagamento DATE NOT NULL,
                    estado VARCHAR(50) NOT NULL DEFAULT 'scheduled',
                    notas TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_confirming_parcelas_invoice ON confirming_parcelas(invoice_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_confirming_parcelas_contract ON confirming_parcelas(confirming_contract_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_confirming_parcelas_estado ON confirming_parcelas(estado)")

            conn.commit()
        finally:
            if acquired:
                try:
                    cursor.execute("SELECT pg_advisory_unlock(202605)")
                    conn.commit()
                except Exception:
                    pass


def run_migrations_centros_custo():
    """Idempotent migrations: cost_centers, cost_categories, colaboradores,
    colaborador_centro_custo tables + FK columns on invoices.
    Advisory lock 202606."""
    with db_connection() as conn:
        cursor = conn.cursor()
        acquired = False
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202606)")
            acquired = cursor.fetchone()[0]
            if not acquired:
                return

            # ── Centros de custo ───────────────────────────────────────────────
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cost_centers (
                    id SERIAL PRIMARY KEY,
                    code VARCHAR(10) NOT NULL UNIQUE,
                    name VARCHAR(100) NOT NULL,
                    description TEXT,
                    ativo BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # ── Categorias de custo (hierárquicas) ─────────────────────────────
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cost_categories (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(150) NOT NULL,
                    parent_id INTEGER REFERENCES cost_categories(id),
                    ativo BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute(
                "ALTER TABLE credit_contracts ADD COLUMN IF NOT EXISTS "
                "categoria_custo_id INTEGER REFERENCES cost_categories(id)"
            )
            # Add unique index for (name, COALESCE(parent_id::text,'')) to make seed idempotent.
            # IMPORTANT: we use a text cast (parent_id::text) rather than COALESCE(parent_id,-1).
            # The integer sentinel caused the Replit deployment platform to apply int4_ops to ALL
            # index columns when generating production migration SQL from pg_dump output — including
            # the varchar `name` column, which PostgreSQL correctly rejects. Casting to text means
            # pg_dump shows COALESCE((parent_id)::text, ''::text) with no integer expressions,
            # so the platform generates valid SQL for both columns. ON CONFLICT clauses must
            # reference the same expression: COALESCE(parent_id::text, '').
            cursor.execute('DROP INDEX IF EXISTS uq_cost_categories_name_parent')
            cursor.execute('''
                CREATE UNIQUE INDEX uq_cost_categories_name_parent
                ON cost_categories (name, COALESCE(parent_id::text, ''))
            ''')

            # ── Colaboradores ──────────────────────────────────────────────────
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS colaboradores (
                    id SERIAL PRIMARY KEY,
                    nome VARCHAR(150) NOT NULL,
                    salario_bruto NUMERIC(12,2) NOT NULL DEFAULT 0,
                    premio_bruto NUMERIC(12,2) NOT NULL DEFAULT 0,
                    irs_taxa NUMERIC(6,4) NOT NULL DEFAULT 0,
                    data_inicio DATE,
                    ativo BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # ── Alocação de colaboradores a centros de custo ───────────────────
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS colaborador_centro_custo (
                    id SERIAL PRIMARY KEY,
                    colaborador_id INTEGER NOT NULL REFERENCES colaboradores(id) ON DELETE CASCADE,
                    centro_custo_id INTEGER NOT NULL REFERENCES cost_centers(id) ON DELETE CASCADE,
                    percentagem NUMERIC(6,3) NOT NULL DEFAULT 100.0,
                    UNIQUE(colaborador_id, centro_custo_id)
                )
            ''')
            cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_colab_cc_colab ON colaborador_centro_custo(colaborador_id)'
            )

            # ── FK columns on invoices ─────────────────────────────────────────
            cursor.execute(
                'ALTER TABLE invoices ADD COLUMN IF NOT EXISTS '
                'centro_custo_id INTEGER REFERENCES cost_centers(id)'
            )
            cursor.execute(
                'ALTER TABLE invoices ADD COLUMN IF NOT EXISTS '
                'categoria_custo_id INTEGER REFERENCES cost_categories(id)'
            )

            # ── Correct old wrong cost center names (from previous incorrect seed) ──
            cursor.execute("UPDATE cost_centers SET name='Produção',            description='Centro de produção de gelados e pastelaria' WHERE code='P'")
            cursor.execute("UPDATE cost_centers SET name='Geral',               description='Custos gerais não alocados a loja ou área específica' WHERE code='G'")
            cursor.execute("UPDATE cost_centers SET name='Distribuição',        description='Entregas, transporte e logística' WHERE code='D'")
            cursor.execute("UPDATE cost_centers SET name='Eventos',             description='Eventos e B2B' WHERE code='E'")
            cursor.execute("UPDATE cost_centers SET name='Faturas Partilhadas', description='Faturas com custo partilhado entre centros' WHERE code='FP'")

            # ── Seed: centros de custo ─────────────────────────────────────────
            CENTROS = [
                ('P',  'Produção',            'Centro de produção de gelados e pastelaria'),
                ('M',  'Matosinhos',          'Loja de Matosinhos'),
                ('B',  'Bolhão',              'Loja do Bolhão'),
                ('G',  'Geral',               'Custos gerais não alocados a loja ou área específica'),
                ('D',  'Distribuição',        'Entregas, transporte e logística'),
                ('E',  'Eventos',             'Eventos e B2B'),
                ('FP', 'Faturas Partilhadas', 'Faturas com custo partilhado entre centros'),
            ]
            _valid_codes = [c[0] for c in CENTROS]
            for code, name, desc in CENTROS:
                cursor.execute('''
                    INSERT INTO cost_centers (code, name, description)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (code) DO NOTHING
                ''', (code, name, desc))
            # Deactivate any cost center whose code is not in the canonical set
            cursor.execute(
                'UPDATE cost_centers SET ativo=FALSE WHERE code != ALL(%s)',
                (_valid_codes,)
            )
            # Ensure all canonical cost centers are active
            cursor.execute(
                'UPDATE cost_centers SET ativo=TRUE WHERE code = ANY(%s)',
                (_valid_codes,)
            )

            # ── Correct old wrong top-level category names ─────────────────────
            cursor.execute(
                "UPDATE cost_categories SET name='Impostos' WHERE name='Impostos e Encargos' AND parent_id IS NULL"
            )

            # ── Seed: categorias de custo (correct tree) ───────────────────────
            CATEGORIAS_TOP = [
                ('Custos Fixos Operacionais', [
                    'Rendas', 'Telecomunicações', 'Royalties', 'Contabilidade', 'Advogados',
                    'Marketing', 'Música', 'Sistemas', 'Segurança', 'HACCP', 'SST',
                    'Ecrãs', 'Controlo de Pragas', 'Créditos', 'Leasings', 'Seguros',
                ]),
                ('Custos Variáveis Operacionais', [
                    'Água', 'Energia', 'Limpezas', 'Consumíveis', 'Transportes', 'Economato',
                ]),
                ('Compras', [
                    'Matéria Prima',
                ]),
                ('Impostos', [
                    'IVA', 'DMR', 'Retenção IRS', 'IRC e Imposto de Selo',
                    'TSU', 'Pagamento por Conta', 'IES',
                ]),
            ]
            _valid_top_names = [t[0] for t in CATEGORIAS_TOP]

            # Deactivate all top-level categories not in the canonical set (and their children)
            cursor.execute(
                '''UPDATE cost_categories SET ativo=FALSE
                   WHERE parent_id IS NULL AND name != ALL(%s)''',
                (_valid_top_names,)
            )
            cursor.execute(
                '''UPDATE cost_categories SET ativo=FALSE
                   WHERE parent_id IN (
                       SELECT id FROM cost_categories
                       WHERE parent_id IS NULL AND name != ALL(%s)
                   )''',
                (_valid_top_names,)
            )
            # Ensure canonical top-level categories are active
            cursor.execute(
                'UPDATE cost_categories SET ativo=TRUE WHERE parent_id IS NULL AND name = ANY(%s)',
                (_valid_top_names,)
            )

            for top_name, sub_names in CATEGORIAS_TOP:
                cursor.execute('''
                    INSERT INTO cost_categories (name, parent_id)
                    VALUES (%s, NULL)
                    ON CONFLICT (name, COALESCE(parent_id::text, '')) DO NOTHING
                ''', (top_name,))
                # Always fetch id (insert may have been skipped due to conflict)
                cursor.execute(
                    'SELECT id FROM cost_categories WHERE name = %s AND parent_id IS NULL',
                    (top_name,)
                )
                row = cursor.fetchone()
                if row:
                    parent_id = row[0]
                    # Deactivate subcategories under this parent not in the canonical child set
                    cursor.execute(
                        'UPDATE cost_categories SET ativo=FALSE WHERE parent_id=%s AND name != ALL(%s)',
                        (parent_id, sub_names if sub_names else [''])
                    )
                    for sub in sub_names:
                        cursor.execute('''
                            INSERT INTO cost_categories (name, parent_id)
                            VALUES (%s, %s)
                            ON CONFLICT (name, COALESCE(parent_id::text, '')) DO NOTHING
                        ''', (sub, parent_id))
                        # Ensure canonical subcategories are active
                        cursor.execute(
                            'UPDATE cost_categories SET ativo=TRUE WHERE name=%s AND parent_id=%s',
                            (sub, parent_id)
                        )

            conn.commit()

            # ── Startup migration: JSON→DB for colaboradores ───────────────────
            # If colaboradores table is empty but JSON config exists, migrate it now.
            try:
                cursor.execute('SELECT COUNT(*) FROM colaboradores')
                colab_count = cursor.fetchone()[0]
                if colab_count == 0:
                    # Try to load from cashflow_config JSON
                    cursor.execute(
                        "SELECT value FROM cashflow_config WHERE key = 'salarios_colaboradores'"
                    )
                    row = cursor.fetchone()
                    if row and row[0] and row[0].strip() not in ('', '[]'):
                        conn.commit()  # commit schema changes before calling helper
                        from db.centros_custo import migrate_colaboradores_from_json
                        n = migrate_colaboradores_from_json(row[0])
                        if n:
                            import logging as _logging
                            _logging.getLogger(__name__).info(
                                'run_migrations_centros_custo: migrated %d colaboradores from JSON', n
                            )
            except Exception as _exc:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    'run_migrations_centros_custo: JSON->DB colaboradores migration skipped: %s', _exc
                )

            conn.commit()
        finally:
            if acquired:
                try:
                    cursor.execute("SELECT pg_advisory_unlock(202606)")
                    conn.commit()
                except Exception:
                    pass


def run_data_fix_quebras_march2026():
    """
    One-time idempotent cleanup of bad quebras records created in March 2026.

    Two records have absurd values (3196.9 kg and 2197.814 kg) caused by
    estimated production being entered in grams instead of kg.  Four more
    records are exact duplicates created within seconds by double-click.

    Each candidate is validated against its expected (data, loja, sabor, motivo)
    fingerprint before deletion so this is safe in any environment — records that
    exist but do not match the expected fingerprint are silently skipped.

    Safe to call multiple times — exits silently when records are already gone.
    """
    EXPECTED = {
        21: ('2026-03-04', 'Bolhão',     'Baunilha',  'Quebra de produção (estimado vs real)'),
        22: ('2026-03-03', 'Bolhão',     'Cheesecake','Quebra de produção (estimado vs real)'),
        48: ('2026-03-09', 'Matosinhos', 'Framboesa', 'Quebra de produção (estimado vs real)'),
        49: ('2026-03-09', 'Matosinhos', 'Maracujá',  'Quebra de produção (estimado vs real)'),
        52: ('2026-03-11', 'Matosinhos', 'Amendoim',  'Quebra de produção (estimado vs real)'),
        69: ('2026-03-13', 'Matosinhos', 'Ganache',   'Quebra de produção (estimado vs real)'),
    }
    bad_ids = list(EXPECTED.keys())
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT id, data, loja, sabor, motivo FROM quebras WHERE id = ANY(%s)",
                (bad_ids,)
            )
            rows = cursor.fetchall()
            if not rows:
                return

            confirmed_ids = []
            for row in rows:
                rid, rdata, rloja, rsabor, rmotivo = row[0], str(row[1]), row[2], row[3], row[4]
                exp_data, exp_loja, exp_sabor, exp_motivo = EXPECTED[rid]
                if rdata == exp_data and rloja == exp_loja and rsabor == exp_sabor and rmotivo == exp_motivo:
                    confirmed_ids.append(rid)
                else:
                    logger.warning(
                        "run_data_fix_quebras_march2026: ID %d fingerprint mismatch — skipping "
                        "(got data=%s loja=%s sabor=%s, expected data=%s loja=%s sabor=%s)",
                        rid, rdata, rloja, rsabor, exp_data, exp_loja, exp_sabor,
                    )

            if not confirmed_ids:
                return

            cursor.execute("DELETE FROM quebras WHERE id = ANY(%s)", (confirmed_ids,))
            deleted = cursor.rowcount
            conn.commit()
            logger.info(
                "run_data_fix_quebras_march2026: deleted %d bad quebras record(s) with IDs %s",
                deleted, confirmed_ids,
            )
        except Exception as exc:
            logger.error("run_data_fix_quebras_march2026 failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
def run_data_fix_delete_auto_quebras():
    """
    One-time idempotent deletion of all auto-generated quebras created during
    gelado production registration (motivo = 'Quebra de produção (estimado vs real)').

    These rows recorded plan-vs-actual differences, not real stock losses.
    The auto-creation logic has been removed from executar_plano_dia; this
    function purges the historical records created before that change.

    Becomes a no-op immediately after the first run (no matching rows remain).
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "DELETE FROM quebras WHERE motivo = 'Quebra de produção (estimado vs real)'"
            )
            deleted = cursor.rowcount
            conn.commit()
            logger.info(
                "run_data_fix_delete_auto_quebras: deleted %d auto-generated quebra(s)",
                deleted,
            )
        except Exception as exc:
            logger.error("run_data_fix_delete_auto_quebras failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
def run_data_fix_pesagem_april2026():
    """
    One-time idempotent correction of 7 plano_producao rows where
    pesagem_matosinhos was entered in grams instead of kg (April 2026).

    Each row is validated against its (data, sabor) fingerprint before
    any UPDATE, and the guard condition (>= 50) makes this a pure no-op
    once the values have been corrected.

    Confirmed production rows (queried 2026-04-03):
      id=25  2026-03-02 Pistacchio      5556 g → 5.556 kg
      id=31  2026-03-02 Extra noir      5210 g → 5.210 kg
      id=602 2026-03-23 Extra noir       214 g → 0.214 kg
      id=823 2026-03-29 Framboesa        460 g → 0.460 kg
      id=835 2026-03-29 Noz Pecan e Maple  410 g → 0.410 kg
      id=838 2026-03-30 Coco             496 g → 0.496 kg
      id=866 2026-03-30 Iogurte          208 g → 0.208 kg
    """
    EXPECTED = {
        # IDs 25 and 31 were removed: rows were altered after fix was written
        # (now hold different sabor/data) and their pesagem is already corrected.
        602: ('2026-03-23', 'Extra noir'),
        823: ('2026-03-29', 'Framboesa'),
        835: ('2026-03-29', 'Noz Pecan e Maple'),
        838: ('2026-03-30', 'Coco'),
        866: ('2026-03-30', 'Iogurte'),
    }
    bad_ids = list(EXPECTED.keys())
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT id, data, sabor, pesagem_matosinhos"
                " FROM plano_producao WHERE id = ANY(%s)",
                (bad_ids,)
            )
            rows = cursor.fetchall()
            if not rows:
                return

            confirmed_ids = []
            for row in rows:
                rid, rdata, rsabor, rpesagem = row[0], str(row[1]), row[2].strip(), float(row[3])
                exp_data, exp_sabor = EXPECTED[rid]
                if rdata == exp_data and rsabor == exp_sabor and rpesagem >= 50:
                    confirmed_ids.append(rid)
                elif rdata != exp_data or rsabor != exp_sabor:
                    logger.warning(
                        "run_data_fix_pesagem_april2026: ID %d fingerprint mismatch"
                        " — got data=%s sabor=%r, expected data=%s sabor=%r — skipping",
                        rid, rdata, rsabor, exp_data, exp_sabor,
                    )

            if not confirmed_ids:
                return

            cursor.execute(
                "UPDATE plano_producao"
                " SET pesagem_matosinhos = pesagem_matosinhos / 1000.0"
                " WHERE id = ANY(%s) AND pesagem_matosinhos >= 50",
                (confirmed_ids,)
            )
            updated = cursor.rowcount
            conn.commit()
            logger.info(
                "run_data_fix_pesagem_april2026: corrected %d row(s) with IDs %s"
                " (divided pesagem_matosinhos by 1000)",
                updated, confirmed_ids,
            )

            cursor.execute(
                "SELECT id, data, sabor, pesagem_matosinhos"
                " FROM plano_producao WHERE id = ANY(%s)"
                " ORDER BY id",
                (confirmed_ids,)
            )
            for vrow in cursor.fetchall():
                logger.info(
                    "run_data_fix_pesagem_april2026: verification id=%d data=%s sabor=%s"
                    " pesagem_matosinhos=%.4f kg",
                    vrow[0], vrow[1], vrow[2], float(vrow[3]),
                )
        except Exception as exc:
            logger.error("run_data_fix_pesagem_april2026 failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
def run_data_fix_march1_dedup():
    """
    One-time idempotent fix: remove duplicate rows from vendas_detalhe for
    2026-03-01.  On that date every product was imported twice for both lojas,
    adding ~3 130 € to the all-products total and ~2 826 € to the gelado_kpi
    indicator.  We keep the row with the lowest id for each (data, loja,
    produto) combination and delete the rest.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202608)")
            if not cursor.fetchone()[0]:
                logger.info("run_data_fix_march1_dedup: lock held by another worker, skipping")
                return

            cursor.execute("""
                SELECT COUNT(*) FROM (
                    SELECT data, loja, produto
                    FROM vendas_detalhe
                    WHERE data = '2026-03-01'
                    GROUP BY data, loja, produto
                    HAVING COUNT(*) > 1
                ) dup
            """)
            pending = cursor.fetchone()[0]
            if pending == 0:
                logger.info("run_data_fix_march1_dedup: no duplicates found for 2026-03-01, skipping")
                return

            cursor.execute("""
                DELETE FROM vendas_detalhe
                WHERE data = '2026-03-01'
                  AND id NOT IN (
                      SELECT MIN(id)
                      FROM vendas_detalhe
                      WHERE data = '2026-03-01'
                      GROUP BY loja, produto
                  )
            """)
            deleted = cursor.rowcount
            conn.commit()
            logger.info(
                "run_data_fix_march1_dedup: removed %d duplicate rows for 2026-03-01",
                deleted,
            )
        except Exception as exc:
            logger.error("run_data_fix_march1_dedup failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
def run_data_fix_gelado_kpi_classification():
    """
    One-time idempotent fix: set gelado_kpi = TRUE for cone and brioche
    products that were inserted into produtos_vendas_config with an incorrect
    FALSE classification before the keyword rules were applied consistently.

    Confirmed products that contain gelado-related keywords but are currently
    classified FALSE (they are genuine gelado-container/serving products and
    must count towards the euro/kg KPI):
      - Cone(s)
      - Cones (Box 3) Uber
      - Cones (Box 3) Bolt
      - Mini Cone
      - Mini Cones (Box 10) Uber
      - Mini Cones (Box 10) Bolt
      - Cone Simples (avulso)
      - Brioche simples

    Deliberately excluded from this fix (not gelado containers):
      - Taxa Caixa Takeaway / Uber / Bolt / Glovo (fee lines)
      - Caixa Panna 0,5L Bolt (cream product)
      - Gelado Avelã / Baunilha / Chocolate (scoop add-ons for coffee — to be
        reviewed separately with the user before changing)
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202609)")
            if not cursor.fetchone()[0]:
                logger.info("run_data_fix_gelado_kpi_classification: lock held by another worker, skipping")
                return

            produtos_to_fix = [
                'Cone(s)',
                'Cones (Box 3) Uber',
                'Cones (Box 3) Bolt',
                'Mini Cone',
                'Mini Cones (Box 10) Uber',
                'Mini Cones (Box 10) Bolt',
                'Cone Simples (avulso)',
                'Brioche simples',
            ]

            cursor.execute("""
                SELECT COUNT(*) FROM produtos_vendas_config
                WHERE produto = ANY(%s) AND gelado_kpi = FALSE
                  AND dose_config_pendente = FALSE
                  AND EXISTS (
                      SELECT 1 FROM produto_regra_dose_historico prd
                      WHERE prd.produto_vendas_config_id=
                            produtos_vendas_config.id
                        AND CURRENT_DATE BETWEEN prd.valid_from
                            AND COALESCE(prd.valid_to, 'infinity'::date)
                  )
            """, (produtos_to_fix,))
            pending = cursor.fetchone()[0]
            if pending == 0:
                logger.info("run_data_fix_gelado_kpi_classification: all products already correctly classified, skipping")
                return

            cursor.execute("""
                UPDATE produtos_vendas_config
                SET gelado_kpi = TRUE
                WHERE produto = ANY(%s) AND gelado_kpi = FALSE
                  AND dose_config_pendente = FALSE
                  AND EXISTS (
                      SELECT 1 FROM produto_regra_dose_historico prd
                      WHERE prd.produto_vendas_config_id=
                            produtos_vendas_config.id
                        AND CURRENT_DATE BETWEEN prd.valid_from
                            AND COALESCE(prd.valid_to, 'infinity'::date)
                  )
            """, (produtos_to_fix,))
            updated = cursor.rowcount
            conn.commit()
            logger.info(
                "run_data_fix_gelado_kpi_classification: updated %d products to gelado_kpi=TRUE",
                updated,
            )
        except Exception as exc:
            logger.error("run_data_fix_gelado_kpi_classification failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
def run_data_fix_normalise_sabor_names():
    """One-time idempotent fix: rename non-canonical sabor spellings in stock_gelado
    and stock_producao to their canonical forms.

    Covers ALL-CAPS OCR output from the Matosinhos production sheet,
    abbreviated forms ("Choc. Branco"), and alternative spellings
    ("Flor de leite" → "Fior di Latte", "Ricotta" → "Ricota", etc.).

    Advisory lock 202622.  The fix is a pure no-op once all rows are canonical.
    """
    RENAMES = [
        # canonical_name, [list of non-canonical lower(sabor) variants]
        ("Açaí",               ["açai", "acai"]),
        ("Café",               ["cafe"]),
        ("Chocolate Branco",   ["chocolate branco", "choc. branco", "choc branco"]),
        ("Fior di Latte",      ["flor de leite", "flor di latte", "fior de leite",
                                 "fior di latte"]),
        ("Maracujá",           ["maracuja"]),
        ("Noz Pecan e Maple",  ["noz pecan e maple"]),
        ("Pistacchio V.",      ["pistacchio vegan", "pistacchio v.",
                                 "pistachio v.", "pistachio vegan"]),
        ("Pistacchio",         ["pistacchio", "pistachio"]),
        ("Ricota, Noz e Mel",  ["ricota, noz e mel", "ricotta, noz e mel"]),
    ]
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202622)")
            if not cursor.fetchone()[0]:
                logger.info("run_data_fix_normalise_sabor_names: lock held by another worker, skipping")
                return

            total = 0
            for canonical, variants in RENAMES:
                for table in ("stock_gelado", "stock_producao"):
                    cursor.execute(
                        f"SELECT COUNT(*) FROM {table}"
                        " WHERE lower(sabor) = ANY(%s) AND sabor != %s",
                        (variants, canonical),
                    )
                    pending = cursor.fetchone()[0]
                    if pending == 0:
                        continue

                    # stock_producao has a UNIQUE constraint on (data, sabor, loja).
                    # Delete variant rows that would conflict with an existing
                    # canonical row for the same (data, loja) before renaming.
                    if table == "stock_producao":
                        cursor.execute(
                            """
                            DELETE FROM stock_producao
                            WHERE lower(sabor) = ANY(%s) AND sabor != %s
                              AND (data, loja) IN (
                                  SELECT data, loja FROM stock_producao
                                  WHERE sabor = %s
                              )
                            """,
                            (variants, canonical, canonical),
                        )
                        deleted = cursor.rowcount
                        if deleted:
                            logger.info(
                                "run_data_fix_normalise_sabor_names: stock_producao"
                                " — removed %d conflicting variant row(s) for %r",
                                deleted, canonical,
                            )

                    cursor.execute(
                        f"UPDATE {table} SET sabor = %s"
                        " WHERE lower(sabor) = ANY(%s) AND sabor != %s",
                        (canonical, variants, canonical),
                    )
                    updated = cursor.rowcount
                    total += updated
                    if updated:
                        logger.info(
                            "run_data_fix_normalise_sabor_names: %s.sabor → %r: %d row(s) updated",
                            table, canonical, updated,
                        )

            conn.commit()
            logger.info("run_data_fix_normalise_sabor_names: done — %d row(s) updated total", total)
        except Exception as exc:
            logger.error("run_data_fix_normalise_sabor_names failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
def run_data_fix_cremino_stock_producao():
    """One-time idempotent fix: correct the Cremino/Matosinhos stock_producao entry
    that was entered in grams instead of kg (≈47,412 g should be ≈47.4 kg).

    The guard condition (quantidade_kg >= 50) makes this a pure no-op once the
    value has been corrected.

    Advisory lock 202623.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202623)")
            if not cursor.fetchone()[0]:
                logger.info("run_data_fix_cremino_stock_producao: lock held by another worker, skipping")
                return

            cursor.execute(
                "SELECT COUNT(*) FROM stock_producao"
                " WHERE sabor = 'Cremino' AND loja = 'Matosinhos' AND quantidade_kg >= 50"
            )
            pending = cursor.fetchone()[0]
            if pending == 0:
                logger.info("run_data_fix_cremino_stock_producao: no bad rows found, skipping")
                return

            cursor.execute(
                "UPDATE stock_producao"
                " SET quantidade_kg = ROUND((quantidade_kg / 1000.0)::numeric, 3)"
                " WHERE sabor = 'Cremino' AND loja = 'Matosinhos' AND quantidade_kg >= 50"
            )
            updated = cursor.rowcount
            conn.commit()
            logger.info(
                "run_data_fix_cremino_stock_producao: corrected %d row(s)"
                " (divided Cremino/Matosinhos quantidade_kg by 1000)",
                updated,
            )
        except Exception as exc:
            logger.error("run_data_fix_cremino_stock_producao failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
def run_migrations_caixa_loja():
    """Add caixa_loja column to produtos_vendas_config and classify in-store boxes.

    Advisory lock 202610. In-store boxes (Caixa Gelado Pequena/Media/Grande/Mini)
    are always sold at exactly €/kg, so they are excluded from the euro/kg KPI.
    Platform boxes (Uber / Bolt / Glovo) have a fixed price per order and MUST
    remain in the KPI to track whether collaborators are portioning correctly.

    Classification rule: caixa_loja = TRUE when product name contains
    'Caixa Gelado' AND does NOT contain 'Uber', 'Bolt', or 'Glovo'.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202610)")
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_caixa_loja: lock held by another worker, skipping")
                return

            cursor.execute("""
                ALTER TABLE produtos_vendas_config
                ADD COLUMN IF NOT EXISTS caixa_loja BOOLEAN NOT NULL DEFAULT FALSE
            """)

            cursor.execute("""
                UPDATE produtos_vendas_config
                SET caixa_loja = TRUE
                WHERE produto ILIKE '%Caixa Gelado%'
                  AND produto NOT ILIKE '%Uber%'
                  AND produto NOT ILIKE '%Bolt%'
                  AND produto NOT ILIKE '%Glovo%'
                  AND caixa_loja = FALSE
            """)
            updated = cursor.rowcount
            conn.commit()
            logger.info("run_migrations_caixa_loja: %d in-store box products marked caixa_loja=TRUE", updated)
        except Exception as exc:
            logger.error("run_migrations_caixa_loja failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
def run_migrations_preco_caixa_kg():
    """Create config_preco_caixa_kg table for historical in-store box price per kg.

    Advisory lock 202611. Stores the €/kg price of in-store gelado boxes over
    time. Used by the euro/kg KPI to estimate kg consumed in boxes per period.
    Initial record: 2024-01-01 at €30.00/kg.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202611)")
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_preco_caixa_kg: lock held by another worker, skipping")
                return

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS config_preco_caixa_kg (
                    id SERIAL PRIMARY KEY,
                    data_inicio DATE NOT NULL UNIQUE,
                    preco_kg NUMERIC(5,2) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                INSERT INTO config_preco_caixa_kg (data_inicio, preco_kg)
                VALUES ('2024-01-01', 30.00)
                ON CONFLICT (data_inicio) DO NOTHING
            """)
            conn.commit()
            logger.info("run_migrations_preco_caixa_kg: table ready")
        except Exception as exc:
            logger.error("run_migrations_preco_caixa_kg failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
def run_data_fix_pesagem_matosinhos_backfill():
    """
    Idempotent backfill: mirror all plano_producao rows where
    pesagem_matosinhos > 0 into stock_gelado (loja='Matosinhos', tipo='inicio').

    Runs after stock uniqueness is installed. The INSERT targets the active-row
    partial unique index and is safe against concurrent writers while preserving
    inactive historical rows.

    Sabor names are normalised with normalise_sabor before insert so that
    case variants ('Chocolate branco' vs 'Chocolate Branco') are collapsed to
    the canonical form.

    Advisory lock 202607.
    """
    from sabor_utils import normalise_sabor
    from psycopg2.extras import execute_values
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202607)")
            if not cursor.fetchone()[0]:
                logger.info("run_data_fix_pesagem_matosinhos_backfill: lock held by another worker, skipping")
                return

            cursor.execute("""
                SELECT s.id AS store_id
                FROM stores s
                WHERE s.name = 'Matosinhos'
                LIMIT 1
            """)
            row = cursor.fetchone()
            store_id = row[0] if row else None

            cursor.execute("""
                SELECT data, sabor, pesagem_matosinhos
                FROM plano_producao
                WHERE pesagem_matosinhos > 0
            """)
            plano_rows = cursor.fetchall()

            if not plano_rows:
                logger.info("run_data_fix_pesagem_matosinhos_backfill: nothing to backfill, skipping")
                conn.commit()
                return

            rows = [
                (data, 'Matosinhos', normalise_sabor(sabor_raw), kg, 'inicio', store_id)
                for data, sabor_raw, kg in plano_rows
            ]
            execute_values(
                cursor,
                """INSERT INTO stock_gelado (data, loja, sabor, quantidade_kg, tipo, store_id)
                   VALUES %s
                   ON CONFLICT (data, store_id, sabor, tipo)
                       WHERE is_active = TRUE AND store_id IS NOT NULL
                   DO NOTHING""",
                rows,
            )
            inserted = cursor.rowcount
            conn.commit()
            if inserted > 0:
                logger.info(
                    "run_data_fix_pesagem_matosinhos_backfill: inserted %d rows into stock_gelado",
                    inserted,
                )
            else:
                logger.info("run_data_fix_pesagem_matosinhos_backfill: all rows already present, nothing inserted")
        except Exception as exc:
            logger.error("run_data_fix_pesagem_matosinhos_backfill failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
def run_migrations_transferencias_motivo():
    """Idempotent migration: adds motivo_rejeicao column to ordens_transferencia.
    Advisory lock 202616."""
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202616)")
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_transferencias_motivo: lock held by another worker, skipping")
                return
            cursor.execute(
                "ALTER TABLE ordens_transferencia ADD COLUMN IF NOT EXISTS motivo_rejeicao TEXT"
            )
            conn.commit()
            logger.info("run_migrations_transferencias_motivo: motivo_rejeicao column added/verified")
        except Exception as exc:
            logger.error("run_migrations_transferencias_motivo failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(202616)")
                conn.commit()
            except Exception:
                pass


def run_migrations_transferencias_eventos():
    """Idempotent migration: creates transferencias_eventos audit log table.
    Advisory lock 202617."""
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202617)")
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_transferencias_eventos: lock held by another worker, skipping")
                return
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS transferencias_eventos (
                    id          SERIAL PRIMARY KEY,
                    ordem_id    INTEGER NOT NULL REFERENCES ordens_transferencia(id) ON DELETE CASCADE,
                    event_type  VARCHAR(30) NOT NULL CHECK (
                        event_type IN (
                            'criado', 'confirmado', 'rejeitado', 'executado',
                            'aceite', 'problema_reportado'
                        )
                    ),
                    utilizador  VARCHAR(100),
                    motivo      TEXT,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_transf_eventos_ordem ON transferencias_eventos(ordem_id)"
            )
            conn.commit()
            logger.info("run_migrations_transferencias_eventos: table ready")
        except Exception as exc:
            logger.error("run_migrations_transferencias_eventos failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(202617)")
                conn.commit()
            except Exception:
                pass


def run_migrations_transferencias_aceitacao_opcional():
    """Complete dispatched transfers immediately and separate store acknowledgement."""
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_advisory_lock(202645)")

            cursor.execute("""
                ALTER TABLE ordens_transferencia
                    ADD COLUMN IF NOT EXISTS rececao_estado VARCHAR(30),
                    ADD COLUMN IF NOT EXISTS aceite_por VARCHAR(100),
                    ADD COLUMN IF NOT EXISTS aceite_em TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS problema_por VARCHAR(100),
                    ADD COLUMN IF NOT EXISTS problema_em TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS motivo_problema TEXT
            """)
            cursor.execute("""
                ALTER TABLE rececao_mercadoria
                    ADD COLUMN IF NOT EXISTS ordem_transferencia_id INTEGER
                        REFERENCES ordens_transferencia(id) ON DELETE SET NULL
            """)
            cursor.execute("""
                ALTER TABLE contagem_stock
                    ADD COLUMN IF NOT EXISTS ordem_transferencia_id INTEGER
                        REFERENCES ordens_transferencia(id) ON DELETE SET NULL
            """)
            cursor.execute("""
                ALTER TABLE transferencias_eventos
                    DROP CONSTRAINT IF EXISTS transferencias_eventos_event_type_check
            """)
            cursor.execute("""
                ALTER TABLE transferencias_eventos
                    ADD CONSTRAINT transferencias_eventos_event_type_check
                    CHECK (event_type IN (
                        'criado', 'confirmado', 'rejeitado', 'executado',
                        'aceite', 'problema_reportado'
                    ))
            """)

            # Link legacy confirmed receipts only when the match is unambiguous.
            cursor.execute("""
                WITH candidates AS (
                    SELECT rm.id AS receipt_id, o.id AS order_id,
                           COUNT(*) OVER (PARTITION BY rm.id) AS orders_per_receipt,
                           COUNT(*) OVER (PARTITION BY o.id) AS receipts_per_order
                    FROM rececao_mercadoria rm
                    JOIN ordens_transferencia o
                      ON o.status='confirmada'
                     AND o.destino_tipo='loja'
                     AND o.area_origem='Gelado'
                     AND o.loja_destino=rm.loja
                     AND COALESCE(o.sabor, o.produto)=rm.sabor
                     AND o.quantidade=rm.quantidade
                     AND o.confirmado_em=rm.created_at
                    WHERE rm.ordem_transferencia_id IS NULL
                      AND rm.tipo_produto='gelado' AND COALESCE(rm.lote, '')=''
                )
                UPDATE rececao_mercadoria rm
                SET ordem_transferencia_id=c.order_id
                FROM candidates c
                WHERE rm.id=c.receipt_id
                  AND c.orders_per_receipt=1 AND c.receipts_per_order=1
            """)
            cursor.execute("""
                WITH candidates AS (
                    SELECT cs.id AS receipt_id, o.id AS order_id,
                           COUNT(*) OVER (PARTITION BY cs.id) AS orders_per_receipt,
                           COUNT(*) OVER (PARTITION BY o.id) AS receipts_per_order
                    FROM contagem_stock cs
                    JOIN ordens_transferencia o
                      ON o.status='confirmada'
                     AND o.destino_tipo='loja'
                     AND LOWER(o.area_origem)=cs.tipo
                     AND o.loja_destino=cs.loja
                     AND o.produto=cs.produto
                     AND o.quantidade::integer=cs.quantidade
                     AND o.confirmado_em=cs.created_at
                    WHERE cs.ordem_transferencia_id IS NULL
                      AND cs.origem='transferencia'
                )
                UPDATE contagem_stock cs
                SET ordem_transferencia_id=c.order_id
                FROM candidates c
                WHERE cs.id=c.receipt_id
                  AND c.orders_per_receipt=1 AND c.receipts_per_order=1
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_rececao_transfer_order
                ON rececao_mercadoria(ordem_transferencia_id)
                WHERE ordem_transferencia_id IS NOT NULL
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_contagem_transfer_order
                ON contagem_stock(ordem_transferencia_id)
                WHERE ordem_transferencia_id IS NOT NULL
            """)

            # Existing confirmations were store acceptances in the old flow.
            cursor.execute("""
                UPDATE ordens_transferencia
                SET rececao_estado = CASE
                        WHEN destino_tipo='b2b' THEN 'nao_aplicavel'
                        WHEN status='confirmada' THEN 'aceite'
                        WHEN status='rejeitada' THEN 'problema'
                        ELSE 'por_verificar'
                    END,
                    aceite_por = CASE WHEN status='confirmada'
                                      THEN confirmado_por ELSE aceite_por END,
                    aceite_em = CASE WHEN status='confirmada'
                                     THEN confirmado_em ELSE aceite_em END,
                    problema_por = CASE WHEN status='rejeitada'
                                        THEN confirmado_por ELSE problema_por END,
                    problema_em = CASE WHEN status='rejeitada'
                                       THEN confirmado_em ELSE problema_em END,
                    motivo_problema = CASE WHEN status='rejeitada'
                                           THEN motivo_rejeicao ELSE motivo_problema END
                WHERE rececao_estado IS NULL
            """)
            cursor.execute("""
                UPDATE ordens_transferencia
                SET loja_origem='Bolhão'
                WHERE status='pendente' AND destino_tipo='b2b'
                  AND area_origem='Gelado' AND loja_origem IS NULL
            """)

            # Materialize every legacy pending internal receipt before completion.
            cursor.execute("""
                INSERT INTO rececao_mercadoria (
                    data, loja, tipo_produto, produto, sabor, lote, quantidade,
                    unidade, ordem_transferencia_id, created_at
                )
                SELECT o.data, o.loja_destino, 'gelado', o.produto,
                       COALESCE(o.sabor, o.produto), '', o.quantidade, 'kg',
                       o.id, COALESCE(o.created_at, NOW())
                FROM ordens_transferencia o
                WHERE o.status='pendente' AND o.destino_tipo='loja'
                  AND o.area_origem='Gelado'
                ON CONFLICT (ordem_transferencia_id)
                    WHERE ordem_transferencia_id IS NOT NULL DO NOTHING
            """)
            cursor.execute("""
                INSERT INTO contagem_stock (
                    data, loja, produto, quantidade, tipo, origem,
                    produto_pastelaria_id, ordem_transferencia_id, created_at
                )
                SELECT o.data, o.loja_destino, o.produto, o.quantidade::integer,
                       LOWER(o.area_origem), 'transferencia',
                       CASE WHEN o.area_origem='Pastelaria'
                            THEN o.produto_pastelaria_id ELSE NULL END,
                       o.id, COALESCE(o.created_at, NOW())
                FROM ordens_transferencia o
                WHERE o.status='pendente' AND o.destino_tipo='loja'
                  AND o.area_origem IN ('Pastelaria', 'Confeitaria')
                ON CONFLICT (ordem_transferencia_id)
                    WHERE ordem_transferencia_id IS NOT NULL DO NOTHING
            """)
            cursor.execute("""
                INSERT INTO transferencias_eventos (
                    ordem_id, event_type, utilizador, created_at
                )
                SELECT o.id, 'executado', o.criado_por,
                       COALESCE(o.created_at, NOW())
                FROM ordens_transferencia o
                WHERE o.status='pendente'
                  AND NOT EXISTS (
                      SELECT 1 FROM transferencias_eventos e
                      WHERE e.ordem_id=o.id AND e.event_type='executado'
                  )
            """)
            cursor.execute("""
                UPDATE ordens_transferencia
                SET status='confirmada',
                    confirmado_por=COALESCE(confirmado_por, criado_por),
                    confirmado_em=COALESCE(confirmado_em, created_at, NOW())
                WHERE status='pendente'
            """)
            cursor.execute("""
                ALTER TABLE ordens_transferencia
                    ALTER COLUMN rececao_estado SET DEFAULT 'por_verificar',
                    ALTER COLUMN rececao_estado SET NOT NULL
            """)
            conn.commit()
            logger.info(
                "run_migrations_transferencias_aceitacao_opcional: ready"
            )
        except Exception as exc:
            logger.error(
                "run_migrations_transferencias_aceitacao_opcional failed: %s",
                exc,
            )
            conn.rollback()
            raise
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(202645)")
                conn.commit()
            except Exception:
                pass


def run_backfill_transferencias_eventos():
    """Backfill synthetic events for existing ordens_transferencia rows that have none.

    Inserts:
      - 'criado'     from created_at / criado_por
      - 'confirmado' from confirmado_em / confirmado_por  (if status=confirmada)
      - 'rejeitado'  from confirmado_em / confirmado_por  (if status=rejeitada), with motivo
    Already-backfilled orders (any event exists) are skipped via LEFT JOIN.
    Advisory lock 202618 ensures only one worker runs the backfill.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202618)")
            if not cursor.fetchone()[0]:
                logger.info("run_backfill_transferencias_eventos: lock held by another worker, skipping")
                return

            # Guard: table must exist before backfill (migration may not have committed yet)
            cursor.execute("""
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'transferencias_eventos'
            """)
            if not cursor.fetchone():
                logger.info("run_backfill_transferencias_eventos: table not yet created, skipping")
                return
            # Insert 'criado' events for orders with no existing events
            cursor.execute("""
                INSERT INTO transferencias_eventos (ordem_id, event_type, utilizador, motivo, created_at)
                SELECT o.id, 'criado', o.criado_por, NULL,
                       COALESCE(o.created_at, o.data::timestamptz)
                FROM ordens_transferencia o
                LEFT JOIN transferencias_eventos e ON e.ordem_id = o.id
                WHERE e.id IS NULL
            """)
            n_criado = cursor.rowcount

            # Insert 'confirmado' events for confirmed orders with no 'confirmado' event
            cursor.execute("""
                INSERT INTO transferencias_eventos (ordem_id, event_type, utilizador, motivo, created_at)
                SELECT o.id, 'confirmado', o.confirmado_por, NULL,
                       COALESCE(o.confirmado_em, o.created_at, o.data::timestamptz)
                FROM ordens_transferencia o
                LEFT JOIN transferencias_eventos e
                       ON e.ordem_id = o.id AND e.event_type = 'confirmado'
                WHERE o.status = 'confirmada' AND e.id IS NULL
            """)
            n_conf = cursor.rowcount

            # Insert 'rejeitado' events for rejected orders with no 'rejeitado' event
            cursor.execute("""
                INSERT INTO transferencias_eventos (ordem_id, event_type, utilizador, motivo, created_at)
                SELECT o.id, 'rejeitado', o.confirmado_por, o.motivo_rejeicao,
                       COALESCE(o.confirmado_em, o.created_at, o.data::timestamptz)
                FROM ordens_transferencia o
                LEFT JOIN transferencias_eventos e
                       ON e.ordem_id = o.id AND e.event_type = 'rejeitado'
                WHERE o.status = 'rejeitada' AND e.id IS NULL
            """)
            n_rej = cursor.rowcount

            conn.commit()
            logger.info(
                "run_backfill_transferencias_eventos: inserted %d criado, %d confirmado, %d rejeitado events",
                n_criado, n_conf, n_rej,
            )
        except Exception as exc:
            logger.error("run_backfill_transferencias_eventos failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(202618)")
                conn.commit()
            except Exception:
                pass


_LOCK_TAREFAS = 202619
_LOCK_TAREFAS_V2 = 202620
_LOCK_TAREFAS_V3 = 202621


def run_migrations_tarefas():
    """Create tarefas + tarefas_registos tables; add acesso_tarefas to users.
    Advisory lock 202619."""
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_TAREFAS,))
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_tarefas: lock held by another worker, skipping")
                return

            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS acesso_tarefas BOOLEAN DEFAULT FALSE")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tarefas (
                    id            SERIAL PRIMARY KEY,
                    nome          VARCHAR(200) NOT NULL,
                    tipo          VARCHAR(20)  NOT NULL CHECK (tipo IN ('abertura','fecho')),
                    frequencia    VARCHAR(20)  NOT NULL CHECK (frequencia IN ('diaria','semanal','mensal')),
                    dia_semana    SMALLINT     CHECK (dia_semana BETWEEN 0 AND 6),
                    dia_mes       SMALLINT     CHECK (dia_mes BETWEEN 1 AND 31),
                    utilizador_id INTEGER      REFERENCES users(id) ON DELETE SET NULL,
                    ativo         BOOLEAN      NOT NULL DEFAULT TRUE,
                    created_at    TIMESTAMP    DEFAULT NOW()
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tarefas_registos (
                    id            SERIAL PRIMARY KEY,
                    tarefa_id     INTEGER NOT NULL REFERENCES tarefas(id) ON DELETE CASCADE,
                    data          DATE    NOT NULL,
                    estado        VARCHAR(20) NOT NULL CHECK (estado IN ('feita','bloqueada')),
                    motivo        TEXT,
                    utilizador_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at    TIMESTAMP DEFAULT NOW(),
                    UNIQUE (tarefa_id, data)
                )
            """)

            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_tarefas_registos_data ON tarefas_registos(data)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_tarefas_utilizador ON tarefas(utilizador_id)"
            )

            conn.commit()
            logger.info("run_migrations_tarefas: tables and column created/verified")
        except Exception as exc:
            logger.error("run_migrations_tarefas failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_TAREFAS,))
                conn.commit()
            except Exception:
                pass


_LOCK_FECHO_CAIXA_AUDIT = 202640


def run_migrations_fecho_caixa_audit():
    """Create fecho_caixa_audit table for tracking manager edits/deletes. Advisory lock 202640."""
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_FECHO_CAIXA_AUDIT,))
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_fecho_caixa_audit: lock held by another worker, skipping")
                return

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS fecho_caixa_audit (
                    id SERIAL PRIMARY KEY,
                    fecho_caixa_id INTEGER NOT NULL,
                    acao VARCHAR(10) NOT NULL CHECK (acao IN (\'edit\', \'delete\')),
                    utilizador VARCHAR(100) NOT NULL,
                    timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
                    valores_anteriores JSONB NOT NULL
                )
            ''')
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_fecho_caixa_audit_fecho_id "
                "ON fecho_caixa_audit(fecho_caixa_id)"
            )

            conn.commit()
            logger.info("run_migrations_fecho_caixa_audit: table ready")
        except Exception as exc:
            logger.error("run_migrations_fecho_caixa_audit failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_FECHO_CAIXA_AUDIT,))
                conn.commit()
            except Exception:
                pass


_LOCK_PESAGEM_DRAFT_BATCHES = 202693


def run_migrations_pesagem_draft_batches():
    """Create durable manual weighing drafts and confirmation receipts."""
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (_LOCK_PESAGEM_DRAFT_BATCHES,),
            )
            if not cursor.fetchone()[0]:
                logger.info(
                    "run_migrations_pesagem_draft_batches: "
                    "lock held by another worker, skipping"
                )
                return

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pesagem_draft_batches (
                    id UUID PRIMARY KEY,
                    loja VARCHAR(100) NOT NULL,
                    store_id INTEGER REFERENCES stores(id),
                    status VARCHAR(20) NOT NULL DEFAULT 'draft'
                        CHECK (
                            status IN (
                                'draft', 'registering', 'registered',
                                'confirming', 'confirmed', 'failed'
                            )
                        ),
                    expected_count INTEGER NOT NULL DEFAULT 0
                        CHECK (expected_count >= 0),
                    revision INTEGER NOT NULL DEFAULT 1
                        CHECK (revision >= 1),
                    inserted_count INTEGER NOT NULL DEFAULT 0
                        CHECK (inserted_count >= 0),
                    created_by_id INTEGER,
                    created_by VARCHAR(100) NOT NULL,
                    updated_by VARCHAR(100) NOT NULL,
                    error_message TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    confirmed_at TIMESTAMPTZ,
                    registered_at TIMESTAMPTZ,
                    registered_by_id INTEGER,
                    registered_by VARCHAR(100),
                    receipt_snapshot JSONB
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pesagem_draft_entries (
                    id BIGSERIAL PRIMARY KEY,
                    batch_id UUID NOT NULL
                        REFERENCES pesagem_draft_batches(id)
                        ON DELETE CASCADE,
                    position INTEGER NOT NULL CHECK (position >= 0),
                    data DATE NOT NULL,
                    sabor VARCHAR(255) NOT NULL,
                    quantidade_kg NUMERIC(10,4) NOT NULL
                        CHECK (
                            quantidade_kg >= 0
                            AND quantidade_kg < 1000
                            AND quantidade_kg <> 'NaN'::numeric
                        ),
                    suspeito BOOLEAN NOT NULL DEFAULT FALSE,
                    stock_id INTEGER,
                    UNIQUE(batch_id, position),
                    UNIQUE(batch_id, data, sabor)
                )
            """)
            cursor.execute("""
                ALTER TABLE pesagem_draft_batches
                DROP CONSTRAINT IF EXISTS pesagem_draft_batches_status_check
            """)
            cursor.execute("""
                ALTER TABLE pesagem_draft_batches
                ADD CONSTRAINT pesagem_draft_batches_status_check
                CHECK (
                    status IN (
                        'draft', 'registering', 'registered',
                        'confirming', 'confirmed', 'failed'
                    )
                )
            """)
            cursor.execute("""
                ALTER TABLE pesagem_draft_batches
                ADD COLUMN IF NOT EXISTS registered_at TIMESTAMPTZ
            """)
            cursor.execute("""
                ALTER TABLE pesagem_draft_batches
                ADD COLUMN IF NOT EXISTS registered_by_id INTEGER
            """)
            cursor.execute("""
                ALTER TABLE pesagem_draft_batches
                ADD COLUMN IF NOT EXISTS registered_by VARCHAR(100)
            """)
            cursor.execute("""
                ALTER TABLE pesagem_draft_batches
                ADD COLUMN IF NOT EXISTS receipt_snapshot JSONB
            """)
            cursor.execute("""
                ALTER TABLE pesagem_draft_entries
                ADD COLUMN IF NOT EXISTS stock_id INTEGER
            """)
            cursor.execute("""
                UPDATE pesagem_draft_batches b
                SET receipt_snapshot = snapshots.snapshot
                FROM (
                    SELECT e.batch_id,
                           jsonb_agg(
                               jsonb_build_object(
                                   'data', e.data::text,
                                   'sabor', e.sabor,
                                   'quantidade_kg', e.quantidade_kg,
                                   'suspeito', e.suspeito
                               )
                               ORDER BY e.position
                           ) AS snapshot
                    FROM pesagem_draft_entries e
                    GROUP BY e.batch_id
                ) snapshots
                WHERE b.id = snapshots.batch_id
                  AND b.status = 'confirmed'
                  AND b.receipt_snapshot IS NULL
            """)
            cursor.execute("""
                ALTER TABLE pesagem_draft_batches
                ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1
            """)
            cursor.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1
                        FROM pg_constraint
                        WHERE conname = 'ck_pesagem_draft_entry_finite'
                    ) THEN
                        ALTER TABLE pesagem_draft_entries
                        ADD CONSTRAINT ck_pesagem_draft_entry_finite
                        CHECK (
                            quantidade_kg >= 0
                            AND quantidade_kg < 1000
                            AND quantidade_kg <> 'NaN'::numeric
                        );
                    END IF;
                END $$;
            """)
            cursor.execute("""
                DROP INDEX IF EXISTS uq_pesagem_draft_open_loja
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_pesagem_draft_open_loja
                ON pesagem_draft_batches(loja)
                WHERE status IN (
                    'draft', 'registering', 'registered',
                    'confirming', 'failed'
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_pesagem_draft_entries_batch
                ON pesagem_draft_entries(batch_id, position)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_pesagem_draft_receipts
                ON pesagem_draft_batches(loja, confirmed_at DESC)
                WHERE status = 'confirmed'
            """)
            conn.commit()
            logger.info(
                "run_migrations_pesagem_draft_batches: tables ready"
            )
        except Exception as exc:
            logger.error(
                "run_migrations_pesagem_draft_batches failed: %s",
                exc,
            )
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (_LOCK_PESAGEM_DRAFT_BATCHES,),
                )
                conn.commit()
            except Exception:
                pass


_LOCK_PESAGEM_DAY_JUSTIFICATIONS = 202694


def run_migrations_pesagem_day_justifications():
    """Create immutable audited exceptions for days without EOD weighing."""
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (_LOCK_PESAGEM_DAY_JUSTIFICATIONS,),
            )
            if not cursor.fetchone()[0]:
                logger.info(
                    "run_migrations_pesagem_day_justifications: "
                    "lock held by another worker, skipping"
                )
                return
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pesagem_day_justifications (
                    id BIGSERIAL PRIMARY KEY,
                    store_id INTEGER NOT NULL REFERENCES stores(id),
                    loja VARCHAR(100) NOT NULL,
                    data DATE NOT NULL,
                    reason TEXT NOT NULL CHECK (length(trim(reason)) >= 5),
                    created_by_id INTEGER,
                    created_by VARCHAR(100) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(store_id, data)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS
                    idx_pesagem_day_justifications_date
                ON pesagem_day_justifications(data, store_id)
            """)
            conn.commit()
            logger.info(
                "run_migrations_pesagem_day_justifications: table ready"
            )
        except Exception as exc:
            logger.error(
                "run_migrations_pesagem_day_justifications failed: %s",
                exc,
            )
            conn.rollback()
            raise
        finally:
            try:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (_LOCK_PESAGEM_DAY_JUSTIFICATIONS,),
                )
                conn.commit()
            except Exception:
                pass


_LOCK_PESAGEM_AUDIT = 202695


def run_migrations_pesagem_audit():
    """Add soft deletion and immutable weighing audit events."""
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (_LOCK_PESAGEM_AUDIT,),
            )
            if not cursor.fetchone()[0]:
                logger.info(
                    "run_migrations_pesagem_audit: "
                    "lock held by another worker, skipping"
                )
                return

            cursor.execute("""
                ALTER TABLE stock_gelado
                ADD COLUMN IF NOT EXISTS is_active BOOLEAN
                    NOT NULL DEFAULT TRUE
            """)
            cursor.execute("""
                ALTER TABLE stock_gelado
                ADD COLUMN IF NOT EXISTS source_batch_id UUID
                    REFERENCES pesagem_draft_batches(id)
            """)
            cursor.execute("""
                ALTER TABLE stock_gelado
                ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMPTZ
            """)
            cursor.execute("""
                ALTER TABLE stock_gelado
                ADD COLUMN IF NOT EXISTS deactivated_by_id INTEGER
            """)
            cursor.execute("""
                ALTER TABLE stock_gelado
                ADD COLUMN IF NOT EXISTS deactivated_by VARCHAR(100)
            """)
            cursor.execute("""
                ALTER TABLE stock_gelado
                ADD COLUMN IF NOT EXISTS deactivation_reason TEXT
            """)
            cursor.execute("""
                ALTER TABLE stock_gelado
                ADD COLUMN IF NOT EXISTS restored_at TIMESTAMPTZ
            """)
            cursor.execute("""
                ALTER TABLE stock_gelado
                ADD COLUMN IF NOT EXISTS restored_by_id INTEGER
            """)
            cursor.execute("""
                ALTER TABLE stock_gelado
                ADD COLUMN IF NOT EXISTS restored_by VARCHAR(100)
            """)
            cursor.execute("""
                ALTER TABLE stock_gelado
                DROP CONSTRAINT IF EXISTS
                    uq_stock_gelado_data_loja_sabor_tipo
            """)
            cursor.execute("""
                WITH unique_store_names AS (
                    SELECT lower(name) AS normalized_name, MIN(id) AS store_id
                    FROM stores
                    GROUP BY lower(name)
                    HAVING COUNT(*) = 1
                )
                UPDATE stock_gelado sg
                SET store_id = usn.store_id
                FROM unique_store_names usn
                WHERE sg.store_id IS NULL
                  AND lower(sg.loja) = usn.normalized_name
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pesagem_audit_events (
                    id BIGSERIAL PRIMARY KEY,
                    event_uuid UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
                    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    event_type VARCHAR(30) NOT NULL CHECK (
                        event_type IN (
                            'create', 'batch_register', 'batch_confirm',
                            'edit', 'delete',
                            'delete_day', 'restore', 'justify', 'failure'
                        )
                    ),
                    outcome VARCHAR(20) NOT NULL DEFAULT 'success'
                        CHECK (outcome IN ('success', 'failed')),
                    stock_id INTEGER,
                    batch_id UUID,
                    store_id INTEGER,
                    loja VARCHAR(100) NOT NULL,
                    event_date DATE,
                    actor_id INTEGER,
                    actor_username VARCHAR(100) NOT NULL,
                    origin VARCHAR(50) NOT NULL,
                    reason TEXT,
                    affected_count INTEGER NOT NULL DEFAULT 0,
                    expected_count INTEGER,
                    before_data JSONB,
                    after_data JSONB,
                    safe_cause TEXT
                )
            """)
            cursor.execute("""
                ALTER TABLE pesagem_audit_events
                DROP CONSTRAINT IF EXISTS pesagem_audit_events_event_type_check
            """)
            cursor.execute("""
                ALTER TABLE pesagem_audit_events
                ADD CONSTRAINT pesagem_audit_events_event_type_check
                CHECK (
                    event_type IN (
                        'create', 'batch_register', 'batch_confirm',
                        'edit', 'delete', 'delete_day', 'restore',
                        'justify', 'failure'
                    )
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_pesagem_audit_store_date
                ON pesagem_audit_events(
                    store_id, event_date, occurred_at DESC
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_pesagem_audit_batch
                ON pesagem_audit_events(batch_id, occurred_at DESC)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_pesagem_audit_stock
                ON pesagem_audit_events(stock_id, occurred_at DESC)
            """)
            cursor.execute("""
                WITH ranked AS (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY
                                   CASE
                                       WHEN store_id IS NOT NULL
                                           THEN 'store:' || store_id::text
                                       ELSE 'legacy:' || lower(loja)
                                   END,
                                   data, sabor, tipo
                               ORDER BY id DESC
                           ) AS duplicate_rank
                    FROM stock_gelado
                    WHERE is_active = TRUE
                ),
                deactivated AS (
                    UPDATE stock_gelado sg
                    SET is_active = FALSE,
                        deactivated_at = NOW(),
                        deactivated_by = 'migration',
                        deactivation_reason =
                            'Duplicado reconciliado por identidade estável'
                    FROM ranked r
                    WHERE sg.id = r.id
                      AND r.duplicate_rank > 1
                    RETURNING sg.*
                )
                INSERT INTO pesagem_audit_events (
                    event_type, outcome, stock_id, batch_id, store_id,
                    loja, event_date, actor_username, origin, reason,
                    affected_count, before_data, after_data
                )
                SELECT
                    'delete', 'success', id, source_batch_id, store_id,
                    loja, data, 'migration', 'stable_store_reconciliation',
                    'Duplicado reconciliado por identidade estável', 1,
                    jsonb_build_object(
                        'id', id, 'data', data, 'sabor', sabor,
                        'quantidade_kg', quantidade_kg, 'tipo', tipo,
                        'local', local, 'is_active', TRUE
                    ),
                    jsonb_build_object(
                        'id', id, 'data', data, 'sabor', sabor,
                        'quantidade_kg', quantidade_kg, 'tipo', tipo,
                        'local', local, 'is_active', FALSE
                    )
                FROM deactivated
            """)
            cursor.execute("""
                DROP INDEX IF EXISTS
                    uq_stock_gelado_active_day_store_flavour_type
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_stock_gelado_active_store_identity
                ON stock_gelado(data, store_id, sabor, tipo)
                WHERE is_active = TRUE AND store_id IS NOT NULL
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_stock_gelado_active_legacy_identity
                ON stock_gelado(data, lower(loja), sabor, tipo)
                WHERE is_active = TRUE AND store_id IS NULL
            """)
            cursor.execute("""
                CREATE OR REPLACE FUNCTION prevent_pesagem_audit_mutation()
                RETURNS trigger AS $$
                BEGIN
                    RAISE EXCEPTION
                        'pesagem audit events are immutable';
                END;
                $$ LANGUAGE plpgsql
            """)
            cursor.execute("""
                DROP TRIGGER IF EXISTS trg_pesagem_audit_immutable
                ON pesagem_audit_events
            """)
            cursor.execute("""
                CREATE TRIGGER trg_pesagem_audit_immutable
                BEFORE UPDATE OR DELETE ON pesagem_audit_events
                FOR EACH ROW EXECUTE FUNCTION
                    prevent_pesagem_audit_mutation()
            """)

            cursor.execute("""
                DROP INDEX IF EXISTS uq_pesagem_draft_open_loja
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_pesagem_draft_open_store
                ON pesagem_draft_batches(store_id)
                WHERE store_id IS NOT NULL
                  AND status IN (
                      'draft', 'registering', 'registered',
                      'confirming', 'failed'
                  )
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_pesagem_draft_open_legacy_loja
                ON pesagem_draft_batches(lower(loja))
                WHERE store_id IS NULL
                  AND status IN (
                      'draft', 'registering', 'registered',
                      'confirming', 'failed'
                  )
            """)
            conn.commit()
            logger.info("run_migrations_pesagem_audit: schema ready")
        except Exception as exc:
            conn.rollback()
            logger.error("run_migrations_pesagem_audit failed: %s", exc)
            raise
        finally:
            try:
                cursor.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (_LOCK_PESAGEM_AUDIT,),
                )
                conn.commit()
            except Exception:
                pass


def run_migrations_tarefas_v2():
    """Add loja_id + equipa columns to tarefas; make frequencia nullable.
    Advisory lock 202620."""
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_TAREFAS_V2,))
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_tarefas_v2: lock held by another worker, skipping")
                return

            cursor.execute(
                "ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS loja_id INTEGER REFERENCES stores(id)"
            )
            cursor.execute(
                "ALTER TABLE tarefas ADD COLUMN IF NOT EXISTS equipa VARCHAR(50) "
                "CHECK (equipa IN ('Produção','Vendas','Logística','Compras'))"
            )
            cursor.execute(
                "ALTER TABLE tarefas ALTER COLUMN frequencia DROP NOT NULL"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_tarefas_loja ON tarefas(loja_id)"
            )
            conn.commit()
            logger.info("run_migrations_tarefas_v2: loja_id, equipa, nullable frequencia — done")
        except Exception as exc:
            logger.error("run_migrations_tarefas_v2 failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_TAREFAS_V2,))
                conn.commit()
            except Exception:
                pass


def run_migrations_tarefas_v3():
    """Extend tarefas_registos.estado CHECK to include 'em_curso'.
    Advisory lock 202621."""
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_TAREFAS_V3,))
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_tarefas_v3: lock held by another worker, skipping")
                return

            cursor.execute(
                "ALTER TABLE tarefas_registos "
                "DROP CONSTRAINT IF EXISTS tarefas_registos_estado_check"
            )
            cursor.execute("""
                ALTER TABLE tarefas_registos
                ADD CONSTRAINT tarefas_registos_estado_check
                CHECK (estado IN ('feita','bloqueada','em_curso'))
            """)
            conn.commit()
            logger.info("run_migrations_tarefas_v3: estado constraint extended to include em_curso")
        except Exception as exc:
            logger.error("run_migrations_tarefas_v3 failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_TAREFAS_V3,))
                conn.commit()
            except Exception:
                pass


def run_migrations_colaboradores_smart():
    """Idempotent migration: adds CCT+IRS smart-salary columns to colaboradores.
    Advisory lock 202615."""
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202615)")
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_colaboradores_smart: lock held by another worker, skipping")
                return

            cursor.execute('''
                ALTER TABLE colaboradores
                    ADD COLUMN IF NOT EXISTS categoria_profissional VARCHAR(100),
                    ADD COLUMN IF NOT EXISTS nivel_remuneratorio    SMALLINT DEFAULT 1,
                    ADD COLUMN IF NOT EXISTS estado_civil           VARCHAR(20) DEFAULT 'solteiro',
                    ADD COLUMN IF NOT EXISTS num_dependentes        SMALLINT DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS irs_override           BOOLEAN DEFAULT FALSE
            ''')
            conn.commit()
            logger.info("run_migrations_colaboradores_smart: columns added/verified on colaboradores")
        except Exception as exc:
            logger.error("run_migrations_colaboradores_smart failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(202615)")
                conn.commit()
            except Exception:
                pass


_LOCK_BATCH_ID = 202624


def run_migrations_batch_id():
    """Idempotent: add batch_id TEXT column to ordens_transferencia.
    Uses advisory lock 202624.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_BATCH_ID,))
        if not cursor.fetchone()[0]:
            logger.info("run_migrations_batch_id: lock held by another worker, skipping")
            return
        cursor.execute("""
            ALTER TABLE ordens_transferencia
                ADD COLUMN IF NOT EXISTS batch_id TEXT
        """)
        conn.commit()
        logger.info("run_migrations_batch_id: batch_id column ensured on ordens_transferencia")


_LOCK_AGENTE = 202700


def run_migrations_agente():
    """Idempotent: create Agente Scoopy tables.
    Uses advisory lock 202700.
    Tables: agente_conversas, agente_mensagens, agente_memoria, agente_operacoes_pendentes.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_AGENTE,))
        if not cursor.fetchone()[0]:
            logger.info("run_migrations_agente: lock held by another worker, skipping")
            return
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agente_conversas (
                    id          SERIAL PRIMARY KEY,
                    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    titulo      VARCHAR(500) NOT NULL DEFAULT 'Nova conversa',
                    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_agente_conv_user ON agente_conversas(user_id, updated_at DESC)")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agente_mensagens (
                    id           SERIAL PRIMARY KEY,
                    conversa_id  INTEGER NOT NULL REFERENCES agente_conversas(id) ON DELETE CASCADE,
                    role         VARCHAR(20) NOT NULL,
                    content      TEXT NOT NULL,
                    tool_name    VARCHAR(100),
                    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_agente_msg_conv ON agente_mensagens(conversa_id, created_at ASC)")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agente_memoria (
                    id          SERIAL PRIMARY KEY,
                    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    chave       VARCHAR(200) NOT NULL,
                    valor       TEXT NOT NULL,
                    categoria   VARCHAR(50) NOT NULL DEFAULT 'geral',
                    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (user_id, chave)
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_agente_mem_user ON agente_memoria(user_id)")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agente_operacoes_pendentes (
                    id               SERIAL PRIMARY KEY,
                    conversa_id      INTEGER NOT NULL REFERENCES agente_conversas(id) ON DELETE CASCADE,
                    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    sql_proposto     TEXT NOT NULL,
                    descricao        TEXT NOT NULL,
                    impacto_estimado TEXT,
                    estado           VARCHAR(20) NOT NULL DEFAULT 'pendente',
                    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    executada_at     TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_agente_ops_user ON agente_operacoes_pendentes(user_id, estado)")

            conn.commit()
            logger.info("run_migrations_agente: Agente Scoopy tables created/verified")
        except Exception as exc:
            logger.error("run_migrations_agente failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_AGENTE,))
                conn.commit()
            except Exception:
                pass


_LOCK_STOCK_GELADO_MARCH2026_DEDUP = 202624
_LOCK_STOCK_GELADO_DEDUP_UNIQUE = 202651


def run_data_fix_stock_gelado_march2026_dedup():
    """One-time idempotent fix for stock_gelado data errors found in April 2026:

    1. Bolhão 31/03/2026 — "Fior di Latte" entered twice (IDs 1496 + 2214).
       Keep the earlier row (lower id), delete the duplicate.

    2. Bolhão 31/03/2026 — "Pistacchio Veg" is a duplicate of "Pistacchio V."
       (IDs 1503 + 2379).  Keep "Pistacchio V." (lower id), delete "Pistacchio Veg".

    3. Matosinhos 01/04/2026 — "Café" (4.460 kg, tipo='inicio') was never inserted.
       All surrounding dates have it; it was simply missed.

    Net effect: corrects the Março 2026 stock final from 92.96 kg → 94.807 kg.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_STOCK_GELADO_MARCH2026_DEDUP,))
            if not cursor.fetchone()[0]:
                logger.info("run_data_fix_stock_gelado_march2026_dedup: lock held by another worker, skipping")
                return

            fixed = []

            cursor.execute("""
                SELECT COUNT(*) FROM stock_gelado
                WHERE loja = 'Bolhão' AND data = '2026-03-31' AND tipo = 'fim'
                  AND sabor = 'Fior di Latte'
            """)
            if cursor.fetchone()[0] > 1:
                cursor.execute("""
                    DELETE FROM stock_gelado
                    WHERE loja = 'Bolhão' AND data = '2026-03-31' AND tipo = 'fim'
                      AND sabor = 'Fior di Latte'
                      AND id != (
                          SELECT MIN(id) FROM stock_gelado
                          WHERE loja = 'Bolhão' AND data = '2026-03-31' AND tipo = 'fim'
                            AND sabor = 'Fior di Latte'
                      )
                """)
                fixed.append(f"deleted {cursor.rowcount} duplicate 'Fior di Latte' row(s) for Bolhão 2026-03-31")

            cursor.execute("""
                SELECT COUNT(*) FROM stock_gelado
                WHERE loja = 'Bolhão' AND data = '2026-03-31' AND tipo = 'fim'
                  AND sabor = 'Pistacchio Veg'
            """)
            if cursor.fetchone()[0] > 0:
                cursor.execute("""
                    DELETE FROM stock_gelado
                    WHERE loja = 'Bolhão' AND data = '2026-03-31' AND tipo = 'fim'
                      AND sabor = 'Pistacchio Veg'
                """)
                fixed.append(f"deleted {cursor.rowcount} 'Pistacchio Veg' duplicate row(s) for Bolhão 2026-03-31 (kept 'Pistacchio V.')")

            cursor.execute("""
                SELECT COUNT(*) FROM stock_gelado
                WHERE loja = 'Matosinhos' AND data = '2026-04-01' AND tipo = 'inicio'
                  AND sabor = 'Café'
            """)
            if cursor.fetchone()[0] == 0:
                cursor.execute("SELECT id FROM stores WHERE name = 'Matosinhos' LIMIT 1")
                row = cursor.fetchone()
                store_id = row[0] if row else None
                cursor.execute("""
                    INSERT INTO stock_gelado (data, loja, sabor, quantidade_kg, tipo, store_id)
                    VALUES ('2026-04-01', 'Matosinhos', 'Café', 4.460, 'inicio', %s)
                """, (store_id,))
                fixed.append("inserted Café 4.460 kg for Matosinhos 2026-04-01 tipo=inicio")

            conn.commit()

            if fixed:
                for msg in fixed:
                    logger.info("run_data_fix_stock_gelado_march2026_dedup: %s", msg)
            else:
                logger.info("run_data_fix_stock_gelado_march2026_dedup: nothing to fix, already clean")

        except Exception as exc:
            logger.error("run_data_fix_stock_gelado_march2026_dedup failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


def run_data_fix_stock_gelado_dedup_and_unique():
    """Idempotent migration that eliminates duplicate rows in stock_gelado and
    adds a UNIQUE constraint to prevent recurrence.

    Steps performed (in a single transaction under advisory lock 202651):

    1. Normalise sabor names in plano_producao — collapses 'Chocolate branco',
       'CHOCOLATE BRANCO', etc. into the canonical 'Chocolate Branco' form.

    2. Normalise sabor names in stock_gelado — updates variant spellings to their
       canonical form.  Rows whose canonical name already exists for the same
       (data, loja, tipo) are deleted (the canonical row is kept); otherwise the
       sabor column is updated in-place.

    3. Deduplicate stock_gelado — for every remaining group sharing the same
       (data, loja, sabor, tipo), keep the row with the lowest id and delete
       the rest.

    4. Add UNIQUE constraint uq_stock_gelado_data_loja_sabor_tipo.  Skips the
       entire function early if the constraint already exists (idempotent).
    """
    from sabor_utils import normalise_sabor
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_STOCK_GELADO_DEDUP_UNIQUE,))
            if not cursor.fetchone()[0]:
                logger.info("run_data_fix_stock_gelado_dedup_and_unique: lock held by another worker, skipping")
                return

            cursor.execute("""
                SELECT
                    to_regclass(
                        'uq_stock_gelado_active_store_identity'
                    ) IS NOT NULL
                    OR to_regclass('pesagem_audit_events') IS NOT NULL
            """)
            if cursor.fetchone()[0]:
                logger.info(
                    "run_data_fix_stock_gelado_dedup_and_unique: "
                    "active-row unique index already installed, skipping"
                )
                return

            cursor.execute("""
                SELECT COUNT(*) FROM pg_constraint
                WHERE conname = 'uq_stock_gelado_data_loja_sabor_tipo'
                  AND conrelid = 'stock_gelado'::regclass
            """)
            if cursor.fetchone()[0] > 0:
                logger.info("run_data_fix_stock_gelado_dedup_and_unique: constraint already exists, nothing to do")
                return

            # ── Phase 1: normalise + dedup (committed before ALTER TABLE) ──
            # Committing phase 1 releases all row-level locks acquired during
            # the UPDATEs/DELETEs, so the subsequent ALTER TABLE (phase 2) can
            # acquire its AccessExclusiveLock without deadlocking against
            # concurrent reads that were waiting for those same rows.

            # Step 1: normalise plano_producao.sabor
            # plano_producao has a pre-existing UNIQUE (data, sabor) constraint,
            # so we can't UPDATE a variant to canonical when canonical already
            # exists on the same date — that would violate the constraint.
            # For each variant→canonical pair we keep the MIN(id) row across
            # both spellings on each date (id-based retention, not
            # spelling-based), then rename the surviving row to the canonical.
            cursor.execute("SELECT DISTINCT sabor FROM plano_producao WHERE sabor IS NOT NULL")
            plano_sabores = [r[0] for r in cursor.fetchall()]
            pp_deleted = 0
            pp_updated = 0
            for s in plano_sabores:
                canonical = normalise_sabor(s)
                if canonical == s:
                    continue
                # Delete the higher-id row(s) in the (variant, canonical) pair
                # on each date — id-based, so the oldest entry always survives.
                cursor.execute("""
                    WITH combined AS (
                        SELECT id,
                               ROW_NUMBER() OVER (PARTITION BY data ORDER BY id) AS rn
                        FROM plano_producao
                        WHERE sabor = ANY(%s)
                    )
                    DELETE FROM plano_producao
                    WHERE id IN (SELECT id FROM combined WHERE rn > 1)
                """, ([s, canonical],))
                pp_deleted += cursor.rowcount
                cursor.execute(
                    "UPDATE plano_producao SET sabor = %s WHERE sabor = %s",
                    (canonical, s),
                )
                pp_updated += cursor.rowcount
            if pp_deleted or pp_updated:
                logger.info(
                    "run_data_fix_stock_gelado_dedup_and_unique: "
                    "plano_producao sabor normalisation — deleted %d duplicates, renamed %d rows",
                    pp_deleted, pp_updated,
                )

            # Step 2: normalise stock_gelado.sabor
            # stock_gelado has NO pre-existing unique constraint, so we UPDATE
            # all variant names to canonical directly without deleting first.
            # This may temporarily produce same-key rows; the window-function
            # dedup in step 3 always retains MIN(id), so the oldest/most-
            # legitimate row is kept regardless of which spelling it had.
            cursor.execute("SELECT DISTINCT sabor FROM stock_gelado WHERE sabor IS NOT NULL")
            sg_sabores = [r[0] for r in cursor.fetchall()]
            sg_renamed = 0
            for s in sg_sabores:
                canonical = normalise_sabor(s)
                if canonical == s:
                    continue
                cursor.execute(
                    "UPDATE stock_gelado SET sabor = %s WHERE sabor = %s",
                    (canonical, s),
                )
                sg_renamed += cursor.rowcount
            if sg_renamed:
                logger.info(
                    "run_data_fix_stock_gelado_dedup_and_unique: "
                    "stock_gelado sabor normalisation — renamed %d rows",
                    sg_renamed,
                )

            # Step 3: deduplicate stock_gelado keeping MIN(id) per group
            # Window-function approach avoids the NOT-IN-with-NULL pitfall
            # that would silently skip all deletes.
            cursor.execute("""
                WITH ranked AS (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY data, loja, sabor, tipo
                               ORDER BY id
                           ) AS rn
                    FROM stock_gelado
                )
                DELETE FROM stock_gelado
                WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
            """)
            deleted = cursor.rowcount
            if deleted > 0:
                logger.info(
                    "run_data_fix_stock_gelado_dedup_and_unique: deleted %d duplicate rows",
                    deleted,
                )
            else:
                logger.info("run_data_fix_stock_gelado_dedup_and_unique: no duplicates found")

            # Post-dedup validation: total must equal unique combos.
            cursor.execute("""
                SELECT COUNT(*) AS total,
                       COUNT(DISTINCT (data, loja, sabor, tipo)) AS unique_combos
                FROM stock_gelado
            """)
            sg_total, sg_unique = cursor.fetchone()
            logger.info(
                "run_data_fix_stock_gelado_dedup_and_unique: post-dedup — "
                "total=%d unique_combos=%d duplicates_remaining=%d",
                sg_total, sg_unique, sg_total - sg_unique,
            )
            if sg_total != sg_unique:
                raise RuntimeError(
                    f"Dedup incomplete: {sg_total - sg_unique} duplicate rows remain — "
                    "aborting constraint creation"
                )

            # COMMIT phase 1 — releases all row locks so ALTER TABLE
            # can acquire AccessExclusiveLock without deadlocking.
            conn.commit()

            # ── Phase 2: add UNIQUE constraint (separate short transaction) ──
            # The advisory lock is still held at session level.
            cursor.execute("""
                ALTER TABLE stock_gelado
                ADD CONSTRAINT uq_stock_gelado_data_loja_sabor_tipo
                UNIQUE (data, loja, sabor, tipo)
            """)
            conn.commit()
            logger.info(
                "run_data_fix_stock_gelado_dedup_and_unique: UNIQUE constraint added successfully"
            )

        except Exception as exc:
            logger.error("run_data_fix_stock_gelado_dedup_and_unique failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


def run_migrations_user_audit_log():
    """Create user_audit_log table for tracking user management actions.

    Advisory lock 202620. Idempotent — safe to call on every startup.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202620)")
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_user_audit_log: lock held by another worker, skipping")
                return

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_audit_log (
                    id SERIAL PRIMARY KEY,
                    actor_id INTEGER,
                    actor_username VARCHAR(100) NOT NULL,
                    action_type VARCHAR(50) NOT NULL,
                    target_user_id INTEGER,
                    target_username VARCHAR(100) NOT NULL,
                    details JSONB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_audit_log_created ON user_audit_log(created_at DESC)"
            )
            conn.commit()
            logger.info("run_migrations_user_audit_log: table ready")
        except Exception as exc:
            logger.error("run_migrations_user_audit_log failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


def run_migrations_conta_vendas_diarias():
    """Add conta_vendas_diarias column to produtos_vendas_config.

    Advisory lock 202613. Defaults to TRUE so all existing products continue
    to appear in Vendas Diárias totals until the user explicitly excludes them.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202613)")
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_conta_vendas_diarias: lock held by another worker, skipping")
                return

            cursor.execute("""
                ALTER TABLE produtos_vendas_config
                ADD COLUMN IF NOT EXISTS conta_vendas_diarias BOOLEAN NOT NULL DEFAULT TRUE
            """)
            conn.commit()
            logger.info("run_migrations_conta_vendas_diarias: column added (or already present)")
        except Exception as exc:
            logger.error("run_migrations_conta_vendas_diarias failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


def run_migrations_b2b_vendas_diarias():
    """Add b2b column to produtos_vendas_config.

    Advisory lock 202626. Defaults to FALSE — marking a product as B2B is an
    explicit opt-in via the "Filtro Vendas Diárias" tile. The classification
    is retroactive by design: since the Dashboard de Vendas aggregation joins
    on this column at query time (not a one-off backfill), flagging a product
    reclassifies its full existing vendas_detalhe history (2025 and 2026)
    into the B2B channel, not just future sales.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202626)")
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_b2b_vendas_diarias: lock held by another worker, skipping")
                return

            cursor.execute("""
                ALTER TABLE produtos_vendas_config
                ADD COLUMN IF NOT EXISTS b2b BOOLEAN NOT NULL DEFAULT FALSE
            """)
            conn.commit()
            logger.info("run_migrations_b2b_vendas_diarias: column added (or already present)")
        except Exception as exc:
            logger.error("run_migrations_b2b_vendas_diarias failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


def run_migrations_stock_gelado_carapinas():
    """Idempotent: create stock_gelado_carapinas table for per-carapina weighing breakdown.

    Advisory lock 202615.
    Each stock_gelado record with multiple carapinas will have N rows here (one per carapina).
    Records with a single carapina are NOT stored — the total in stock_gelado is sufficient.
    ON DELETE CASCADE ensures cleanup when a stock_gelado row is removed.
    """
    from db.connection import db_connection, logger
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202615)")
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_stock_gelado_carapinas: lock held, skipping")
                return
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stock_gelado_carapinas (
                    id              SERIAL PRIMARY KEY,
                    stock_gelado_id INTEGER NOT NULL REFERENCES stock_gelado(id) ON DELETE CASCADE,
                    carapina_numero SMALLINT NOT NULL,
                    quantidade_kg   REAL NOT NULL,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_sgc_stock_id
                ON stock_gelado_carapinas(stock_gelado_id)
            """)
            conn.commit()
            logger.info("run_migrations_stock_gelado_carapinas: table ready")
        except Exception as exc:
            logger.error("run_migrations_stock_gelado_carapinas failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


def run_migrations_cost_center_allocation():
    """Idempotent: create cost_center_allocation table for P&L store distribution config.

    Advisory lock 202614. Table stores one row per (categoria_custo_id, store_id):
      - mode='volume_vendas': sentinel row with store_id=NULL
      - mode='tudo_loja':     one row with the chosen store_id, percentagem=100
      - mode='manual':        one row per store with custom percentagem
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202614)")
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_cost_center_allocation: lock held, skipping")
                return

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS cost_center_allocation (
                    id                 SERIAL PRIMARY KEY,
                    categoria_custo_id INTEGER NOT NULL REFERENCES cost_categories(id) ON DELETE CASCADE,
                    store_id           INTEGER REFERENCES stores(id) ON DELETE CASCADE,
                    percentagem        REAL    NOT NULL DEFAULT 0,
                    modo               VARCHAR(50) NOT NULL DEFAULT 'volume_vendas',
                    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_cost_center_allocation
                ON cost_center_allocation (categoria_custo_id, COALESCE(store_id, -1))
            """)
            conn.commit()
            logger.info("run_migrations_cost_center_allocation: table ready")
        except Exception as exc:
            logger.error("run_migrations_cost_center_allocation failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


_LOCK_NORMALISE_SUPPLIER_NIFS = 202720


def run_migrations_normalise_supplier_nifs():
    """Normalise all existing non-null NIF values in suppliers: strip spaces/dashes, uppercase.

    Execution order:
    1. Detect groups of suppliers whose NIFs would collide after normalisation.
       For each collision group keep the row with the most invoices (lowest id as
       tiebreaker), re-link all invoices from the losers to the survivor, then
       delete the loser rows.
    2. UPDATE every remaining non-null NIF that is not already in normal form.
    3. Verify: count any non-normalised NIFs still present and log a warning if
       any remain (indicates a constraint or trigger blocked the update).

    Idempotent: once data is already clean both steps above match zero rows and
    complete as no-ops.  An advisory lock (202720) ensures concurrent Gunicorn
    workers skip rather than race on the first startup.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_NORMALISE_SUPPLIER_NIFS,))
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_normalise_supplier_nifs: lock held by another worker, skipping")
                return

            # Step 1 — resolve collisions before touching the unique index
            cursor.execute("""
                SELECT normalized_nif,
                       array_agg(id ORDER BY invoice_count DESC, id ASC) AS ids
                FROM (
                    SELECT s.id,
                           UPPER(REPLACE(REPLACE(s.nif, ' ', ''), '-', '')) AS normalized_nif,
                           COUNT(i.id) AS invoice_count
                    FROM suppliers s
                    LEFT JOIN invoices i ON i.supplier_id = s.id
                    WHERE s.nif IS NOT NULL
                    GROUP BY s.id
                ) sub
                GROUP BY normalized_nif
                HAVING COUNT(*) > 1
            """)
            collision_groups = cursor.fetchall()

            merged_suppliers = 0
            relinked_invoices = 0
            for _norm_nif, ids in collision_groups:
                survivor_id = ids[0]
                for dup_id in ids[1:]:
                    cursor.execute(
                        "UPDATE invoices SET supplier_id = %s WHERE supplier_id = %s",
                        (survivor_id, dup_id),
                    )
                    relinked_invoices += cursor.rowcount
                    cursor.execute("DELETE FROM suppliers WHERE id = %s", (dup_id,))
                    merged_suppliers += 1

            if merged_suppliers:
                logger.info(
                    "run_migrations_normalise_supplier_nifs: merged %d duplicate supplier(s), "
                    "%d invoice(s) re-linked",
                    merged_suppliers,
                    relinked_invoices,
                )

            # Step 2 — normalise every NIF not already in normal form
            cursor.execute("""
                UPDATE suppliers
                SET nif = UPPER(REPLACE(REPLACE(nif, ' ', ''), '-', ''))
                WHERE nif IS NOT NULL
                  AND nif IS DISTINCT FROM UPPER(REPLACE(REPLACE(nif, ' ', ''), '-', ''))
            """)
            updated = cursor.rowcount
            conn.commit()
            logger.info(
                "run_migrations_normalise_supplier_nifs: normalised %d NIF(s)", updated
            )

            # Step 3 — verify no non-normalised NIFs remain
            cursor.execute("""
                SELECT COUNT(*) FROM suppliers
                WHERE nif IS NOT NULL
                  AND nif IS DISTINCT FROM UPPER(REPLACE(REPLACE(nif, ' ', ''), '-', ''))
            """)
            remaining = cursor.fetchone()[0]
            if remaining:
                logger.warning(
                    "run_migrations_normalise_supplier_nifs: %d NIF(s) still not in normal form "
                    "after migration — manual inspection required",
                    remaining,
                )
            else:
                logger.info("run_migrations_normalise_supplier_nifs: verification passed — all NIFs normalised")

        except Exception as exc:
            logger.error(
                "run_migrations_normalise_supplier_nifs FAILED — supplier NIFs may remain "
                "inconsistent, manual cleanup required: %s",
                exc,
            )
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_NORMALISE_SUPPLIER_NIFS,))
                conn.commit()
            except Exception:
                pass


def run_migrations_suppliers_nullable_nif():
    """Allow suppliers.nif to be NULL (needed for suppliers that have no NIF).

    * Drops the NOT NULL constraint on suppliers.nif.
    * Replaces the column-level UNIQUE constraint with a partial unique index
      (WHERE nif IS NOT NULL) so that multiple null-NIF suppliers can coexist.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            # Drop NOT NULL
            cursor.execute("""
                ALTER TABLE suppliers ALTER COLUMN nif DROP NOT NULL
            """)
            # Replace the UNIQUE constraint with a partial index that ignores NULLs
            cursor.execute("""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'suppliers_nif_key'
                          AND conrelid = 'suppliers'::regclass
                    ) THEN
                        ALTER TABLE suppliers DROP CONSTRAINT suppliers_nif_key;
                    END IF;
                END $$;
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS suppliers_nif_unique
                    ON suppliers (nif)
                    WHERE nif IS NOT NULL
            """)
            # Before creating the name-uniqueness index, remove any duplicate
            # null-NIF suppliers (keeping the one with the most invoices, or the
            # lowest id as a tiebreaker).
            cursor.execute("""
                DELETE FROM suppliers
                WHERE nif IS NULL
                  AND id NOT IN (
                      SELECT DISTINCT ON (LOWER(name)) id
                      FROM suppliers
                      WHERE nif IS NULL
                      ORDER BY LOWER(name),
                               (SELECT COUNT(*) FROM invoices WHERE supplier_id = suppliers.id) DESC,
                               id ASC
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM invoices WHERE supplier_id = suppliers.id
                  )
            """)
            deduped = cursor.rowcount
            if deduped:
                logger.info(
                    "run_migrations_suppliers_nullable_nif: removed %d duplicate null-NIF supplier(s)", deduped)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS suppliers_name_no_nif_unique
                    ON suppliers (LOWER(name))
                    WHERE nif IS NULL
            """)
            conn.commit()
            logger.info("run_migrations_suppliers_nullable_nif: nif column made nullable, name uniqueness index created")
        except Exception as exc:
            logger.error("run_migrations_suppliers_nullable_nif failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


def run_migrations_onedrive_retry():
    """Idempotent: add onedrive_retry_at and onedrive_failed columns to invoices."""
    with db_connection() as conn:
        try:
            cursor = conn.cursor()
            cursor.execute(
                "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS onedrive_retry_at TIMESTAMP"
            )
            cursor.execute(
                "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS onedrive_failed BOOLEAN DEFAULT FALSE"
            )
            conn.commit()
            logger.info("run_migrations_onedrive_retry: columns ensured")
        except Exception as exc:
            logger.error("run_migrations_onedrive_retry failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


_LOCK_NORMALISE_PRODUCAO_SABORES = 202650


def run_migrations_normalise_producao_sabores():
    """Idempotent: normalise sabor names in the producao table.

    Fixes historical entries where OCR or manual input produced non-canonical
    names that differ from the nome_corrente values in receitas_gelado.
    Also ensures the Stracciatella Ruby recipe exists.
    Advisory lock 202650.

    Mapping applied:
      TIRAMISU                     → Tiramisu
      Flor de leite                → Fior di Latte
      Noz Pecan e Maple<space>     → Noz Pecan e Maple
      Base Chocolate Nivà          → Base chocolate niva
      GRANTORINO TUORLO ZUCCHERATO → Nocciolato
    """
    with db_connection() as conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_NORMALISE_PRODUCAO_SABORES,))
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_normalise_producao_sabores: lock held, skipping")
                return

            fixes = [
                # Original batch
                ("TIRAMISU",                     "Tiramisu"),
                ("Flor de leite",                "Fior di Latte"),
                ("Noz Pecan e Maple ",            "Noz Pecan e Maple"),
                ("Base Chocolate Nivà",           "Base chocolate niva"),
                ("GRANTORINO TUORLO ZUCCHERATO",  "Nocciolato"),
                # Capitalisation mismatches
                ("Extra noir",                   "Extra Noir"),
                ("Doce de leite",                "Doce de Leite"),
                ("Chocolate branco",             "Chocolate Branco"),
                ("Queijo da serra",              "Queijo da Serra"),
                ("Chocolate com laranja",        "Chocolate com Laranja"),
                ("Creme de natal",               "Creme de Natal"),
                # ALL NATURAL supplier-name → canonical nome_corrente
                ("ALL NATURAL MORANGO",                   "Morango"),
                ("ALL NATURAL CHOCOLATE HORTELA SORBETTO","Extra Noir Hortelã"),
                ("CARAMELLO DULCE DE LECHE FRANCISCO(1)", "Doce de Leite"),
                ("ALL NATURAL ANANAS ABACAXI",            "Abacaxi"),
                ("Ananás",                                "Abacaxi"),
                ("ALL NATURAL FRUTOS DO BOSQUE",          "Frutos Vermelhos"),
                ("Frutos do Bosque",                      "Frutos Vermelhos"),
                ("ZERO ALL NATURAL FRAGOLA",              "Morango"),
            ]
            total_updated = 0
            for old_name, new_name in fixes:
                cursor.execute(
                    "UPDATE producao SET sabor = %s WHERE sabor = %s",
                    (new_name, old_name),
                )
                n = cursor.rowcount
                if n > 0:
                    logger.info(
                        "run_migrations_normalise_producao_sabores: producao %r → %r (%d rows)",
                        old_name, new_name, n,
                    )
                total_updated += n

            # Align receitas_gelado.nome_corrente to match the canonical targets above.
            # The schema seed may insert lowercase variants; these UPDATEs ensure
            # receitas_gelado stays consistent with what producao.sabor now contains
            # — including on a fresh install.
            receita_fixes = [
                ("ALL NATURAL CHOCOLATE SORBETTO",         "Extra Noir"),
                ("ALL NATURAL CHOCOLATE HORTELA SORBETTO", "Extra Noir Hortelã"),
                ("CARAMELLO DULCE DE LECHE FRANCISCO",     "Doce de Leite"),
                ("CIOCCOBIANCO  RISOLATTE NEW",             "Chocolate Branco"),
                ("CHOCOLATE COM LARANJA",                   "Chocolate com Laranja"),
                ("EUSKALDUNA",                              "Queijo da Serra"),
                ("CREMA DI NATALE",                         "Creme de Natal"),
                ("ALL NATURAL MORANGO",                     "Morango"),
                ("ALL NATURAL ANANAS ABACAXI",              "Abacaxi"),
                ("ALL NATURAL FRUTOS DO BOSQUE",            "Frutos Vermelhos"),
                ("FIORDILATTE",                             "Fior di Latte"),
                ("GRANTORINO TUORLO ZUCCHERATO",            "Nocciolato"),
            ]
            for nome_receita, canonical in receita_fixes:
                cursor.execute(
                    "UPDATE receitas_gelado SET nome_corrente = %s WHERE nome = %s AND nome_corrente != %s",
                    (canonical, nome_receita, canonical),
                )
                if cursor.rowcount > 0:
                    logger.info(
                        "run_migrations_normalise_producao_sabores: receitas_gelado %r nome_corrente → %r",
                        nome_receita, canonical,
                    )

            # Ensure Stracciatella Ruby recipe exists — idempotent via ON CONFLICT.
            # NOTE: we use ON CONFLICT DO NOTHING on (nome) because the previous
            # WHERE NOT EXISTS guard had a race condition: two workers could both
            # pass the check and then one would hit the UNIQUE constraint on nome,
            # aborting the whole migration transaction.
            cursor.execute(
                """INSERT INTO receitas_gelado (nome, nome_corrente, ativo, conta_eurokg)
                   VALUES ('STRACCIATELLA RUBY', 'Stracciatella Ruby', TRUE, TRUE)
                   ON CONFLICT (nome) DO NOTHING""",
            )
            if cursor.rowcount > 0:
                logger.info("run_migrations_normalise_producao_sabores: inserted Stracciatella Ruby recipe")

            # General normalisation: update any producao.sabor that matches a
            # receita nome (case-insensitive) but is stored under the old nome
            # rather than the canonical nome_corrente.  This covers all past and
            # future aliases without needing per-pair hardcoded entries above.
            cursor.execute(
                """
                UPDATE producao p
                SET sabor = COALESCE(r.nome_corrente, r.nome)
                FROM receitas_gelado r
                WHERE UPPER(p.sabor) = UPPER(r.nome)
                  AND r.nome_corrente IS NOT NULL
                  AND r.nome_corrente != ''
                  AND p.sabor != r.nome_corrente
                """
            )
            general_updated = cursor.rowcount
            if general_updated > 0:
                logger.info(
                    "run_migrations_normalise_producao_sabores: general nome→nome_corrente normalisation updated %d producao rows",
                    general_updated,
                )
            total_updated += general_updated

            # Clean up orphan receitas_gelado rows: rows where nome_corrente IS NULL
            # and whose nome (case-insensitive, trimmed) matches an existing
            # nome_corrente from another row.  These orphans are created when a
            # CalybraBox upload encounters an unknown description and inserts a
            # placeholder entry; once the description is mapped to a canonical alias,
            # the orphan becomes a duplicate display name.  Safe to delete because
            # producao.sabor is already normalised to nome_corrente values and
            # nothing references the orphan row's id.
            cursor.execute(
                """
                DELETE FROM receitas_gelado AS orphan
                USING receitas_gelado AS canonical
                WHERE orphan.nome_corrente IS NULL
                  AND LOWER(TRIM(orphan.nome)) = LOWER(TRIM(canonical.nome_corrente))
                  AND orphan.id != canonical.id
                """
            )
            orphans_deleted = cursor.rowcount
            if orphans_deleted > 0:
                logger.info(
                    "run_migrations_normalise_producao_sabores: deleted %d orphan receitas_gelado rows whose nome matched an existing nome_corrente",
                    orphans_deleted,
                )

            conn.commit()
            logger.info(
                "run_migrations_normalise_producao_sabores: done (%d producao rows updated)",
                total_updated,
            )
        except Exception as exc:
            logger.error("run_migrations_normalise_producao_sabores failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


_LOCK_QUANTIDADE_KG_TO_NUMERIC = 202660


def run_migrations_quantidade_kg_to_numeric():
    """Idempotent: migrate quantidade_kg columns from REAL to NUMERIC(10,4).

    REAL (single-precision float, 4 bytes) accumulates floating-point errors
    that caused underflow bugs in stock subtraction operations.  NUMERIC(10,4)
    provides exact fixed-point arithmetic — no rounding workarounds required.

    Affected tables: stock_producao, stock_gelado, transferencias.
    Advisory lock 202660.
    """
    target_tables = ["stock_producao", "stock_gelado", "transferencias"]
    with db_connection() as conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_QUANTIDADE_KG_TO_NUMERIC,))
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_quantidade_kg_to_numeric: lock held by another worker, skipping")
                return

            for table in target_tables:
                cursor.execute("""
                    SELECT data_type FROM information_schema.columns
                    WHERE table_name = %s AND column_name = 'quantidade_kg'
                """, (table,))
                row = cursor.fetchone()
                if row is None:
                    logger.info(
                        "run_migrations_quantidade_kg_to_numeric: %s.quantidade_kg not found, skipping",
                        table,
                    )
                    continue
                if row[0].lower() == 'real':
                    cursor.execute(
                        f"ALTER TABLE {table} ALTER COLUMN quantidade_kg TYPE NUMERIC(10,4) USING quantidade_kg::numeric(10,4)"
                    )
                    logger.info(
                        "run_migrations_quantidade_kg_to_numeric: %s.quantidade_kg converted REAL → NUMERIC(10,4)",
                        table,
                    )
                else:
                    logger.info(
                        "run_migrations_quantidade_kg_to_numeric: %s.quantidade_kg already %s, skipping",
                        table, row[0],
                    )

            conn.commit()
            logger.info("run_migrations_quantidade_kg_to_numeric: done")
        except Exception as exc:
            logger.error("run_migrations_quantidade_kg_to_numeric failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


def run_migrations_loja_origem():
    """Add loja_origem column to ordens_transferencia (idempotent, advisory lock 202620).

    This column is set when a retail store initiates an outgoing gelado transfer
    (as opposed to production-initiated orders which leave it NULL).  The stock-
    movement ledger uses it as the canonical outbound source once populated.
    """
    with db_connection() as conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_try_advisory_lock(202620)")
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_loja_origem: lock held by another worker, skipping")
                return
            cursor.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'ordens_transferencia' AND column_name = 'loja_origem'
            """)
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE ordens_transferencia ADD COLUMN loja_origem VARCHAR(100)")
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ordens_loja_origem "
                    "ON ordens_transferencia (loja_origem) WHERE loja_origem IS NOT NULL"
                )
                logger.info("run_migrations_loja_origem: loja_origem column added")
            else:
                logger.info("run_migrations_loja_origem: loja_origem already present")
            conn.commit()
        except Exception as exc:
            logger.error("run_migrations_loja_origem failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


def run_migrations_transferencias_destino():
    """Add typed destinations to transfer orders (idempotent, advisory lock 202676)."""
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_lock(202676)")
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_transferencias_destino: lock held by another worker, skipping")
                return
            cursor.execute("""
                ALTER TABLE ordens_transferencia
                ADD COLUMN IF NOT EXISTS destino_tipo VARCHAR(20) NOT NULL DEFAULT 'loja'
            """)
            cursor.execute("""
                ALTER TABLE ordens_transferencia
                ADD COLUMN IF NOT EXISTS destino_nome VARCHAR(255)
            """)
            cursor.execute("""
                UPDATE ordens_transferencia
                SET destino_tipo = 'loja'
                WHERE destino_tipo IS NULL OR destino_tipo NOT IN ('loja', 'b2b')
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_ordens_transferencia_destino
                ON ordens_transferencia (destino_tipo, destino_nome)
            """)
            conn.commit()
            logger.info("run_migrations_transferencias_destino: destination columns ready")
        except Exception as exc:
            logger.error("run_migrations_transferencias_destino failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(202676)")
                conn.commit()
            except Exception:
                pass


def run_migrations_invoice_installments():
    """Idempotent: create invoice_installments table and add installment_total to invoices.

    Supports splitting invoice payment into N installments, each with its own
    amount, due date, paid date, and status.  Advisory lock 202623.
    """
    with db_connection() as conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_try_advisory_lock(202623)")
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_invoice_installments: lock held by another worker, skipping")
                return

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS invoice_installments (
                    id SERIAL PRIMARY KEY,
                    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
                    installment_number INTEGER NOT NULL,
                    total_installments INTEGER NOT NULL,
                    amount_eur NUMERIC(10,2) NOT NULL,
                    due_date DATE,
                    paid_date DATE,
                    status VARCHAR(30) NOT NULL DEFAULT 'pending_review',
                    paid_by VARCHAR(100),
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_inv_inst_invoice "
                "ON invoice_installments(invoice_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_inv_inst_status "
                "ON invoice_installments(status) WHERE status != 'paid'"
            )
            cursor.execute(
                "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS installment_total INTEGER DEFAULT 0"
            )
            conn.commit()
            logger.info("run_migrations_invoice_installments: done")
        except Exception as exc:
            logger.error("run_migrations_invoice_installments failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


def run_migrations_supplier_aliases():
    """Idempotent: create supplier_aliases and supplier_merge_ignored tables.

    supplier_aliases stores alternate names/NIFs (variants) for a canonical
    supplier so OCR can recognise any variant and redirect to the right record.
    Populated automatically by merge_supplier().

    supplier_merge_ignored records pairs the user has dismissed so they do not
    reappear in the "Possíveis duplicados" suggestions.

    Advisory lock 202625.
    """
    with db_connection() as conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_try_advisory_lock(202625)")
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_supplier_aliases: lock held by another worker, skipping")
                return

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS supplier_aliases (
                    id SERIAL PRIMARY KEY,
                    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
                    alias_name VARCHAR(255) NOT NULL,
                    alias_nif VARCHAR(20),
                    source VARCHAR(50) DEFAULT 'merge',
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(alias_name)
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_sup_alias_supplier "
                "ON supplier_aliases(supplier_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_sup_alias_name "
                "ON supplier_aliases(LOWER(alias_name))"
            )

            # supplier_id_a is always stored as the smaller of the two IDs
            # (enforced by the application), making (a, b) a valid PK for pair identity.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS supplier_merge_ignored (
                    supplier_id_a INTEGER NOT NULL,
                    supplier_id_b INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (supplier_id_a, supplier_id_b),
                    CHECK (supplier_id_a < supplier_id_b)
                )
            """)

            conn.commit()
            logger.info("run_migrations_supplier_aliases: done")
        except Exception as exc:
            logger.error("run_migrations_supplier_aliases failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


def run_migrations_invoice_centros_custo():
    """Idempotent: create invoice_centros_custo junction table for multi-CC allocation.

    Allows each invoice to be allocated to one or more cost centers with an
    optional percentage split. Advisory lock 202608.
    """
    with db_connection() as conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_try_advisory_lock(202608)")
            if not cursor.fetchone()[0]:
                return

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS invoice_centros_custo (
                    id SERIAL PRIMARY KEY,
                    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
                    centro_custo_id INTEGER NOT NULL REFERENCES cost_centers(id) ON DELETE CASCADE,
                    percentagem NUMERIC(5,2) NOT NULL DEFAULT 100.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(invoice_id, centro_custo_id)
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_icc_invoice ON invoice_centros_custo(invoice_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_icc_cc ON invoice_centros_custo(centro_custo_id)"
            )
            conn.commit()
            logger.info("run_migrations_invoice_centros_custo: table ensured")
        except Exception as exc:
            logger.error("run_migrations_invoice_centros_custo failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


_LOCK_INVOICE_STATUS_CONFIG = 202659
_LOCK_B2B = 202670

_STATUS_CONFIG_DEFAULTS = [
    ('draft',          'Rascunho',          'bg-secondary',         0, True),
    ('pending_review', 'Pendente revisão',  'bg-warning text-dark', 1, True),
    ('scheduled',      'Agendada',          'bg-info text-dark',    2, True),
    ('paid',           'Paga',              'bg-success',           3, True),
    ('overdue',        'Vencida',           'bg-danger',            4, True),
    ('cancelled',      'Cancelada',         'bg-secondary',         5, True),
]


def run_migrations_pdf_filename_backfill():
    """Back-fill pdf_filename for rows where pdf_data is present but pdf_filename is NULL.

    Runs once on startup; idempotent and safe to repeat.
    """
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE invoices
                SET pdf_filename = 'fatura.pdf'
                WHERE pdf_data IS NOT NULL
                  AND octet_length(pdf_data) > 0
                  AND pdf_filename IS NULL
            """)
            updated = cursor.rowcount
            conn.commit()
            if updated:
                logger.info(
                    "run_migrations_pdf_filename_backfill: back-filled pdf_filename for %d row(s)",
                    updated,
                )
    except Exception as exc:
        logger.error("run_migrations_pdf_filename_backfill failed: %s", exc)


def run_migrations_b2b():
    """Create clientes_b2b and faturas_clientes tables for B2B/Events invoicing."""
    with db_connection() as conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_B2B,))
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_b2b: lock held by another worker, skipping")
                return
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS clientes_b2b (
                    id         SERIAL PRIMARY KEY,
                    codigo     VARCHAR(100),
                    nome       VARCHAR(255) NOT NULL,
                    nif        VARCHAR(50)  NOT NULL UNIQUE,
                    tipo       VARCHAR(20)  NOT NULL DEFAULT 'b2b',
                    incluir_mapas BOOLEAN   NOT NULL DEFAULT TRUE,
                    criado_em  TIMESTAMP    NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP    NOT NULL DEFAULT NOW()
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS faturas_clientes (
                    id              SERIAL PRIMARY KEY,
                    cliente_id      INTEGER NOT NULL REFERENCES clientes_b2b(id) ON DELETE CASCADE,
                    numero          VARCHAR(100) NOT NULL UNIQUE,
                    data            DATE NOT NULL,
                    data_vencimento DATE,
                    documento       VARCHAR(100),
                    document_type   VARCHAR(30) NOT NULL DEFAULT 'fatura'
                        CHECK (document_type IN ('fatura', 'nota_credito', 'nota_debito')),
                    armazem         VARCHAR(100),
                    total_bruto     NUMERIC(12,2) NOT NULL DEFAULT 0,
                    total_liquido   NUMERIC(12,2) NOT NULL DEFAULT 0,
                    desconto_global NUMERIC(12,2) NOT NULL DEFAULT 0,
                    total_imposto   NUMERIC(12,2) NOT NULL DEFAULT 0,
                    total           NUMERIC(12,2) NOT NULL DEFAULT 0,
                    observacoes     TEXT,
                    anulado         BOOLEAN NOT NULL DEFAULT FALSE,
                    criado_em       TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at      TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_faturas_clientes_data ON faturas_clientes(data)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_faturas_clientes_cliente ON faturas_clientes(cliente_id)"
            )
            conn.commit()
            logger.info("run_migrations_b2b: complete")
        except Exception as exc:
            logger.error("run_migrations_b2b failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


_LOCK_FATURAS_CLIENTES_STATUS = 202671


def run_migrations_faturas_clientes_status():
    """Add status column (pendente/pago/vencido) to faturas_clientes.

    Advisory lock 202671 ensures only one worker runs the DDL.
    """
    with db_connection() as conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_FATURAS_CLIENTES_STATUS,))
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_faturas_clientes_status: lock held by another worker, skipping")
                return
            cursor.execute("""
                ALTER TABLE faturas_clientes
                ADD COLUMN IF NOT EXISTS status VARCHAR(20)
                    NOT NULL DEFAULT 'pendente'
                    CHECK (status IN ('pendente','pago','vencido'))
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_faturas_clientes_status
                    ON faturas_clientes(status)
            """)
            conn.commit()
            logger.info("run_migrations_faturas_clientes_status: complete")
        except Exception as exc:
            logger.error("run_migrations_faturas_clientes_status failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


_LOCK_FATURAS_CLIENTES_DATA_PAGAMENTO = 202672


def run_migrations_faturas_clientes_data_pagamento():
    """Add data_pagamento column (date payment was received) to faturas_clientes.

    Advisory lock 202672 ensures only one worker runs the DDL.
    """
    with db_connection() as conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_FATURAS_CLIENTES_DATA_PAGAMENTO,))
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_faturas_clientes_data_pagamento: lock held by another worker, skipping")
                return
            cursor.execute("""
                ALTER TABLE faturas_clientes
                ADD COLUMN IF NOT EXISTS data_pagamento DATE
            """)
            conn.commit()
            logger.info("run_migrations_faturas_clientes_data_pagamento: complete")
        except Exception as exc:
            logger.error("run_migrations_faturas_clientes_data_pagamento failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


_LOCK_FATURAS_CLIENTES_DOCUMENT_TYPE = 202674


def run_migrations_faturas_clientes_document_type():
    """Store the source document type and repair types from earlier imports."""
    with db_connection() as conn:
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (_LOCK_FATURAS_CLIENTES_DOCUMENT_TYPE,),
            )
            if not cursor.fetchone()[0]:
                logger.info(
                    "run_migrations_faturas_clientes_document_type: lock held by another worker, skipping"
                )
                return
            cursor.execute("""
                ALTER TABLE faturas_clientes
                ADD COLUMN IF NOT EXISTS document_type VARCHAR(30)
                    NOT NULL DEFAULT 'fatura'
            """)
            cursor.execute("""
                UPDATE faturas_clientes
                   SET armazem = document_type,
                       document_type = armazem
                 WHERE document_type NOT IN ('fatura', 'nota_credito', 'nota_debito')
                   AND armazem IN ('fatura', 'nota_credito', 'nota_debito')
            """)
            repaired_swapped_rows = cursor.rowcount
            cursor.execute("""
                UPDATE faturas_clientes
                   SET document_type = CASE
                       WHEN LOWER(COALESCE(documento, '')) LIKE '%nota%cr%'
                            OR LOWER(numero) LIKE 'nc %'
                            OR LOWER(numero) LIKE 'nc/%'
                         THEN 'nota_credito'
                       WHEN LOWER(COALESCE(documento, '')) LIKE '%nota%d%b%'
                            OR LOWER(numero) LIKE 'nd %'
                            OR LOWER(numero) LIKE 'nd/%'
                         THEN 'nota_debito'
                       ELSE 'fatura'
                   END
                 WHERE (
                       document_type = 'fatura'
                       AND (
                           LOWER(COALESCE(documento, '')) LIKE '%nota%cr%'
                           OR LOWER(COALESCE(documento, '')) LIKE '%nota%d%b%'
                           OR LOWER(numero) LIKE 'nc %'
                           OR LOWER(numero) LIKE 'nc/%'
                           OR LOWER(numero) LIKE 'nd %'
                           OR LOWER(numero) LIKE 'nd/%'
                       )
                 )
            """)
            classified_legacy_rows = cursor.rowcount
            cursor.execute("""
                SELECT 1
                  FROM pg_constraint
                 WHERE conrelid = 'faturas_clientes'::regclass
                   AND conname = 'faturas_clientes_document_type_check'
            """)
            if not cursor.fetchone():
                cursor.execute("""
                    ALTER TABLE faturas_clientes
                    ADD CONSTRAINT faturas_clientes_document_type_check
                    CHECK (document_type IN ('fatura', 'nota_credito', 'nota_debito'))
                """)
            conn.commit()
            logger.info(
                "run_migrations_faturas_clientes_document_type: complete "
                "(legacy classified=%d, swapped rows repaired=%d)",
                classified_legacy_rows,
                repaired_swapped_rows,
            )
        except Exception as exc:
            logger.error("run_migrations_faturas_clientes_document_type failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


_LOCK_PRODUTO_ALIASES = 202673


def run_migrations_produto_aliases():
    """Create produtos_vendas_aliases table and seed the three historical renames.

    Maps old POS product names to their current active equivalent so the
    Dashboard de Vendas can aggregate them together in the Top/Bottom rankings.
    Advisory lock 202673 ensures only one worker runs the DDL.
    """
    _SEED = [
        ('Copo Mini',    'Copo Piccolo'),
        ('Copo Pequeno', 'Copo Classico'),
        ('Cone Pequeno', 'Cone Classico'),
    ]
    with db_connection() as conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_PRODUTO_ALIASES,))
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_produto_aliases: lock held by another worker, skipping")
                return
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS produtos_vendas_aliases (
                    id          SERIAL PRIMARY KEY,
                    nome_antigo TEXT NOT NULL UNIQUE,
                    nome_atual  TEXT NOT NULL,
                    created_at  TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_pva_nome_antigo "
                "ON produtos_vendas_aliases(nome_antigo)"
            )
            for nome_antigo, nome_atual in _SEED:
                cursor.execute(
                    "INSERT INTO produtos_vendas_aliases (nome_antigo, nome_atual) "
                    "VALUES (%s, %s) ON CONFLICT (nome_antigo) DO NOTHING",
                    (nome_antigo, nome_atual),
                )
            conn.commit()
            logger.info("run_migrations_produto_aliases: done")
        except Exception as exc:
            logger.error("run_migrations_produto_aliases failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


def run_migrations_invoice_status_config():
    """Create invoice_status_config table and seed default statuses if empty.

    Advisory lock 202659 ensures only one worker runs the DDL.
    """
    with db_connection() as conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_INVOICE_STATUS_CONFIG,))
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_invoice_status_config: lock held by another worker, skipping")
                return
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS invoice_status_config (
                    key        TEXT PRIMARY KEY,
                    label      TEXT NOT NULL,
                    bg_class   TEXT NOT NULL DEFAULT 'bg-secondary',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    active     BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("SELECT COUNT(*) FROM invoice_status_config")
            if cursor.fetchone()[0] == 0:
                for key, label, bg_class, sort_order, active in _STATUS_CONFIG_DEFAULTS:
                    cursor.execute(
                        """INSERT INTO invoice_status_config (key, label, bg_class, sort_order, active)
                           VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                        (key, label, bg_class, sort_order, active),
                    )
            conn.commit()
            logger.info("run_migrations_invoice_status_config: complete")
        except Exception as exc:
            logger.error("run_migrations_invoice_status_config failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


_LOCK_INVOICE_PAYMENT_AUDIT = 202681


def run_migrations_invoice_payment_audit():
    """Ensure invoice_payments has confirmed_by; backfill records for paid invoices that lack one.

    Advisory lock 202681 ensures idempotency across workers.
    """
    with db_connection() as conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_INVOICE_PAYMENT_AUDIT,))
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_invoice_payment_audit: lock held by another worker, skipping")
                return

            # Ensure confirmed_by column exists (added after initial table creation)
            cursor.execute(
                "ALTER TABLE invoice_payments ADD COLUMN IF NOT EXISTS confirmed_by VARCHAR(100)"
            )

            # Backfill: one retroactive record per paid invoice that has no invoice_payments row
            cursor.execute("""
                INSERT INTO invoice_payments
                    (invoice_id, paid_date, status, confirmed_by, notes, updated_at)
                SELECT
                    i.id,
                    COALESCE(i.paid_date, i.updated_at::date, NOW()::date),
                    'paid',
                    'migração',
                    'Registo criado retroactivamente — origem desconhecida',
                    NOW()
                FROM invoices i
                WHERE i.status = 'paid'
                  AND NOT EXISTS (
                      SELECT 1 FROM invoice_payments ip WHERE ip.invoice_id = i.id
                  )
                ON CONFLICT (invoice_id) DO NOTHING
            """)
            n = cursor.rowcount
            conn.commit()
            if n > 0:
                logger.info("run_migrations_invoice_payment_audit: backfilled %d retroactive payment records", n)
            else:
                logger.info("run_migrations_invoice_payment_audit: no backfill needed")
        except Exception as exc:
            logger.error("run_migrations_invoice_payment_audit failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


_LOCK_CONTABILIDADE = 202680


def run_migrations_contabilidade():
    """Add accounting columns to invoices, create tickets tables, add acesso_contabilidade to users.

    Advisory lock 202680 ensures only one worker runs the DDL.
    """
    with db_connection() as conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_CONTABILIDADE,))
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_contabilidade: lock held by another worker, skipping")
                return

            # ── invoices: accounting columns ───────────────────────────────
            cursor.execute("""
                ALTER TABLE invoices
                    ADD COLUMN IF NOT EXISTS accounting_status      VARCHAR(30)
                        DEFAULT 'por_contabilizar',
                    ADD COLUMN IF NOT EXISTS accounting_notes       TEXT,
                    ADD COLUMN IF NOT EXISTS accounting_updated_by  VARCHAR(100),
                    ADD COLUMN IF NOT EXISTS accounting_updated_at  TIMESTAMP
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_invoices_accounting_status "
                "ON invoices(accounting_status)"
            )

            # ── users: acesso_contabilidade ───────────────────────────────
            cursor.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                "acesso_contabilidade BOOLEAN DEFAULT FALSE"
            )

            # ── contabilidade_tickets ─────────────────────────────────────
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS contabilidade_tickets (
                    id         SERIAL PRIMARY KEY,
                    invoice_id INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
                    titulo     TEXT NOT NULL,
                    descricao  TEXT,
                    prazo      DATE,
                    status     VARCHAR(20) NOT NULL DEFAULT 'aberto',
                    criado_por VARCHAR(100) NOT NULL,
                    criado_em  TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_ct_status "
                "ON contabilidade_tickets(status)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_ct_invoice_id "
                "ON contabilidade_tickets(invoice_id)"
            )

            # ── contabilidade_ticket_respostas ────────────────────────────
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS contabilidade_ticket_respostas (
                    id          SERIAL PRIMARY KEY,
                    ticket_id   INTEGER NOT NULL
                                    REFERENCES contabilidade_tickets(id) ON DELETE CASCADE,
                    mensagem    TEXT NOT NULL,
                    novo_status VARCHAR(20),
                    criado_por  VARCHAR(100) NOT NULL,
                    criado_em   TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_ctr_ticket_id "
                "ON contabilidade_ticket_respostas(ticket_id)"
            )

            conn.commit()
            logger.info("run_migrations_contabilidade: complete")
        except Exception as exc:
            logger.error("run_migrations_contabilidade failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


_LOCK_SUPPLIER_ENTIDADE_GOV = 202701


def run_migrations_supplier_entidade_governamental():
    """Add entidade_governamental flag to suppliers and pre-tag known government NIFs.

    The flag is used by the Compras invoice list to exclude AT/SS documents
    (which belong in Financeiro, not Compras).  Known NIFs pre-tagged:
      500757155 — Autoridade Tributária (AT)
      506826066 — Segurança Social (SS)
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_SUPPLIER_ENTIDADE_GOV,))
        if not cursor.fetchone()[0]:
            logger.info("run_migrations_supplier_entidade_governamental: lock held, skipping")
            return
        try:
            # Add column if missing
            cursor.execute("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name = 'suppliers' AND column_name = 'entidade_governamental'
            """)
            if cursor.fetchone()[0] == 0:
                cursor.execute("""
                    ALTER TABLE suppliers
                    ADD COLUMN entidade_governamental BOOLEAN NOT NULL DEFAULT false
                """)
                logger.info("run_migrations_supplier_entidade_governamental: column added")

            # Pre-tag known government NIFs
            _GOV_NIFS = ['500757155', '506826066']
            cursor.execute("""
                UPDATE suppliers
                SET entidade_governamental = true
                WHERE nif = ANY(%s) AND entidade_governamental = false
            """, (_GOV_NIFS,))
            if cursor.rowcount:
                logger.info(
                    "run_migrations_supplier_entidade_governamental: tagged %d govt suppliers",
                    cursor.rowcount,
                )
            conn.commit()
            logger.info("run_migrations_supplier_entidade_governamental: complete")
        except Exception as exc:
            logger.error("run_migrations_supplier_entidade_governamental failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


_LOCK_SUPPLIER_CENTRO_CUSTO = 202702

_LOCK_SUPPLIER_CATEGORIA_CUSTO = 202704


def run_migrations_supplier_categoria_custo():
    """Add categoria_custo_id FK to suppliers (default cost category per supplier).

    Advisory lock 202704.  Column add is idempotent.  Best-effort backfill from
    the legacy text `category` field: an ILIKE match against cost_categories.name
    fills rows where the name matches exactly (case-insensitive).
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_SUPPLIER_CATEGORIA_CUSTO,))
        if not cursor.fetchone()[0]:
            logger.info("run_migrations_supplier_categoria_custo: lock held, skipping")
            return
        try:
            cursor.execute("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name = 'suppliers' AND column_name = 'categoria_custo_id'
            """)
            if cursor.fetchone()[0] == 0:
                cursor.execute("""
                    ALTER TABLE suppliers
                    ADD COLUMN categoria_custo_id INTEGER
                        REFERENCES cost_categories(id) ON DELETE SET NULL
                """)
                logger.info("run_migrations_supplier_categoria_custo: column added")
                # Best-effort backfill from legacy text category field (only if column still exists)
                cursor.execute("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'suppliers' AND column_name = 'category'
                """)
                if cursor.fetchone() is not None:
                    cursor.execute("""
                        UPDATE suppliers s
                        SET categoria_custo_id = cc.id
                        FROM cost_categories cc
                        WHERE s.categoria_custo_id IS NULL
                          AND s.category IS NOT NULL
                          AND cc.name ILIKE s.category
                    """)
                    affected = cursor.rowcount
                    if affected:
                        logger.info(
                            "run_migrations_supplier_categoria_custo: backfilled %d suppliers",
                            affected,
                        )
            conn.commit()
            logger.info("run_migrations_supplier_categoria_custo: complete")
        except Exception as exc:
            logger.error("run_migrations_supplier_categoria_custo failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


_LOCK_COST_CATEGORY_IS_CMVMC = 202703


def run_migrations_cost_category_is_cmvmc():
    """Add is_cmvmc flag to cost_categories and perform a one-time legacy seed.

    Advisory lock 202703.

    The name-pattern seed (marking existing "Matéria Prima" / "cmvmc" categories)
    runs ONLY when the column is first added to the schema.  On subsequent startups
    the column already exists so the block is skipped entirely, preserving any
    administrator changes made via the UI.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_COST_CATEGORY_IS_CMVMC,))
        if not cursor.fetchone()[0]:
            logger.info("run_migrations_cost_category_is_cmvmc: lock held, skipping")
            return
        try:
            cursor.execute("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name = 'cost_categories' AND column_name = 'is_cmvmc'
            """)
            if cursor.fetchone()[0] == 0:
                # Column does not exist yet — add it and seed legacy categories.
                cursor.execute("""
                    ALTER TABLE cost_categories
                    ADD COLUMN is_cmvmc BOOLEAN NOT NULL DEFAULT FALSE
                """)
                logger.info("run_migrations_cost_category_is_cmvmc: column added")

                # One-time seed: categories whose name matched the old heuristic
                # are pre-marked as CMVMC so existing data is not disrupted.
                # This block never runs again after this startup.
                cursor.execute("""
                    UPDATE cost_categories
                    SET is_cmvmc = TRUE
                    WHERE LOWER(name) LIKE '%mat_ria%'
                       OR LOWER(name) LIKE '%materia%'
                       OR LOWER(name) LIKE '%cmvmc%'
                """)
                affected = cursor.rowcount
                logger.info(
                    "run_migrations_cost_category_is_cmvmc: seeded is_cmvmc=TRUE for %d categories",
                    affected,
                )
            else:
                logger.info("run_migrations_cost_category_is_cmvmc: column exists, skipping seed")

            conn.commit()
            logger.info("run_migrations_cost_category_is_cmvmc: complete")
        except Exception as exc:
            logger.error("run_migrations_cost_category_is_cmvmc failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


_LOCK_CUSTOS_RECORRENTES = 202620


def run_migrations_custos_recorrentes():
    """Create the custos_recorrentes table (replaces avencas + debitos_directos JSON).

    Advisory lock 202620.  Runs after run_migrations_supplier_centro_custo so
    the suppliers and cost_centers tables are guaranteed to exist.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_CUSTOS_RECORRENTES,))
        if not cursor.fetchone()[0]:
            logger.info("run_migrations_custos_recorrentes: lock held, skipping")
            return
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS custos_recorrentes (
                    id               SERIAL PRIMARY KEY,
                    supplier_id      INTEGER        NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
                    tipologia        VARCHAR(20)    NOT NULL
                                     CHECK (tipologia IN ('fixo', 'variavel')),
                    frequencia       VARCHAR(20)    NOT NULL
                                     CHECK (frequencia IN ('semanal','quinzenal','mensal',
                                                           'trimestral','semestral','anual')),
                    data_cobranca    DATE           NOT NULL,
                    centro_custo_id  INTEGER        REFERENCES cost_centers(id) ON DELETE SET NULL,
                    valor            NUMERIC(12,2),
                    notas            TEXT,
                    ativo            BOOLEAN        NOT NULL DEFAULT TRUE,
                    created_at       TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
                    updated_at       TIMESTAMPTZ    NOT NULL DEFAULT NOW()
                )
            """)
            conn.commit()
            logger.info("run_migrations_custos_recorrentes: table ready")
        except Exception as exc:
            logger.error("run_migrations_custos_recorrentes failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
            raise  # fail loudly — do not start with a missing table
        finally:
            try:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_CUSTOS_RECORRENTES,))
                conn.commit()
            except Exception:
                pass


def run_migrations_supplier_centro_custo():
    """Add centro_custo_id FK to suppliers (default cost centre per supplier).

    Runs after run_migrations_centros_custo so cost_centers table is guaranteed
    to exist.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_SUPPLIER_CENTRO_CUSTO,))
        if not cursor.fetchone()[0]:
            logger.info("run_migrations_supplier_centro_custo: lock held, skipping")
            return
        try:
            cursor.execute("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name = 'suppliers' AND column_name = 'centro_custo_id'
            """)
            if cursor.fetchone()[0] == 0:
                cursor.execute("""
                    ALTER TABLE suppliers
                    ADD COLUMN centro_custo_id INTEGER REFERENCES cost_centers(id) ON DELETE SET NULL
                """)
                logger.info("run_migrations_supplier_centro_custo: column added")
            conn.commit()
            logger.info("run_migrations_supplier_centro_custo: complete")
        except Exception as exc:
            logger.error("run_migrations_supplier_centro_custo failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


_LOCK_DROP_SUPPLIER_CATEGORY = 685685


def run_migrations_drop_supplier_category():
    """Idempotent: drop the legacy suppliers.category free-text column.

    The column is no longer read or written by any code path; the FK
    categoria_custo_id is the authoritative category reference.  Dropping it
    removes any risk of stale free-text values causing confusion.

    Uses advisory lock 685685 to serialise concurrent worker executions.
    Safe to call repeatedly — IF NOT EXISTS / column-existence checks make it
    a no-op once the column is gone.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", (_LOCK_DROP_SUPPLIER_CATEGORY,))
            if not cursor.fetchone()[0]:
                logger.info("run_migrations_drop_supplier_category: lock held, skipping")
                return
            # Check whether the column still exists before attempting DROP
            cursor.execute("""
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'suppliers'
                  AND column_name = 'category'
            """)
            if cursor.fetchone() is None:
                logger.info("run_migrations_drop_supplier_category: column already absent, nothing to do")
                return
            cursor.execute("ALTER TABLE suppliers DROP COLUMN IF EXISTS category")
            conn.commit()
            logger.info("run_migrations_drop_supplier_category: suppliers.category dropped")
        except Exception as exc:
            logger.error("run_migrations_drop_supplier_category failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


_LOCK_BACKFILL_INVOICE_CATEGORIA_CUSTO = 202705


def run_backfill_invoice_categoria_custo():
    """Set invoices.categoria_custo_id from the linked supplier's default category.

    Advisory lock 202705 (transaction-scoped — released automatically on
    commit or rollback, safe for connection pools).  Only touches non-draft
    invoices where categoria_custo_id IS NULL and supplier_id IS NOT NULL and
    the supplier itself has a default categoria_custo_id set.
    Idempotent — safe to run more than once.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        # pg_try_advisory_xact_lock releases automatically at transaction end,
        # so it is safe in connection pools (no lock leak between requests).
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", (_LOCK_BACKFILL_INVOICE_CATEGORIA_CUSTO,))
        if not cursor.fetchone()[0]:
            logger.info("run_backfill_invoice_categoria_custo: lock held, skipping")
            return
        try:
            cursor.execute("""
                UPDATE invoices i
                SET categoria_custo_id = s.categoria_custo_id
                FROM suppliers s
                WHERE i.supplier_id = s.id
                  AND i.categoria_custo_id IS NULL
                  AND s.categoria_custo_id IS NOT NULL
                  AND COALESCE(i.status, '') != 'draft'
            """)
            affected = cursor.rowcount
            conn.commit()
            logger.info(
                "run_backfill_invoice_categoria_custo: updated %d invoices", affected
            )
        except Exception as exc:
            logger.error("run_backfill_invoice_categoria_custo failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass


_LOCK_ACESSO_COMPRAS = 202710


def run_migrations_acesso_compras():
    """Add acesso_compras column to users table.

    Backfills TRUE for users with role='compras' so they retain access.
    Advisory lock 202710 for idempotency across workers.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_ACESSO_COMPRAS,))
        if not cursor.fetchone()[0]:
            logger.info("run_migrations_acesso_compras: lock held by another worker, skipping")
            return

        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'users' AND column_name = 'acesso_compras'
        """)
        if cursor.fetchone()[0] == 0:
            cursor.execute("ALTER TABLE users ADD COLUMN acesso_compras BOOLEAN DEFAULT FALSE")
            logger.info("run_migrations_acesso_compras: added acesso_compras column")

        # Backfill: gestores, admins, and role='compras' users get TRUE
        cursor.execute("""
            UPDATE users SET acesso_compras = TRUE
            WHERE (acesso_gestor = TRUE OR acesso_administrativo = TRUE OR role = 'compras')
              AND (acesso_compras IS NULL OR acesso_compras = FALSE)
        """)
        affected = cursor.rowcount
        if affected:
            logger.info("run_migrations_acesso_compras: backfilled %d user(s)", affected)
        cursor.execute("UPDATE users SET acesso_compras = FALSE WHERE acesso_compras IS NULL")
        conn.commit()


def _backfill_initial_product_dose_ranges(cursor, product_ids=None):
    """Extend only each product/article's first dose to its first stable-ID sale.

    Later versions remain untouched. A shared first rule is extended to the
    earliest sale among the products whose first association uses that rule.
    """
    product_scope = ""
    params = ()
    if product_ids:
        product_scope = "AND produto_vendas_config_id=ANY(%s)"
        params = (list(product_ids),)

    cursor.execute(f"""
        WITH first_sales AS (
            SELECT produto_vendas_config_id AS product_id,
                   MIN(data) AS first_sale
            FROM vendas_detalhe
            WHERE produto_vendas_config_id IS NOT NULL
              {product_scope}
            GROUP BY produto_vendas_config_id
        ),
        first_associations AS (
            SELECT DISTINCT ON (prd.produto_vendas_config_id)
                   prd.id AS association_id,
                   prd.produto_vendas_config_id AS product_id,
                   prd.regra_dose_id,
                   prd.valid_from AS association_start
            FROM produto_regra_dose_historico prd
            ORDER BY prd.produto_vendas_config_id, prd.valid_from, prd.id
        ),
        eligible AS (
            SELECT first_assoc.regra_dose_id,
                   MIN(first_sales.first_sale) AS new_start
            FROM first_associations first_assoc
            JOIN first_sales
              ON first_sales.product_id=first_assoc.product_id
            JOIN gramas_gelado_historico hist
              ON hist.id=first_assoc.regra_dose_id
            WHERE first_sales.first_sale < hist.valid_from
              AND NOT EXISTS (
                  SELECT 1
                  FROM gramas_gelado_historico earlier
                  WHERE LOWER(BTRIM(earlier.artigo))=
                        LOWER(BTRIM(hist.artigo))
                    AND earlier.valid_from < hist.valid_from
              )
            GROUP BY first_assoc.regra_dose_id
        )
        UPDATE gramas_gelado_historico hist
        SET valid_from=eligible.new_start
        FROM eligible
        WHERE hist.id=eligible.regra_dose_id
          AND eligible.new_start < hist.valid_from
    """, params)
    rules_extended = cursor.rowcount

    cursor.execute(f"""
        WITH first_sales AS (
            SELECT produto_vendas_config_id AS product_id,
                   MIN(data) AS first_sale
            FROM vendas_detalhe
            WHERE produto_vendas_config_id IS NOT NULL
              {product_scope}
            GROUP BY produto_vendas_config_id
        ),
        first_associations AS (
            SELECT DISTINCT ON (prd.produto_vendas_config_id)
                   prd.id AS association_id,
                   prd.produto_vendas_config_id AS product_id,
                   prd.regra_dose_id,
                   prd.valid_from AS association_start
            FROM produto_regra_dose_historico prd
            ORDER BY prd.produto_vendas_config_id, prd.valid_from, prd.id
        )
        UPDATE produto_regra_dose_historico prd
        SET valid_from=first_sales.first_sale
        FROM first_associations first_assoc
        JOIN first_sales
          ON first_sales.product_id=first_assoc.product_id
        JOIN gramas_gelado_historico hist
          ON hist.id=first_assoc.regra_dose_id
        WHERE prd.id=first_assoc.association_id
          AND first_sales.first_sale < first_assoc.association_start
          AND NOT EXISTS (
              SELECT 1
              FROM gramas_gelado_historico earlier
              WHERE LOWER(BTRIM(earlier.artigo))=LOWER(BTRIM(hist.artigo))
                AND earlier.valid_from < hist.valid_from
          )
    """, params)
    associations_extended = cursor.rowcount
    return rules_extended, associations_extended


def _backfill_alias_initial_product_dose_ranges(cursor):
    """Extend canonical first rules across exact product rename aliases.

    This is deliberately conservative: only a single canonical configuration
    with one first association/rule is changed, and only when that rule is not
    shared with another product.  Alias rows remain historical presentation
    identities; no association is created for them.
    """
    cursor.execute("""
        SELECT DISTINCT canonical.id
        FROM produtos_vendas_aliases alias
        JOIN produtos_vendas_config canonical
          ON canonical.produto=alias.nome_atual
        WHERE NOT EXISTS (
            SELECT 1
            FROM produtos_vendas_aliases alias_target
            WHERE alias_target.nome_antigo=canonical.produto
        )
        ORDER BY canonical.id
    """)
    canonical_ids = [row[0] for row in cursor.fetchall()]
    rules_extended = 0
    associations_extended = 0

    for product_id in canonical_ids:
        try:
            family_ids = get_product_family_ids(cursor, product_id)
        except ValueError as exc:
            logger.warning(
                "alias dose migration skipped product %s: %s",
                product_id,
                exc,
            )
            continue
        if len(family_ids) <= 1:
            continue

        cursor.execute("""
            SELECT MIN(data)
            FROM vendas_detalhe
            WHERE produto_vendas_config_id=ANY(%s)
        """, (family_ids,))
        first_sale = cursor.fetchone()[0]
        if first_sale is None:
            continue

        cursor.execute("""
            SELECT prd.id, prd.regra_dose_id, prd.valid_from, prd.valid_to,
                   hist.valid_from AS rule_from, hist.valid_to AS rule_to,
                   hist.artigo
            FROM produto_regra_dose_historico prd
            JOIN gramas_gelado_historico hist
              ON hist.id=prd.regra_dose_id
            WHERE prd.produto_vendas_config_id=%s
            ORDER BY prd.valid_from, prd.id
            LIMIT 1
        """, (product_id,))
        first = cursor.fetchone()
        if not first or first_sale >= first[2]:
            continue
        (
            association_id,
            rule_id,
            association_from,
            association_to,
            rule_from,
            rule_to,
            article,
        ) = first
        if (
            (association_to is not None and association_to < first_sale)
            or (rule_to is not None and rule_to < first_sale)
        ):
            continue

        cursor.execute("""
            SELECT 1
            FROM gramas_gelado_historico earlier
            WHERE LOWER(BTRIM(earlier.artigo))=LOWER(BTRIM(%s))
              AND earlier.valid_from < %s
            LIMIT 1
        """, (article, rule_from))
        if cursor.fetchone():
            continue

        cursor.execute("""
            SELECT 1
            FROM produto_regra_dose_historico other
            WHERE other.regra_dose_id=%s
              AND other.produto_vendas_config_id<>%s
            LIMIT 1
        """, (rule_id, product_id))
        if cursor.fetchone():
            logger.warning(
                "alias dose migration skipped shared rule %s for product %s",
                rule_id,
                product_id,
            )
            continue

        cursor.execute("""
            UPDATE gramas_gelado_historico
            SET valid_from=%s
            WHERE id=%s AND valid_from=%s
        """, (first_sale, rule_id, rule_from))
        rules_extended += cursor.rowcount
        cursor.execute("""
            UPDATE produto_regra_dose_historico
            SET valid_from=%s
            WHERE id=%s AND valid_from=%s
        """, (first_sale, association_id, association_from))
        associations_extended += cursor.rowcount

    return rules_extended, associations_extended


def run_migrations_doseamento_gelado():
    """Version gelato dose rules so historical sales keep their original grams."""
    with db_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ('schema:doseamento-gelado',),
            )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS gramas_gelado_historico (
                    id SERIAL PRIMARY KEY,
                    artigo VARCHAR(255) NOT NULL,
                    gramas NUMERIC(10,3),
                    tipo_dose VARCHAR(20) NOT NULL DEFAULT 'fixa',
                    valid_from DATE NOT NULL,
                    valid_to DATE,
                    created_by VARCHAR(100),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT gramas_gelado_historico_tipo_check
                        CHECK (tipo_dose IN ('fixa', 'peso')),
                    CONSTRAINT gramas_gelado_historico_gramas_check
                        CHECK (
                            (tipo_dose = 'fixa' AND gramas > 0)
                            OR (tipo_dose = 'peso' AND gramas IS NULL)
                        ),
                    CONSTRAINT gramas_gelado_historico_datas_check
                        CHECK (valid_to IS NULL OR valid_to >= valid_from)
                )
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_gramas_gelado_historico_artigo_ativo
                ON gramas_gelado_historico (LOWER(BTRIM(artigo)))
                WHERE valid_to IS NULL
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS
                    idx_gramas_gelado_historico_vigencia
                ON gramas_gelado_historico (
                    LOWER(BTRIM(artigo)), valid_from, valid_to
                )
            """)
            cursor.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
            cursor.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'gramas_gelado_historico_sem_sobreposicao'
                    ) THEN
                        ALTER TABLE gramas_gelado_historico
                        ADD CONSTRAINT gramas_gelado_historico_sem_sobreposicao
                        EXCLUDE USING gist (
                            (LOWER(BTRIM(artigo))) WITH =,
                            (daterange(
                                valid_from,
                                COALESCE(valid_to, 'infinity'::date),
                                '[]'
                            )) WITH &&
                        );
                    END IF;
                END
                $$;
            """)
            cursor.execute("""
                ALTER TABLE gramas_gelado_historico
                ADD COLUMN IF NOT EXISTS evidence_reference TEXT
            """)
            cursor.execute("""
                ALTER TABLE gramas_gelado_historico
                ADD COLUMN IF NOT EXISTS import_batch_id UUID
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS gramas_gelado_import_audit (
                    id UUID PRIMARY KEY,
                    created_by VARCHAR(100) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    source_name TEXT,
                    row_count INTEGER NOT NULL CHECK (row_count > 0),
                    rows_json JSONB NOT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_gramas_gelado_import_audit_recent
                ON gramas_gelado_import_audit (created_at DESC)
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS gramas_gelado_import_preview (
                    id UUID PRIMARY KEY,
                    created_by VARCHAR(100) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMPTZ NOT NULL,
                    source_name TEXT,
                    rows_json JSONB NOT NULL,
                    changes_json JSONB NOT NULL,
                    history_hash VARCHAR(64) NOT NULL,
                    consumed_at TIMESTAMPTZ
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_gramas_gelado_import_preview_expiry
                ON gramas_gelado_import_preview (expires_at)
                WHERE consumed_at IS NULL
            """)
            cursor.execute("""
                ALTER TABLE vendas_detalhe
                ADD COLUMN IF NOT EXISTS peso_vendido_kg NUMERIC(12,4)
            """)
            cursor.execute("""
                ALTER TABLE vendas_detalhe
                ADD COLUMN IF NOT EXISTS produto_vendas_config_id INTEGER
                    REFERENCES produtos_vendas_config(id) ON DELETE RESTRICT
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_vendas_detalhe_produto_config
                ON vendas_detalhe (produto_vendas_config_id, data)
            """)
            cursor.execute("""
                ALTER TABLE produtos_vendas_config
                ADD COLUMN IF NOT EXISTS dose_config_pendente BOOLEAN
                    NOT NULL DEFAULT FALSE
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS produto_regra_dose_historico (
                    id BIGSERIAL PRIMARY KEY,
                    produto_vendas_config_id INTEGER NOT NULL
                        REFERENCES produtos_vendas_config(id) ON DELETE RESTRICT,
                    regra_dose_id INTEGER NOT NULL
                        REFERENCES gramas_gelado_historico(id) ON DELETE RESTRICT,
                    valid_from DATE NOT NULL,
                    valid_to DATE,
                    created_by VARCHAR(100),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT produto_regra_dose_datas_check
                        CHECK (valid_to IS NULL OR valid_to >= valid_from)
                )
            """)
            cursor.execute("""
                ALTER TABLE produto_regra_dose_historico
                DROP CONSTRAINT IF EXISTS
                    produto_regra_dose_historico_produto_vendas_config_id_regra_key
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_produto_regra_dose_vigencia
                ON produto_regra_dose_historico
                    (produto_vendas_config_id, valid_from, valid_to)
            """)
            cursor.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'produto_regra_dose_sem_sobreposicao'
                    ) THEN
                        ALTER TABLE produto_regra_dose_historico
                        ADD CONSTRAINT produto_regra_dose_sem_sobreposicao
                        EXCLUDE USING gist (
                            produto_vendas_config_id WITH =,
                            daterange(
                                valid_from,
                                COALESCE(valid_to, 'infinity'::date),
                                '[]'
                            ) WITH &&
                        );
                    END IF;
                END
                $$;
            """)
            cursor.execute("""
                ALTER TABLE vendas_detalhe
                ALTER COLUMN quantidade TYPE NUMERIC(12,3)
                USING quantidade::NUMERIC(12,3)
            """)
            cursor.execute("""
                INSERT INTO gramas_gelado_historico (
                    artigo, gramas, tipo_dose, valid_from
                )
                SELECT artigo, gramas, 'fixa', CURRENT_DATE
                FROM gramas_gelado atual
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM gramas_gelado_historico hist
                    WHERE LOWER(BTRIM(hist.artigo)) =
                          LOWER(BTRIM(atual.artigo))
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS app_schema_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cursor.execute("""
                SELECT 1 FROM app_schema_migrations
                WHERE name = 'doseamento_seed_cutoff_v2'
            """)
            if not cursor.fetchone():
                cursor.execute("""
                    UPDATE gramas_gelado_historico
                    SET valid_from = CURRENT_DATE
                    WHERE valid_from = DATE '2000-01-01'
                """)
                cursor.execute("""
                    INSERT INTO app_schema_migrations (name)
                    VALUES ('doseamento_seed_cutoff_v2')
                """)
            cursor.execute("""
                UPDATE vendas_detalhe vd
                SET produto_vendas_config_id = pvc.id
                FROM produtos_vendas_config pvc
                WHERE vd.produto_vendas_config_id IS NULL
                  AND LOWER(BTRIM(vd.produto)) = LOWER(BTRIM(pvc.produto))
            """)
            cursor.execute("""
                SELECT 1 FROM app_schema_migrations
                WHERE name = 'doseamento_explicit_product_rules_v1'
            """)
            if not cursor.fetchone():
                cursor.execute("""
                    WITH families AS (
                        SELECT pvc.id AS product_id,
                               LOWER(BTRIM(hist.artigo)) AS family,
                               MAX(LENGTH(BTRIM(hist.artigo))) AS pattern_length
                        FROM produtos_vendas_config pvc
                        JOIN gramas_gelado_historico hist
                          ON LOWER(pvc.produto) LIKE
                             '%%' || LOWER(BTRIM(hist.artigo)) || '%%'
                        WHERE pvc.gelado_kpi = TRUE
                        GROUP BY pvc.id, LOWER(BTRIM(hist.artigo))
                    ),
                    ranked AS (
                        SELECT *,
                               MAX(pattern_length) OVER (
                                   PARTITION BY product_id
                               ) AS max_length
                        FROM families
                    ),
                    winners AS (
                        SELECT *, COUNT(*) OVER (
                            PARTITION BY product_id
                        ) AS winner_count
                        FROM ranked WHERE pattern_length=max_length
                    )
                    INSERT INTO produto_regra_dose_historico (
                        produto_vendas_config_id, regra_dose_id,
                        valid_from, valid_to, created_by
                    )
                    SELECT winner.product_id, hist.id,
                           hist.valid_from, hist.valid_to, 'migração'
                    FROM winners winner
                    JOIN gramas_gelado_historico hist
                      ON LOWER(BTRIM(hist.artigo))=winner.family
                    WHERE winner.winner_count = 1
                """)
                cursor.execute("""
                    UPDATE produtos_vendas_config pvc
                    SET dose_config_pendente = TRUE, gelado_kpi = FALSE
                    WHERE pvc.gelado_kpi = TRUE
                      AND NOT EXISTS (
                          SELECT 1 FROM produto_regra_dose_historico prd
                          WHERE prd.produto_vendas_config_id = pvc.id
                      )
                """)
                cursor.execute("""
                    INSERT INTO app_schema_migrations (name)
                    VALUES ('doseamento_explicit_product_rules_v1')
                """)
            cursor.execute("""
                SELECT 1 FROM app_schema_migrations
                WHERE name = 'doseamento_explicit_product_rules_v2'
            """)
            if not cursor.fetchone():
                cursor.execute("""
                    INSERT INTO produtos_vendas_config (
                        produto, dose_config_pendente
                    )
                    SELECT DISTINCT vd.produto, EXISTS (
                        SELECT 1 FROM regras_negocio rn
                        WHERE rn.area='gelado_kpi'
                          AND LOWER(vd.produto) LIKE
                              '%%' || LOWER(rn.palavra_chave) || '%%'
                    )
                    FROM vendas_detalhe vd
                    WHERE NOT EXISTS (
                        SELECT 1 FROM produtos_vendas_config pvc
                        WHERE pvc.produto=vd.produto
                    )
                    ON CONFLICT (produto) DO NOTHING
                """)
                cursor.execute("""
                    UPDATE vendas_detalhe vd
                    SET produto_vendas_config_id=pvc.id
                    FROM produtos_vendas_config pvc
                    WHERE vd.produto_vendas_config_id IS NULL
                      AND pvc.produto=vd.produto
                """)
                cursor.execute("""
                    WITH ambiguous AS (
                        SELECT prd.produto_vendas_config_id
                        FROM produto_regra_dose_historico prd
                        JOIN gramas_gelado_historico hist
                          ON hist.id=prd.regra_dose_id
                        WHERE prd.created_by='migração'
                        GROUP BY prd.produto_vendas_config_id
                        HAVING COUNT(DISTINCT LOWER(BTRIM(hist.artigo))) > 1
                    )
                    DELETE FROM produto_regra_dose_historico prd
                    USING ambiguous
                    WHERE prd.produto_vendas_config_id=
                          ambiguous.produto_vendas_config_id
                      AND prd.created_by='migração'
                """)
                cursor.execute("""
                    UPDATE produtos_vendas_config pvc
                    SET gelado_kpi=FALSE, dose_config_pendente=TRUE
                    WHERE pvc.gelado_kpi=TRUE
                      AND NOT EXISTS (
                          SELECT 1 FROM produto_regra_dose_historico prd
                          WHERE prd.produto_vendas_config_id=pvc.id
                      )
                """)
                cursor.execute("""
                    INSERT INTO app_schema_migrations (name)
                    VALUES ('doseamento_explicit_product_rules_v2')
                """)
            cursor.execute("""
                SELECT 1 FROM app_schema_migrations
                WHERE name='doseamento_first_rule_full_history_v1'
            """)
            if not cursor.fetchone():
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("dose-config-writes",),
                )
                rules_extended, associations_extended = (
                    _backfill_initial_product_dose_ranges(cursor)
                )
                cursor.execute("""
                    INSERT INTO app_schema_migrations (name)
                    VALUES ('doseamento_first_rule_full_history_v1')
                """)
                logger.info(
                    "run_migrations_doseamento_gelado: extended %d first "
                    "dose rule(s) and %d product association(s)",
                    rules_extended,
                    associations_extended,
                )
            cursor.execute("""
                DROP TRIGGER IF EXISTS trg_enforce_gelado_kpi_dose_rule
                ON produtos_vendas_config
            """)
            cursor.execute("""
                DROP FUNCTION IF EXISTS enforce_gelado_kpi_dose_rule()
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS produtos_vendas_aliases (
                    id SERIAL PRIMARY KEY,
                    nome_antigo TEXT NOT NULL UNIQUE,
                    nome_atual TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cursor.execute("""
                INSERT INTO produtos_vendas_aliases (nome_antigo, nome_atual)
                VALUES
                    ('Açai Bow', 'Açaí Bowl'),
                    ('Açai Bowl', 'Açaí Bowl')
                ON CONFLICT (nome_antigo) DO UPDATE
                SET nome_atual=EXCLUDED.nome_atual
            """)
            cursor.execute("""
                SELECT 1 FROM app_schema_migrations
                WHERE name='doseamento_alias_first_rule_full_history_v1'
            """)
            if not cursor.fetchone():
                alias_rules_extended, alias_associations_extended = (
                    _backfill_alias_initial_product_dose_ranges(cursor)
                )
                cursor.execute("""
                    INSERT INTO app_schema_migrations (name)
                    VALUES ('doseamento_alias_first_rule_full_history_v1')
                """)
                logger.info(
                    "run_migrations_doseamento_gelado: extended %d alias "
                    "dose rule(s) and %d product association(s)",
                    alias_rules_extended,
                    alias_associations_extended,
                )
            cursor.execute("""
                SELECT 1 FROM app_schema_migrations
                WHERE name='doseamento_acai_identity_v1'
            """)
            if not cursor.fetchone():
                cursor.execute("""
                    UPDATE produtos_vendas_config canonical
                    SET gelado_kpi=TRUE
                    WHERE canonical.produto='Açaí Bowl'
                      AND EXISTS (
                        SELECT 1
                        FROM produtos_vendas_config variant
                        WHERE variant.produto IN (
                            'Açai Bow', 'Açai Bowl', 'Açaí Bowl'
                        )
                          AND (
                            variant.gelado_kpi=TRUE
                            OR EXISTS (
                              SELECT 1
                              FROM produto_regra_dose_historico prd
                              WHERE prd.produto_vendas_config_id=variant.id
                                AND CURRENT_DATE BETWEEN prd.valid_from
                                    AND COALESCE(
                                        prd.valid_to, 'infinity'::date
                                    )
                            )
                          )
                      )
                """)
                cursor.execute("""
                    UPDATE produtos_vendas_config
                    SET gelado_kpi=FALSE, dose_config_pendente=FALSE
                    WHERE produto IN ('Açai Bow', 'Açai Bowl')
                      AND EXISTS (
                        SELECT 1 FROM produtos_vendas_config canonical
                        WHERE canonical.produto='Açaí Bowl'
                      )
                """)
                cursor.execute("""
                    INSERT INTO app_schema_migrations (name)
                    VALUES ('doseamento_acai_identity_v1')
                """)
            cursor.execute("""
                UPDATE produtos_vendas_config pvc
                SET gelado_kpi=TRUE, dose_config_pendente=FALSE
                WHERE EXISTS (
                    SELECT 1 FROM produto_regra_dose_historico prd
                    WHERE prd.produto_vendas_config_id=pvc.id
                      AND CURRENT_DATE BETWEEN prd.valid_from
                          AND COALESCE(prd.valid_to, 'infinity'::date)
                )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM produtos_vendas_aliases alias
                    JOIN produtos_vendas_config canonical
                      ON canonical.produto=alias.nome_atual
                    WHERE alias.nome_antigo=pvc.produto
                  )
            """)
            cursor.execute("""
                UPDATE produtos_vendas_config pvc
                SET dose_config_pendente=TRUE
                WHERE pvc.gelado_kpi=TRUE
                  AND NOT EXISTS (
                    SELECT 1 FROM produto_regra_dose_historico prd
                    WHERE prd.produto_vendas_config_id=pvc.id
                      AND CURRENT_DATE BETWEEN prd.valid_from
                          AND COALESCE(prd.valid_to, 'infinity'::date)
                )
            """)
            cursor.execute("""
                UPDATE produtos_vendas_config
                SET dose_config_pendente=FALSE
                WHERE gelado_kpi=FALSE
            """)
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("run_migrations_doseamento_gelado failed")
            raise

_LOCK_COST_CENTERS_STORE_ID = 202695


def run_migrations_cost_centers_store_id():
    """Add store_id FK to cost_centers and backfill from name-matching.

    Adds: cost_centers.store_id INTEGER REFERENCES stores(id)
    Backfill: for each CC whose name (case-insensitive) matches an active store,
              set store_id to that store's PK.

    Advisory lock 202695 for idempotency across workers.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_COST_CENTERS_STORE_ID,))
        if not cursor.fetchone()[0]:
            logger.info("run_migrations_cost_centers_store_id: lock held by another worker, skipping")
            return

        # Add column if missing
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'cost_centers' AND column_name = 'store_id'
        """)
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                ALTER TABLE cost_centers
                ADD COLUMN store_id INTEGER REFERENCES stores(id) ON DELETE SET NULL
            """)
            logger.info("run_migrations_cost_centers_store_id: added store_id column")

        # Backfill: match CC name to store name (case-insensitive)
        cursor.execute("""
            UPDATE cost_centers cc
            SET store_id = s.id
            FROM stores s
            WHERE LOWER(cc.name) = LOWER(s.name)
              AND s.is_active = TRUE
              AND cc.store_id IS NULL
        """)
        affected = cursor.rowcount
        if affected:
            logger.info(
                "run_migrations_cost_centers_store_id: backfilled store_id for %d cost center(s)",
                affected,
            )

        conn.commit()


_LOCK_PASTELARIA_PLANO = 202716
_LOCK_PASTELARIA_PRODUCT_STATE_AUDIT = 202717
_LOCK_PASTELARIA_COUNT_PRODUCT_ID = 202718


def run_migrations_pastelaria_plano():
    """Add structured cake configuration fields and configurable cake sizes."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", (_LOCK_PASTELARIA_PLANO,))
        if not cursor.fetchone()[0]:
            logger.info("run_migrations_pastelaria_plano: lock held, skipping")
            return

        for column, definition in (
            ("item_tipo", "VARCHAR(20) NOT NULL DEFAULT 'standard'"),
            ("bolo_tamanho", "VARCHAR(20)"),
            ("bolo_sabor_1", "VARCHAR(255)"),
            ("bolo_sabor_2", "VARCHAR(255)"),
            ("bolo_sabor_3", "VARCHAR(255)"),
            ("bolo_cobertura", "VARCHAR(255)"),
        ):
            cursor.execute(
                f"ALTER TABLE plano_producao_pastelaria "
                f"ADD COLUMN IF NOT EXISTS {column} {definition}"
            )

        cursor.execute("""
            UPDATE plano_producao_pastelaria
            SET item_tipo = 'standard'
            WHERE item_tipo IS NULL
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tamanhos_bolo_pastelaria (
                id SERIAL PRIMARY KEY,
                nome VARCHAR(20) NOT NULL UNIQUE,
                ativo BOOLEAN NOT NULL DEFAULT TRUE,
                ordem INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for ordem, nome in enumerate(("11 cm", "22 cm", "26 cm"), start=1):
            cursor.execute("""
                INSERT INTO tamanhos_bolo_pastelaria (nome, ordem)
                VALUES (%s, %s)
                ON CONFLICT (nome) DO NOTHING
            """, (nome, ordem))
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pastelaria_stock_minimos (
                produto_id INTEGER NOT NULL REFERENCES produtos_pastelaria(id) ON DELETE RESTRICT,
                store_id INTEGER NOT NULL REFERENCES stores(id),
                quantidade_minima INTEGER NOT NULL DEFAULT 0 CHECK (quantidade_minima >= 0),
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (produto_id, store_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pastelaria_planos_prioridade (
                id SERIAL PRIMARY KEY,
                data_contagem DATE NOT NULL,
                versao INTEGER NOT NULL DEFAULT 1,
                generated_by VARCHAR(255),
                generated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute(
            "ALTER TABLE pastelaria_planos_prioridade "
            "ADD COLUMN IF NOT EXISTS versao INTEGER NOT NULL DEFAULT 1"
        )
        cursor.execute(
            "ALTER TABLE pastelaria_planos_prioridade "
            "DROP CONSTRAINT IF EXISTS pastelaria_planos_prioridade_data_contagem_key"
        )
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                uq_pastelaria_planos_prioridade_data_versao
            ON pastelaria_planos_prioridade(data_contagem, versao)
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pastelaria_plano_prioridade_linhas (
                id SERIAL PRIMARY KEY,
                plano_id INTEGER NOT NULL REFERENCES pastelaria_planos_prioridade(id) ON DELETE CASCADE,
                produto_id INTEGER REFERENCES produtos_pastelaria(id) ON DELETE SET NULL,
                produto VARCHAR(500) NOT NULL,
                stock_minimo_total INTEGER NOT NULL,
                stock_contado_total INTEGER NOT NULL,
                quantidade_total INTEGER NOT NULL,
                percentagem_falta NUMERIC(8,5) NOT NULL,
                prioridade INTEGER NOT NULL,
                distribuicao_lojas JSONB NOT NULL DEFAULT '[]'::jsonb,
                UNIQUE (plano_id, produto_id)
            )
        """)
        cursor.execute("""
            ALTER TABLE pastelaria_plano_prioridade_linhas
            ALTER COLUMN produto_id DROP NOT NULL
        """)
        cursor.execute("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname='pastelaria_stock_minimos_produto_id_fkey'
                      AND confdeltype <> 'r'
                ) THEN
                    ALTER TABLE pastelaria_stock_minimos
                    DROP CONSTRAINT pastelaria_stock_minimos_produto_id_fkey;
                    ALTER TABLE pastelaria_stock_minimos
                    ADD CONSTRAINT pastelaria_stock_minimos_produto_id_fkey
                    FOREIGN KEY (produto_id) REFERENCES produtos_pastelaria(id)
                    ON DELETE RESTRICT;
                END IF;
            END $$;
        """)
        cursor.execute("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname='pastelaria_plano_prioridade_linhas_produto_id_fkey'
                      AND confdeltype <> 'n'
                ) THEN
                    ALTER TABLE pastelaria_plano_prioridade_linhas
                    DROP CONSTRAINT pastelaria_plano_prioridade_linhas_produto_id_fkey;
                    ALTER TABLE pastelaria_plano_prioridade_linhas
                    ADD CONSTRAINT pastelaria_plano_prioridade_linhas_produto_id_fkey
                    FOREIGN KEY (produto_id) REFERENCES produtos_pastelaria(id)
                    ON DELETE SET NULL;
                END IF;
            END $$;
        """)
        conn.commit()
        logger.info("run_migrations_pastelaria_plano: schema ready")


def run_migrations_pastelaria_count_product_id():
    """Attach Pastelaria stock counts to stable catalogue product identities."""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pg_try_advisory_xact_lock(%s)",
            (_LOCK_PASTELARIA_COUNT_PRODUCT_ID,),
        )
        if not cursor.fetchone()[0]:
            logger.info(
                "run_migrations_pastelaria_count_product_id: lock held, skipping"
            )
            return

        cursor.execute("""
            ALTER TABLE contagem_stock
            ADD COLUMN IF NOT EXISTS produto_pastelaria_id INTEGER
        """)
        cursor.execute("""
            ALTER TABLE ordens_transferencia
            ADD COLUMN IF NOT EXISTS produto_pastelaria_id INTEGER
        """)
        cursor.execute("""
            ALTER TABLE producao_pastelaria
            ADD COLUMN IF NOT EXISTS produto_pastelaria_id INTEGER
        """)
        cursor.execute("""
            ALTER TABLE plano_producao_pastelaria
            ADD COLUMN IF NOT EXISTS produto_pastelaria_id INTEGER
        """)
        cursor.execute("""
            ALTER TABLE stock_producao_pastelaria
            ADD COLUMN IF NOT EXISTS produto_pastelaria_id INTEGER
        """)
        cursor.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname='contagem_stock_produto_pastelaria_id_fkey'
                ) THEN
                    ALTER TABLE contagem_stock
                    ADD CONSTRAINT contagem_stock_produto_pastelaria_id_fkey
                    FOREIGN KEY (produto_pastelaria_id)
                    REFERENCES produtos_pastelaria(id) ON DELETE SET NULL;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname='ordens_transferencia_produto_pastelaria_id_fkey'
                ) THEN
                    ALTER TABLE ordens_transferencia
                    ADD CONSTRAINT ordens_transferencia_produto_pastelaria_id_fkey
                    FOREIGN KEY (produto_pastelaria_id)
                    REFERENCES produtos_pastelaria(id) ON DELETE SET NULL;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname='producao_pastelaria_produto_pastelaria_id_fkey'
                ) THEN
                    ALTER TABLE producao_pastelaria
                    ADD CONSTRAINT producao_pastelaria_produto_pastelaria_id_fkey
                    FOREIGN KEY (produto_pastelaria_id)
                    REFERENCES produtos_pastelaria(id) ON DELETE SET NULL;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname='plano_producao_pastelaria_produto_id_fkey'
                ) THEN
                    ALTER TABLE plano_producao_pastelaria
                    ADD CONSTRAINT plano_producao_pastelaria_produto_id_fkey
                    FOREIGN KEY (produto_pastelaria_id)
                    REFERENCES produtos_pastelaria(id) ON DELETE SET NULL;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname='stock_producao_pastelaria_produto_id_fkey'
                ) THEN
                    ALTER TABLE stock_producao_pastelaria
                    ADD CONSTRAINT stock_producao_pastelaria_produto_id_fkey
                    FOREIGN KEY (produto_pastelaria_id)
                    REFERENCES produtos_pastelaria(id) ON DELETE SET NULL;
                END IF;
            END $$;
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS app_schema_migrations (
                name VARCHAR(255) PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        migration_name = 'pastelaria_count_product_id_backfill_v1'
        cursor.execute(
            "SELECT 1 FROM app_schema_migrations WHERE name=%s FOR UPDATE",
            (migration_name,),
        )
        if cursor.fetchone() is None:
            cursor.execute("""
                WITH catalogue_labels AS (
                    SELECT id,
                           CONCAT_WS(', ',
                               NULLIF(BTRIM(tipologia), ''),
                               NULLIF(BTRIM(sabor), ''),
                               NULLIF(BTRIM(cobertura), '')
                           ) AS label
                    FROM produtos_pastelaria
                ),
                unique_labels AS (
                    SELECT MIN(id) AS id, label
                    FROM catalogue_labels
                    GROUP BY label
                    HAVING COUNT(*) = 1
                )
                UPDATE contagem_stock cs
                SET produto_pastelaria_id = ul.id
                FROM unique_labels ul
                WHERE cs.tipo = 'pastelaria'
                  AND cs.produto_pastelaria_id IS NULL
                  AND cs.produto = ul.label
            """)
            cursor.execute(
                "INSERT INTO app_schema_migrations (name) VALUES (%s)",
                (migration_name,),
            )
        order_migration_name = 'pastelaria_transfer_product_id_backfill_v1'
        cursor.execute(
            "SELECT 1 FROM app_schema_migrations WHERE name=%s FOR UPDATE",
            (order_migration_name,),
        )
        if cursor.fetchone() is None:
            cursor.execute("""
                WITH catalogue_labels AS (
                    SELECT id,
                           CONCAT_WS(', ',
                               NULLIF(BTRIM(tipologia), ''),
                               NULLIF(BTRIM(sabor), ''),
                               NULLIF(BTRIM(cobertura), '')
                           ) AS label
                    FROM produtos_pastelaria
                ),
                unique_labels AS (
                    SELECT MIN(id) AS id, label
                    FROM catalogue_labels
                    GROUP BY label
                    HAVING COUNT(*) = 1
                )
                UPDATE ordens_transferencia ot
                SET produto_pastelaria_id = ul.id
                FROM unique_labels ul
                WHERE LOWER(ot.area_origem) = 'pastelaria'
                  AND ot.produto_pastelaria_id IS NULL
                  AND ot.produto = ul.label
            """)
            cursor.execute(
                "INSERT INTO app_schema_migrations (name) VALUES (%s)",
                (order_migration_name,),
            )
        production_migration_name = 'pastelaria_production_product_id_backfill_v1'
        cursor.execute(
            "SELECT 1 FROM app_schema_migrations WHERE name=%s FOR UPDATE",
            (production_migration_name,),
        )
        if cursor.fetchone() is None:
            cursor.execute("""
                WITH catalogue_labels AS (
                    SELECT id,
                           CONCAT_WS(', ',
                               NULLIF(BTRIM(tipologia), ''),
                               NULLIF(BTRIM(sabor), ''),
                               NULLIF(BTRIM(cobertura), '')
                           ) AS label
                    FROM produtos_pastelaria
                ),
                unique_labels AS (
                    SELECT MIN(id) AS id, label
                    FROM catalogue_labels
                    GROUP BY label
                    HAVING COUNT(*) = 1
                )
                UPDATE producao_pastelaria pp
                SET produto_pastelaria_id = ul.id
                FROM unique_labels ul
                WHERE pp.produto_pastelaria_id IS NULL
                  AND pp.produto = ul.label
            """)
            cursor.execute(
                "INSERT INTO app_schema_migrations (name) VALUES (%s)",
                (production_migration_name,),
            )
        support_migration_name = 'pastelaria_support_stock_product_id_backfill_v1'
        cursor.execute(
            "SELECT 1 FROM app_schema_migrations WHERE name=%s FOR UPDATE",
            (support_migration_name,),
        )
        if cursor.fetchone() is None:
            for table_name in (
                'plano_producao_pastelaria',
                'stock_producao_pastelaria',
            ):
                cursor.execute(f"""
                    WITH catalogue_labels AS (
                        SELECT id,
                               CONCAT_WS(', ',
                                   NULLIF(BTRIM(tipologia), ''),
                                   NULLIF(BTRIM(sabor), ''),
                                   NULLIF(BTRIM(cobertura), '')
                               ) AS label
                        FROM produtos_pastelaria
                    ),
                    unique_labels AS (
                        SELECT MIN(id) AS id, label
                        FROM catalogue_labels
                        GROUP BY label
                        HAVING COUNT(*) = 1
                    )
                    UPDATE {table_name} source
                    SET produto_pastelaria_id = ul.id
                    FROM unique_labels ul
                    WHERE source.produto_pastelaria_id IS NULL
                      AND source.produto = ul.label
                """)
            cursor.execute(
                "INSERT INTO app_schema_migrations (name) VALUES (%s)",
                (support_migration_name,),
            )
        dedupe_migration_name = 'pastelaria_support_identity_dedupe_v1'
        cursor.execute(
            "SELECT 1 FROM app_schema_migrations WHERE name=%s FOR UPDATE",
            (dedupe_migration_name,),
        )
        if cursor.fetchone() is None:
            cursor.execute("""
                WITH grouped AS (
                    SELECT data, produto_pastelaria_id, MAX(id) AS keeper_id,
                           MAX(producao_estimada) AS producao_estimada,
                           MAX(producao_real) AS producao_real,
                           BOOL_OR(no_plano) AS no_plano,
                           MAX(producao_estimada_bolhao)
                               AS producao_estimada_bolhao,
                           MAX(producao_estimada_matosinhos)
                               AS producao_estimada_matosinhos
                    FROM plano_producao_pastelaria
                    WHERE produto_pastelaria_id IS NOT NULL
                    GROUP BY data, produto_pastelaria_id
                    HAVING COUNT(*) > 1
                )
                UPDATE plano_producao_pastelaria plan
                SET producao_estimada = grouped.producao_estimada,
                    producao_real = grouped.producao_real,
                    no_plano = grouped.no_plano,
                    producao_estimada_bolhao =
                        grouped.producao_estimada_bolhao,
                    producao_estimada_matosinhos =
                        grouped.producao_estimada_matosinhos
                FROM grouped
                WHERE plan.id=grouped.keeper_id
            """)
            cursor.execute("""
                DELETE FROM plano_producao_pastelaria plan
                USING plano_producao_pastelaria keeper
                WHERE plan.data=keeper.data
                  AND plan.produto_pastelaria_id=keeper.produto_pastelaria_id
                  AND plan.id < keeper.id
                  AND plan.produto_pastelaria_id IS NOT NULL
            """)
            cursor.execute("""
                WITH grouped AS (
                    SELECT data, produto_pastelaria_id, MAX(id) AS keeper_id,
                           SUM(quantidade) AS quantidade
                    FROM stock_producao_pastelaria
                    WHERE produto_pastelaria_id IS NOT NULL
                    GROUP BY data, produto_pastelaria_id
                    HAVING COUNT(*) > 1
                )
                UPDATE stock_producao_pastelaria stock
                SET quantidade=grouped.quantidade
                FROM grouped
                WHERE stock.id=grouped.keeper_id
            """)
            cursor.execute("""
                DELETE FROM stock_producao_pastelaria stock
                USING stock_producao_pastelaria keeper
                WHERE stock.data=keeper.data
                  AND stock.produto_pastelaria_id=keeper.produto_pastelaria_id
                  AND stock.id < keeper.id
                  AND stock.produto_pastelaria_id IS NOT NULL
            """)
            cursor.execute(
                "INSERT INTO app_schema_migrations (name) VALUES (%s)",
                (dedupe_migration_name,),
            )
        cursor.execute("""
            ALTER TABLE plano_producao_pastelaria
            DROP CONSTRAINT IF EXISTS
                plano_producao_pastelaria_data_produto_key
        """)
        cursor.execute("""
            ALTER TABLE stock_producao_pastelaria
            DROP CONSTRAINT IF EXISTS
                stock_producao_pastelaria_data_produto_key
        """)
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                idx_plano_pastelaria_data_product_identity
            ON plano_producao_pastelaria (data, produto_pastelaria_id)
            WHERE produto_pastelaria_id IS NOT NULL
        """)
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                idx_stock_pastelaria_data_product_identity
            ON stock_producao_pastelaria (data, produto_pastelaria_id)
            WHERE produto_pastelaria_id IS NOT NULL
        """)
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                idx_plano_pastelaria_date_unlinked_label
            ON plano_producao_pastelaria (data, produto)
            WHERE produto_pastelaria_id IS NULL
        """)
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                idx_stock_pastelaria_date_unlinked_label
            ON stock_producao_pastelaria (data, produto)
            WHERE produto_pastelaria_id IS NULL
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_contagem_stock_pastelaria_product
            ON contagem_stock (produto_pastelaria_id, loja, data DESC, id DESC)
            WHERE tipo = 'pastelaria'
        """)
        conn.commit()
        logger.info("run_migrations_pastelaria_count_product_id: schema ready")


def run_migrations_pastelaria_product_state_audit():
    """Create the audit trail for Pastelaria catalogue state changes.

    Existing products deliberately receive no rows here.  An audit row is
    written only by the state-change transaction after its optimistic
    concurrency check succeeds.
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT pg_try_advisory_xact_lock(%s)",
            (_LOCK_PASTELARIA_PRODUCT_STATE_AUDIT,),
        )
        if not cursor.fetchone()[0]:
            logger.info(
                "run_migrations_pastelaria_product_state_audit: lock held, skipping"
            )
            return

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pastelaria_produto_estado_audit (
                id BIGSERIAL PRIMARY KEY,
                produto_id INTEGER NOT NULL
                    REFERENCES produtos_pastelaria(id) ON DELETE RESTRICT,
                actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                actor_username VARCHAR(100) NOT NULL,
                previous_active BOOLEAN NOT NULL,
                new_active BOOLEAN NOT NULL,
                changed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (previous_active <> new_active)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
                idx_pastelaria_produto_estado_audit_recent
            ON pastelaria_produto_estado_audit(changed_at DESC, id DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS
                idx_pastelaria_produto_estado_audit_product
            ON pastelaria_produto_estado_audit(produto_id, changed_at DESC, id DESC)
        """)
        conn.commit()
        logger.info("run_migrations_pastelaria_product_state_audit: schema ready")
