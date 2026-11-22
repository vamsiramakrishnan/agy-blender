"""The installer's name for :mod:`antigravity3d.safe_paths` (the rules moved there so the edit command and the
comparison harness use the same ones)."""

from antigravity3d.safe_paths import UnsafePath, asset_name, inside, no_links, safe_name  # noqa: F401

__all__ = ["UnsafePath", "asset_name", "inside", "no_links", "safe_name"]
