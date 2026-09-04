"""Auth shims for the twin blueprint (C4).

The host Sentinel AI app owns authentication. The twin must not import its
decorators directly -- that would couple this module to 119 routes we are
forbidden to touch (C6) -- so it resolves them at registration time and falls
back to its own Flask-Login-based implementations.

Secure by default: if the twin cannot determine who the caller is, it returns
401/403 rather than serving officials' data. ``TWIN_DEV_OPEN_AUTH=1`` disables
both checks for local development and is refused when the app is not in debug
or testing mode.
"""

import functools
import os

from flask import current_app, jsonify, request

#: Roles allowed on twin state/summary/cell routes.
TWIN_ROLES = ("official", "analyst")

# Injected by create_twin_blueprint() when the host app supplies its own.
_login_required = None
_role_required = None


def _dev_open_auth():
    if os.getenv("TWIN_DEV_OPEN_AUTH", "0") != "1":
        return False
    try:
        return bool(current_app.debug or current_app.testing)
    except RuntimeError:
        return False


def _unauthorised(message, code):
    payload = {"error": message, "status": code}
    if request.accept_mimetypes.accept_html and not request.path.startswith("/api/"):
        payload["login_url"] = "/login"
    return jsonify(payload), code


def _current_user():
    try:
        from flask_login import current_user
    except ImportError:
        return None
    return current_user


def _user_roles(user):
    """Extract role names from a host user object without assuming its shape."""
    attr = os.getenv("TWIN_ROLE_ATTR", "role")
    raw = getattr(user, attr, None)
    if raw is None and attr != "roles":
        raw = getattr(user, "roles", None)
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        return frozenset({raw.strip().lower()})
    try:
        names = set()
        for item in raw:
            names.add(str(getattr(item, "name", item)).strip().lower())
        return frozenset(names)
    except TypeError:
        return frozenset({str(raw).strip().lower()})


def _default_login_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if _dev_open_auth():
            return view(*args, **kwargs)
        user = _current_user()
        if user is None or not getattr(user, "is_authenticated", False):
            return _unauthorised("authentication required", 401)
        return view(*args, **kwargs)

    return wrapper


def _default_role_required(*roles):
    allowed = frozenset(r.strip().lower() for r in roles)

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            if _dev_open_auth():
                return view(*args, **kwargs)
            user = _current_user()
            if user is None or not getattr(user, "is_authenticated", False):
                return _unauthorised("authentication required", 401)
            if not (_user_roles(user) & allowed):
                return _unauthorised(
                    "role %s required" % "/".join(sorted(allowed)), 403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def configure(login_required=None, role_required=None):
    """Adopt the host app's decorators if it has them."""
    global _login_required, _role_required
    _login_required = login_required
    _role_required = role_required


def login_required(view):
    if _login_required is not None:
        return _login_required(view)
    return _default_login_required(view)


def role_required(*roles):
    if _role_required is not None:
        return _role_required(*roles)
    return _default_role_required(*roles)


def twin_roles_required(view):
    """Shorthand for the official/analyst gate applied to most twin routes."""
    return login_required(role_required(*TWIN_ROLES)(view))


def official_only(view):
    """Section 7: /refresh and /seed are official-only (a stricter gate than
    the general official/analyst twin_roles_required)."""
    return login_required(role_required("official")(view))
