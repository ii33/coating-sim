# Assistant settings probe diagnosis

Date: 2026-10-12

The two old static assertions in `tests/test_settings_model_features.py:388` and
`:404` require `_auto_discover` itself to contain
`_begin_models_probe(automatic=True)`. That no longer matches the runtime flow:
`_auto_discover` requests a forced endpoint-bound key refresh when the bound
model cache is not fresh, and
`_on_bound_key_lookup_ready` starts the automatic probe only after checking the
current endpoint, credential generation, saved-key state and fingerprint,
assistant enabled/closed/pause/busy gates, and cache freshness. The earlier
legacy suite actually reported 63 passed and 2 failed; that historical result
is retained in the adaptation evidence. It was a stale source-text assertion,
not evidence that the callback had sent an unauthorized request.

On the parent thread's explicit instruction, the two tests were replaced with
behavior checks using the same fake bridge fixtures and real unbound methods.
They verify disabled/missing-key/fingerprint/fresh-cache gates at the initial
boundary, stale endpoint/generation rejection at the callback, the valid forced
refresh-to-GET path, no automatic candidate POST, and an explicit manual
candidate POST captured by the fake network. Other assertions about manual
refresh entrypoints remain. The targeted replacement run passed 2 tests;
`coat_sim/ui/settings.py` was not changed. Final combined results are recorded
in the adaptation evidence.

`tests/test_ai_settings_probe_contract.py` invokes those real unbound bridge
methods on `SimpleNamespace` fixtures. Its fake future, discovery bus, timers,
signals, and network capture verify the stale-cache refresh-to-callback path,
rejection of stale generation/endpoint and other probe gates, one `GET /models`
dispatch after a valid callback, and no candidate `POST` during automatic
fallback. It does not construct `SettingsBridge`, read system credentials, or
send provider/chat requests. All credential strings and replies are fixtures.
