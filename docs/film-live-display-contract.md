# Live optimization display and candidate ranking

Updated: 2026-10-23. This is optional observation, not a new optimizer,
acceptance rule, saved optical result, or application authority.

## Ownership and supported paths

Ordinary thickness and structure optimization, hybrid automatic synthesis,
and both current ideal sensitivity policies emit observations
at existing complete-candidate boundaries. Analysis-only sensitivity and the
thickness-error table do not manufacture an optimization ranking. Finite
perturbation scenarios, finite-difference probes, needle derivatives, partial
responses, failed evaluations, discarded cancellation batches, and unconsumed
speculative batch work are not candidates.

The optimizer owns its actual incumbent. The display observer independently
owns at most ten small candidate summaries and current/incumbent immutable
condition-row references, not a design archive or Jacobian. The worker owns the
producer; the scheduler owns one latest in-memory packet per job; the GUI owns
one bounded latest mailbox. None of these replaces the formal result or its
request/replay/source/edit-epoch/confirmation checks.

## Display-only score

Let E be the existing complete frozen-target weighted RMS error; N the actual
number of front and back film rows; and D their physical thickness sum in nm.
Substrate and surrounding media do not count. A legitimate zero-thickness row
still counts; no deletion/merge is invented for a better display score.

Weights must be finite and positive and are normalized to sum to one. Positive
finite anchors are not process assumptions or layer/thickness limits:

Q = wE/(1+E) + wN/(1+N/Nref) + wD/(1+D/Dref)

S = 50 * original_nominal_targets_met + 50 * Q

Defaults are 90/5/5 and anchors 10 film rows / 1000 nm. These are project display
preferences, not a physical objective. They are frozen when submission begins;
edits during asynchronous preparation or an active job affect only the next
submission. Existing search fitness, strict champion, Pareto archive,
sensitivity F, nominal/guard acceptance, and apply behavior do not change.

Sort by the actual nominal boolean first, then Q descending, then canonical
physical-design hash ascending. Never sort by rounded S: a strict-boundary fail
can have E=0, and finite arithmetic/display rounding can make boundary scores
look equal. Value-target nominal comparison keeps its original semantics.
Physical identity ignores editor layer IDs and notes, uses converted physical
nm, and keeps material/set/topology distinctions. Duplicate physical designs
occupy one row. Frozen grid/material/target/configuration/context/scope must
match; training and independent audit responses are not interchangeable.

Each row contains score, RMS and maximum normalized violation, actual nominal
state, N, D, physical ID and source; sensitivity additionally shows guard and F
where available. Fewer than ten distinct complete candidates means fewer rows.
Normal complete but nominal/guard-failing trials may be displayed explicitly as
unusable. Ranking rows are read-only and cannot bypass result application.
The highest display score is not relabeled as the optimizer's best spectrum.

The comparison canvas uses live curves only for the active run with preview on
and a valid matching frame. Off, missing/mismatched frames and terminal/cancelled
runs keep or restore the existing formal original/candidate chart, with waiting
clearly labeled. A retained ranking never hides the reliable final spectrum.

## Preview and bounded work

First opening defaults to preview off; the same window remembers an explicit
choice. Top-ten metadata remains active when curves are off. Producer rate is
at most 1 Hz, with one terminal bypass using only an existing complete incumbent.
Each current/best curve has at most 1024 original-grid samples. The selected
objective, R/T metric and angle must already exist in that complete evaluation;
otherwise show waiting. No interpolation, extra wavelength, forward simulation,
Jacobian, or changed optimization grid is introduced for display.

Check enabled/rate before curve selection, copying or spectral serialization.
After the worker has applied off, spectrum construction/copy/serialization is
zero; small metadata JSON is still allowed. Retained best values must be owned
immutable rows, never an array the next evaluation will overwrite. A batch may
expose its existing response only during the accepted-batch callback; it is not
retained by a provider. Batch current is the last complete accepted input, not
the chunk winner.

