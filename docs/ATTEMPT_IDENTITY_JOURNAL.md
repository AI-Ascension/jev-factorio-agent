# Original-attempt checkpoint ownership

The controller retains an independent per-checkpoint identity journal in
`<checkpoint>.safety/attempt-identity-v1.jsonl`. The checkpoint remains the
campaign data format. Parsing a checkpoint for inspection does not authorize
execution. Executable controller restore, CLI resume preflight and supervisor
launch/repair preflight validate the selected checkpoint against its journal.

The journal binds the checkpoint path, campaign session and target, attempt ID
and origin, immutable action/plan/step tuple, start provenance, destination unit
and receipt when present. Existing native actor, source, paid ownership and
receipt verification remain separately required. The journal does not replace
those checks or establish native acceptance.

One checkpoint owner holds the local thread and OS lock through validation,
observation, dispatch and checkpoint settlement. Nested background observations
retain the outer owner and loaded journal. Exceptions remove reentrant authority
and release the local registry even if OS cleanup fails. An original journal or
checkpoint failure remains the primary exception, with cleanup failures chained.
On POSIX fork, the child drops copied thread ownership and closes its inherited
lock descriptors without unlocking the parent's shared file descriptions. It
must acquire a new OS lock and reload the journal before operating. Unwinding
a copied parent context cannot release the parent's lock. A fork does not
transfer controller or native campaign authority to the child.

Before saving, the owner appends a durable checkpoint intent containing the
expected checkpoint digest and complete active attempt tuples. After the atomic
checkpoint write is verified, it appends the bound checkpoint and prepared
attempt records. Before backend entry it journals `dispatch_started`; backend
return is journaled separately. Ambiguous entry remains an unresolved obligation.
The existing exact retained-transfer proof is the only narrowly qualified retry
path. A preparation record alone does not authorize replay.

Restore checks the complete composed checkpoint and exact bytes under the owner
lock. An interrupted checkpoint intent can be reconciled only against its exact
old or new digest. If a verified bound checkpoint survived but its prepared
records did not, restore validates its full committed active tuple set and the
complete resulting ownership before appending the missing metadata. It retains
the original ID and leaves checkpoint bytes intact. It does not fabricate a
dispatch witness. Changed checkpoint bytes or foreign session/target journal
records fail closed. Repeated restore does not duplicate preparation records.

A terminal result is journaled before the checkpoint releases an attempt.
Settlement retains the first result and original identity. Background craft
transfers the same ID into its background owner and finishes it once. Missing,
ambiguous or incompatible evidence retains the pending obligation. Capacity
limits fail closed; unresolved rows are never evicted to make room. Journal
compaction needs a separately reviewed protocol.

Existing pending checkpoints without a journal require an explicit one-use
owner-authorized legacy migration. The supervisor's
`inspect-legacy-attempt-migration` command reports the exact captured checkpoint
digest and tuple-set digest. `authorize-legacy-attempt-migration` requires both
pins, the exact session and target, and the exclusive supervisor/checkpoint
ownership boundaries. It records the captured original IDs rather than replacing
them. Wrong, stale or reused authority is rejected. Neither command supplies
native deployment, source migration, spending or timing authority. Do not run
them against another owner's checkpoint or infer permission from this document.

The hash chain detects malformed or inconsistent records. It cannot defeat a
principal who can rewrite both the checkpoint and journal. It is an operational
identity boundary, not an independently controlled cryptographic trust anchor.

The focused tests use real local locks and explicitly synthetic backends.
Prepared-binding crash controls and public step/resume controls are offline
evidence. Retained legacy shared-connector composition has explicit one-use
approval and selected CLI preflight controls. Real POSIX fork and fresh-process
controls cover exclusion, reacquisition/reload and copied-context unwinding.
Complete top-level CLI, legacy, fsync/crash, Windows and async lineage matrices
still require qualification against the final candidate. Native campaign acceptance requires the existing owner,
reviewed deployed source, retained save/receipts and actual timing authority.
