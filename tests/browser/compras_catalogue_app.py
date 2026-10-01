"""Isolated in-memory server for the Compras catalogue browser regression."""

from copy import deepcopy
from pathlib import Path

from flask import Blueprint, Flask, jsonify, redirect, session, url_for

from flask_app.routes import compras as compras_routes
from flask_app.routes.compras import compras_bp
from flask_app.routes.faturas import faturas_bp


STATE = {"articles": [], "operations": []}
SUPPLIERS = [
    {"id": 1, "name": "Fornecedor Antigo"},
    {"id": 2, "name": "Fornecedor Novo"},
]


def reset_state():
    STATE["articles"] = [
        {
            "id": article_id,
            "fornecedor": "Etiqueta histórica",
            "produto": f"Artigo {article_id:03d}",
            "marca": None,
            "unidade": "un",
            "categoria_artigo": "Por classificar",
            "origem_id": None,
            "origem_nome": None,
            "origem_tipo": "por_resolver",
            "origem_original": "Etiqueta histórica",
            "origem_revisao_estado": "por_rever",
            "fornecedor_oficial_id": 1,
            "fornecedor_oficial_nome": "Fornecedor Antigo",
            "ativo": True,
        }
        for article_id in range(1, 91)
    ]
    STATE["operations"] = []


def _article(article_id):
    return next(
        (article for article in STATE["articles"] if article["id"] == article_id),
        None,
    )


def _list_articles(apenas_ativos=True):
    articles = STATE["articles"]
    if apenas_ativos:
        articles = [article for article in articles if article["ativo"]]
    return deepcopy(articles)


def _add_article(produto, supplier_id, marca, unidade, actor, categoria_artigo):
    if any(article["produto"] == produto for article in STATE["articles"]):
        return False
    supplier = next(s for s in SUPPLIERS if s["id"] == supplier_id)
    article_id = max((a["id"] for a in STATE["articles"]), default=0) + 1
    STATE["articles"].append({
        "id": article_id,
        "fornecedor": "Etiqueta histórica",
        "produto": produto,
        "marca": marca,
        "unidade": unidade,
        "categoria_artigo": categoria_artigo,
        "origem_id": None,
        "origem_nome": None,
        "origem_tipo": "por_resolver",
        "origem_original": None,
        "origem_revisao_estado": None,
        "fornecedor_oficial_id": supplier_id,
        "fornecedor_oficial_nome": supplier["name"],
        "ativo": True,
    })
    STATE["operations"].append({"action": "add", "article_id": article_id})
    return True


def _set_article_supplier(article_id, supplier_id, actor):
    article = _article(article_id)
    if article is None:
        return None
    supplier = next((s for s in SUPPLIERS if s["id"] == supplier_id), None)
    if supplier_id is not None and supplier is None:
        raise ValueError("Fornecedor inválido.")
    new_name = supplier["name"] if supplier else None
    changed = (
        article["fornecedor_oficial_id"] != supplier_id
        or article["fornecedor_oficial_nome"] != new_name
    )
    article["fornecedor_oficial_id"] = supplier_id
    article["fornecedor_oficial_nome"] = new_name
    if changed:
        STATE["operations"].append({
            "action": "set_supplier",
            "article_id": article_id,
            "supplier_id": supplier_id,
        })
    return {"found": True, "changed": changed, "supplier_name": new_name}


def _toggle_article(article_id, active):
    article = _article(article_id)
    if article is None:
        return
    article["ativo"] = active
    STATE["operations"].append({
        "action": "toggle",
        "article_id": article_id,
        "active": active,
    })


def _delete_article(article_id):
    STATE["articles"] = [
        article for article in STATE["articles"] if article["id"] != article_id
    ]


def _configure_catalogue():
    compras_routes.get_artigos_administrativos = _list_articles
    compras_routes.get_suppliers = lambda: deepcopy(SUPPLIERS)
    compras_routes.add_artigo_administrativo = _add_article
    compras_routes.set_artigo_fornecedor_oficial = _set_article_supplier
    compras_routes.toggle_artigo_administrativo = _toggle_article
    compras_routes.delete_artigo_administrativo = _delete_article


def create_test_app():
    _configure_catalogue()
    reset_state()
    project_root = Path(__file__).resolve().parents[2]
    app = Flask(
        __name__,
        template_folder=str(project_root / "flask_app" / "templates"),
        static_folder=str(project_root / "flask_app" / "static"),
    )
    app.secret_key = "compras-catalogue-browser-test"
    app.config["TESTING"] = True

    home = Blueprint("home", __name__)

    @home.get("/")
    def index():
        return "Início"

    auth = Blueprint("auth", __name__)

    @auth.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("home.index"))

    app.register_blueprint(home)
    app.register_blueprint(auth)
    app.register_blueprint(
        faturas_bp, url_prefix="/financeiro/faturas"
    )
    app.register_blueprint(compras_bp, url_prefix="/compras")

    @app.context_processor
    def inject_user():
        return {"user": session.get("user"), "nav_pages": []}

    @app.get("/test-login")
    def test_login():
        session["user"] = {
            "id": 9001,
            "username": "compras-browser-test",
            "acesso_compras": True,
        }
        return redirect(url_for("compras.artigos"))

    @app.post("/_test-reset")
    def test_reset():
        reset_state()
        return "ok"

    @app.get("/_test-state")
    def test_state():
        return jsonify(deepcopy(STATE))

    @app.get("/_health")
    def health():
        return "ok"

    return app


if __name__ == "__main__":
    create_test_app().run(host="127.0.0.1", port=8766, use_reloader=False)