The closed outer packet is schema 1, kind coat_sim.film_live_display,
job_id/input_digest/generation/control_sequence/display. The inner display is
schema 1 with context/scope/sequence, selection, current, best and top10.
Total outer UTF-8 JSON is capped at 128 KiB, top10 at ten entries, and each curve
at 1024 points. Values must be finite and match the fixed schema. Bad, stale,
oversize or unknown-context display is dropped, not made a calculation failure.
Two curve slots are not a 128 KiB cap on the whole optimizer process: existing
immutable complete condition rows can remain referenced; no full Jacobian is
retained for display. Small pressure tests do not establish process RSS bounds.

## Scheduler, remote admission and dynamic control

metadata.film_display_config freezes schema/generation/score/enabled/selection
without changing the optical request. Generation is 32 hex characters; an old
job without configuration uses a deterministic job/digest-derived generation.
The existing owned job directory contains film-display-control.json (4 KiB)
and film-display.json (128 KiB), written with the existing scheduler JSON I/O.
These are replaceable observation/control snapshots, not a new result backend.

film_display_control requires exact active job, actual admitted digest,
generation, enabled, selection and expected server sequence. Server CAS assigns
the next sequence; callers cannot choose it. Cancel-requested or terminal jobs
reject controls. A late old on cannot overwrite a newer off. Only a still-latest
GUI intent can explicitly retry a CAS conflict once; the client does not blindly
retry an obsolete request. On registry restart, an existing valid control restores its
sequence and selection; a launched job with missing/corrupt control is explicitly
control-unavailable rather than acknowledging an epoch the worker cannot accept.
Terminal history reads do not allocate display registries. All status/control RPC
work is off the Qt thread.

A bound remote can add device binding/portability data at admission. GUI binding
therefore uses the authenticated admitted status's actual digest after exact
job/configuration/generation checks, not a guess from the pre-admission spec.
Source object identity, design revision/edit epoch, window lifecycle, attempt,
job, context and packet sequence are checked before rendering.

Scheduler status/snapshot/events responses attach only current in-memory display;
SQLite scheduler_events, durable transition history and scalar progress remain
unchanged. A terminal event and last display in the same reply are both handled.
Slow GUI consumers get one queued wake and the latest packet, not an unbounded
frame backlog. Display loss/restart does not change durable results; formal
results still arrive through the existing reliable result path. Closing the
window detaches display observation, not the durable calculation.

## Validation and measured boundary

Core tests compare fixed seed and evaluation budgets with previews on/off, not
wall-clock-stopped search trajectories. The small ordinary baseline used 9
search evaluations + 1 validation and 10 real simulate calls in both modes;
TRF used 4 + 1 evaluations with the same paired-Jacobian calls. Batch DE used
9 + 1 with the same condition forward count and incumbent. Sensitivity paths
likewise retained identical forward/Jacobian counts and incumbent analyses.
No display-only forward or Jacobian call is permitted.

An existing 4097-point response produced 1024 original-grid points per slot and
a largest 48,436-byte packet; off copied zero points. An owned-file small case
produced two metadata packets totaling 9,212 bytes when off, and two packets
totaling 9,694 bytes when on (including the terminal bypass). Its measured
copy/JSON/file-write times are small-example observations, not a speedup or
worst-case latency guarantee. A slow-UI test sends 51 frames with one queued
wake. Pressure tests retain top10 and two condition-row tuples rather than
candidate-count-sized history; temporary allocation high-water marks still grow.
Large-grid sustained RSS, slow-disk worst cases and real remote/IPC behavior
require separate platform validation. A mocked transport is not that proof.

Relevant tests: test_film_candidate_display.py,
test_film_sensitivity_candidate_display.py, test_film_display_transport.py and
test_film_candidate_display_gui.py. Run with the project's standard test runner
and Qt lifecycle/network isolation; production hardware acceleration is unchanged.
