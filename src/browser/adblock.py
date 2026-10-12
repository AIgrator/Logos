"""EasyList-based ad detection and request blocking (no new dependencies).

The industry solution for "is this an ad?" is community filter lists, not a
hand-written host list. This module downloads EasyList
(https://easylist.to/easylist/easylist.txt), caches it under
cache/, and evaluates the network-filter subset:
  - blocking rules (domain anchors ``||host^``, substrings, wildcards),
  - ``@@`` exceptions (checked first, as upstream intends),
  - options: ``$popup`` (new-tab ads), ``$third-party``/``$~third-party``,
    ``$domain=``, resource types (script/image/...) and ``$match-case``.

Two entry points used by BrowserManager:
  - matches_popup(url, opener_url): new-tab ad detection for [AD?] marks.
  - should_block(url, resource_type, opener_url): Playwright route decisions.
    Never blocks non-http(s) URLs (data:/blob:/about:/file:).

When no list is available (offline first run), a tiny built-in host-hint
fallback keeps obvious ad networks flagged. Rules refresh in a background
thread when the cache is older than MAX_AGE_DAYS.
"""

import logging
import os
import re
import threading
import time
import urllib.request
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

EASYLIST_URL = "https://easylist.to/easylist/easylist.txt"
DEFAULT_CACHE_FILE = os.path.join("cache", "easylist.txt")
MAX_AGE_DAYS = 7

#: Offline fallback when EasyList is unavailable.
FALLBACK_AD_HOSTS = (
    "doubleclick", "googlesyndication", "googleadservices", "adsbygoogle",
    "popads", "popcash", "propellerads", "adnxs", "criteo", "taboola",
    "outbrain", "revcontent", "mgid", "adsterra",
)

_ABP_TYPES = frozenset({
    "script", "image", "stylesheet", "object", "subdocument",
    "xmlhttprequest", "websocket", "webrtc", "font", "media",
    "ping", "other", "document", "popup",
})

#: Playwright resource_type -> ABP type options.
_RESOURCE_MAP = {
    "document": {"document"},
    "stylesheet": {"stylesheet"},
    "image": {"image"},
    "media": {"media"},
    "font": {"font"},
    "script": {"script"},
    "texttrack": {"other"},
    "xhr": {"xmlhttprequest"},
    "fetch": {"xmlhttprequest"},
    "eventsource": {"xmlhttprequest"},
    "websocket": {"websocket"},
    "manifest": {"other"},
    "other": {"other"},
}


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _is_third_party(url_host: str, opener_host: str) -> bool:
    if not url_host or not opener_host or url_host == opener_host:
        return False
    if url_host.endswith("." + opener_host) or opener_host.endswith("." + url_host):
        return False
    return True


def _pattern_to_regex(pattern: str) -> "re.Pattern | None":
    """Converts an ABP address pattern (no anchors) to a regex, or None."""
    try:
        out = []
        for ch in pattern:
            if ch == "*":
                out.append(".*")
            elif ch == "^":
                out.append("[^a-zA-Z0-9_.%-]")
            else:
                out.append(re.escape(ch))
        return re.compile("".join(out), re.IGNORECASE)
    except re.error:
        return None


class _Rule:
    __slots__ = ("domain", "path_regex", "regex", "start_anchor",
                 "end_anchor", "options", "is_exception", "raw")

    def __init__(self, domain=None, path_regex=None, regex=None,
                 start_anchor=False, end_anchor=False,
                 options=frozenset(), is_exception=False, raw=""):
        self.domain = domain
        self.path_regex = path_regex
        self.regex = regex
        self.start_anchor = start_anchor
        self.end_anchor = end_anchor
        self.options = options
        self.is_exception = is_exception
        self.raw = raw


def _split_options(rule: str):
    """Splits 'pattern$options' — only when the tail looks like options."""
    if "$" not in rule:
        return rule, frozenset()
    head, tail = rule.rsplit("$", 1)
    if re.fullmatch(r"[a-zA-Z~_,=|\-.\/]+", tail or "") and any(
            c.isalpha() for c in tail):
        opts = frozenset(o for o in tail.split(",") if o)
        if opts and all(o.split("=")[0].strip("~") in _ABP_TYPES
                        or o in ("third-party", "~third-party", "match-case")
                        or o.startswith("domain=") for o in opts):
            return head, opts
    return rule, frozenset()


