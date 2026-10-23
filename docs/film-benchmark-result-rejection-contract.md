# Film synthesis benchmark result rejection contract

Updated: 2026-10-23.

## Scope and ownership

`tools/run_film_synthesis_benchmarks.py::execute_run` owns the benchmark
terminal summary and success statistics. Production `execute_job` owns execution;
`coat_sim.core.film_synthesis_jobs.is_synthesis_result` remains the formal gate
for accepting its result. The formal physical-design binding and complete
training/audit-grid checks are unchanged and must not be weakened for benchmarks.

## Rejected responses are diagnostic-only

When production execution raises, or its returned value fails the formal gate:

- `result.error` records the exception, and progress records an error followed by
  a terminal event.
- The original returned value remains in `result.response` for diagnosis. This
  does not accept any of its state, target, score, candidate, work, or cleanup
  claims as benchmark evidence.
- The terminal summary uses the existing empty-evidence failure path:
  `state = runner_failed`, `targets_met = false`,
  `strict_raw_targets_met = false`, and
  `first_audited_success_seconds = null`.
- Independent-audit availability, candidate-evidence matching, and verified
  cleanup are false. Both raw-bound records report unavailable evidence. False
  cleanup verification means the rejected payload does not certify cleanup; it
  does not assert that a process is still alive.
- Earlier passing checkpoint observations cannot supply first-success time for
  an invalid terminal response. Checkpoint files/events remain diagnostic records.
- Non-object returned values or result bodies still produce a durable failed
  result and terminal event rather than failing again while deriving a summary.

The project-defined statistics gate is therefore:

`accepted_response = response if error is null, otherwise empty evidence`.

All terminal summaries and success statistics use `accepted_response`; the raw
`response` is retained only in the diagnostic result field. This prevents an
invalid envelope with otherwise passing physical scores from being counted as
success. Suite aggregation retains its existing `partial_or_invalid` outcome,
nonzero exit status, and stop-on-error behavior, with zero raw successes from a
rejected run.

## Unchanged behavior and limits

No physical calculation, optimization, sample grid, bound comparison, execution
budget, cancellation mechanism, accepted-result counters, or successful-result
timing rule changes. This is a tightening of invalid-result statistics, not a
new success criterion. It provides no continuous optical certificate and does
not replace the production job's immutable frozen-request check or scheduler
payload binding.

The request-hash stability test holds the available-memory probe constant:
the frozen AUTO resource policy intentionally records the measurement at freeze,
so changing available memory is a legitimate input change, unlike generated job
identity. Production resource-policy freezing is unchanged.

## Regression evidence

`tests/test_film_synthesis_benchmark_runner.py` uses controlled real production
executions with one worker. It requires early rejection for mismatched candidate
evidence, missing training conditions, missing audit conditions, and an invalid
result kind with otherwise intact optical evidence. It also covers non-object
responses/bodies, persisted terminal records, and suite raw-success aggregation.
The existing successful production execution and checkpoint-design timing tests
remain in the same complete module.

These rules are project-defined failure accounting. No external validation or
new optical-method claim is implied.
