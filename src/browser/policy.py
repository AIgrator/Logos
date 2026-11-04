"""Safety helpers for browser automation: URL policy and secret redaction.

The browser is a powerful SSRF/exfiltration surface: a model can be tricked
into navigating to ``http://127.0.0.1:<port>`` or an internal host by text on
a page. Every navigation (including redirects) therefore passes
``validate_url`` before a request is allowed.

This is a policy layer, not a sandbox: it blocks obvious local/private
targets and masks secrets in logs/prompts. User-facing approvals for risky
actions live in ``browser_service`` / ``src/handlers/tool_exec_worker.py``.
"""
import ipaddress
import socket
import threading
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import url2pathname

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

# Tool results that carry page-authored text (untrusted data, not orders).
UNTRUSTED_RESULT_TOOLS = frozenset({
    "browser_read", "browser_snapshot", "browser_find", "browser_eval",
    "browser_console", "browser_network",
})

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


_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")

# How far the controlled browser may reach. The default is `any`: this is the
# user's own browser on their own machine, and a dev server on
# 127.0.0.1:3000 or a page opened through the LAN address is a normal way to
# work. The two stricter modes exist because the agent reads page text, and a
# page can contain instructions ("open the router admin page", "read this
# file"); a user who hands the browser to such a page can narrow access here.
NETWORK_ACCESS_MODES = ("any", "local", "public")
NETWORK_ACCESS_LABELS = {
    "any": "Any address",
    "local": "Internet and this computer",
    "public": "Internet only",
}
DEFAULT_NETWORK_ACCESS = "any"


def normalize_network_access(value, default: str = DEFAULT_NETWORK_ACCESS) -> str:
    """Maps a stored setting value to one of NETWORK_ACCESS_MODES."""
    if isinstance(value, bool):
        # The older `block_private_networks` flag was never shown in the UI, so
        # `True` (its shipped default) meant "nobody chose anything": fall back
        # to the default. An explicit `False` still means unrestricted.
        return DEFAULT_NETWORK_ACCESS if value else "any"
    text = str(value or "").strip().lower()
    return text if text in NETWORK_ACCESS_MODES else default


def resolve_network_access(settings: dict | None) -> str:
    """Reads browser settings into a network-access mode.

    ``network_access`` wins; ``block_private_networks`` is the pre-0.2 boolean
    form and is still honoured so an existing settings.json is not ignored.
    """
    data = settings if isinstance(settings, dict) else {}
    if str(data.get("network_access") or "").strip():
        return normalize_network_access(data.get("network_access"))
    if "block_private_networks" in data:
        return normalize_network_access(data.get("block_private_networks"))
    return DEFAULT_NETWORK_ACCESS


def _embedded_ipv4(ip):
    """Returns the IPv4 address an IPv6 address only wraps, or None.

    A NAT64 resolver answers with 64:ff9b::<v4> for an IPv4-only site, and that
    prefix sits inside ::/8, which Python reports as IETF-reserved: a public
    site (ukr.net -> 64:ff9b::23ba:da43 = 35.186.218.67) looked local. The
    wrapped IPv4 is what the policy is about, so 64:ff9b::7f00:1 is still the
    loopback and stays blocked. IPv4-mapped (::ffff:0:0/96) is the same idea;
    the local-use NAT64 prefix (64:ff9b:1::/48) is deliberately left alone.
    """
    if ip.version != 6:
        return None
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64_WELL_KNOWN:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _address_kind(ip) -> str | None:
    """Classifies one address: 'loopback', 'local', or None for the internet.

    0.0.0.0 and :: count as loopback because browsers treat them as this
    machine. Any IPv6 address wrapping an IPv4 one is classified by the wrapped
    address (see _embedded_ipv4).
    """
    embedded = _embedded_ipv4(ip)
    if embedded is not None:
        ip = embedded
    if ip.is_loopback or ip.is_unspecified:
        return "loopback"
    if (ip.is_private or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or not ip.is_global):
        return "local"
    return None


def _is_blocked_ip(address: str, network_access: str = DEFAULT_NETWORK_ACCESS) -> bool:
    mode = normalize_network_access(network_access)
    try:
        kind = _address_kind(ipaddress.ip_address(address))
    except ValueError:
        return True
    if kind is None or mode == "any":
        return False
    # "Internet and this computer" allows the loopback only; a LAN address
    # still needs "Any address".
    return not (kind == "loopback" and mode == "local")




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


