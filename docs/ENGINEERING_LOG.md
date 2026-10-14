# Engineering log

Feature notes moved out of the README. Each section links to the detailed document for that change. These notes describe source integration and offline evidence; unless a section says otherwise, none of them is a production treatment or a native gameplay acceptance.

## Mining automation for existing manual cells

The opt-in `--mining-outposts` extension adds paid iron/copper drill-to-chest
outposts when an established furnace lacks a supported direct input route.
Existing furnaces and receipts remain unchanged; ore is collected only after
native flow evidence and then hauled in batches. See
[Mining outposts](MINING_OUTPOSTS.md) for required controller flags,
idle-boundary checkpoint enablement, supervisor handoff limits, and native
benchmark requirements. This is not full belt transport or a live deployment.

## Planning efficiency and staged autonomy

The [bounded planning and autonomy plan](PLANNING_AUTONOMY.md) documents
snapshot-local planning improvements, safe additive expansion requirements, and
development VM acceptance gates. The [offline evidence audit](EVIDENCE_AUDIT.md)
verifies capture hashes and reports measurement coverage without contacting a
backend or treating missing gameplay metrics as zero. Source integration is not
production cutover or proof of native throughput improvement.

## Ore-side successors

The opt-in [ore-side successor lifecycle](ORE_SIDE_SUCCESSORS.md) adds separately owned
production, paid construction, native flow checks and bounded downstream-use
qualification without replacing the established factory. Development VM
validation and production cutover remain separate gates.

## Native acceptance preparation

[Native acceptance tooling](NATIVE_ACCEPTANCE.md) provides a fixed read-only development
preflight, private checksummed operational captures and paired measurement
checks. It never starts gameplay or authorizes production cutover.

## First-rocket launch prerequisites

[Launch readiness](LAUNCH_READINESS.md) adds paid landing-pad construction or
reuse, bounded timed fish acquisition/satellite fallback, reserved cargo loading,
and native guarded launch for unmodified base 2.0.77. Existing production and
pending-action history are retained. Synthetic tests do not replace the native
development-VM and production cutover gates.

## Campaign throughput diagnostics and dev pilots

[Campaign throughput](CAMPAIGN_THROUGHPUT.md) implements the September 25
progress analysis with opt-in observation profiling/consolidation, bounded
lead-time science supply, progress alerts and conservative research lookahead.
Controller and supervisor treatment flags remain explicit and default off.
Offline reports separate useful science consumption from action counts; source
integration and synthetic tests do not prove native improvement or authorize
production cutover.

## Gameplay/latency tracker #103 (offline work, acceptance pending)

The [implementation/evidence boundaries](ISSUE_103_OFFLINE_IMPLEMENTATION.md)
describe the maintenance, grouped-fuel, planner-reuse, checkpoint and attribution
changes. The [read-only capacity audit](CAPACITY_AUDIT.md) and
[native acceptance runbook](NATIVE_ACCEPTANCE_103.md) do not authorize a live
change or prove native progress. General solid transport and its coal/downstream
consumers remain separate implementation work; keep #92/#103 open until their
actual native requirements are met.

The [grouped manual fuel policy](GROUPED_FUEL_POLICY.md) documents bounded
service/reserves, observation-only depletion estimates and unchanged failure/receipt
authority. Its fixture results do not establish native throughput or coal automation.

## Experimental solid-route foundation

The [solid-corridor contract and qualification boundary](SOLID_ROUTES_100.md)
implements an opt-in Python composition API for paid straight corridors. It is
not exposed as a production CLI/supervisor treatment. Python/Lua fixtures do not
establish native transport, coal-network capability or campaign acceptance; see
#100 and #92 for the remaining gates.

See [negotiated atomic observation v2](ATOMIC_OBSERVATION.md) for the optional single-command read contract, query bounds and native qualification boundary.

### Bounded downstream solid investment (source-only follow-up)

The experimental solid-route composition can opt into `solid_science_policy=True`
with its explicit intent allowlist. It preserves ready science, requires the paid
kit already carried, and labels estimated payback separately from verified
service-history evidence. See [policy boundaries](SOLID_INVESTMENT_102.md)
and [resume review corrections](SOLID_RESUME_REVIEW.md). This does not enable
a production CLI treatment or qualify a native campaign.

### Experimental coal-source bundles

The explicit source-only [coal supply experiment](COAL_SUPPLY_EXPERIMENT.md)
adds paid independent electric coal branches, bounded explicit mixed downstream
routes, shared remaining-kit locks and checkpoint recovery tests. The complete
intent list stays immutable and uses at most four disjoint routes. It is not
enabled by the production CLI or supervisor. Automatic coal kit acquisition and the
coal demand/payback policy remain incomplete; modeled builds do not establish
native flow, a complete production treatment, or acceptance in #101/#92/#103.

## Signed compatible recovery across earlier changed-contract epochs

An equal-contract recovery can follow an earlier, consumed changed-contract
handoff without rewriting the old compatible-source records. A discontinuity
requires a separate signed epoch-boundary witness. The supervisor authenticates
the complete terminal checkpoint and retained source receipts before signing
the witness; its complete body binds the new authorization digest, prior epoch,
ordered consumed handoffs, event preimages, and current epoch endpoint.

Reload verifies the signature against the independently enrolled public signer
bytes and checks the unchanged consumed ledger. The signed event preimages remain
available after ordinary history rolls out. Only sources in the newest compatible
epoch share selection budgets. This does not authorize another once-only decision,
reset a budget, or make unsigned checkpoint history an authority.
