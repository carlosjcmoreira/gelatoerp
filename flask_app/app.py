import os
import sys
import threading
import logging
from flask import Flask, session, redirect, url_for, g, request
from functools import wraps

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from database import init_database, run_migrations, run_faturas_migrations, run_migrations_m0, run_migrations_forecast, sync_produtos_vendas_config, seed_artigos_administrativos, authenticate_user, create_session
from db.cashflow import run_migrations_cashflow
from db.schema import (run_migrations_credito, run_data_fix_quebras_march2026,
                        run_data_fix_pesagem_april2026, run_migrations_centros_custo,
                        run_data_fix_delete_auto_quebras,
                        run_data_fix_pesagem_matosinhos_backfill,
                        run_data_fix_march1_dedup,
                        run_data_fix_gelado_kpi_classification,
                        run_migrations_caixa_loja,
                        run_migrations_preco_caixa_kg,
                        run_migrations_stock_producao_lojas,
                        run_migrations_colaboradores_smart)
from db.tiles import run_migrations_tile_config
from db.avencas import run_migrations_avencas


def _start_sheets_sync_scheduler():
    """Background thread that syncs leads from Google Sheets daily at 08:00."""
    def _worker():
        import time
        from datetime import datetime, timedelta
        while True:
            now = datetime.now()
            target = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            logger.info("Next Google Sheets sync scheduled at %s (in %.0f s)", target.strftime('%Y-%m-%d %H:%M'), wait_seconds)
            time.sleep(wait_seconds)
            try:
                from flask_app.google_sheets_sync import sync_leads_from_sheet
                inserted, updated, errors = sync_leads_from_sheet()
                logger.info("Auto sync Google Sheets: %d new, %d updated, %d errors", inserted, updated, errors)
            except Exception as e:
                logger.warning("Auto sync Google Sheets failed: %s", e)

    t = threading.Thread(target=_worker, daemon=True, name="sheets-sync")
    t.start()


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

        logger.info("_seed_all_tiles: all module tiles seeded")
    except Exception as exc:
        logger.error("_seed_all_tiles failed: %s", exc)


def create_app():
    app = Flask(__name__, static_folder='static', template_folder='templates')
    app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(32).hex())
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['PERMANENT_SESSION_LIFETIME'] = 60 * 60 * 24 * 30
    app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20 MB for PDF uploads

    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    with app.app_context():
        init_database()
        run_migrations()
        run_faturas_migrations()
        run_data_fix_delete_auto_quebras()
        run_migrations_m0()
        run_migrations_forecast()
        run_migrations_cashflow()
        run_migrations_credito()
        run_data_fix_quebras_march2026()
        run_data_fix_pesagem_april2026()
        run_data_fix_pesagem_matosinhos_backfill()
        run_data_fix_march1_dedup()
        run_data_fix_gelado_kpi_classification()
        run_migrations_caixa_loja()
        run_migrations_preco_caixa_kg()
        run_migrations_centros_custo()
        run_migrations_colaboradores_smart()
        run_migrations_stock_producao_lojas()
        run_migrations_avencas()
        run_migrations_tile_config()
        _seed_all_tiles()
        sync_produtos_vendas_config()
        seed_artigos_administrativos()

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
    from flask_app.routes.meteorologia import meteorologia_bp
    from flask_app.routes.forecast import forecast_bp
    from flask_app.routes.cashflow import cashflow_bp
    from flask_app.routes.centros_custo import centros_custo_bp
    from flask_app.routes.categorias_custo import categorias_custo_bp
    from flask_app.routes.avencas import avencas_bp

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
    app.register_blueprint(meteorologia_bp, url_prefix='/meteorologia')
    app.register_blueprint(forecast_bp, url_prefix='/forecast')
    app.register_blueprint(cashflow_bp, url_prefix='/financeiro/cashflow')
    app.register_blueprint(centros_custo_bp, url_prefix='/financeiro/centros-custo')
    app.register_blueprint(categorias_custo_bp, url_prefix='/financeiro/categorias')
    app.register_blueprint(avencas_bp, url_prefix='/financeiro/avencas')

    import weather_scheduler
    weather_scheduler.start_weather_scheduler()

    @app.route('/healthcheck')
    def healthcheck():
        return 'OK', 200

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
        g.user = session.get('user')

    @app.context_processor
    def inject_globals():
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
        return dict(user=user, time_slots=time_slots, event_type_options=event_type_options,
                    nav_pages=nav_pages, mobile_nav_primary_count=mobile_nav_primary_count)

    return app




if __name__ == '__main__':
    app = create_app()
    if os.environ.get('EVENTOS_SYNC_ENABLED', '1') == '1':
        _start_sheets_sync_scheduler()
    app.run(host='0.0.0.0', port=5000, debug=False)
