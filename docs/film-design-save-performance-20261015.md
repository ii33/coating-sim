# Film design save and generated naming checks

Date: 2026-10-15. Scope: the managed film editor's synchronous Save path,
parameterized generation identity and naming, and exact material provenance.
No solver, sampling algorithm, numeric precision, or file schema changes.

## Measured work

The save profile found two complete material refreshes per ordinary Save, repeated
normalization/deep-copy of already-owned optical datasets during provider
composition, and a full picker rebuild before every Save even when its source
values had not changed. It also found that generated candidates minted a new
internal design ID while Apply retained the managed handle, causing the next
managed Save to reject the design-ID mismatch.

Internal snapshot composition now shares normalized, exclusively-owned records.
Every public getter/picker result remains detached. Every ordinary Save still
reads mutable project/global sources and rejects a changed same-ID material.
A no-op refresh retains provider identity; a real change installs a new provider.
A successful publication refreshes project-owned content/roles without a second
read of the same global catalog. Atomic persistence, whole-project validation,
revision checks, dependency closure, optical tables, and full-precision layer
values remain intact. The final success signal is still after the store returns.

## Comparable non-profiled measurement

Both versions used the same application data directory, with 20 optical materials
visible including the synthetic project material. The final baseline used a small package overlay restoring only
`project_film_service.py`, `project_film_session.py`, and `film_design_session.py`
to their pre-change versions; both used the same project-store implementation.
The synthetic temporary project had six saved films, 80 layers per film, and a
2,001-point n/k table. Five consecutive name-edit + Save operations were measured
with `perf_counter`, timing Save only; fixture creation and the preceding name
edit were outside the timed interval. The temporary project was removed afterward.

- Baseline seconds: 1.462176, 1.277082, 1.313890, 1.454164, 1.280073
- Changed seconds: 0.581253, 0.644102, 0.496818, 0.488130, 0.550008
- Median: 1.313890 s → 0.550008 s, approximately 58.1% lower

An earlier same-workload cProfile run localized the CPU work: median 6.000 s →
2.350 s with instrumentation. Those times include profiler overhead and are not
user-facing latency measurements. The non-profiled comparison above is the
performance result. An initial copied-source run without the shared application
data was rejected as incomparable and is not used here.

Reproduce with the project's existing test environment:

    PYTHONPATH=. python tests/benchmark_film_design_save.py

For a baseline source copy, prepend that copy to PYTHONPATH and pass
`--application-root /absolute/path/to/the/same/data-owner-checkout`.
`--profile` enables the optional diagnostic profile. The benchmark verifies that
the final saved design exactly matches the edit session, and prints source path,
material count and catalog error so missing catalog data cannot silently skew it.
There is no timing threshold in pytest.

## Correctness and naming

Apply replaces current contents while preserving internal design ID and managed
handle; new layer IDs still represent the replaced stack. New/Import/Save Copy
remain explicit managed-document creation operations. Generation captures all
new material references, retains the exact selected optical-set IDs, rejects a
preview whose material source changed, and restores the original material overlay
on its single-step Undo. Frozen default drafts carry the complete generated
closure on first save. A material edited after Apply causes Save to fail without
changing the saved film or discarding the dirty draft.

Automatic names use a short current template description, actual pair count and
wavelength, for example `HR (HL)^8 · 550nm`. Repeated generation does not append
another title. Exact user names, including the Chinese name `膜系` and names at
the 120-character limit, stay intact. Only strict known automatic/legacy formats
are regenerated; no name provenance is inferred from free-form notes.

## Verification and limits

Focused contracts exercise the work count (one global catalog read and no picker
rebuild per unchanged Save), repeated Save source checks, same-ID stale-material
rejection, detached public snapshots, actual generation → managed save, explicit
Save Copy, full-precision thickness, selected datasets, preview invalidation,
Undo, and frozen-default dependency closure. Related project-film, catalog,
concurrency, generator/reference Qt, and controller tests were run separately
from the complete suite.

Independent review also reproduced and closed two boundary defects: an older
core preview could bypass source checks after a newer preview was built, and
Undo could preserve a later optimization material selection while restoring
an incomplete frozen first-save closure. Templates and reference recipes now
share the session's latest complete-candidate/source guard. Undo merges the
preserved workflow's references into the restored owned closure and rejects
incompatible references before changing state; a recoverable conflict keeps
Undo available. In-memory negative controls reinstating those two defects
caused both new regression tests to fail as expected.
The final review run passed 53 focused tests in 20.77 s, including real Qt
repeated generation/Undo and reference-recipe application, frozen transitions,
snapshot isolation and concurrency. This is a focused result, not a full-suite
or static-analysis result.

Measurements are from this Linux cloud filesystem, not Windows UI acceptance or
a guarantee for arbitrary project sizes. Save remains synchronous; the store
still validates/publishes the complete saved input graph, and every Save still
reads the current global catalog. Those retained safety costs and disk/fsync
latency can dominate large projects. No full suite, full static check, real
Windows UI benchmark, or GPU computation was run for this save-only change.
