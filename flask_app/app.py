import os
import sys
import threading
import logging
import uuid
from flask import Flask, session, redirect, url_for, g, request, render_template
from functools import wraps

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from database import init_database, run_migrations, run_faturas_migrations, run_migrations_m0, run_migrations_forecast, run_migrations_wind_config, sync_produtos_vendas_config, seed_artigos_administrativos, authenticate_user, create_session
from db.cashflow import run_migrations_cashflow
from db.schema import (run_migrations_credito, run_data_fix_quebras_march2026,
                        run_data_fix_pesagem_april2026, run_migrations_centros_custo,
                        run_data_fix_delete_auto_quebras,
                        run_data_fix_pesagem_matosinhos_backfill,
                        run_data_fix_march1_dedup,
                        run_data_fix_gelado_kpi_classification,
                        run_data_fix_normalise_sabor_names,
                        run_data_fix_cremino_stock_producao,
                        run_data_fix_stock_gelado_march2026_dedup,
                        run_data_fix_stock_gelado_dedup_and_unique,
                        run_migrations_caixa_loja,
                        run_migrations_preco_caixa_kg,
                        run_migrations_stock_producao_lojas,
                        run_migrations_colaboradores_smart,
                        run_migrations_transferencias_motivo,
                        run_migrations_transferencias_eventos,
                        run_backfill_transferencias_eventos,
                        run_migrations_batch_id,
                        run_migrations_agente,
                        run_migrations_conta_vendas_diarias,
                        run_migrations_b2b_vendas_diarias,
                        run_migrations_stock_gelado_carapinas)
from db.tiles import run_migrations_tile_config
from db.pagamentos import run_migrations_tesouraria_manuais
from db.schema import run_migrations_custos_recorrentes
from db.schema import run_migrations_tarefas, run_migrations_tarefas_v2, run_migrations_tarefas_v3
from db.schema import run_migrations_fecho_caixa_audit
from db.schema import run_migrations_user_audit_log
from db.schema import run_migrations_cost_center_allocation
from db.schema import run_migrations_suppliers_nullable_nif, run_migrations_normalise_supplier_nifs
from db.schema import run_migrations_onedrive_retry
from db.schema import run_migrations_invoice_centros_custo, run_migrations_invoice_installments
from db.schema import run_migrations_supplier_aliases
from db.schema import run_migrations_normalise_producao_sabores
from db.schema import run_migrations_quantidade_kg_to_numeric
from db.schema import run_migrations_loja_origem, run_migrations_transferencias_destino
from db.schema import run_migrations_invoice_status_config, run_migrations_produto_aliases
from db.schema import run_migrations_pdf_filename_backfill
from db.schema import run_migrations_b2b, run_migrations_faturas_clientes_status
from db.schema import (
    run_migrations_faturas_clientes_data_pagamento,
    run_migrations_faturas_clientes_document_type,
)
from db.schema import run_migrations_contabilidade, run_migrations_invoice_payment_audit
from db.schema import (run_migrations_supplier_entidade_governamental,
                       run_migrations_cost_category_is_cmvmc,
                       run_migrations_supplier_categoria_custo)
from db.orcamento import run_migrations_orcamento
from db.faturas import run_migrations_saved_invoice_views, run_migrations_invoice_audit_complete
from db.faturas_clientes import promote_overdue as _promote_overdue_faturas_clientes
from db.schema import run_migrations_supplier_centro_custo, run_backfill_invoice_categoria_custo
from db.schema import run_migrations_drop_supplier_category, run_migrations_acesso_compras
from db.schema import run_migrations_cost_centers_store_id
from db.schema import (
    run_migrations_pastelaria_plano,
    run_migrations_pastelaria_count_product_id,
    run_migrations_pastelaria_product_state_audit,
)
from db.schema import run_migrations_eventos_v2_foundation, run_migrations_eventos_customer_portal


def _start_sheets_sync_scheduler():
    """Start the durable Sheets job worker and its daily 08:00 enqueue."""
    from flask_app.google_sheets_sync import start_sheet_sync_worker
    start_sheet_sync_worker(schedule_daily=True)


