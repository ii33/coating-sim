# Assistant offline research provider contract

2026-10-19. The canonical discovery and source-availability contract is
[Unified offline assistant library](unified-offline-library-contract.md).

The host owns provider selection per request. Fresh installations use the
manifest-allowlisted public text corpus through `UnifiedReferenceLibrary`, which
also discovers packaged material datasets, the packaged/installed RII snapshot,
and source-registry metadata. Existing `research_library_search/read/status`
tools use that same unified provider; MCP `library_search/read/status` are the
common public entry in every assistant mode. Data has no separate database page
or external-library path setup in Settings. The physical package resource paths
are relative to installed modules, independent of process cwd.

Existing explicit private roots or legacy sidecars retain their current host
selection, separate model-context authorization, request epoch and live
grant/cancellation checks. Invalid private configuration does not silently read
a different private root. Those compatibility settings and user research are
not deleted or rewritten by this integration. Private files never enter the
public packaged-data provider. Public library MCP remains independently usable.

Only manifest-listed text documents are exposed; traversal, links, nonregular
files and checksum mismatches fail closed. Search/read retain original source
URL, license, evidence scope, source commit/blob and the packaged content hash.
No linked work is fetched. Metadata-only registry sources are explicitly not
bundled full text. Rights to a dataset do not imply rights to a linked article.

The existing bounded search/read budgets, read-only behavior and untrusted-data
rules continue to apply. Source-specific failures and incomplete scans remain
visible while independent owners can still be searched. Missing selected storage
is reported without creating it; damaged selected resources are not substituted
with another version. No network, indexing job, import, material write or
automatic source installation occurs. A changed live grant withholds in-flight
local-tool results; a request never silently changes private scope.

Interface truth: `core/unified_reference_library.py`, `agent/research_library.py`,
`agent/worker.py`, `mcp/server.py` and the corresponding targeted contract tests.
