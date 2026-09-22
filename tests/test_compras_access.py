"""
End-to-end access-control tests for the Compras module.

Run with: python -m unittest tests.test_compras_access -v

Tests register the **real** `compras_bp` (same URL rules and decorators as
production) inside a minimal Flask app, and patch only the database/template
calls that the route bodies need so that they reach 200 without a live DB.

Covers:
- User with acesso_compras=True reaches /compras/, /compras/faturas,
  /compras/nova-fatura (HTTP 200, no redirect).
- User without acesso_compras (and without acesso_administrativo / acesso_gestor)
  is redirected away from every compras route.
- Unauthenticated requests redirect to login.
- acesso_administrativo alone also grants access (alternative gate).
- acesso_gestor super-user flag still grants access.
- acesso_financeiro alone does NOT grant compras access.
"""

import unittest
from unittest.mock import patch, MagicMock
from flask import Blueprint, Flask


# ---------------------------------------------------------------------------
# Build a minimal Flask app that registers the REAL compras blueprint
# ---------------------------------------------------------------------------

def _make_app():
    """Return a Flask test app with the real compras_bp registered."""
    from flask_app.routes.compras import compras_bp

    app = Flask(__name__)
    app.secret_key = 'test-secret-key'
    app.config['TESTING'] = True

    # Stub blueprints required by auth redirect targets
    auth_bp = Blueprint('auth', __name__)

    @auth_bp.route('/login')
    def login():
        return 'login', 200

    home_bp = Blueprint('home', __name__)

    @home_bp.route('/')
    def index():
        return 'home', 200

    app.register_blueprint(auth_bp)
    app.register_blueprint(home_bp)
    app.register_blueprint(compras_bp, url_prefix='/compras')

    return app


# ---------------------------------------------------------------------------
# Route-body mocks — applied for every test so DB is never contacted.
# "blocked" tests never reach the body; "allowed" tests get 200 stubs.
# ---------------------------------------------------------------------------

_DB_PATCHES = [
    # db.tiles (imported inside index() body)
    patch('db.tiles.seed_tile_config'),
    patch('db.tiles.get_tile_visibility',   return_value={}),
    patch('db.tiles.get_tile_labels',        return_value={}),
    patch('db.tiles.get_tile_icons',         return_value={}),
    patch('db.tiles.get_module_labels',      return_value={}),
    # faturas route (module-level imports in flask_app.routes.compras)
    patch('flask_app.routes.compras.count_invoices',            return_value=0),
    patch('flask_app.routes.compras.get_invoices',              return_value=[]),
    patch('flask_app.routes.compras.get_payment_methods_config',return_value=[]),
    patch('flask_app.routes.compras.get_distinct_supplier_names',return_value=[]),
    patch('flask_app.routes.compras.get_stores_list',           return_value=[]),
    # nova-fatura route (GET body)
    patch('flask_app.routes.compras.get_suppliers',             return_value=[]),
    patch('flask_app.routes.compras.get_cost_centers',          return_value=[]),
    patch('flask_app.routes.compras.get_cost_categories_tree',  return_value=[]),
    patch('flask_app.routes.compras.get_artigos_administrativos', return_value=[]),
    patch('flask_app.routes.compras.get_compras_origens',        return_value=[]),
    # render_template — return a plain string so no template files are needed
    # Must be patched where it is *used* (imported into compras module), not at flask.templating
    patch('flask_app.routes.compras.render_template',           return_value='ok'),
]


# ---------------------------------------------------------------------------
# Shared user builder
# ---------------------------------------------------------------------------

_BASE_USER = {
    'id': 1,
    'username': 'testuser',
    'role': 'producao',
    'nome': 'Test User',
    'acesso_eurokg': False,
    'acesso_producao': False,
    'acesso_vendas': False,
    'acesso_pastelaria': False,
    'acesso_confeitaria': False,
    'acesso_gestor': False,
    'acesso_administrativo': False,
    'acesso_financeiro': False,
    'acesso_eventos': False,
    'acesso_tarefas': False,
    'acesso_contabilidade': False,
    'acesso_compras': False,
    'vendas_store_ids': [],
    'loja_id': None,
}