def _start_event_portal_cleanup_scheduler():
    """Remove expired private proof files once a day in the single scheduler worker."""
    def _worker():
        import time
        from flask_app.services.event_portal import cleanup_expired_portal_proofs

        upload_root = os.path.join(
            os.path.dirname(__file__), '..', 'private_uploads', 'event_proofs'
        )
        while True:
            try:
                removed = cleanup_expired_portal_proofs(upload_root)
                if removed:
                    logger.info("Removed %d expired event portal proof file(s)", removed)
            except Exception as exc:
                logger.warning("Event portal proof cleanup failed: %s", exc)
            time.sleep(24 * 60 * 60)

    threading.Thread(
        target=_worker, daemon=True, name="event-portal-proof-cleanup"
    ).start()


def _seed_all_tiles():
    """Seed tile_config for all modules using their canonical TABS definitions.

    Called once at startup after run_migrations_tile_config so the admin
    Gestão de Tiles page always shows every known tile regardless of whether
    the user has visited each module.
    """
    try:
        from db.tiles import seed_tile_config

        from flask_app.routes.producao import TABS as PRODUCAO_TABS
        seed_tile_config('producao', [{'id': t['id'], 'label': t['label']} for t in PRODUCAO_TABS])

        from flask_app.routes.pastelaria import TABS as PASTELARIA_TABS
        seed_tile_config('pastelaria', [{'id': t['id'], 'label': t['label']} for t in PASTELARIA_TABS])

        from flask_app.routes.vendas import TAB_DEFS as VENDAS_TABS
        seed_tile_config('vendas', [{'id': t['id'], 'label': t['label']} for t in VENDAS_TABS])

        from flask_app.routes.gestor import TABS as GESTOR_TABS
        seed_tile_config('gestor', [{'id': t['id'], 'label': t['label']} for t in GESTOR_TABS])

        from flask_app.routes.financeiro import FINANCEIRO_GROUPS
        fin_tiles = [{'id': m['key'], 'label': m['label']} for g in FINANCEIRO_GROUPS for m in g['modules']]
        seed_tile_config('financeiro', fin_tiles)

        from db.connection import db_connection as _dbc
        with _dbc() as _conn:
            _conn.cursor().execute(
                "DELETE FROM tile_config WHERE module = 'financeiro' AND tile_id = 'cashflow'"
            )
            # Remove retired producao tiles (merged into sabores_receitas in task #612)
            _conn.cursor().execute(
                "DELETE FROM tile_config WHERE module = 'producao' AND tile_id IN ('receitas', 'sabores_ativos')"
            )
            # Pastelaria now works from the weekly manual plan; production
            # registration is retained only as a legacy-compatible route.
            _conn.cursor().execute(
                "DELETE FROM tile_config WHERE module = 'pastelaria' AND tile_id = 'produzir'"
            )
            _conn.commit()

        logger.info("_seed_all_tiles: all module tiles seeded")
    except Exception as exc:
        logger.error("_seed_all_tiles failed: %s", exc)


