from flask import session, redirect, url_for, flash, request
from functools import wraps


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


def _has_perm(user, perm):
    """Check if user has the given permission.
    For acesso_vendas, derives from vendas_store_ids (non-empty list)."""
    if user.get('acesso_gestor'):
        return True
    if perm == 'acesso_vendas':
        return bool(user.get('vendas_store_ids'))
    return bool(user.get(perm))


def perm_required(perm):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user' not in session:
                return redirect(url_for('auth.login'))
            user = session['user']
            if not _has_perm(user, perm):
                if request.method == 'POST':
                    flash('Não tens permissão para realizar esta acção.', 'danger')
                return redirect(url_for('home.index'))
            return f(*args, **kwargs)
        return decorated
    return decorator


def any_perm_required(*perms):
    """Decorator that grants access when the user has ANY of the listed permissions.

    Usage::

        @any_perm_required('acesso_financeiro', 'acesso_compras')
        def my_view():
            ...
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user' not in session:
                return redirect(url_for('auth.login'))
            user = session['user']
            if not any(_has_perm(user, p) for p in perms):
                if request.method == 'POST':
                    flash('Não tens permissão para realizar esta acção.', 'danger')
                return redirect(url_for('home.index'))
            return f(*args, **kwargs)
        return decorated
    return decorator