def _validate_local_file(text: str, parts) -> str:
    """Allows any existing local file: this is the user's own browser.

    A project-root restriction would not protect anything — Browser mode
    already reads files through ``read_files`` / ``search_file_content`` — and
    it blocks the obvious intent: opening a local HTML page or a saved file.
    Only a host is refused, because ``file://server/share`` is a network path
    with different semantics.
    """
    if (parts.netloc or "").lower() not in ("", "localhost"):
        raise UrlBlockedError(
            f"Refusing to open '{text[:200]}': local files must use the form "
            f"file:///<path> without a host."
        )
    try:
        target = Path(url2pathname(parts.path)).resolve()
    except (OSError, ValueError) as e:
        raise UrlBlockedError(
            f"Refusing to open '{text[:200]}': {e}") from e
    if not target.is_file():
        raise UrlBlockedError(
            f"Refusing to open '{text[:200]}': the file does not exist."
        )
    return text


def _refusal(host: str, kind: str, mode: str) -> UrlBlockedError:
    """Builds the model-readable refusal for a loopback/local target."""
    label = NETWORK_ACCESS_LABELS.get(mode, mode)
    where = "this computer (loopback)" if kind == "loopback" else "the local network"
    return UrlBlockedError(
        f"Refusing to open '{host}': it is {where}, and browser network access "
        f"is set to '{label}' (Settings → Browser). Browser tasks may only use "
        f"the addresses that setting allows."
    )


def validate_url(url: str, allowed_domains=None,
                 network_access: str = DEFAULT_NETWORK_ACCESS,
                 block_private_networks=None) -> str:
    """Validates a URL for browser navigation/requests.

    Returns the normalized URL. Raises UrlBlockedError with a model-readable
    message otherwise. ``network_access`` decides how far the browser may
    reach (see NETWORK_ACCESS_MODES, default "any" = no restriction); the
    allowlist can only narrow access, it never re-enables a blocked target.
    ``file://`` accepts any existing local file.
    """
    mode = normalize_network_access(
        network_access if block_private_networks is None else
        ("public" if block_private_networks else "any"))
    text = (url or "").strip()
    if not text:
        raise UrlBlockedError("Refusing to open an empty URL.")
    try:
        parts = urlsplit(text)
    except ValueError as e:
        raise UrlBlockedError(f"Refusing to open invalid URL '{text[:200]}': {e}") from e
    scheme = (parts.scheme or "").lower()
    if scheme == "file":
        return _validate_local_file(text, parts)
    if scheme not in _ALLOWED_SCHEMES:
        raise UrlBlockedError(
            f"Refusing to open '{text[:200]}': only http:// and https:// URLs are allowed."
        )
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise UrlBlockedError(f"Refusing to open '{text[:200]}': the URL has no host.")
    if host in _BLOCKED_HOSTNAMES:
        # "localhost" is this machine; the other names are loopback aliases.
        if mode == "public":
            raise UrlBlockedError(
                f"Refusing to open local host '{host}': browser network access is "
                f"set to '{NETWORK_ACCESS_LABELS['public']}' (Settings → Browser)."
            )
    elif host.endswith(_BLOCKED_SUFFIXES):
        # .local / .internal / .home.arpa are local-network names, never public.
        if mode != "any":
            raise UrlBlockedError(
                f"Refusing to open local-network host '{host}': browser network "
                f"access is set to '{NETWORK_ACCESS_LABELS.get(mode, mode)}' "
                f"(Settings → Browser)."
            )
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        kind = _address_kind(literal)
        if kind is not None and mode != "any" and not (
                kind == "loopback" and mode == "local"):
            raise _refusal(host, kind, mode)
    elif mode != "any":
        for address in _resolve(host):
            try:
                kind = _address_kind(ipaddress.ip_address(address))
            except ValueError:
                kind = "local"
            if kind is not None and not (kind == "loopback" and mode == "local"):
                raise _refusal(host, kind, mode)
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
    elif action_name == "browser_eval":
        script = str(args.get("script") or "").strip().replace("\n", " ")
        if len(script) > 200:
            script = script[:200] + "…"
        checks.append({
            "code": "page_javascript",
            "message": f"Runs JavaScript inside the page: {script or '(empty)'}",
        })
    return checks
