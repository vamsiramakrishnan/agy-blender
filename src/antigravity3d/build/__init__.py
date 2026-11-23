"""The script API an asset is written with: ``from agy3d import Asset`` (CONTRACT.md section 1; the reference page is
``plugin_script_mode/SKILL.md``). ``handoff`` reads what ``Asset.save()`` writes."""

from antigravity3d.build.asset import Asset, Part
from antigravity3d.build.shapes import BuildError

__all__ = ["Asset", "BuildError", "Part"]
