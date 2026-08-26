"""Google Play Developer API receipt validation (AUTH-2 server tail).

Site config keys (site_config.json):

    play_billing_package_name          e.g. "com.codestech.ai.home.interior"
    play_billing_service_account_file  absolute path to the service-account
                                       JSON key, OR
    play_billing_service_account_json  the key inlined as a dict

Fail-closed, with no exceptions: when the service account isn't configured,
`require_configured()` raises and the grant endpoints refuse. There is no
escape hatch.

There used to be one — `play_billing_relaxed`, which let a developer_mode
site grant 31 days of Pro and any credit pack from a made-up string. It is
removed. A key that turns receipt validation into theatre is not worth the
convenience, and the only thing standing between it and production was an
operator never copying one line into the wrong site_config.

To exercise purchases locally, use Play Console **license testers**: they
issue real purchase tokens against real products, so the code under test is
the code that runs in production.
"""

import frappe

_SCOPE = "https://www.googleapis.com/auth/androidpublisher"

# These are the only network calls in the codebase that gate money, and they
# were the only ones with no timeout at all — a hung Play API socket would pin
# a gunicorn worker until the OS gave up, while the user's purchase sat
# unacknowledged.
_PLAY_HTTP_TIMEOUT_SEC = 15

# Process-wide, not request-wide. The client was memoized on `frappe.local`,
# which is torn down after every request, so each call paid for a fresh
# discovery-document build and a fresh OAuth token exchange.
_SERVICE = None

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
    }


def is_configured():
    c = _conf()
    return bool(c["package_name"] and (c["sa_file"] or c["sa_json"]))


def warn_if_relaxed_configured():
    """Log if a site still carries the removed `play_billing_relaxed` key.

    The key no longer does anything. Saying so once is worth more than silence:
    whoever set it believed validation was being skipped, and should find out
    that it is not, rather than discovering it through a support ticket.
    """
    if frappe.get_site_config().get("play_billing_relaxed"):
        frappe.log_error(
            "play_billing_relaxed is set but no longer exists. Receipt "
            "validation is always enforced. Remove the key from site_config; "
            "use Play Console license testers for QA.",
            "play_billing.relaxed_removed",
        )


def require_configured():
    if not is_configured():
        raise PlayBillingError(
            "VALIDATION_UNAVAILABLE",
            "Purchase validation is not available right now. Please try again later.",
            503,
        )


def _service():
    """Build (and memoize per-process) the androidpublisher client."""
    global _SERVICE
    if _SERVICE is not None:
        return _SERVICE

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
        try:
            import google_auth_httplib2
            import httplib2

            authed_http = google_auth_httplib2.AuthorizedHttp(
                creds, http=httplib2.Http(timeout=_PLAY_HTTP_TIMEOUT_SEC)
            )
            svc = build(
                "androidpublisher", "v3", http=authed_http, cache_discovery=False
            )
        except ImportError:
            # Older google-api-python-client without google_auth_httplib2.
            # Untimed, but still better than refusing to validate at all.
            frappe.log_error(
                "google_auth_httplib2 unavailable — Play API calls will not be "
                "bounded by a timeout.",
                "play_billing.no_timeout",
            )
            svc = build(
                "androidpublisher", "v3", credentials=creds, cache_discovery=False
            )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "play_billing.service_init")
        raise PlayBillingError(
            "VALIDATION_UNAVAILABLE",
            "Purchase validation is misconfigured on the server.",
            503,
        )
    _SERVICE = svc
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