def _parse_rule(line: str):
    line = line.strip()
    if not line or line.startswith("!") or line.startswith("["):
        return None
    # Cosmetic / snippet / scriptlet rules are not network filters.
    if ("##" in line or "#@#" in line or "#?#" in line or "#$#" in line):
        return None
    is_exception = line.startswith("@@")
    if is_exception:
        line = line[2:]
    pattern, options = _split_options(line)
    domain, regex = None, None
    if pattern.startswith("||"):
        rest = pattern[2:]
        domain = re.split(r"[\^/]", rest, maxsplit=1)[0].lower()
        if not domain:
            return None
        path_pat = rest[len(domain):]
        path_regex = None
        if path_pat and path_pat != "^":
            path_regex = _pattern_to_regex(path_pat)
            if path_regex is None:
                return None
        return _Rule(domain=domain, path_regex=path_regex, options=options,
                     is_exception=is_exception, raw=line)
    start_anchor = pattern.startswith("|")
    if start_anchor:
        pattern = pattern[1:]
    end_anchor = pattern.endswith("|")
    if end_anchor:
        pattern = pattern[:-1]
    if pattern:
        regex = _pattern_to_regex(pattern)
        if regex is None:
            return None
    else:
        return None
    return _Rule(domain=domain, regex=regex, start_anchor=start_anchor,
                 end_anchor=end_anchor, options=options,
                 is_exception=is_exception, raw=line)


def _rule_matches(rule: _Rule, url: str, url_host: str, opener_host: str,
                  abp_types: set, popup_wanted: bool) -> bool:
    opts = rule.options
    has_popup = "popup" in opts
    # matches_popup() only consults $popup rules;
    # should_block() skips them (they mark tabs, not requests).
    if popup_wanted and not has_popup:
        return False
    if not popup_wanted and has_popup:
        return False
    if "third-party" in opts and not _is_third_party(url_host, opener_host):
        return False
    if "~third-party" in opts and _is_third_party(url_host, opener_host):
        return False
    for opt in opts:
        if opt.startswith("domain="):
            allowed = [d for d in opt[7:].split("|") if d and not d.startswith("~")]
            excluded = [d[1:] for d in opt[7:].split("|") if d.startswith("~")]
            ref = (opener_host or url_host).lower()
            if allowed and not any(ref == a.lower() or ref.endswith("." + a.lower()) for a in allowed):
                return False
            if any(ref == e.lower() or ref.endswith("." + e.lower()) for e in excluded):
                return False
    match_case = "match-case" in opts
    if rule.domain:
        if not (url_host == rule.domain or url_host.endswith("." + rule.domain)):
            return False
        if rule.path_regex is not None:
            hay = url if "match-case" in opts else url.lower()
            if not rule.path_regex.search(hay):
                return False
    elif rule.regex is not None:
        hay = url if match_case else url.lower()
        if not rule.regex.search(hay):
            return False
    type_opts = {o for o in opts if o in _ABP_TYPES - {"popup", "document"}}
    if type_opts and not (type_opts & abp_types):
        return False
    if rule.start_anchor and rule.domain is None:
        return url.lower().startswith(rule.raw.split("$")[0].lstrip("|").lower())
    return True


