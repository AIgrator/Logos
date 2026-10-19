"""Safety helpers for browser automation: URL policy and secret redaction.

The browser is a powerful SSRF/exfiltration surface: a model can be tricked
into navigating to ``http://127.0.0.1:<port>`` or an internal host by text on
a page. Every navigation (including redirects) therefore passes
``validate_url`` before a request is allowed.

This is a policy layer, not a sandbox: it blocks obvious local/private
targets and masks secrets in logs/prompts. User-facing approvals for risky
actions live in ``browser_handler`` / ``src/handlers/tool_exec_worker.py``.
"""
import ipaddress
import socket
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTNAMES = {
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")

_SENSITIVE_QUERY_KEYS = {
    "token", "access_token", "refresh_token", "id_token", "api_key",
    "apikey", "key", "secret", "client_secret", "password", "passwd",
    "pwd", "session", "sessionid", "session_id", "auth", "authorization",
    "code", "jwt", "sig", "signature", "credential", "credentials",
}
_SENSITIVE_FIELD_WORDS = {
    "password", "passwd", "passcode", "pwd", "otp", "one-time",
    "verification code", "security code", "cvv", "cvc", "card number",
    "credit card", "debit card", "ssn", "social security", "secret",
    "api key", "token", "private key", "seed phrase", "recovery phrase",
}

_dns_cache: dict[str, tuple[str, ...]] = {}
_dns_lock = threading.Lock()


class UrlBlockedError(ValueError):
    """Raised when a URL violates the browser network policy."""


class BrowserCancelledError(Exception):
    """Raised inside a running browser tool when it has been superseded."""


def _resolve(host: str) -> tuple[str, ...]:
    with _dns_lock:
        cached = _dns_cache.get(host)
    if cached is not None:
        return cached
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise UrlBlockedError(
            f"Refusing to open '{host}': the host could not be resolved ({e})."
        ) from e
    addresses = tuple({info[4][0] for info in infos})
    with _dns_lock:
        _dns_cache[host] = addresses
    return addresses


def _is_blocked_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_loopback or ip.is_private or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        or not ip.is_global
    )


def _host_allowed(host: str, allowed_domains) -> bool:
    if not allowed_domains:
        return True
    host = host.lower().rstrip(".")
    for entry in allowed_domains:
        entry = str(entry or "").strip().lower()
        if "://" in entry:
            entry = (urlsplit(entry).hostname or "").lower()
        entry = entry.lstrip("*.").lstrip(".").rstrip(".")
        if not entry:
            continue
        if host == entry or host.endswith("." + entry):
            return True
    return False


def validate_url(url: str, allowed_domains=None,
                 block_private_networks: bool = True) -> str:
    """Validates a URL for browser navigation/requests.

    Returns the normalized URL. Raises UrlBlockedError with a model-readable
    message otherwise. Private/loopback targets are always blocked when
    ``block_private_networks`` is true; the allowlist can only narrow access,
    it never re-enables a blocked network target.
    """
    text = (url or "").strip()
    if not text:
        raise UrlBlockedError("Refusing to open an empty URL.")
    try:
        parts = urlsplit(text)
    except ValueError as e:
        raise UrlBlockedError(f"Refusing to open invalid URL '{text[:200]}': {e}") from e
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UrlBlockedError(
            f"Refusing to open '{text[:200]}': only http:// and https:// URLs are allowed."
        )
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise UrlBlockedError(f"Refusing to open '{text[:200]}': the URL has no host.")
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise UrlBlockedError(
            f"Refusing to open local host '{host}'. Browser tasks may only use public web pages."
        )
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if block_private_networks and _is_blocked_ip(str(literal)):
            raise UrlBlockedError(
                f"Refusing to open private/loopback address '{host}'. "
                f"Browser tasks may only use public web pages."
            )
    elif block_private_networks:
        for address in _resolve(host):
            if _is_blocked_ip(address):
                raise UrlBlockedError(
                    f"Refusing to open '{host}': it resolves to a private/loopback "
                    f"address ({address}). Browser tasks may only use public web pages."
                )
    if not _host_allowed(host, allowed_domains):
        raise UrlBlockedError(
            f"Refusing to open '{host}': it is not in browser.allowed_domains."
        )
    return text


def _mask_value(value: str) -> str:
    if not value:
        return value
    return "***"


def redact_url(url: str) -> str:
    """Masks userinfo and secret-looking query values for logs/prompts."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "***@" + netloc.rsplit("@", 1)[1]
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        masked = []
        for key, value in pairs:
            lowered = key.lower()
            if lowered in _SENSITIVE_QUERY_KEYS or any(
                    marker in lowered for marker in ("token", "secret", "password",
                                                     "passwd", "api_key", "apikey")):
                masked.append((key, _mask_value(value)))
            else:
                masked.append((key, value))
        query = urlencode(masked, doseq=True).replace("%2A%2A%2A", "***")
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def is_sensitive_text(*values) -> bool:
    """Heuristic for password/OTP/card/secret field names and descriptions."""
    joined = " ".join(str(v or "") for v in values).lower()
    return any(word in joined for word in _SENSITIVE_FIELD_WORDS)


def classify_action(action_name: str, action_args: dict, element: dict | None = None,
                    current_url: str = "") -> list[dict]:
    """Returns approval checks for actions that can cause real-world effects.

    The model may be steered by untrusted page text, so code (not the model)
    decides when a human must confirm. Fail-closed: unknown extra effects are
    not auto-approved.
    """
    args = action_args or {}
    element = element or {}
    checks: list[dict] = []
    if action_name in ("browser_click", "browser_press"):
        text = " ".join(str(v or "") for v in (
            args.get("description"), element.get("name"), element.get("role"),
        )).lower()
        risky = {
            "buy", "purchase", "checkout", "pay", "payment", "order",
            "delete", "remove", "destroy", "send", "submit", "publish",
            "post", "confirm", "accept", "transfer", "subscribe",
            "unsubscribe", "cancel subscription", "book", "reserve",
            "download", "install", "upload",
        }
        hits = sorted(word for word in risky if word in text)
        if hits:
            checks.append({
                "code": "risky_action",
                "message": f"{action_name} may commit an irreversible/published action "
                           f"({', '.join(hits)}).",
            })
    elif action_name == "browser_fill":
        if is_sensitive_text(args.get("description"), element.get("name"),
                             element.get("role"), element.get("type")):
            checks.append({
                "code": "sensitive_field",
                "message": "Text will be typed into a password/secret/payment field.",
            })
    elif action_name == "browser_handle_dialog":
        accept = args.get("accept", True)
        if isinstance(accept, str):
            accept = accept.strip().lower() not in ("false", "no", "0", "")
        if accept:
            checks.append({
                "code": "dialog_accept",
                "message": "The next JavaScript dialog will be accepted.",
            })
    elif action_name == "browser_tabs":
        if str(args.get("action") or "").lower() == "close":
            checks.append({
                "code": "close_tab",
                "message": f"Tab {args.get('index', '(current)')} will be closed.",
            })
    return checks
