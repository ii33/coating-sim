# Built-in research provider contract

2026-10-18. The host owns provider selection per request. An empty configured root
uses the shipped, read-only research corpus when no legacy external sidecar is
present. A nonempty root or an existing sidecar selects an external/private
source; invalid configuration never silently falls back to another source.

Inputs are host configuration and the bounded packaged manifest, never model
arguments. The model receives only bounded search/read/status operations. The
packaged root is relative to the installed module (including frozen module
resources), independent of the process working directory. Only manifest-listed
text documents are exposed; path traversal, links, nonregular files and checksum
mismatches fail closed. Manifest version and limited coverage are returned with
results; shipped corpus availability never implies the whole remote repository
was reviewed. No cloning, network access, indexing, or runtime writes occur.

The host is the sole source-selection state owner. Public shipped reference
material does not require access to private project context. External roots keep
the separate model-context permission and live grant/cancellation checks.
Changing the root invalidates the current request epoch even if it later changes
back. In-flight results are withheld after revocation. Tools never rebase onto a
new root midway through a request.

Missing or corrupt packaged resources return `builtin_missing`, with repair or
reinstallation guidance rather than requesting an absolute path. Invalid external
configuration returns `unreadable`; denied external context returns
`context_not_authorized`. Neither exposes private metadata or document contents.
Status reports `source_kind`, version, actual readiness, and scope. Search/read
preserve existing bounded pagination, scan budgets, and untrusted-reference rules.

Interface truth: `agent/research_library.py`, `agent/worker.py`, and
`tests/test_assistant_builtin_research_library.py`.
