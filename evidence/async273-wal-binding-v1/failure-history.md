# WAL binding test history

The first regression was run against the assigned base before implementation:
`tests/test_async_provider_wal.py::test_public_mock_decision_lease_saves_before_health_clear`
failed because `ProviderCircuit.prepare_decision_lease` did not exist. The full
captured failure is in `baseline-feature-absence.log`.

During development, the captured `assigned-focused-tests.log` run had 129
passing tests and one failure. An authorization-restart assertion had been
placed in the incident/budget-rewrite test; the production code correctly
rejected the altered pre-attempt health digest before it reached the expected
authorization check. The assertion was moved to the authorization-change test.

Other intermediate failures were corrected before the final run: the new
subprocess test initially called `AsyncJevClient.close()` instead of
`aclose()`; a stale-lease assertion initially expected the less-specific
baseline-change message although the stronger one-WAL-per-sidecar guard fired;
and that assertion edit briefly introduced an indentation error. These were
test-harness issues, not production failures. Their console output is retained
in the session transcript; the captured 129-pass/1-failure run and final
success log are preserved here.

The ownership-lock additions also had intermediate test-harness failures: a
child-process test used `AsyncJevClient.close()` instead of `aclose()`, a stale
lease initially expected the generic stale-baseline message instead of the
more specific one-WAL-per-sidecar rejection, and an assertion edit briefly
had bad indentation. A later aggregate run correctly rejected the tampered
incident baseline but an authorization assertion had been placed in that test;
the check now resides in the authorization recovery test. All were corrected.

Final assigned focused command result is recorded in
`assigned-focused-tests-final.log` (133 passed). No failure was suppressed or
used to weaken production checks.
