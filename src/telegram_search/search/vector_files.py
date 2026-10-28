"""Native compaction with conservative handling of protected historical versions."""

import re
from datetime import timedelta

TAGGED_CLEANUP = re.compile(
    r"^(?:lance error: )?Cleanup error: [1-9][0-9]* tagged version\(s\) "
    r"have been marked for cleanup\."
)


def optimize_files(table, *, cleanup_older_than=timedelta(days=7)):
    before = table.version
    try:
        table.optimize(cleanup_older_than=cleanup_older_than, delete_unverified=False)
    except RuntimeError as error:
        # Lance 0.25/0.39 compacts first, then rejects GC when an age-eligible
        # version is tagged. That pin must stay: it protects live readers.
        # Accept only this specific native GC refusal AND a committed new
        # version, never an arbitrary IO/compaction failure or no-op.
        message = str(error)
        if not TAGGED_CLEANUP.match(message):
            raise
        table.checkout_latest()
        if table.version <= before:
            raise
        return {"optimized": True, "cleanup_deferred": True}
    return {"optimized": True, "cleanup_deferred": False}
