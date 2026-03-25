from flask import Blueprint, render_template, abort
from flask_app.auth import perm_required
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
import database as db

store_placeholder_bp = Blueprint('store_placeholder', __name__)


@store_placeholder_bp.route('/<int:store_id>')
@perm_required('acesso_gestor')
def index(store_id):
    store = db.get_store(store_id)
    if not store or not store['is_active']:
        abort(404)
    return render_template('store_placeholder/index.html', store=store)
