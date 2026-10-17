"""Provider contract tests with synthetic bibliographic records only."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from coat_sim.agent.research_http import HttpResponse, SafeHttpClient
from coat_sim.agent.research_sources import CrossrefSource, OpenAlexSource, normalize_doi


def client_for(payload, status=200, headers=None):
    calls = []

    def transport(target, outgoing, timeout, cap):
        calls.append((target, dict(outgoing)))
        return HttpResponse(status, headers or {}, json.dumps(payload).encode(), "")

    client = SafeHttpClient(resolver=lambda _: ["93.184.216.34"], transport=transport,
                            min_interval_s=0, sleep=lambda _: None)
    return client, calls


def oa_record(**overrides):
    return {"id": "https://openalex.org/W123", "doi": "https://doi.org/10.1234/TEST",
            "display_name": "Synthetic coating source", "publication_year": 2025,
            "authorships": [{"author": {"display_name": "Example Author"}}],
            "best_oa_location": {"license": "cc-by", "pdf_url": "https://publisher.example/p.pdf"},
            "has_content": {"pdf": True},
            "content_urls": {"pdf": "https://content.openalex.org/works/W123.pdf"}, **overrides}


def test_openalex_anonymous_search_minimal_request_preserves_provenance_and_usage():
    client, calls = client_for({"results": [oa_record()], "meta": {"next_cursor": "next", "cost_usd": 0.01}},
                              headers={"X-RateLimit-Remaining": "99"})
    source = OpenAlexSource(client)
    assert calls == []
    result = source.search("synthetic coating", limit=6)
    assert result["ok"] and result["adapter"] == "project_rest_not_official_mcp"
    args = parse_qs(urlsplit(calls[0][0].url).query)
    assert args == {"search": ["synthetic coating"], "per_page": ["6"]}
    assert "authorization" not in calls[0][1]
    assert result["next_cursor"] is None and result["cursor_requires_key"]
    assert result["usage"]["reported"]["cost_usd"] == 0.01
    record = result["results"][0]
    assert record["doi"] == "10.1234/test" and not record["doi_verified"]
    assert record["evidence_type"] == "source_statement" and record["untrusted_content"]
    assert record["license"] == "cc-by" and record["has_content"]["pdf"]
    assert record["content_urls"]["pdf"].endswith("W123.pdf")


def test_openalex_cursor_beyond_first_requires_key_without_sending_request():
    client, calls = client_for({"results": [], "meta": {}})
    source = OpenAlexSource(client)
    assert source.search("coating", cursor="later")["error"] == "api_key_required_for_cursor"
    assert calls == []
    assert source.search("coating", cursor="*")["ok"]


def test_openalex_explicit_key_only_bearer_and_never_returned():
    secret = "synthetic-test-token"
    client, calls = client_for({"results": [oa_record(display_name="Title " + secret)],
                               "meta": {"next_cursor": "later"}})
    result = OpenAlexSource(client, api_key=secret).search("coating", cursor="later")
    assert calls[0][1]["authorization"] == "Bearer " + secret
    assert secret not in calls[0][0].url and secret not in json.dumps(result)
    assert result["next_cursor"] == "later"


@pytest.mark.parametrize("identifier", ["10.1234/TEST", "https://doi.org/10.1234/test"])
def test_exact_openalex_doi_is_verified_only_when_returned_doi_agrees(identifier):
    client, _ = client_for(oa_record())
    result = OpenAlexSource(client).lookup(identifier)
    assert result["ok"] and result["work"]["doi_verified"]
    client, _ = client_for(oa_record(doi="https://doi.org/10.1234/different"))
    assert OpenAlexSource(client).lookup(identifier)["error"] == "identifier_conflict"


def test_openalex_id_lookup_does_not_assert_unchecked_doi_is_verified():
    client, _ = client_for(oa_record())
    result = OpenAlexSource(client).lookup("https://openalex.org/W123")
    assert result["ok"] and not result["work"]["doi_verified"]
    assert result["work"]["verification"] == "exact_openalex_id"
    assert OpenAlexSource(client).lookup("W999")["error"] == "identifier_conflict"


def test_crossref_search_is_candidate_not_doi_verification_and_mailto_opt_in():
    payload = {"message": {"items": [{"DOI": "10.1234/test", "title": ["Synthetic title"],
                                     "published": {"date-parts": [[2024]]},
                                     "license": [{"URL": "https://license.example/cc-by"}]}]}}
    client, calls = client_for(payload)
    result = CrossrefSource(client).search("test title")
    assert result["ok"] and not result["results"][0]["doi_verified"]
    assert parse_qs(urlsplit(calls[0][0].url).query) == {"query.bibliographic": ["test title"], "rows": ["6"]}
    client, calls = client_for(payload)
    CrossrefSource(client, mailto="explicit@example.org").search("test")
    assert parse_qs(urlsplit(calls[0][0].url).query)["mailto"] == ["explicit@example.org"]


def test_crossref_lookup_checks_doi_conflict_and_handles_missing_fulltext():
    client, _ = client_for({"message": {"DOI": "10.1234/TEST", "title": ["Test"]}})
    result = CrossrefSource(client).lookup("doi:10.1234/test")
    assert result["ok"] and result["work"]["doi_verified"]
    assert result["work"]["links"] == [] and result["work"]["license"] == ""
    client, _ = client_for({"message": {"DOI": "10.1234/conflict"}})
    assert CrossrefSource(client).lookup("10.1234/test")["error"] == "identifier_conflict"


@pytest.mark.parametrize("provider", [OpenAlexSource, CrossrefSource])
@pytest.mark.parametrize("status", [403, 429, 503])
def test_provider_errors_are_safe_bounded_and_keep_status(provider, status):
    client, calls = client_for({"message": "secret provider body"}, status=status,
                              headers={"X-RateLimit-Remaining": "0"})
    result = provider(client).search("test")
    assert result["ok"] is False and result["status"] == status
    assert len(calls) == (1 if status == 403 else 3)
    assert "secret provider body" not in json.dumps(result)


@pytest.mark.parametrize("provider", [OpenAlexSource, CrossrefSource])
def test_invalid_input_never_calls_network(provider):
    client, calls = client_for({})
    source = provider(client)
    assert source.search("x" * 241)["error"] == "invalid_query"
    assert source.search("x", limit=101)["error"] == "invalid_limit"
    assert not source.lookup("https://127.0.0.1/project/private")["ok"]
    assert calls == []


def test_doi_normalization_is_not_url_or_similarity_guessing():
    assert normalize_doi("doi:10.1234/ABC") == "10.1234/abc"
    for value in ("matching title", "https://evil.example/10.1234/test", "10.1234/a b", None):
        with pytest.raises(ValueError):
            normalize_doi(value)


def test_malformed_provider_payload_is_rejected_and_excess_results_capped():
    client, _ = client_for({"results": "wrong"})
    assert OpenAlexSource(client).search("test")["error"] == "invalid_provider_response"
    client, _ = client_for({"message": {"items": "wrong"}})
    assert CrossrefSource(client).search("test")["error"] == "invalid_provider_response"
    client, _ = client_for({"results": [oa_record()] * 20})
    assert len(OpenAlexSource(client).search("test", limit=2)["results"]) == 2
