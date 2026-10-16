# Last successfully saved deposition configuration

Date: 2026-10-16. This changes opening policy, not algorithms or document formats.

The opening root is a managed `deposition_configuration` (`.cdep`), containing
the process identity and deposition calculation parameters. A `process` is only
its dependency. Saving a machine, process, film, material, recovery draft or
project information never guesses or creates a deposition configuration.

The existing manifest `default_configuration_id` stores the most recently
successfully saved configuration. Creating, copying or editing one configuration
updates this field in the same validated, CAS-protected `_publish` transaction.
The legacy `make_default` argument remains accepted but cannot suppress the new
policy. A rejected, interrupted or stale save changes neither the configuration
nor the opening root. Saving unchanged configuration content still commits a
root change when another configuration was saved more recently; if both content
and root already match, the existing no-op behavior is retained.

A transaction saving exactly one configuration selects that identity. An
unordered transaction saving multiple configurations retains the previous root;
mapping iteration and filenames supply no business order. An internal
`save_documents(default_configuration_id=...)` call can explicitly identify one
of the configurations saved in that batch. Sequential successful transactions
naturally retain their last successful configuration.

Selectively importing a configuration with its closure from an external project
or package is import, not an editor save, and retains the target's opening root.
Standalone `.cdep` dependency capture remains unsupported and rejects without
publication. Opening/reconstructing a complete project package preserves its
persisted source root. A subsequent explicit configuration Save uses the policy
above. No mtime, previous-window selection or unsaved session state is consulted.

Normal session opening reuses `load_project_default` and its validated dependency
closure. Empty and unconfigured projects keep their explicit initial states;
missing or corrupt configured dependencies fail without selecting a substitute.
Explicit recovery continues to preserve unsaved input through its existing
separate review. A successful save does not replace another live editor's draft.

The shared process operation bar retains explicit Save Configuration but removes
manual selection of the opening root. Internal compatibility APIs and the
persisted field remain available for package reconstruction and legacy clients.
