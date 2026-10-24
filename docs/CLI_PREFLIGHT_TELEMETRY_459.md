# CLI preflight and run telemetry

The hierarchical CLI treats checkpoint selection as an admission boundary.
Source compatibility, treatment binding, retained recovery/archive state, the
selected composed checkpoint schema, and the decision client are validated
before it creates a `ResearchLog` or opens dashboard `EventWriter` output.
The selected checkpoint bytes are checked again immediately before backend
attachment. A rejected invocation therefore leaves an existing dashboard run
unchanged and does not create dashboard or research run files.

After admission, the CLI opens the optional research and dashboard writers and
continues through backend and controller initialization. Once initialized, the
normal `run_started` and terminal events are preserved, including an error from
a run that has already crossed that boundary. Async provider cleanup still runs
before either writer is sealed. Setup-timing output is registered only after
admission, so an invalid selected checkpoint cannot leave a partial timing
artifact.

The regression tests use the public `python -m jev_factorio` entry point with
the mock backend. They check rejected and admitted runs, existing and absent
dashboard paths, research logs, and a controlled post-admission failure. These
offline checks do not establish native Factorio acceptance.
