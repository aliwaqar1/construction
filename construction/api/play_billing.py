"""Google Play Developer API receipt validation (AUTH-2 server tail).

Site config keys (site_config.json):

    play_billing_package_name          e.g. "com.codestech.ai.home.interior"
    play_billing_service_account_file  absolute path to the service-account
                                       JSON key, OR
    play_billing_service_account_json  the key inlined as a dict
    play_billing_relaxed               OPTIONAL, default false. When true and
                                       validation is NOT configured, callers
                                       may fall back to trusting the client
                                       (local QA only — never production).

Fail-closed by design: when the service account isn't configured and
`play_billing_relaxed` is off, `require_configured()` raises and the grant
endpoints refuse. That closes the trust-the-client hole even before the
Play Console / service-account setup is done.
"""

import frappe

_SCOPE = "https://www.googleapis.com/auth/androidpublisher"

# Values of Purchases.products purchaseState.
PRODUCT_STATE_PURCHASED = 0
PRODUCT_STATE_CANCELED = 1
PRODUCT_STATE_PENDING = 2

# subscriptionsv2 states under which the user is entitled to access. CANCELED
# means auto-renew was turned off but the paid period is still running —
# access continues until expiryTime.
_ENTITLED_SUB_STATES = {
    "SUBSCRIPTION_STATE_ACTIVE",
    "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
    "SUBSCRIPTION_STATE_CANCELED",
}


class PlayBillingError(Exception):
    """Raised for any validation failure. `code`/`http_status` map straight
    onto the API's `_error_response` shape."""

    def __init__(self, code, message, http_status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _conf():
    sc = frappe.get_site_config()
    return {
        "package_name": sc.get("play_billing_package_name"),
        "sa_file": sc.get("play_billing_service_account_file"),
        "sa_json": sc.get("play_billing_service_account_json"),
        "relaxed": bool(sc.get("play_billing_relaxed")),
    }


def is_configured():
    c = _conf()
    return bool(c["package_name"] and (c["sa_file"] or c["sa_json"]))


def relaxed_mode():
    """True only when validation is unconfigured AND the site explicitly
    opted into the QA fallback."""
    return not is_configured() and _conf()["relaxed"]


def require_configured():
    if not is_configured():
        raise PlayBillingError(
            "VALIDATION_UNAVAILABLE",
            "Purchase validation is not available right now. Please try again later.",
            503,
        )


def _service():
    """Build (and memoize per-process) the androidpublisher client."""
    svc = getattr(frappe.local, "_play_billing_service", None)
    if svc is not None:
        return svc

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    c = _conf()
    try:
        if c["sa_json"]:
            creds = service_account.Credentials.from_service_account_info(
                c["sa_json"], scopes=[_SCOPE]
            )
        else:
            creds = service_account.Credentials.from_service_account_file(
                c["sa_file"], scopes=[_SCOPE]
            )
        svc = build("androidpublisher", "v3", credentials=creds, cache_discovery=False)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "play_billing.service_init")
        raise PlayBillingError(
            "VALIDATION_UNAVAILABLE",
            "Purchase validation is misconfigured on the server.",
            503,
        )
    frappe.local._play_billing_service = svc
    return svc


def _http_error_status(exc):
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _parse_play_time(value):
    """RFC3339 UTC timestamp (or epoch-millis string) → naive datetime in the
    site timezone, comparable with `frappe.utils.now_datetime()`."""
    from datetime import datetime, timezone

    if not value:
        return None
    dt = None
    s = str(value)
    if s.isdigit():
        dt = datetime.fromtimestamp(int(s) / 1000.0, tz=timezone.utc)
    else:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    try:
        from frappe.utils import convert_utc_to_system_timezone

        return convert_utc_to_system_timezone(dt.replace(tzinfo=None))
    except Exception:
        return dt.replace(tzinfo=None)


def verify_subscription(purchase_token):
    """Validate a subscription purchase token against Play.

    Returns::

        {
          "entitled": bool,        # user should currently have access
          "state": str,            # raw subscriptionState
          "product_id": str|None,  # from Play's line items — server truth,
                                   # NOT the client-claimed product id
          "expires_at": datetime|None,  # site-tz naive
          "obfuscated_account_id": str|None,
          "linked_purchase_token": str|None,
        }

    Raises PlayBillingError on any failure (including "token not found").
    """
    require_configured()
    from googleapiclient.errors import HttpError

    c = _conf()
    try:
        resp = (
            _service()
            .purchases()
            .subscriptionsv2()
            .get(packageName=c["package_name"], token=purchase_token)
            .execute()
        )
    except HttpError as e:
        if _http_error_status(e) in (400, 404, 410):
            raise PlayBillingError(
                "INVALID_RECEIPT", "This purchase could not be verified with Google Play.", 400
            )
        frappe.log_error(frappe.get_traceback(), "play_billing.verify_subscription")
        raise PlayBillingError(
            "PLAY_API_ERROR", "Google Play verification failed. Please try again.", 502
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "play_billing.verify_subscription")
        raise PlayBillingError(
            "PLAY_API_ERROR", "Google Play verification failed. Please try again.", 502
        )

    state = resp.get("subscriptionState") or ""
    line = (resp.get("lineItems") or [{}])[0]
    ext = resp.get("externalAccountIdentifiers") or {}
    return {
        "entitled": state in _ENTITLED_SUB_STATES,
        "state": state,
        "product_id": line.get("productId"),
        "expires_at": _parse_play_time(line.get("expiryTime")),
        "obfuscated_account_id": ext.get("obfuscatedExternalAccountId"),
        "linked_purchase_token": resp.get("linkedPurchaseToken"),
    }


def verify_product(purchase_token, product_id):
    """Validate a one-time (consumable) product purchase token against Play.

    Returns::

        {
          "purchase_state": int,   # 0 purchased / 1 canceled / 2 pending
          "order_id": str|None,    # stable Play order id — best dedup key
          "obfuscated_account_id": str|None,
        }

    Raises PlayBillingError on any failure.
    """
    require_configured()
    from googleapiclient.errors import HttpError

    c = _conf()
    try:
        resp = (
            _service()
            .purchases()
            .products()
            .get(packageName=c["package_name"], productId=product_id, token=purchase_token)
            .execute()
        )
    except HttpError as e:
        if _http_error_status(e) in (400, 404, 410):
            raise PlayBillingError(
                "INVALID_RECEIPT", "This purchase could not be verified with Google Play.", 400
            )
        frappe.log_error(frappe.get_traceback(), "play_billing.verify_product")
        raise PlayBillingError(
            "PLAY_API_ERROR", "Google Play verification failed. Please try again.", 502
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "play_billing.verify_product")
        raise PlayBillingError(
            "PLAY_API_ERROR", "Google Play verification failed. Please try again.", 502
        )

    try:
        purchase_state = int(resp.get("purchaseState"))
    except (TypeError, ValueError):
        purchase_state = PRODUCT_STATE_CANCELED
    return {
        "purchase_state": purchase_state,
        "order_id": resp.get("orderId"),
        "obfuscated_account_id": resp.get("obfuscatedExternalAccountId"),
    }