class EasyListEngine:
    """Domain-indexed matcher over EasyList network rules."""

    def __init__(self, cache_path: str = DEFAULT_CACHE_FILE, max_age_days: int = MAX_AGE_DAYS):
        self.cache_path = cache_path
        self.max_age_days = max_age_days
        self._lock = threading.Lock()
        self._domain_rules: dict = {}
        self._generic_rules: list = []
        self._loaded = False
        self.rule_count = 0

    # -- loading ------------------------------------------------------

    def ensure_loaded(self) -> bool:
        """Parses the cache synchronously; refreshes stale lists in background.

        No cache at all (first run) downloads synchronously once — later runs
        use the cache instantly and refresh stale lists in the background.
        """
        with self._lock:
            if self._loaded:
                return self.rule_count > 0
            text = self._read_cache()
            if text:
                self._parse(text)
                if not self._is_stale():
                    return self.rule_count > 0
            elif self._download():
                text = self._read_cache()
                if text:
                    self._parse(text)
                    return self.rule_count > 0
            if not getattr(self, "_refreshing", False):
                self._refreshing = True
                threading.Thread(target=self._refresh_in_background, daemon=True).start()
            return self.rule_count > 0

    def _download(self) -> bool:
        """Downloads the list into the cache file. Returns success."""
        try:
            req = urllib.request.Request(EASYLIST_URL, headers={"User-Agent": "LogosBrowser/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            os.makedirs(os.path.dirname(os.path.abspath(self.cache_path)), exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                f.write(text)
            return True
        except Exception as e:
            logger.warning(f"EasyList download failed (keeping cache/fallback): {e}")
            return False

    def _read_cache(self):
        try:
            with open(self.cache_path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return None

    def _is_stale(self) -> bool:
        try:
            age_days = (time.time() - os.path.getmtime(self.cache_path)) / 86400.0
            return age_days > self.max_age_days
        except OSError:
            return True

    def _refresh_in_background(self):
        if self._download():
            with self._lock:
                text = self._read_cache()
                if text:
                    self._parse(text)
            logger.info(f"EasyList refreshed: {self.rule_count} network rules.")
        with self._lock:
            self._refreshing = False

    def _parse(self, text: str) -> None:
        domain_rules: dict = {}
        generic: list = []
        count = 0
        for line in text.splitlines():
            rule = _parse_rule(line)
            if rule is None:
                continue
            count += 1
            if rule.domain:
                domain_rules.setdefault(rule.domain, []).append(rule)
            else:
                generic.append(rule)
        self._domain_rules = domain_rules
        self._generic_rules = generic
        self._loaded = True
        self.rule_count = count

    def load_rules_text(self, text: str) -> None:
        """Injects rules directly (tests)."""
        with self._lock:
            self._parse(text)

    # -- matching -----------------------------------------------------

    def _candidates(self, url_host: str):
        yield from self._generic_rules
        if not url_host:
            return
        yield from self._domain_rules.get(url_host, [])
        parts = url_host.split(".")
        for i in range(1, len(parts)):
            yield from self._domain_rules.get(".".join(parts[i:]), [])

    @staticmethod
    def _abp_types(resource_type: str | None) -> set:
        if not resource_type:
            return set(_ABP_TYPES)
        return _RESOURCE_MAP.get(str(resource_type).lower(), {"other"})

    def _match_any(self, url: str, opener_url: str, abp_types: set,
                   popup_wanted: bool, only_exceptions: bool) -> bool:
        url_host = _host_of(url)
        opener_host = _host_of(opener_url)
        for rule in self._candidates(url_host):
            if rule.is_exception != only_exceptions:
                continue
            if _rule_matches(rule, url, url_host, opener_host, abp_types, popup_wanted):
                return True
        return False

    def matches_popup(self, url: str, opener_url: str = "") -> bool:
        """True when EasyList marks this new-tab URL as an ad popup."""
        if not self.rule_count:
            return False
        if self._match_any(url, opener_url, {"popup"}, True, True):
            return False  # explicit @@ exception wins
        return self._match_any(url, opener_url, {"popup"}, True, False)

    def should_block(self, url: str, resource_type: str | None = None,
                     opener_url: str = "") -> bool:
        """True when the request should be aborted. Never blocks non-http(s)."""
        try:
            scheme = (urlsplit(url).scheme or "").lower()
        except Exception:
            return False
        if scheme not in ("http", "https"):
            return False
        if not self.rule_count:
            return False
        abp_types = self._abp_types(resource_type)
        if self._match_any(url, opener_url, abp_types, False, True):
            return False
        return self._match_any(url, opener_url, abp_types, False, False)

    # -- offline fallback ----------------------------------------------

    @staticmethod
    def fallback_ad_host(url: str, title: str = "") -> bool:
        low = f"{url or ''} {title or ''}".lower()
        return any(h in low for h in FALLBACK_AD_HOSTS)