def create_app():
    app = Flask(__name__, static_folder='static', template_folder='templates')
    app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(32).hex())
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['PERMANENT_SESSION_LIFETIME'] = 60 * 60 * 24 * 30
    app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024  # 4 MB — max single chunk for chunked PDF upload

    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    @app.before_request
    def start_request_metrics():
        from performance_metrics import begin_request
        g._performance_token = begin_request()
        g.request_id = uuid.uuid4().hex

    @app.after_request
    def log_request_metrics(response):
        from performance_metrics import snapshot
        metrics = snapshot()
        response_bytes = response.calculate_content_length()
        route = request.url_rule.rule if request.url_rule else '<unmatched>'
        request_logger = logging.getLogger('gunicorn.error')
        if not request_logger.handlers:
            request_logger = logger
        request_logger.info(
            'request_metrics request_id=%s method=%s route=%s status=%d '
            'duration_ms=%.1f db_duration_ms=%.1f query_count=%d response_bytes=%s',
            g.get('request_id', '-'), request.method, route, response.status_code,
            metrics['duration_ms'], metrics['db_duration_ms'],
            metrics['query_count'],
            response_bytes if response_bytes is not None else 'unknown',
        )
        response.headers['X-Request-ID'] = g.get('request_id', '-')
        return response

    @app.teardown_request
    def finish_request_metrics(_error):
        from performance_metrics import end_request
        end_request(g.pop('_performance_token', None))

    with app.app_context():
        init_database()
        run_migrations()
        run_faturas_migrations()
        run_migrations_m0()
        run_migrations_forecast()
        run_migrations_wind_config()
        run_migrations_cashflow()
        run_migrations_credito()
        run_migrations_eventos_v2_foundation()
        run_migrations_eventos_customer_portal()
        run_data_fix_stock_gelado_dedup_and_unique()
        run_migrations_caixa_loja()
        run_migrations_preco_caixa_kg()
        run_migrations_centros_custo()
        run_migrations_cost_centers_store_id()
        run_migrations_pastelaria_plano()
        run_migrations_pastelaria_count_product_id()
        run_migrations_pastelaria_product_state_audit()
        run_migrations_colaboradores_smart()
        run_migrations_transferencias_motivo()
        run_migrations_transferencias_eventos()
        run_migrations_batch_id()
        run_migrations_stock_producao_lojas()
        run_migrations_tarefas()
        run_migrations_tarefas_v2()
        run_migrations_tarefas_v3()
        run_migrations_tile_config()
        run_migrations_agente()
        run_migrations_fecho_caixa_audit()
        run_migrations_conta_vendas_diarias()
        run_migrations_b2b_vendas_diarias()
        run_migrations_tesouraria_manuais()
        run_migrations_user_audit_log()
        run_migrations_cost_center_allocation()
        run_migrations_stock_gelado_carapinas()
        run_migrations_suppliers_nullable_nif()
        run_migrations_normalise_supplier_nifs()
        run_migrations_onedrive_retry()
        run_migrations_invoice_centros_custo()
        run_migrations_invoice_installments()
        run_migrations_supplier_aliases()
        run_migrations_supplier_centro_custo()
        run_migrations_custos_recorrentes()
        run_migrations_normalise_producao_sabores()
        run_migrations_quantidade_kg_to_numeric()
        run_migrations_loja_origem()
        run_migrations_transferencias_destino()
        run_migrations_invoice_status_config()
        run_migrations_produto_aliases()
        run_migrations_pdf_filename_backfill()
        run_migrations_b2b()
        run_migrations_faturas_clientes_status()
        run_migrations_faturas_clientes_data_pagamento()
        run_migrations_faturas_clientes_document_type()
        run_migrations_contabilidade()
        run_migrations_invoice_payment_audit()
        run_migrations_saved_invoice_views()
        run_migrations_invoice_audit_complete()
        run_migrations_supplier_entidade_governamental()
        run_migrations_orcamento()
        run_migrations_cost_category_is_cmvmc()
        run_migrations_supplier_categoria_custo()
        run_migrations_drop_supplier_category()
        run_migrations_acesso_compras()
        # Legacy corrections swallow/log their own failures, so they must
        # remain retryable on every boot rather than being marked complete.
        run_data_fix_delete_auto_quebras()
        run_data_fix_quebras_march2026()
        run_data_fix_pesagem_april2026()
        run_data_fix_march1_dedup()
        run_data_fix_gelado_kpi_classification()
        run_data_fix_normalise_sabor_names()
        run_data_fix_cremino_stock_producao()
        run_data_fix_stock_gelado_march2026_dedup()
        run_data_fix_pesagem_matosinhos_backfill()
        run_backfill_transferencias_eventos()
        from db.custos_recorrentes import run_backfill_custos_recorrentes
        run_backfill_custos_recorrentes()
        try:
            from db.faturas import backfill_supplier_ids as _backfill_suppliers
            _backfill_suppliers()
        except Exception as exc:
            logger.warning('backfill_supplier_ids startup failed: %s', exc)
        try:
            run_backfill_invoice_categoria_custo()
        except Exception as exc:
            logger.warning('run_backfill_invoice_categoria_custo startup failed: %s', exc)
        # These are recurring reconciliations, not historical one-off fixes.
        _seed_all_tiles()
        sync_produtos_vendas_config()
        seed_artigos_administrativos()
        try:
            _promote_overdue_faturas_clientes()
        except Exception as exc:
            logger.warning('promote_overdue (faturas_clientes) startup failed: %s', exc)

    from flask_app.routes.auth import auth_bp
    from flask_app.routes.home import home_bp
    from flask_app.routes.eurokg import eurokg_bp
    from flask_app.routes.producao import producao_bp
    from flask_app.routes.vendas import vendas_bp
    from flask_app.routes.pastelaria import pastelaria_bp
    from flask_app.routes.confeitaria import confeitaria_bp
    from flask_app.routes.gestor import gestor_bp
    from flask_app.routes.compras import compras_bp
    from flask_app.routes.logistica import logistica_bp
    from flask_app.routes.eventos import eventos_bp
    from flask_app.routes.financeiro import financeiro_bp
    from flask_app.routes.credito import credito_bp
    from flask_app.routes.faturas import faturas_bp
    from flask_app.routes.pagamentos import pagamentos_bp
    from flask_app.routes.store_placeholder import store_placeholder_bp
    from flask_app.routes.forecast import forecast_bp
    from flask_app.routes.cashflow import cashflow_bp
    from flask_app.routes.centros_custo import centros_custo_bp
    from flask_app.routes.categorias_custo import categorias_custo_bp
    from flask_app.routes.tarefas import tarefas_bp
    from flask_app.routes.agente import agente_bp
    from flask_app.routes.admin import admin_bp
    from flask_app.routes.contabilidade import contabilidade_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(home_bp)
    app.register_blueprint(eurokg_bp, url_prefix='/eurokg')
    app.register_blueprint(producao_bp, url_prefix='/producao')
    app.register_blueprint(vendas_bp, url_prefix='/vendas')
    app.register_blueprint(pastelaria_bp, url_prefix='/pastelaria')
    app.register_blueprint(confeitaria_bp, url_prefix='/confeitaria')
    app.register_blueprint(gestor_bp, url_prefix='/gestor')
    app.register_blueprint(compras_bp, url_prefix='/compras')
    app.register_blueprint(logistica_bp, url_prefix='/logistica')
    app.register_blueprint(eventos_bp, url_prefix='/eventos')
    app.register_blueprint(financeiro_bp, url_prefix='/financeiro')
    app.register_blueprint(credito_bp, url_prefix='/financeiro/credito')
    app.register_blueprint(faturas_bp, url_prefix='/financeiro/faturas')
    app.register_blueprint(pagamentos_bp, url_prefix='/financeiro/pagamentos')
    app.register_blueprint(store_placeholder_bp, url_prefix='/loja')
    app.register_blueprint(forecast_bp, url_prefix='/forecast')
    app.register_blueprint(cashflow_bp, url_prefix='/financeiro/cashflow')
    app.register_blueprint(centros_custo_bp, url_prefix='/financeiro/centros-custo')
    app.register_blueprint(categorias_custo_bp, url_prefix='/financeiro/categorias')
    app.register_blueprint(tarefas_bp, url_prefix='/tarefas')
    app.register_blueprint(agente_bp, url_prefix='/agente')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(contabilidade_bp, url_prefix='/contabilidade')

    @app.errorhandler(413)
    def request_entity_too_large(e):
        from flask import flash, redirect, request as _req
        flash('Ficheiro demasiado grande (máximo 5 MB). Escolhe um PDF mais pequeno ou comprime-o primeiro.', 'warning')
        referrer = _req.referrer or url_for('home.index')
        return redirect(referrer), 303

    @app.route('/healthcheck')
    def healthcheck():
        return 'OK', 200

    @app.route('/favicon.ico')
    def favicon():
        return redirect(url_for('static', filename='favicon.svg'), code=302)

    DEV_TOKEN = os.environ.get('DEV_AUTO_LOGIN_TOKEN', '')

    @app.route('/dev-login/<token>')
    def dev_auto_login(token):
        if DEV_TOKEN and token == DEV_TOKEN:
            user = authenticate_user('carlosjcmoreira', 'itinerantaroma')
            if user:
                tok = create_session(user['id'])
                session.permanent = True
                session['user'] = user
                session['token'] = tok
            return redirect(url_for('home.index'))
        return redirect(url_for('auth.login'))

    @app.before_request
    def load_user():
        token = session.get('token')
        if token:
            from db.auth import get_session_user
            fresh = get_session_user(token)
            if fresh:
                # Refresh the session cookie so it stays in sync with the DB.
                # This means permission changes made by admins take effect on
                # the very next page load for the affected user.
                session['user'] = fresh
                g.user = fresh
            else:
                # Token expired or user deactivated — clear the stale session.
                session.clear()
                g.user = None
        else:
            g.user = session.get('user')

    @app.template_global()
    def badge_attrs(bg_class_val):
        """Return dict(css_class, inline_style) for a status badge bg_class value.

        If bg_class_val starts with '#', it is treated as a hex color and rendered
        via inline style.  Otherwise it is used as Bootstrap badge class(es).
        """
        val = (bg_class_val or 'bg-secondary').strip()
        if val.startswith('#'):
            return {'css_class': 'badge', 'inline_style': f'background-color:{val};color:#fff'}
        return {'css_class': f'badge {val}', 'inline_style': ''}

    @app.context_processor
    def inject_globals():
        from flask_app.analytics import consume_analytics_events
        time_slots = ['%02d:%02d' % (h, m) for h in range(6, 24) for m in [0, 15, 30, 45]]
        event_type_options = [
            ('Corporativo', 'Corporativo/ Corporate'),
            ('Casamento', 'Casamento/ Wedding'),
            ('Privado', 'Privado/ Private'),
            ('Outro', 'Other:'),
        ]
        user = g.get('user')
        nav_pages = []
        mobile_nav_primary_count = 4
        if user:
            try:
                from flask_app.services.navigation import compute_nav_pages, MOBILE_NAV_PRIMARY_COUNT
                nav_pages = compute_nav_pages(user)
                mobile_nav_primary_count = MOBILE_NAV_PRIMARY_COUNT
            except Exception as exc:
                logger.warning("inject_globals: failed to compute nav_pages: %s", exc)
        status_colors = {}
        status_labels = {}
        status_bulk_allowed = ['pending_review', 'scheduled', 'paid', 'cancelled']
        try:
            from db.faturas import (get_invoice_status_colors_map, get_invoice_status_labels_map,
                                    get_invoice_status_bulk_allowed)
            status_colors = get_invoice_status_colors_map()
            status_labels = get_invoice_status_labels_map()
            status_bulk_allowed = get_invoice_status_bulk_allowed()
        except Exception as exc:
            logger.debug("inject_globals: could not load status maps: %s", exc)
        return dict(user=user, time_slots=time_slots, event_type_options=event_type_options,
                    nav_pages=nav_pages, mobile_nav_primary_count=mobile_nav_primary_count,
                    status_colors=status_colors, status_labels=status_labels,
                    status_bulk_allowed=status_bulk_allowed,
                    pop_analytics_events=consume_analytics_events)

    import psycopg2

    @app.errorhandler(psycopg2.OperationalError)
    @app.errorhandler(psycopg2.DatabaseError)
    def handle_db_error(exc):
        logger.error("DB error (503): %s", exc, exc_info=True)
        return render_template('errors/503.html'), 503

    @app.errorhandler(500)
    def handle_500(exc):
        logger.error("Unhandled 500: %s", exc, exc_info=True)
        return render_template('errors/500.html'), 500

    return app


if __name__ == '__main__':
    app = create_app()
    if os.environ.get('EVENTOS_SYNC_ENABLED', '1') == '1':
        _start_sheets_sync_scheduler()
    from flask_app.onedrive_scheduler import start_onedrive_scheduler
    start_onedrive_scheduler()
    app.run(host='0.0.0.0', port=5000, debug=False)
