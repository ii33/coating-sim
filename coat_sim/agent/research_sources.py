"""Opt-in OpenAlex/Crossref REST adapters, not official MCP services.

Only bounded user search phrases or identifiers leave this module. Constructors
do not connect, discover credentials or inspect project/chat content. Results
are untrusted source claims, never instructions or simulation evidence.
"""
from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from coat_sim.agent.research_http import JsonResponse, ResearchHttpError, SafeHttpClient


MAX_QUERY_CHARS = 240
MAX_RESULTS = 100
_DOI = re.compile(r'10\.[0-9]{4,9}/[^\s<>"\\]{1,480}', re.IGNORECASE)
_WORK = re.compile(r"W[0-9]{1,20}", re.IGNORECASE)


def normalize_doi(value: Any) -> str:
    """Normalize an explicit DOI, not a bibliographic similarity judgment."""
    if not isinstance(value, str) or len(value) > 550:
        raise ValueError("invalid_doi")
    doi = value.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):].strip()
            break
    if not _DOI.fullmatch(doi) or any(ord(char) < 33 or ord(char) == 127 for char in doi):
        raise ValueError("invalid_doi")
    return doi.lower()


def _query(value: str) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > MAX_QUERY_CHARS
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("invalid_query")
    return value.strip()


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_RESULTS:
        raise ValueError("invalid_limit")
    return value


def _text(value: Any, cap: int = 500, secret: str = "") -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    text = str(value)
    if secret:
        text = text.replace(secret, "[redacted]")
    return "".join(char for char in text if ord(char) >= 32 and ord(char) != 127)[:cap]


def _url(value: Any, secret: str = "") -> str:
    text = _text(value, 2000, secret)
    try:
        parts = urlsplit(text)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            return ""
        # No provider token or signed URL is saved into evidence metadata.
        return urlunsplit(parts._replace(query="", fragment=""))
    except ValueError:
        return ""


