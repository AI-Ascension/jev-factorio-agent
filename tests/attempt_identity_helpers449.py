"""Model a repair child's ordinary checkpoint transaction without new authority."""
from jev_factorio.attempt_identity_archive import AttemptIdentityArchive
from jev_factorio.memory import checkpoint_memory_type, load_checkpoint_data
from jev_factorio.supervisor import atomic_json


def repair_candidate_write(supervisor, previous, current):
    archive = AttemptIdentityArchive(supervisor.config.checkpoint)

    def decode(data):
        return load_checkpoint_data(
            data, data["session_id"], data["target"],
            memory_type=checkpoint_memory_type(data))

    def write(memory):
        token = archive.before_checkpoint(memory)
        memory.save(archive.checkpoint)
        archive.after_checkpoint(token, memory)

    with archive.owner():
        archive.validate_current(None, previous["target"])
        write(decode(previous))
        archive.validate_current(None, previous["target"])
        try:
            candidate = decode(current)
        except (ValueError, TypeError, KeyError):
            # Deliberately malformed child output stays raw and unbound. The
            # public repair gate must reject it; do not reset or bless a journal.
            atomic_json(archive.checkpoint, current)
        else:
            write(candidate)
