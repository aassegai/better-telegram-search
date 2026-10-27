"""Account for optional runtimes living in owned child processes."""

import psutil


def process_tree_rss():
    parent = psutil.Process()
    processes = [parent, *parent.children(recursive=True)]
    total = 0
    for process in processes:
        try:
            total += process.memory_info().rss
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            # A concurrently unloaded worker no longer consumes our budget.
            continue
    # AccessDenied deliberately propagates: unknown memory must not be treated
    # as zero when admitting another model session.
    return total
