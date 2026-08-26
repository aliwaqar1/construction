"""Parked email/password auth — NOT reachable over HTTP.

These two handlers used to live in `construction.api.v1` as
`@frappe.whitelist(allow_guest=True)` endpoints. They were the only
unauthenticated account-creation path on the server, and nothing ever called
them: the app has no sign-in screen, and its `AuthRepository.login/register`
were never wired to a UI. An open door onto `User` creation, held open for a
feature that does not exist.

They are kept here, intact, because email/password sign-in is a plausible
future feature and re-deriving this is wasted work. Moving them out of `v1.py`
and stripping the `@frappe.whitelist` decorators is what unwires them: with no
decorator there is no route, so `/api/method/construction.api.v1.login` and
`...register` now 404. Importing this module does NOT re-expose anything.

BEFORE RE-ENABLING, restore the decorators AND close what was missing:

  * `register` had no per-IP cap. `anon_bootstrap` has one
    (`_ip_daily_allowed("anon_bootstrap_ip", _BOOTSTRAP_IP_DAILY_CAP)`) precisely
    because unauthenticated row creation needs a ceiling; this path needs the
    same treatment or it is an uncapped way to mint `User` rows.
  * There is no rate limit or lockout on `login`, so it is also an unthrottled
    password-guessing oracle. Frappe's own login throttling does not apply here
    because this calls `LoginManager.authenticate` directly.
  * Decide what happens when a device already holds an anonymous account: today
    `_persist` on the client would swap the token and silently strand the
    anonymous user's credits and synced rows. There is no merge path.

Identity today is anonymous-only — see `anon_bootstrap` in `v1.py`.
"""

import frappe

from construction.api.v1 import _error_response, _log_unhandled, _user_keys


def login(usr=None, pwd=None):
    """Authenticate a user and return api_key/api_secret.

    Body: {"usr": "email", "pwd": "password"}
    """
    data = frappe.local.form_dict if frappe.local.form_dict else {}
    if frappe.request and frappe.request.is_json:
        try:
            raw = frappe.request.data
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            data = frappe.parse_json(raw) or {}
        except Exception:
            return _error_response("INVALID_JSON", "Body is not valid JSON")

    usr = usr or data.get("usr") or data.get("email")
    pwd = pwd or data.get("pwd") or data.get("password")

    if not usr or not pwd:
        return _error_response("MISSING_PARAMS", "'usr' and 'pwd' are required")

    try:
        from frappe.auth import LoginManager
        lm = LoginManager()
        lm.authenticate(user=usr, pwd=pwd)
        lm.post_login()
    except frappe.AuthenticationError:
        return _error_response("INVALID_CREDENTIALS", "Invalid email or password", 401)
    except Exception:
        corr = _log_unhandled("v1.login")
        return _error_response("SERVER_ERROR", "Login failed", 500, correlation_id=corr)

    # Password login is an explicit, authenticated credential exchange, so
    # rotating here is intentional: it invalidates any previously issued token.
    api_key, api_secret = _user_keys(frappe.session.user, rotate=True)
    return {
        "user": frappe.session.user,
        "api_key": api_key,
        "api_secret": api_secret,
        "full_name": frappe.db.get_value("User", frappe.session.user, "full_name"),
    }


def register(email=None, password=None, full_name=None):
    """Create a new user account. Returns api_key/secret on success."""
    data = {}
    if frappe.request and frappe.request.is_json:
        try:
            raw = frappe.request.data
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            data = frappe.parse_json(raw) or {}
        except Exception:
            return _error_response("INVALID_JSON", "Body is not valid JSON")

    email = email or data.get("email")
    password = password or data.get("password")
    full_name = full_name or data.get("full_name") or ""

    if not email or not password:
        return _error_response("MISSING_PARAMS", "'email' and 'password' are required")

    if len(password) < 8:
        return _error_response("INVALID_PARAMS", "Password must be at least 8 characters")

    if frappe.db.exists("User", email):
        return _error_response("USER_EXISTS", "An account with that email already exists", 409)

    try:
        first, _, last = full_name.partition(" ")
        user_doc = frappe.get_doc({
            "doctype": "User",
            "email": email,
            "first_name": first or email.split("@")[0],
            "last_name": last or "",
            "send_welcome_email": 0,
            "enabled": 1,
            "new_password": password,
            "user_type": "Website User",
        })
        user_doc.flags.ignore_permissions = True
        user_doc.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        corr = _log_unhandled("v1.register")
        return _error_response("SERVER_ERROR", "Failed to create account", 500, correlation_id=corr)

    api_key, api_secret = _user_keys(email)
    return {
        "user": email,
        "api_key": api_key,
        "api_secret": api_secret,
        "full_name": full_name,
    }
