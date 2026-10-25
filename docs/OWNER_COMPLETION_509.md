# Existing-owner completion preflight

`jev_factorio.owner_completion.preflight_completed_owner` checks whether an
existing campaign owner's completed checkpoint is consistent with the current
source, retained native installation, and observed paid feature state. It
returns an `OwnerCompletionWitness` only after those checks agree. The witness
is evidence for a caller's separately governed decision; it does not authorize
an action, restart an owner, adopt a checkpoint, or establish that a native
campaign was completed successfully.

The caller must supply the checkpoint and target, the expected and startup
source pins, the owner script and its startup digest, a current runtime identity
sample, and the existing owner's RCON client. The preflight does not create or
close a client, start a backend, install or replace Lua assets, or change the
checkpoint. Local checkpoint and rollback/archive checks happen before the
native query. The function then verifies the completed status and exactly one
matching controller-authored goal event, rejects unresolved outer or
feature-specific owners, and checks the session, source, owner digest, target,
and checkpoint bytes again around the query.

The query also rejects a nonempty native handcraft queue or active submission.
Any retained native craft receipt must be completed and pass the existing
typed receipt checks, including its paid quantities, actor identity, queue
validity and completion tick. It does not run the craft observer or require
previously verified output to remain unconsumed in inventory.

The original actor must have explicit idle walking/mining controls and no
quarantine or retained mining target. A retained fair-action job must be a
known terminal walk/mine receipt for that actor with successful control
cleanup. Pending paths and mining work remain reconciliation obligations;
the completion query does not stop jobs, renew leases or change receipts.

The native query is limited to an exact source-reviewed direct installation
profile whose installed asset hashes match the recorded manifest. It captures
current producer records without calling the mutating `campaign.observe()` or
the optional feature observers. Where a read-only path inspector exists, the
query may use it for current topology. For retained solid and coal owners, the
query binds every counter sample to its native sample header and the
corresponding typed owner projection. When a paid route is present it gathers
five bounded samples across at least 120 and at most 600 game ticks (and never
more than seven). It builds completion flow from family-specific positive
counter deltas within that interval; a prior flow total or diagnostic
timestamp cannot refresh an expired measurement. Solid diagnostics are
reconstructed from current committed cells and offers. Only an otherwise
unresolved intent may use the identity-bound cached diagnostic, and only when
its producer, session, actor, source binding, endpoint units, and timestamp
are valid and no more than 600 ticks old. The query never surveys to fill
missing evidence. Missing, stale, malformed, unbound, or unsupported state
remains unknown and fails closed.

The frozen e759 installation profile retains its original asset hashes.
Completion support must preserve those historical bytes; current direct
installations derive their hashes from the reviewed Lua source separately.
The compatibility controls use an unchanged complete legacy manifest and
reject both corrupted hashes and a replacement hash under the frozen profile.

Retained paid commitments are matched to current records by their source,
layout, part roles and units, receipts, and complete-flow evidence. Successor
projects additionally require the current qualified project, predecessor,
input/output commitments, use receipt, and qualification record. Production
sites without a matching retained paid family or successor binding do not
become owned merely because they appear in the current observation. Completed
connector routes remain valid evidence only when their saved binding and
current native summary agree. Presence of a marker alone does not prove
completion.

The focused tests model source-generated Factorio Lua APIs and counters. For
example, the owner-wait control advances the offline engine tick by 1,800 and
then models mined-resource depletion plus delivery during a 132-tick current
sampling interval. That is not a measurement from a running Factorio world or
an actual 30-second owner execution. These checks are not a Factorio
save/reload test or proof of deployment. The separately governed owner must
adopt the reviewed helper and its compatible source pin before the runtime
behavior changes.

## Captured owner and generator source preparation

`jev_factorio.native_owner_template.prepare_native_owner_completion_source`
prepares the captured V46 or V47 owner source offline. It requires the exact
captured digest, inserts the typed completion preflight and rejects a completed
return while maintenance recovery authority remains unconsumed. Unknown or
modified inputs require a new source review. Preparing an already supported
guarded source returns identical bytes. The function emits source only; it
does not overwrite an owner, update admission pins, consume authority, create
a controller or connect to the game.

`prepare_native_owner_generator_source` corrects the captured
`prepare_v47_artifacts.py` recipe before its owner-source expression renames
V46 to V47. The generated completion branch therefore retains the same typed
gate. Its existing source-pin assertion, version names, attempt limits and
exclusive writes remain unchanged. Supply a separately reviewed standalone
copy of `native_owner_template.py` named `native_owner_template509.py` beside
the prepared generator. This avoids adding a new module to its frozen runtime
repository or shadowing the frozen `jev_factorio` package. The complete helper,
generator and generated-owner bundle requires exact digest/signature review.
Neither preparation nor an existing version-number substitution grants more
attempts, spending or time.

The captured deployed V46 source has SHA256 `3e43cd362a6158134961ddae1900b20952e0469489695b402f0666a183f935a4`;
the staged V47 source has SHA256 `19024f97c757818cce3940a19442c5ea997d628b8870db69451e087d1b114b9e`.
The captured generator has SHA256 `520c70edcbd5bcfc7b55c1aa54efd9e93065e80babfe0baff17d3f45b6ef27ff`.
Historical files and their admission pins stay intact. Prepared sources use
their own byte digests; the earlier offline CRLF patch and a new LF source are
different artifacts even when their parsed Python behavior agrees.

The generator controls isolate the actual captured source expression, then
run the actual generated owner main with private temporary files and forbidden
child/retry interfaces. A full producer checkpoint for another target is
incorrectly reported completed by the original generated owner and rejected
by the corrected owner through the real typed preflight. Caller-wiring positive
and close-failure controls use an explicit witness spy; the paired completion
adapter tests exercise real current source-produced native rows. These controls
are offline and do not establish deployment or native acceptance.

Actual adoption remains a separate governed operation. The runtime source
must contain the reviewed completion helper and satisfy the installed native
asset profile; an older frozen source without that helper cannot qualify.
The original owner lock, compatible source and owner pins, receipts, budgets,
pending state and an unexpired authorized window are required. An expired
campaign cannot be restarted or extended by generating a corrected script.
