# Frozen synthesis history replay contract

当前更新（2026-10-24）：波段 mean/pointwise 两组、版本、raw coverage 与严格判定以[双约束契约](film-band-criteria-contract.md)为准。旧 flat 目标与旧协议不迁移。

Date: 2026-10-22

## Inputs, ownership and admission

The scheduler owns the immutable `spec.json` and original job/result record. Queue
readers never rewrite either file. A history result, current or legacy, is admitted
only after its queue root, job ID, executor, terminal lifecycle, input digest and
complete frozen request match a real `JobSpec` fingerprint. A failed/cancelled job
cannot inherit a completed result's success. Candidate topology, allowed materials,
physical environment, units, revision and both score-design identities are cheap
structural requirements, not proof of physical spectra.

`replay_synthesis_result` is the sole physical history replay entry point. It
re-evaluates every original training condition and the independent dense, offset
and extrema audit, comparing individual actual values and normalized/weighted
violations as well as aggregate scores. Saved acceptance, provenance/replay flags
and mutually consistent design copies are not trusted physical evidence.

## Background loading and errors

The queue page's single-worker executor owns listing replay and its cache. The
result viewer's reader executor owns selection replay. Neither QML property access,
`is_synthesis_result`, live execution receipts, nor cancellation/time-limit receipts
run TMM or a new audit. Invalid binding or physical replay makes a historical result
unavailable with a reason and an unknown nominal status, never a green badge.
Valid nominal failures remain viewable with a warning, independently of solver
tolerance. No manufacture margin is introduced or inferred.

The reader returns a typed wrapper containing the exact original source row and a
separate read-only replayed payload. The parsed original frozen request remains
the serialized/routing identity; a separately resolved execution copy supplies
host-dependent defaults for physical evaluation without retroactively changing
old optional execution metadata. Project routing re-reads that source, checks
current frozen JobSpec binding and selection identity, and requires the payload to
match the viewer's verified selection. Existing project epoch and ownership gates
remain authoritative. No original result is replaced by the replayed copy.

## Cache and state boundaries

Each queue view retains at most 64 small admission/status records, never complete
spectra or replayed result copies. Its digest binds the resolved queue root, job ID,
executor, lifecycle, input digest, current JobSpec fingerprint and the entire
canonical original result (including material snapshots, targets, grids and layer
IDs). Frozen input binding is checked before every cache lookup. Persisted replay
metadata cannot populate or bypass the cache. Source/spec/result changes therefore
invalidate admission without trusting timestamps or previous flags. Loading and
error states remain those of the existing asynchronous queue page; successful
background replay directly supplies the final nominal status, with no indefinite
pending badge.

These are project-defined trust and UI contracts, not a new optical algorithm or a
continuous-spectrum certificate. The replay uses the existing frozen TMM and
independent synthesis audit described in `film-synthesis-audit-contract.md` and
`algorithms.html`; its sampled-domain limitations still apply.
