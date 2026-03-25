"""
Session Service — centralises the "get current user or redirect" pattern
that is repeated at the top of every route handler.

Usage in a route:
    from flask_app.services.session import require_user, require_admin

    @bp.route('/my-page')
    def my_page():
        user, redirect_resp = require_user()
        if redirect_resp:
            return redirect_resp
        ...

Or with the decorator form:
    from flask_app.services.session import login_required, admin_required

    @bp.route('/my-page')
    @login_required
    def my_page():
        user = g.user  # populated by the decorator
        ...
"""
import functools
import logging
from flask import request, redirect, url_for, g

logger = logging.getLogger(__name__)


def _get_token() -> str | None:
    return request.cookies.get('session_token')


def get_current_user() -> dict | None:
    """
    Returns the authenticated user dict for the current request,
    or None if no valid session exists.
    Caches result in Flask's request-scoped `g.user` to avoid repeated DB lookups.
    """
    if hasattr(g, 'user'):
        return g.user
    token = _get_token()
    if not token:
        g.user = None
        return None
    from database import get_session_user
    user = get_session_user(token)
    g.user = user
    return user


def require_user():
    """
    Returns (user_dict, None) if authenticated,
    or (None, redirect_response) if not authenticated.

    Typical use:
        user, resp = require_user()
        if resp:
            return resp
    """
    user = get_current_user()
    if not user:
        return None, redirect(url_for('auth.login'))
    return user, None


def require_admin():
    """
    Returns (user_dict, None) if authenticated AND has admin access,
    or (None, redirect_response) otherwise.
    """
    user, resp = require_user()
    if resp:
        return None, resp
    if not user.get('acesso_administrativo'):
        return None, redirect(url_for('home.index'))
    return user, None


def login_required(fn):
    """
    Decorator: redirects to login if no valid session.
    Populates g.user with the authenticated user dict.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user, resp = require_user()
        if resp:
            return resp
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    """
    Decorator: redirects if no valid session or user is not an admin.
    Populates g.user with the authenticated user dict.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        user, resp = require_admin()
        if resp:
            return resp
        return fn(*args, **kwargs)
    return wrapper
