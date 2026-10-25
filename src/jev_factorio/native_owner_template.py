"""Prepare captured native-owner templates offline; never install or admit one.

A source digest identifies a captured template, not runtime authority. This
module emits Python source only. It never opens RCON, starts a controller,
rewrites a checkpoint, consumes a recovery grant, or updates source/owner pins.
"""
from __future__ import annotations

import hashlib

# Preserve historical templates; unknown input requires fresh source review.
_OWNER_SOURCES = {
    "3e43cd362a6158134961ddae1900b20952e0469489695b402f0666a183f935a4": "legacy",
    "19024f97c757818cce3940a19442c5ea997d628b8870db69451e087d1b114b9e": "legacy",
    "fe2e838ec942ac1c01d0ff5d85a51bf870c88a08008f7771f18bf893701e67c5": "guarded",
    "2c57a414c826287e3579fdd2d5020e4ba64d67788ff52c956f90c75339e22b51": "guarded",
}
_GENERATOR_SOURCE = "520c70edcbd5bcfc7b55c1aa54efd9e93065e80babfe0baff17d3f45b6ef27ff"

_OLD_BRANCH = """            if memory['status'] == 'completed':
                status('completed', session_id=memory['session_id']); return
"""

_OLD_RECOVERY = """        if recovering and json.loads(CHECKPOINT.read_text())['blocked_recovery']['source_revision'] != authority['blocked_source_revision']:
            hold('Blocked decision source differs from recovery authority'); return
"""

_NEW_BRANCH = """            if memory['status'] == 'completed':
                # The status label alone is not completion evidence. Reuse the
                # existing owner and current source-bound RCON attachment to
                # validate the exact checkpoint and all durable obligations.
                if (not native['runtime'] or native['session_id'] != memory['session_id']
                        or native['tick'] < memory.get('last_tick', 0)):
                    hold('Completed checkpoint runtime identity requires reconciliation'); return
                from factorio_rcon import RCONClient
                from jev_factorio.owner_completion import preflight_completed_owner
                completed_client = None
                completion_error = None
                witness = None
                try:
                    completed_client = RCONClient(
                        '127.0.0.1', 27015, os.environ['FACTORIO_RCON_PASSWORD'], timeout=10)
                    witness = preflight_completed_owner(
                        CHECKPOINT, target='rocket_launch', source_root=SOURCE,
                        expected_source_revision=config['source_revision'],
                        startup_source_revision=revision, owner_path=Path(__file__),
                        expected_owner_sha256=config['owner_sha256'],
                        startup_owner_sha256=SCRIPT_SHA, native_identity=native,
                        client=completed_client)
                except Exception as error:
                    completion_error = error
                finally:
                    if completed_client is not None:
                        try:
                            completed_client.close()
                        except Exception as error:
                            if completion_error is None:
                                completion_error = error
                if completion_error is not None:
                    hold('Completed checkpoint typed reconciliation failed ('
                         + type(completion_error).__name__ + ')'); return
                status('completed', session_id=memory['session_id'],
                       completion_checkpoint_sha256=witness.checkpoint_sha256,
                       completion_observation_sha256=witness.observation_sha256,
                       completion_observed_tick=witness.observed_tick)
                return
"""

_NEW_RECOVERY = """        if recovering:
            recovery_memory = json.loads(CHECKPOINT.read_text())
            if recovery_memory.get('status') == 'completed':
                hold('Unconsumed recovery authority cannot authorize a completed return'); return
            blocked = recovery_memory.get('blocked_recovery')
            if (not isinstance(blocked, dict)
                    or blocked.get('source_revision') != authority['blocked_source_revision']):
                hold('Blocked decision source differs from recovery authority'); return
"""

def _captured_text(source: bytes, expected_sha256: str) -> str:
    if type(source) is not bytes or type(expected_sha256) is not str:
        raise ValueError("Native owner source requires exact bytes and a digest")
    if hashlib.sha256(source).hexdigest() != expected_sha256:
        raise ValueError("Native owner source differs from its expected digest")
    return source.decode("utf-8")


def prepare_native_owner_completion_source(
        source: bytes, *, expected_owner_sha256: str) -> bytes:
    """Return the exact captured owner's completion correction, without applying it.

    Already prepared supported templates remain byte-identical. Unknown or
    modified templates fail closed; a digest supplied by a caller cannot add a
    new profile. Returned bytes require their own independent review, signature,
    admitted owner/source pins and deployment authority.
    """
    text = _captured_text(source, expected_owner_sha256)
    profile = _OWNER_SOURCES.get(expected_owner_sha256)
    if profile is None:
        raise ValueError("Native owner template is not a supported captured profile")
    if profile == "guarded":
        if (text.count(_NEW_BRANCH) != 1 or text.count(_NEW_RECOVERY) != 1
                or _OLD_BRANCH in text or _OLD_RECOVERY in text):
            raise ValueError("Prepared native owner gate differs from its profile")
        return source
    if text.count(_OLD_BRANCH) != 1 or text.count(_OLD_RECOVERY) != 1:
        raise ValueError("Native owner template completion shape changed")
    text = text.replace(_OLD_BRANCH, _NEW_BRANCH, 1).replace(
        _OLD_RECOVERY, _NEW_RECOVERY, 1)
    compile(text, "<prepared-native-owner>", "exec")
    prepared = text.encode("utf-8")
    if _OWNER_SOURCES.get(hashlib.sha256(prepared).hexdigest()) != "guarded":
        raise ValueError("Prepared native owner does not match its reviewed profile")
    return prepared


def prepare_native_owner_generator_source(
        source: bytes, *, expected_generator_sha256: str) -> bytes:
    """Correct the captured V47 generator's source expression without executing it.

    Its version naming, attempt limits, source-pin validation and exclusive
    artifact writes remain unchanged. The emitted generator loads a separately
    delivered standalone copy of this helper beside its script, without adding
    this module to its frozen runtime repository or shadowing that package.
    This preparation does not authorize any version or attempt-budget change.
    """
    text = _captured_text(source, expected_generator_sha256)
    if expected_generator_sha256 != _GENERATOR_SOURCE:
        raise ValueError("Native owner generator is not a supported captured profile")
    import_line = "from jev_factorio.provenance import source_revision\n"
    expression = "owner=old.decode().replace('v46','v47').replace('attempts >= 46','attempts >= 47')"
    if text.count(import_line) != 1 or text.count(expression) != 1:
        raise ValueError("Native owner generator completion shape changed")
    replacement = (
        "owner=prepare_native_owner_completion_source(old, "
        "expected_owner_sha256='3e43cd362a6158134961ddae1900b20952e0469489695b402f0666a183f935a4')"
        ".decode().replace('v46','v47').replace('attempts >= 46','attempts >= 47')")
    text = text.replace(import_line, import_line +
                        "from native_owner_template509 import prepare_native_owner_completion_source\n", 1)
    text = text.replace(expression, replacement, 1)
    compile(text, "<prepared-native-owner-generator>", "exec")
    return text.encode("utf-8")
