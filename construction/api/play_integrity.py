"""Google Play Integrity — device attestation for anonymous account creation.

Why this exists
---------------
Account creation is unauthenticated by design: there is no sign-up, so
`anon_bootstrap` will mint an identity for anyone who asks. Each new identity
collects the one-time welcome credit grant. The only things bounding that were
a per-IP daily cap and the global spend kill switch, and the code said so
plainly — they are speed bumps, not a defence. A script rotating device ids
across addresses farms credits at whatever rate it can find IPs.

Play Integrity closes it at the right layer: the device proves to Google that
it is a genuine, Play-installed copy of this app, and Google signs the verdict.
A farm cannot produce that signature, and a stolen token cannot be reused for a
different device id because the nonce is derived from the device id itself.

Site config
-----------
    play_integrity_package_name         defaults to play_billing_package_name
    play_integrity_service_account_file absolute path to a service-account JSON
    play_integrity_service_account_json the key inlined as a dict
        Both default to the play_billing service account. Whichever key you
        use, the service account needs the Play Integrity API enabled in its
        Google Cloud project and must be linked in Play Console.

    play_integrity_mode   off | audit | enforce      (default: enforce)

        off      No verification at all. The pre-attestation behaviour.
        audit    Verify a token when the client sends one and record the
                 verdict, but never withhold anything. This is the mode to sit
                 in while a client build that actually sends tokens rolls out.
        enforce  A passing verdict is REQUIRED for the welcome credit grant.
                 The account is still created either way.

Why `enforce` gates the grant and not the account
-------------------------------------------------
Refusing account creation would brick every already-installed client the moment
the mode flips, and would hand a hard outage to anyone whose device genuinely
cannot attest (rooted phone, custom ROM, sideloaded build, Play Services
missing). Those users can still use the app; they just do not receive free
credits. That targets the actual risk — free credits at scale — without
denying the product to real people.

The rollout ladder is: ship the client that sends tokens, set
`play_integrity_mode: "audit"` until the verdict distribution looks sane, then
remove the key (or set `enforce`) to turn it on.

NOTE the default is `enforce`, not `audit`. A site that has never set the key
is enforcing. Any client that cannot produce a token — an older build, iOS
(`PlayIntegrityService` returns null off Android), a device without Play
Services, an offline first launch — receives NO welcome credits. That is the
intended trade; it is called out here because it is not what an unset config
key usually means.
"""

import base64
import hashlib
import json

import frappe

_SCOPE = "https://www.googleapis.com/auth/playintegrity"

# Same reasoning as play_billing: an unbounded socket to Google pins a worker.
_HTTP_TIMEOUT_SEC = 15

# Process-wide, not request-wide.
_SERVICE = None

MODE_OFF = "off"
MODE_AUDIT = "audit"
MODE_ENFORCE = "enforce"
_VALID_MODES = (MODE_OFF, MODE_AUDIT, MODE_ENFORCE)

# The verdicts we require. `MEETS_DEVICE_INTEGRITY` is the middle tier: a
# genuine Android device with a verified boot state. We deliberately do NOT
# require `MEETS_STRONG_INTEGRITY` (hardware-backed, recent security update) —
# it excludes a lot of legitimate older hardware, which for a construction app
# in its target markets is a large share of real users.
_REQUIRED_DEVICE_VERDICT = "MEETS_DEVICE_INTEGRITY"

# `PLAY_RECOGNIZED` means the binary and certificate match what Play
# distributes. `UNRECOGNIZED_VERSION` covers sideloaded or modified builds —
# exactly the shape a farming harness takes.
_REQUIRED_APP_VERDICT = "PLAY_RECOGNIZED"


class PlayIntegrityError(Exception):
    """Verification could not be completed. Never raised to reject a device —
    a device that fails verification is a verdict, not an error."""


def _conf():
    sc = frappe.get_site_config()
    return {
        "package_name": (
            sc.get("play_integrity_package_name")
            or sc.get("play_billing_package_name")
        ),
        "sa_file": (
            sc.get("play_integrity_service_account_file")
            or sc.get("play_billing_service_account_file")
        ),
        "sa_json": (
            sc.get("play_integrity_service_account_json")
            or sc.get("play_billing_service_account_json")
        ),
        "mode": (sc.get("play_integrity_mode") or MODE_ENFORCE),
    }


def mode():
    """Current enforcement mode. Defaults to `enforce`, and an unrecognised
    value falls back to `enforce` too.

    That is a deliberate choice to fail closed: the thing being protected is
    the welcome credit grant, and an operator typo must not quietly reopen
    the farming vector. The cost is borne by devices that cannot attest —
    they still get an account, they just do not get free credits (see the
    module docstring). Set `play_integrity_mode` to `audit` explicitly while
    rolling out a client build that sends tokens.
    """
    m = str(_conf()["mode"]).strip().lower()
    if m not in _VALID_MODES:
        frappe.log_error(
            "play_integrity_mode=%r is not one of %s; treating as 'enforce'."
            % (m, ", ".join(_VALID_MODES)),
            "play_integrity.bad_mode",
        )
        return MODE_ENFORCE
    return m


