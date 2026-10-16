# Resource information metadata

Date: 2026-10-15.

The public information projection is `name`, `summary`, `notes`, with
`editable_fields` and an optimistic revision `token`. Names are display metadata and never stable identities. On explicit save the
store derives a safe human-readable basename beneath its typed data/revision
directory; stable UUID references and previous bytes are retained. See
[the named-storage contract](project-core-contract.md#human-readable-resource-naming-and-project-information). No field changes an algorithm or physical input.

Persisted aliases:

- Source/substrate/mask/machine/process: `name`, `description`, `notes`
- Film/expression template: `name`, independent optional `summary`, `note`
- Deposition/ray/appearance configuration: `name`, `description`, `notes`

All newly introduced fields are optional and retained only when supplied. An
older normalized resource lacking a name/summary is not rewritten merely by
loading, browsing, or normalizing it. The UI may show a type/short-UUID placeholder
for an unnamed draft; saving an explicit name rejects whitespace-only text.
Source/machine/mask/Film/template/simulation names have a 120-character ceiling;
process/deposition configuration retain their existing 100-character limit.
Summary and notes retain the 20,000-character text bound and are independent.
Typed model and JSON serialization also omit an unprovided optional name;
their default is never emitted as `name: null`. Explicit name edits still
pass the core and Information text constraints, without adding physical defaults.

`ProjectResourceService.resource_information(id)` returns only the small saved
JSON metadata, not a heavyweight simulation/asset load.
`save_resource_information(id, fields, expected_revision=token)` validates and
saves only the provided information fields using the ordinary project
transaction. Unknown fields, invalid text, a stale revision, frozen owner or
invalid target graph fail without replacing the draft/manifest. New simulation
input saves accept all three metadata values in the same atomic transaction.

Metadata of an unsaved draft and an unconfirmed information dialog remains owned
by its editor. Closing/recovery must retain an unconfirmed buffer without
silently applying it; confirmation and actual resource save remain distinct.
Tests verify old resources stay normalized unchanged, all three fields survive
save/reopen/package/selective copy, numerical payloads are unchanged, and stale
or blank-name edits are rejected.
