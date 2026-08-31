import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import logging
from db.connection import db_connection, logger, hash_password
import json
import os


_LOCK_STOCK_PRODUCAO_LOJAS = 202612
_LOCK_EVENTOS_V2_FOUNDATION = 2026821
_LOCK_EVENTOS_CUSTOMER_PORTAL = 2026822


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
                "CREATE INDEX IF NOT EXISTS idx_event_occurrences_event_date "
                "ON event_occurrences(event_date)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_resource_reservations_resource "
                "ON event_resource_reservations(resource_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_history_event_created "
                "ON event_history(event_id, created_at DESC)"
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
                    logistics_message TEXT,
                    sent_quote_version_id BIGINT REFERENCES event_quote_versions(id) ON DELETE RESTRICT,
                    accepted_quote_revision VARCHAR(128),
                    quote_accepted_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS event_portal_brand_configs (
                    id BIGSERIAL PRIMARY KEY,
                    store_id INTEGER NOT NULL UNIQUE REFERENCES stores(id) ON DELETE RESTRICT,
                    brand_name VARCHAR(120) NOT NULL DEFAULT 'Scoopy',
                    logo_filename VARCHAR(255),
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
            cursor.execute("""
                ALTER TABLE event_portal_requests
                ADD COLUMN IF NOT EXISTS access_code_hash VARCHAR(128)
            """)
            cursor.execute("""
                ALTER TABLE event_portal_requests
                ADD COLUMN IF NOT EXISTS sent_quote_version_id BIGINT
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
                quantidade INTEGER NOT NULL,
                valor_euros REAL,
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

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
                UNIQUE(data, produto)
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
                UNIQUE(data, produto)
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
            """, (produtos_to_fix,))
            pending = cursor.fetchone()[0]
            if pending == 0:
                logger.info("run_data_fix_gelado_kpi_classification: all products already correctly classified, skipping")
                return

            cursor.execute("""
                UPDATE produtos_vendas_config
                SET gelado_kpi = TRUE
                WHERE produto = ANY(%s) AND gelado_kpi = FALSE
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

    Must run AFTER run_data_fix_stock_gelado_dedup_and_unique so that the
    UNIQUE constraint uq_stock_gelado_data_loja_sabor_tipo already exists —
    the INSERT uses ON CONFLICT (data, loja, sabor, tipo) DO NOTHING which
    requires that constraint and is safe against concurrent writers.

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
                   ON CONFLICT (data, loja, sabor, tipo) DO NOTHING""",
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
                    event_type  VARCHAR(20) NOT NULL CHECK (event_type IN ('criado', 'confirmado', 'rejeitado')),
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
        conn.commit()
        logger.info("run_migrations_pastelaria_plano: schema ready")
