"""Keyless client for the public ClawHub skill registry.

Discovery uses the JSON API and installs download the same ZIP the ClawHub
CLI uses. Extracted folders are ordinary skills, so the normal discovery
path picks them up. Only the standard library is used.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

BASE_URL = "https://clawhub.ai"
USER_AGENT = "Logos/0.1 (Skill catalogue; +https://github.com/AIgrator/Logos)"
TIMEOUT_SECONDS = 20
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_FILES = 200


class CatalogError(Exception):
    """User-readable catalogue failure."""


def user_root() -> str:
    """Default install target: the user's Logos skills folder."""
    return os.path.join(os.path.expanduser("~"), ".logos", "skills")


def target_path(slug: str, root: str | None = None) -> str:
    """Folder an install of ``slug`` would use."""
    folder = _safe_slug(slug)
    return os.path.join(root or user_root(), folder)


def popular(limit: int = 30) -> list[dict]:
    """Most downloaded skills, healthy entries only."""
    data = _get_json("/api/v1/skills", {
        "limit": int(limit),
        "sort": "downloads",
        "nonSuspiciousOnly": "true",
    })
    return [_normalize(item) for item in data.get("items") or []
            if item.get("slug")]


def search(query: str, limit: int = 30) -> list[dict]:
    """Fuzzy search over the catalogue."""
    query = " ".join(str(query or "").split())
    if not query:
        return popular(limit)
    data = _get_json("/api/v1/search", {
        "q": query,
        "limit": int(limit),
        "nonSuspiciousOnly": "true",
    })
    return [_normalize(item) for item in data.get("results") or []
            if item.get("slug")]


def details(owner: str, slug: str) -> dict:
    """Fresh metadata for one skill, including the declared license."""
    data = _get_json(f"/api/v1/skills/{urllib.parse.quote(slug)}",
                     {"ownerHandle": owner} if owner else None)
    skill = dict(data.get("skill") or {})
    skill["ownerHandle"] = ((data.get("owner") or {}).get("handle")
                            or owner)
    skill["latestVersion"] = data.get("latestVersion") or {}
    skill["stats"] = skill.get("stats") or {}
    result = _normalize(skill)
    if not result["license"]:
        result["license"] = _frontmatter_license(skill.get("description"))
    return result


def install(owner: str, slug: str, root: str | None = None,
            overwrite: bool = False) -> str:
    """Download and unpack one skill; returns the installed folder."""
    folder = _safe_slug(slug)
    if not folder:
        raise CatalogError("The catalogue entry has no valid folder name.")
    root = root or user_root()
    target = os.path.join(root, folder)
    if os.path.isdir(target) and not overwrite:
        raise CatalogError(
            f"'{folder}' is already installed. Replace it to continue.")

    archive_bytes = _download(owner, slug)
    os.makedirs(root, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".logos-skill-", dir=root)
    backup = None
    try:
        _extract(archive_bytes, staging)
        if not os.path.isfile(os.path.join(staging, "SKILL.md")):
            raise CatalogError(
                "The downloaded archive has no SKILL.md at its root, so it "
                "is not a valid Agent Skill.")
        if os.path.isdir(target):
            backup = target + ".old-" + os.path.basename(staging)[-6:]
            os.replace(target, backup)
        try:
            os.replace(staging, target)
        except OSError:
            if backup:
                os.replace(backup, target)
                backup = None
            raise
        staging = ""
        logging.info(f"Skill catalogue: installed {folder} from "
                     f"{owner or 'clawhub'} into {target}.")
        return target
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        if backup:
            shutil.rmtree(backup, ignore_errors=True)


# -- HTTP ---------------------------------------------------------------

def _get_json(path: str, params: dict | None = None) -> dict:
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    raw = _read(url, MAX_JSON_BYTES)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError(
            "The skill catalogue returned an unexpected response.") from exc
    if not isinstance(data, dict):
        raise CatalogError(
            "The skill catalogue returned an unexpected response.")
    return data


def _read(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/zip;q=0.9, */*;q=0.5",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise CatalogError(
                "The skill catalogue is rate-limiting requests. Try again "
                "in a minute.") from exc
        raise CatalogError(
            f"The skill catalogue returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise CatalogError(
            "Could not reach the skill catalogue. Check the internet "
            "connection.") from exc
    except (TimeoutError, OSError) as exc:
        raise CatalogError(
            "The skill catalogue did not respond in time.") from exc
    if len(data) > limit:
        raise CatalogError("The skill catalogue response is too large.")
    return data


def _download(owner: str, slug: str) -> bytes:
    params = {"slug": _safe_slug(slug)}
    if owner:
        params["ownerHandle"] = owner
    url = BASE_URL + "/api/v1/download?" + urllib.parse.urlencode(params)
    raw = _read(url, MAX_ARCHIVE_BYTES)
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        raise CatalogError(
            "The catalogue did not return a ZIP archive for this skill. "
            "Install it from the source repository instead.")
    return raw


# -- archive safety -----------------------------------------------------

def _extract(raw: bytes, destination: str) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise CatalogError(
            "The downloaded skill is not a valid ZIP archive.") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_FILES:
            raise CatalogError("The downloaded skill has too many files.")
        total = 0
        for info in infos:
            name = info.filename
            if "\\" in name or name.startswith("/") or re.match(
                    r"^[A-Za-z]:", name):
                raise CatalogError(
                    "The downloaded skill contains an unsafe path.")
            if any(part == ".." for part in name.split("/")):
                raise CatalogError(
                    "The downloaded skill contains an unsafe path.")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise CatalogError(
                    "The downloaded skill contains a symbolic link.")
            total += info.file_size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise CatalogError("The downloaded skill is too large.")
        archive.extractall(destination)


# -- helpers ------------------------------------------------------------

def _normalize(item: dict) -> dict:
    owner = str(item.get("ownerHandle") or "").strip()
    slug = str(item.get("slug") or "").strip()
    reference = str((item.get("install") or {}).get("reference") or "")
    if not owner and "/" in reference:
        owner = reference.split("/", 1)[0]
    latest = item.get("latestVersion") or {}
    downloads = item.get("downloads")
    if downloads is None:
        downloads = (item.get("stats") or {}).get("downloads") or 0
    canonical = (item.get("links") or {}).get("canonical") or ""
    url = (BASE_URL + canonical if canonical.startswith("/")
           else f"{BASE_URL}/{owner}/skills/{slug}")
    return {
        "owner": owner,
        "slug": slug,
        "reference": f"{owner}/{slug}" if owner else slug,
        "name": str(item.get("displayName") or slug),
        "summary": " ".join(str(
            item.get("summary") or "").split()),
        "downloads": int(downloads or 0),
        "license": latest.get("license") or None,
        "version": latest.get("version") or "",
        "url": url,
    }


def _frontmatter_license(text) -> str | None:
    if not text or "---" not in str(text)[:200]:
        return None
    match = re.search(r"^license:\s*(.+?)\s*$", str(text), re.MULTILINE)
    if not match:
        return None
    value = match.group(1).strip().strip("\"'")
    return value[:200] or None


def _safe_slug(value) -> str:
    raw = str(value or "").strip().lower()
    candidate = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
    candidate = re.sub(r"-{2,}", "-", candidate)
    return candidate[:64].strip("-")