def _user(**overrides):
    return {**_BASE_USER, **overrides}


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestComprasAccess(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _make_app()
        # Start all DB patches for the whole class
        cls._started_patches = []
        for p in _DB_PATCHES:
            p.start()
            cls._started_patches.append(p)

    @classmethod
    def tearDownClass(cls):
        for p in cls._started_patches:
            p.stop()

    def setUp(self):
        self.client = self.app.test_client()

    def _set_session_user(self, user: dict):
        with self.client.session_transaction() as sess:
            sess['user'] = user

    # ------------------------------------------------------------------
    # User with ONLY acesso_compras must reach every compras route (200)
    # ------------------------------------------------------------------

    def test_compras_index_accessible_with_acesso_compras(self):
        """acesso_compras=True → GET /compras/ returns 200."""
        self._set_session_user(_user(acesso_compras=True))
        resp = self.client.get('/compras/')
        self.assertEqual(resp.status_code, 200,
                         "acesso_compras user should not be redirected from /compras/")

    def test_compras_faturas_accessible_with_acesso_compras(self):
        """acesso_compras=True → GET /compras/faturas returns 200."""
        self._set_session_user(_user(acesso_compras=True))
        resp = self.client.get('/compras/faturas')
        self.assertEqual(resp.status_code, 200,
                         "acesso_compras user should not be redirected from /compras/faturas")

    def test_compras_nova_fatura_accessible_with_acesso_compras(self):
        """acesso_compras=True → GET /compras/nova-fatura returns 200."""
        self._set_session_user(_user(acesso_compras=True))
        resp = self.client.get('/compras/nova-fatura')
        self.assertEqual(resp.status_code, 200,
                         "acesso_compras user should not be redirected from /compras/nova-fatura")

    def test_compras_catalogue_accessible_with_acesso_compras(self):
        """acesso_compras=True → GET /compras/artigos returns 200."""
        self._set_session_user(_user(acesso_compras=True))
        resp = self.client.get('/compras/artigos')
        self.assertEqual(resp.status_code, 200)

    # ------------------------------------------------------------------
    # User without any relevant perm must be redirected (3xx)
    # ------------------------------------------------------------------

    def test_compras_index_blocked_without_perm(self):
        """No compras/admin/gestor perm → GET /compras/ redirects."""
        self._set_session_user(_user())
        resp = self.client.get('/compras/')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308),
                      "User without compras permission should be redirected from /compras/")

    def test_compras_faturas_blocked_without_perm(self):
        """No compras/admin/gestor perm → GET /compras/faturas redirects."""
        self._set_session_user(_user())
        resp = self.client.get('/compras/faturas')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308),
                      "User without compras permission should be redirected from /compras/faturas")

    def test_compras_nova_fatura_blocked_without_perm(self):
        """No compras/admin/gestor perm → GET /compras/nova-fatura redirects."""
        self._set_session_user(_user())
        resp = self.client.get('/compras/nova-fatura')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308),
                      "User without compras permission should be redirected from /compras/nova-fatura")

    def test_compras_catalogue_blocked_without_perm(self):
        """No compras/admin/gestor perm → GET /compras/artigos redirects."""
        self._set_session_user(_user())
        resp = self.client.get('/compras/artigos')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308))

    def test_unauthenticated_user_is_redirected(self):
        """No session at all → GET /compras/ redirects to login."""
        resp = self.client.get('/compras/')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308),
                      "Unauthenticated request should redirect")

    # ------------------------------------------------------------------
    # Regression: acesso_administrativo also grants access
    # ------------------------------------------------------------------

    def test_compras_accessible_with_acesso_administrativo(self):
        """acesso_administrativo=True (no acesso_compras) → /compras/ still 200."""
        self._set_session_user(_user(acesso_administrativo=True))
        resp = self.client.get('/compras/')
        self.assertEqual(resp.status_code, 200,
                         "acesso_administrativo should also grant /compras/ access")

    # ------------------------------------------------------------------
    # Regression: acesso_gestor super-user flag grants access everywhere
    # ------------------------------------------------------------------

    def test_compras_accessible_with_acesso_gestor(self):
        """acesso_gestor=True → /compras/ returns 200 (gestor bypasses all perm checks)."""
        self._set_session_user(_user(acesso_gestor=True))
        resp = self.client.get('/compras/')
        self.assertEqual(resp.status_code, 200,
                         "acesso_gestor should always grant /compras/ access")

    # ------------------------------------------------------------------
    # Explicit: acesso_financeiro alone does NOT grant compras access
    # ------------------------------------------------------------------

    def test_compras_blocked_with_only_acesso_financeiro(self):
        """acesso_financeiro=True but acesso_compras=False → /compras/ redirects."""
        self._set_session_user(_user(acesso_financeiro=True))
        resp = self.client.get('/compras/')
        self.assertIn(resp.status_code, (301, 302, 303, 307, 308),
                      "acesso_financeiro alone must NOT grant /compras/ access")


if __name__ == '__main__':
    unittest.main()
