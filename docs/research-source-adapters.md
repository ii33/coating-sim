# Research source adapters

`coat_sim.agent.research_sources` provides project REST adapters for OpenAlex and
Crossref. These are **not official MCP servers**. The preset MCP layer exposes
them through the project's existing capability and permission boundary. Merely
constructing an adapter performs no network access; the caller must explicitly
enable the capability and request a search or lookup. Queries contain only the
supplied short search phrase or identifier. Projects, recipes, chat history and
local documents are never added to requests.

## Metadata contract

`OpenAlexSource(client=None, *, api_key="")` and
`CrossrefSource(client=None, *, mailto="")` implement
`search(query, limit=6, cursor=None)` and `lookup(identifier)`. Responses contain
`ok`, `source`, `results` or `work`, `cached`, and `usage`. Failures use bounded
machine-readable errors. Records carry DOI, provider ID, title, authors, year,
source URL, available fulltext links and original license information when
provided. Missing license or fulltext information remains missing.

Search results are candidates, including Crossref bibliographic matches. Only an
explicit DOI lookup whose response DOI agrees with the requested normalized DOI
sets `doi_verified=true`; an OpenAlex work-ID lookup confirms that ID only.
Results are marked `source_statement` and untrusted. They are neither assistant
inferences nor simulation results, and text from a source never authorizes an
action. This module does not write the project evidence library.

OpenAlex currently permits limited anonymous metadata queries. Continuing cursor
paging requires the user's key. The adapter accepts an explicitly supplied key
and sends it as a Bearer header, never a URL parameter. Hosted OpenAlex PDF/TEI
downloads also require a key and are metered. No account, key or recharge is
created automatically. See [OpenAlex authentication](https://help.openalex.org/api/authentication/)
and [fulltext access](https://help.openalex.org/access/fulltext/).

Crossref's public REST API requires no account or key. The optional `mailto`
parameter is sent only if the user explicitly configures it; no email is
inferred. The adapter uses `/works` with `query.bibliographic` and exact
`/works/{doi}` lookup. Abstracts/fulltext may have separate rights from metadata.
See [Crossref REST documentation](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)
and its [official parameter reference](https://github.com/CrossRef/rest-api-doc).

## HTTP boundary

`research_http.SafeHttpClient.get_bytes(url, *, headers=None, max_bytes=None)`
returns `HttpResponse(status, headers, body, url, cached)`. `get_json` returns
`JsonResponse(data, headers, status, url, cached)`. `ResearchHttpError` exposes
`code`, `status`, and selected usage headers, without provider error bodies.
Resolvers and transports can be injected for deterministic offline tests.

The stdlib transport allows HTTPS port 443, validates all DNS answers as global
addresses, then connects to a validated IP while verifying TLS against the
original hostname. Local/private/link-local/metadata addresses, userinfo,
dangerous schemes and IPv6 transition addresses are rejected. Each redirect is
revalidated; cross-origin redirects drop authentication, custom headers and all
query fields. It ignores environment proxies, cookies and automatic credentials.
Signed cross-origin URLs may consequently fail safely. Returned metadata URLs
omit query strings; no signed access token is persisted as citation metadata.

Project defaults are sequential requests at least 0.5 seconds apart, 24 attempts
per client/session (including failed resolution), two retries, three redirects,
15 seconds per hop and 45 seconds per operation. Hard limits include 60 seconds
per operation and 32 MiB per file. Rate/usage response fields remain visible;
they do not promise free usage or authorize a purchase. HTTP 403 never triggers
an anonymous fallback. HTTP 429/5xx retries are bounded; long or unrecognized
`Retry-After` values return a failure rather than retrying early. The small
credential-separated memory cache expires and is never written to disk.

`tests/test_research_http.py` and `tests/test_research_sources.py` exercise these
contracts using synthetic responses, without live searches or paid calls.
