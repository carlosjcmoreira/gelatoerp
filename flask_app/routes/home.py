import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from flask import Blueprint, render_template, session, redirect
from flask_app.auth import login_required
from flask_app.services.navigation import compute_nav_pages

home_bp = Blueprint('home', __name__)


@home_bp.route('/')
@login_required
def index():
    user = session['user']
    pages = compute_nav_pages(user)

    if not pages:
        return render_template('home.html', no_access=True)

    return redirect(pages[0]['url'])
