"""Sidecar entrypoints for the antigravity-3d Antigravity plugin.

Each module here is started by a ``sidecar.json`` in ``plugin/antigravity-3d``:

- :mod:`.worker_daemon`  -- managed warm Blender pool behind one Unix socket
- :mod:`.depot_watcher`  -- polls a drop directory and starts batch-sanitize runs
- :mod:`.inspector_server` -- localhost HTTP server behind the inspector side pane
- :mod:`.distill_job`    -- weekly failure-to-skill distillation (scheduled)
- :mod:`.qa_state`       -- the on-disk QA record shared by tools and plugin hooks
"""
