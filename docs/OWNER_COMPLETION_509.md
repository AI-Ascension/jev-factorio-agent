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