def _doi_or_empty(value: Any) -> str:
    try:
        return normalize_doi(value)
    except ValueError:
        return ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _sequence(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _year(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 1000 <= value <= 3000 else None


def _usage(response: JsonResponse, secret: str = "") -> dict[str, Any]:
    headers = {key: _text(value, 128, secret) for key, value in response.headers.items()
               if "ratelimit" in key or "rate-limit" in key or key == "retry-after"}
    meta = _mapping(_mapping(response.data).get("meta"))
    # Costs are reported, never inferred to be free or permission to top up.
    reported = {key: value for key, value in meta.items()
                if key in {"cost_usd", "credits_used", "count", "per_page"}
                and isinstance(value, (int, float)) and not isinstance(value, bool)}
    return {"headers": headers, "reported": reported,
            "billing_note": "Provider budgets/prices apply; no purchase or recharge is performed."}


def _failure(source: str, exc: ValueError, secret: str = "") -> dict[str, Any]:
    if isinstance(exc, ResearchHttpError):
        return {"ok": False, "source": source, "error": exc.code,
                "status": exc.status, "usage": {"headers": {
                    key: _text(value, 128, secret) for key, value in exc.headers.items()}}}
    return {"ok": False, "source": source, "error": _text(str(exc), 100, secret)}


def _openalex_record(raw: Mapping[str, Any], *, secret: str = "",
                     verified: bool = False) -> dict[str, Any]:
    location = _mapping(raw.get("best_oa_location"))
    primary = _mapping(raw.get("primary_location"))
    content = _mapping(raw.get("content_urls"))
    has_content = _mapping(raw.get("has_content"))
    authors = []
    for entry in _sequence(raw.get("authorships"))[:20]:
        name = _text(_mapping(_mapping(entry).get("author")).get("display_name"), 160, secret)
        if name:
            authors.append(name)
    return {
        "source": "openalex", "id": _url(raw.get("id"), secret),
        "doi": _doi_or_empty(_text(raw.get("doi"), 550, secret)),
        "title": _text(raw.get("display_name") or raw.get("title"), 1000, secret),
        "authors": authors, "year": _year(raw.get("publication_year")),
        "url": _url(location.get("landing_page_url") or primary.get("landing_page_url"), secret),
        "pdf_url": _url(location.get("pdf_url"), secret),
        "license": _text(location.get("license") or primary.get("license"), 256, secret),
        "content_urls": {key: _url(content[key], secret) for key in ("pdf", "grobid_xml", "tei")
                         if content.get(key) and _url(content[key], secret)},
        "has_content": {key: has_content.get(key) is True for key in ("pdf", "grobid_xml", "tei")},
        "doi_verified": verified, "verification": "exact_identifier" if verified else "search_candidate",
        "evidence_type": "source_statement", "untrusted_content": True,
    }


def _crossref_record(raw: Mapping[str, Any], *, verified: bool = False) -> dict[str, Any]:
    titles = _sequence(raw.get("title"))
    authors = [" ".join(filter(None, (_text(_mapping(author).get("given"), 80),
                                     _text(_mapping(author).get("family"), 120))))
               for author in _sequence(raw.get("author"))[:20]]
    dates = _sequence(_mapping(raw.get("published")).get("date-parts"))
    parts = _sequence(dates[0]) if dates else []
    licenses = [_url(_mapping(item).get("URL")) for item in _sequence(raw.get("license"))[:10]]
    links = [{"url": _url(_mapping(item).get("URL")),
              "content_type": _text(_mapping(item).get("content-type"), 100)}
             for item in _sequence(raw.get("link"))[:10]]
    doi = _doi_or_empty(raw.get("DOI"))
    return {
        "source": "crossref", "id": doi, "doi": doi,
        "title": _text(titles[0] if titles else "", 1000),
        "authors": [name for name in authors if name], "year": _year(parts[0]) if parts else None,
        "url": _url(raw.get("URL")), "licenses": [value for value in licenses if value],
        "license": "; ".join(value for value in licenses if value),
        "links": [link for link in links if link["url"]],
        "doi_verified": verified, "verification": "exact_identifier" if verified else "search_candidate",
        "evidence_type": "source_statement", "untrusted_content": True,
    }


class OpenAlexSource:
    """Project REST adapter; credentials must be explicitly supplied by the user."""

    def __init__(self, client: SafeHttpClient | None = None, *, api_key: str = "") -> None:
        if not isinstance(api_key, str) or len(api_key) > 2048 or any(ord(c) < 33 for c in api_key):
            raise ValueError("invalid_api_key")
        self.client = client or SafeHttpClient(allowed_hosts={"api.openalex.org"})
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self._api_key} if self._api_key else {}

    def search(self, query: str, limit: int = 6, cursor: str | None = None) -> dict[str, Any]:
        try:
            phrase, count = _query(query), _limit(limit)
            if cursor is not None and (not isinstance(cursor, str) or not cursor
                                       or len(cursor) > 2048
                                       or any(ord(c) < 32 for c in cursor)):
                raise ValueError("invalid_cursor")
            if cursor not in (None, "*") and not self._api_key:
                raise ValueError("api_key_required_for_cursor")
            params = {"search": phrase, "per_page": str(count)}
            if cursor is not None:
                params["cursor"] = cursor
            response = self.client.get_json("https://api.openalex.org/works?" + urlencode(params),
                                            headers=self._headers())
            raw = _mapping(response.data)
            if not isinstance(raw.get("results"), list):
                raise ValueError("invalid_provider_response")
            records = [_openalex_record(item, secret=self._api_key)
                       for item in raw["results"][:count] if isinstance(item, dict)]
            next_cursor = _text(_mapping(raw.get("meta")).get("next_cursor"), 2048, self._api_key)
            return {"ok": True, "source": "openalex", "adapter": "project_rest_not_official_mcp",
                    "results": records, "cached": response.cached,
                    "next_cursor": next_cursor if self._api_key else None,
                    "cursor_requires_key": bool(next_cursor and not self._api_key),
                    "usage": _usage(response, self._api_key), "untrusted_content": True}
        except ValueError as exc:
            return _failure("openalex", exc, self._api_key)

    def lookup(self, identifier: str) -> dict[str, Any]:
        try:
            if not isinstance(identifier, str):
                raise ValueError("invalid_identifier")
            work_id = identifier.removeprefix("https://openalex.org/")
            expected_doi = ""
            if _WORK.fullmatch(work_id):
                path = work_id.upper()
            else:
                expected_doi = normalize_doi(identifier)
                path = "https://doi.org/" + quote(expected_doi, safe="/")
            response = self.client.get_json("https://api.openalex.org/works/" + path,
                                            headers=self._headers())
            raw = _mapping(response.data)
            actual_id = str(raw.get("id", "")).removeprefix("https://openalex.org/").upper()
            if (expected_doi and _doi_or_empty(raw.get("doi")) != expected_doi
                    or not expected_doi and actual_id != path):
                raise ValueError("identifier_conflict")
            record = _openalex_record(raw, secret=self._api_key, verified=bool(expected_doi))
            if not expected_doi:
                record["verification"] = "exact_openalex_id"
            return {"ok": True, "source": "openalex", "adapter": "project_rest_not_official_mcp",
                    "work": record, "cached": response.cached,
                    "usage": _usage(response, self._api_key), "untrusted_content": True}
        except ValueError as exc:
            return _failure("openalex", exc, self._api_key)


class CrossrefSource:
    """Public Crossref REST adapter; mailto is never inferred from user history."""

    def __init__(self, client: SafeHttpClient | None = None, *, mailto: str = "") -> None:
        if not isinstance(mailto, str) or (mailto and (len(mailto) > 254
                or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", mailto))):
            raise ValueError("invalid_mailto")
        self.client = client or SafeHttpClient(allowed_hosts={"api.crossref.org"})
        self._mailto = mailto

    def _params(self, values: dict[str, str]) -> str:
        if self._mailto:
            values["mailto"] = self._mailto
        return urlencode(values)

    def search(self, query: str, limit: int = 6, cursor: str | None = None) -> dict[str, Any]:
        try:
            phrase, count = _query(query), _limit(limit)
            if cursor is not None:
                raise ValueError("cursor_not_supported")
            response = self.client.get_json("https://api.crossref.org/works?" + self._params(
                {"query.bibliographic": phrase, "rows": str(count)}))
            message = _mapping(_mapping(response.data).get("message"))
            if not isinstance(message.get("items"), list):
                raise ValueError("invalid_provider_response")
            records = [_crossref_record(item) for item in message["items"][:count]
                       if isinstance(item, dict)]
            return {"ok": True, "source": "crossref", "adapter": "project_rest_not_official_mcp",
                    "results": records, "cached": response.cached, "usage": _usage(response),
                    "untrusted_content": True, "match_note": "Bibliographic candidates require exact DOI lookup."}
        except ValueError as exc:
            return _failure("crossref", exc)

    def lookup(self, identifier: str) -> dict[str, Any]:
        try:
            doi = normalize_doi(identifier)
            url = "https://api.crossref.org/works/" + quote(doi, safe="/")
            if self._mailto:
                url += "?" + self._params({})
            response = self.client.get_json(url)
            message = _mapping(_mapping(response.data).get("message"))
            if _doi_or_empty(message.get("DOI")) != doi:
                raise ValueError("identifier_conflict")
            return {"ok": True, "source": "crossref", "adapter": "project_rest_not_official_mcp",
                    "work": _crossref_record(message, verified=True), "cached": response.cached,
                    "usage": _usage(response), "untrusted_content": True}
        except ValueError as exc:
            return _failure("crossref", exc)
