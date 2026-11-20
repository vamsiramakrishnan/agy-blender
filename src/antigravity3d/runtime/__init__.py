"""Sidecar entrypoints for the antigravity-3d Antigravity plugin.

Entrypoints started by the plugin in ``plugin/antigravity-3d`` (sidecars and the MCP server):

- :mod:`.worker_daemon`  -- managed warm Blender pool behind one Unix socket
- :mod:`.depot_watcher`  -- polls a drop directory and starts batch-sanitize runs
- :mod:`.inspector_server` -- the 3D review inspector as an MCP App, served by :mod:`.mcp_server`
- :mod:`.distill_job`    -- weekly failure-to-skill distillation (scheduled)
- :mod:`.qa_state`       -- the on-disk QA record shared by tools and plugin hooks
"""
