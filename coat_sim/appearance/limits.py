"""Byte budgets for the appearance control and frozen-data protocols.

The public request remains small. A frozen render job can carry all captured
layer fields, so it has a separate bounded budget below the loopback RPC's
64 MiB request limit. These are serialized UTF-8 byte counts, not item counts.
"""

MAX_APPEARANCE_CONTROL_BYTES = 16_000_000
MAX_APPEARANCE_FROZEN_BYTES = 60_000_000
MAX_APPEARANCE_SPEC_BYTES = 60_000_000