def is_configured():
    c = _conf()
    return bool(c["package_name"] and (c["sa_file"] or c["sa_json"]))


def expected_nonce(device_id):
    """The nonce this device must have baked into its integrity token.

    Derived from the device id, so a token minted for one device id cannot be
    replayed to claim another. URL-safe base64 with no padding, which is what
    the Play Integrity client expects.
    """
    digest = hashlib.sha256(("anon:" + (device_id or "")).encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _service():
    """Build (and memoize per-process) the Play Integrity client."""
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
                creds, http=httplib2.Http(timeout=_HTTP_TIMEOUT_SEC)
            )
            svc = build(
                "playintegrity", "v1", http=authed_http, cache_discovery=False
            )
        except ImportError:
            frappe.log_error(
                "google_auth_httplib2 unavailable — Play Integrity calls will "
                "not be bounded by a timeout.",
                "play_integrity.no_timeout",
            )
            svc = build(
                "playintegrity", "v1", credentials=creds, cache_discovery=False
            )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "play_integrity.service_init")
        raise PlayIntegrityError("Play Integrity is misconfigured on the server")
    _SERVICE = svc
    return svc


def _decode(integrity_token):
    """Ask Google to decode and verify the token. Returns the payload dict."""
    c = _conf()
    svc = _service()
    try:
        resp = (
            svc.v1()
            .decodeIntegrityToken(
                packageName=c["package_name"],
                body={"integrityToken": integrity_token},
            )
            .execute()
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "play_integrity.decode")
        raise PlayIntegrityError("Could not decode the integrity token")
    return resp.get("tokenPayloadExternal") or {}


def evaluate(integrity_token, device_id):
    """Verify `integrity_token` for `device_id`.

    Returns a dict:
        {"checked": bool,      # did we actually reach Google?
         "passed": bool,       # did the device meet the bar?
         "reason": str,        # short machine-ish label, for logs and storage
         "verdict": dict}      # the parts of the payload worth keeping

    Never raises for a failing device — that is a verdict, not an error. Raises
    `PlayIntegrityError` only when verification itself could not be performed,
    so the caller can decide whether that should block (it should not: an
    outage at Google must not stop people using the app).
    """
    if not integrity_token:
        return {
            "checked": False,
            "passed": False,
            "reason": "NO_TOKEN",
            "verdict": {},
        }
    if not is_configured():
        raise PlayIntegrityError("Play Integrity is not configured")

    payload = _decode(integrity_token)

    request_details = payload.get("requestDetails") or {}
    app_integrity = payload.get("appIntegrity") or {}
    device_integrity = payload.get("deviceIntegrity") or {}
    account_details = payload.get("accountDetails") or {}

    device_verdicts = device_integrity.get("deviceRecognitionVerdict") or []
    app_verdict = app_integrity.get("appRecognitionVerdict") or ""

    verdict = {
        "device": device_verdicts,
        "app": app_verdict,
        "licensing": account_details.get("appLicensingVerdict") or "",
        "package": request_details.get("requestPackageName")
        or app_integrity.get("packageName")
        or "",
    }

    # 1. The token has to be about THIS app. A token minted for another package
    #    is a valid Google signature over a statement about something else.
    expected_package = _conf()["package_name"]
    if verdict["package"] and expected_package and verdict["package"] != expected_package:
        return {
            "checked": True,
            "passed": False,
            "reason": "WRONG_PACKAGE",
            "verdict": verdict,
        }

    # 2. The nonce has to match the device id being claimed, or one device's
    #    token could be replayed to bless an unlimited number of new ids —
    #    which would leave the farming vector exactly where it was.
    nonce = request_details.get("nonce") or ""
    if nonce != expected_nonce(device_id):
        return {
            "checked": True,
            "passed": False,
            "reason": "NONCE_MISMATCH",
            "verdict": verdict,
        }

    # 3. Genuine, unmodified, Play-distributed binary.
    if app_verdict != _REQUIRED_APP_VERDICT:
        return {
            "checked": True,
            "passed": False,
            "reason": "APP_%s" % (app_verdict or "UNKNOWN"),
            "verdict": verdict,
        }

    # 4. Genuine Android device.
    if _REQUIRED_DEVICE_VERDICT not in device_verdicts:
        return {
            "checked": True,
            "passed": False,
            "reason": "DEVICE_%s" % (",".join(device_verdicts) or "NONE"),
            "verdict": verdict,
        }

    return {"checked": True, "passed": True, "reason": "OK", "verdict": verdict}


def summarize(result):
    """Compact, storable form of an `evaluate()` result — this goes into
    `Anon Device.attestation_verdict`, which is Data(140)."""
    try:
        return json.dumps(
            {
                "r": result.get("reason"),
                "p": 1 if result.get("passed") else 0,
                "d": (result.get("verdict") or {}).get("device"),
                "a": (result.get("verdict") or {}).get("app"),
            },
            separators=(",", ":"),
        )[:140]
    except Exception:
        return (result.get("reason") or "")[:140]
