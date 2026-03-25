from flask import Blueprint, render_template, request, session, redirect, url_for, flash
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from database import authenticate_user, create_session, delete_session

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if 'user' in session:
        return redirect(url_for('home.index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        try:
            user = authenticate_user(username, password)
            if user:
                token = create_session(user['id'])
                session.permanent = True
                session['user'] = user
                session['token'] = token
                return redirect(url_for('home.index'))
            else:
                flash('Credenciais inválidas. Tente novamente.', 'error')
        except Exception as e:
            flash(f'Erro de ligação à base de dados: {e}', 'error')

    return render_template('login.html')


@auth_bp.route('/logout')
def logout():
    token = session.get('token')
    if token:
        try:
            delete_session(token)
        except Exception:
            pass
    session.clear()
    return redirect(url_for('auth.login'))
