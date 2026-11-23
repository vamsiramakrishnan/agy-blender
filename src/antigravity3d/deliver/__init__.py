"""Delivery: put a finished package into a game project and check it there (``agy3d install``).

``install_package(pkg, project, dest=None, dry_run=False, force=False, check=True) -> InstallResult`` is the
interface; ``InstallResult.to_dict()`` is its machine-readable record. Godot 4 and three.js / React Three Fiber projects
(``web_install``) are installed automatically; Unity,
Unreal and Roblox projects are detected and told what to import (automatic installation is not built for them).
"""

from antigravity3d.deliver.detect import ProjectInfo, detect_project
from antigravity3d.deliver.install import Check, InstallResult, check_install, install_package, uninstall

__all__ = ["install_package", "uninstall", "check_install", "detect_project", "InstallResult", "Check", "ProjectInfo"]
