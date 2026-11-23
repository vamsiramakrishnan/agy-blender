"""The guard and the runner of an asset script (S/r6/CONTRACT.md section 3): what the guard refuses and what it does not
pretend to stop, the bare environment a script runs in, its time limit, and errors as the script's author reads them."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from antigravity3d.build import guard, runner


@pytest.mark.parametrize("module", sorted(guard.ALLOWED_IMPORTS))
def test_each_allowed_import_passes(module):
    assert guard.check(f"import {module}\n") is None
    assert guard.check(f"from {module} import x\n") is None


# Ordinary code a model writes in a build script (the reviewer's 60 plausible snippets, S/r6/scratch_review_door/legit.py,
# of which the first guard refused 22), all of which must pass. Not here: `import sys` and `from pathlib import Path`,
# which the guard still refuses on purpose (sys.modules hands out every loaded module; a Path reads and writes files).
LEGIT = {
    "super_init": "class A:\n    def __init__(self):\n        super().__init__()\n",
    "private_attr": "class A:\n    def __init__(self):\n        self.__x = 1\n",
    "dunder_method": "import numpy as np\nx = np.zeros(3)\nprint(x.__len__(), x.__array_interface__ is not None)\n",
    "dict_getitem": "class B(dict):\n    def __missing__(self, k):\n        return 0\n    def __getitem__(self, k):\n        return dict.__getitem__(self, k)\n",
    "future": "from __future__ import annotations\n",
    "stdlib": "import copy, time, operator, statistics, decimal, string, abc, bisect, heapq, warnings, json, re, numbers, fractions\n",
    "typing_extensions": "from typing_extensions import Self\nimport numpy.typing as npt\n",
    "var_open": "open = 3\nvars = [1, 2]\n",
    "kw_open": "def f(open=False, compile=True):\n    return open\nf(open=True)\n",
    "attr_open": "import trimesh\nm = trimesh.creation.box()\nm.open\n",
    "name_dunder": "def f(): pass\nprint(f.__name__, type(1).__name__, __name__)\nif __name__ == '__main__':\n    pass\n",
    "slots_all_doc": "__all__ = ['x']\nclass A:\n    __slots__ = ('x',)\nprint(__doc__)\n",
    "trimesh_creation": "import trimesh\nm = trimesh.creation.cylinder(radius=1, height=2)\nm.apply_transform(trimesh.transformations.rotation_matrix(0.5, [0, 0, 1]))\nu = trimesh.util.concatenate([m, m])\n",
    "numpy_math": "import numpy as np\nx = np.linalg.norm([1, 2]) + np.random.default_rng(1).random()\n",
    "scipy": "from scipy.spatial import ConvexHull\nfrom scipy.interpolate import CubicSpline\nimport scipy.optimize\n",
    "agy3d": "from agy3d import Asset\na = Asset('x', engine='godot_4')\na.load('chair', 'chair.glb')\na.save()\n",
}


@pytest.mark.parametrize("name", sorted(LEGIT))
def test_ordinary_code_passes(name):
    assert guard.check(LEGIT[name], f"{name}.py") is None


def test_every_snippet_of_the_reviewers_list_but_sys_and_pathlib_passes():
    path = Path("/tmp/claude-0/-home-user-agy-blender/4cf334f8-2cad-57dc-8ea7-c084536ceb98/scratchpad/r6/scratch_review_door/legit.py")
    if not path.is_file():
        pytest.skip("the reviewer's list is not on this machine (LEGIT above holds its cases)")
    import ast

    cases = ast.literal_eval(path.read_text().split("cases = ", 1)[1].split("\nbad = 0", 1)[0])
    refused = {k for k, src in cases.items() if guard.check(src, k + ".py")}
    assert refused == {"sys_argv", "file_dir"}


# The accidents: how a model writing a build script reads or writes a file, runs a process or reaches the system.
ACCIDENTS = [
    ("import os\n", "import of os is not allowed"),
    ("import subprocess as sp\n", "import of subprocess is not allowed"),
    ("from pathlib import Path\n", "import from pathlib is not allowed"),
    ("from . import helper\n", "import from . is not allowed"),
    ("import scipy.io\n", "import of scipy.io is not allowed"),
    ("from scipy import io\n", "scipy.io reads or writes a file"),
    ("f = open('notes.txt', 'w')\n", "open() is not allowed in a build script: a build script reads no file"),
    *((f"x = {name}('1')\n", f"{name}() is not allowed in a build script") for name in sorted(guard.FORBIDDEN_CALLS)),
    *((f"import numpy as np\nnp.{fn}('x.npy')\n", f"numpy.{fn} reads or writes a file")
      for fn in ("save", "savez", "savetxt", "load", "loadtxt", "genfromtxt", "fromfile", "memmap", "DataSource")),
    ("import numpy as np\nnp.lib.npyio.DataSource(None)\n", "numpy.lib.npyio.DataSource reads or writes a file"),
    ("from numpy import loadtxt\n", "numpy.loadtxt reads or writes a file"),
    ("import numpy as np\nnp.zeros(3).tofile('x.bin')\n", ".tofile() writes a file"),
    ("import numpy as np\nnp.zeros(3).dump('x.pkl')\n", ".dump() writes a file"),
    ("import trimesh\nm = trimesh.load('chair.glb')\n", "trimesh.load reads or writes a file"),
    ("import trimesh\nm = trimesh.load_mesh('chair.obj')\n", "trimesh.load_mesh reads or writes a file"),
    ("import trimesh\nm = trimesh.creation.box()\nm.export('box.stl')\n", ".export() writes a file"),
    ("import trimesh\nx = trimesh.exchange.export.export_mesh\n", "trimesh.exchange.export.export_mesh reads or writes a file"),
    ("import typing\nenv = typing.sys.modules['os']\n", "typing.sys.modules reaches the system through a library"),
    ("import random\nrandom._os.system('ls')\n", "random._os.system reaches the system through a library"),
    ("import numpy as np\nkey = np.os.environ\n", "np.os.environ reaches the system through a library"),
    ("x = (1).__class__\n", "__class__ reaches Python's internals"),
    ("x = [].__class__.__mro__\n", "reaches Python's internals"),
    ("import math\nprint(math.__spec__)\n", "__spec__ reaches Python's internals"),
    ("def f(): pass\nf.__globals__\n", "__globals__ reaches Python's internals"),
]


@pytest.mark.parametrize("source,rule", ACCIDENTS)
def test_each_accident_is_refused_in_one_line_with_the_line_the_rule_and_what_instead(source, rule):
    why = guard.check("y = 1\n" + source, "chair.py")
    last = source.strip().split("\n")[-1]
    assert why is not None and "\n" not in why, why
    assert rule in why, why
    assert why.startswith("chair.py line ") and (last in why or source.strip().split("\n")[0] in why), why


def test_a_file_hint_names_the_script_api():
    assert "a mesh file comes in with a.load(path)" in guard.check("import numpy as np\nnp.save('a', 1)\n")
    assert guard.check("def f(:\n") is None  # a syntax error is the runner's to report, as one


def test_the_guard_is_not_a_sandbox_what_it_still_lets_through():
    """Honest: the guard reads names, not intent. A function stored under another name before it is called, or reached
    by string formatting, passes; so does a library function the lists do not name. What limits such a script is the
    runner (bare environment, no key, a time limit), not the guard."""
    assert guard.check("import numpy as np\nlib = np.lib\nlib.npyio.loadtxt('/etc/hostname')\n") is None  # renamed first
    assert guard.check("from agy3d import Asset\na = Asset('x', engine='godot_4')\nx = '{0.__class__}'.format(a)\n") is None
    assert guard.check("import dataclasses\nprint(hasattr(dataclasses, 'sys'))\n") is None
    assert "not a sandbox" in guard.__doc__ and "does not stop" in guard.__doc__


@pytest.fixture
def unguarded(monkeypatch):
    """The runner's own behaviour, measured with scripts the guard would refuse (they reach the system on purpose)."""
    monkeypatch.setattr(runner.guard, "check", lambda source, filename="": None)


def run(tmp_path: Path, text: str, **kw) -> runner.RunResult:
    folder = tmp_path / "src"
    folder.mkdir(exist_ok=True)
    (folder / "s.py").write_text(text)
    return runner.run_script(folder / "s.py", tmp_path / "out", **kw)


def test_the_script_sees_a_bare_environment_without_the_servers_key(unguarded, tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "sk-the-servers-key")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("BLENDER_BIN", "/opt/blender/blender")
    # the documented bypass (typing.sys) is the way to look: what it sees is what any script could see
    r = run(tmp_path, "import typing\nos = typing.sys.modules['os']\n"
                      "print('ENV', sorted(os.environ))\nprint('KEY', os.environ.get('GEMINI_API_KEY'))\n"
                      "print('CWD', os.getcwd())\nprint('OUT', os.environ['AGY3D_BUILD_OUT'])\n"
                      "print('BLENDER', os.environ.get('BLENDER_BIN'))\n")
    assert not r.ok and r.no_save
    out = dict(line.split(" ", 1) for line in r.stdout_tail.splitlines())
    assert out["KEY"] == "None"
    names = set(eval(out["ENV"]))  # noqa: S307 - a list literal this test printed
    # LC_CTYPE: set by the interpreter itself when LANG is absent (PEP 538 locale coercion), not passed by the runner
    assert names <= {"PATH", "HOME", "LANG", "TMPDIR", "BLENDER_BIN", "AGY3D_BUILD_OUT", "AGY3D_BUILD_CACHE", "LC_CTYPE"}
    assert {"AGY3D_BUILD_OUT", "AGY3D_BUILD_CACHE", "PATH"} <= names and "HTTPS_PROXY" not in names
    assert out["CWD"] == str((tmp_path / "src").resolve()) and out["OUT"] == str(tmp_path / "out")
    assert out["BLENDER"] == "/opt/blender/blender"


def test_a_file_beside_the_script_cannot_shadow_a_library(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "colorsys.py").write_text("raise SystemExit('shadowed')\n")
    r = run(tmp_path, "import colorsys\nprint(colorsys.rgb_to_hsv(1, 0, 0))\n")
    assert r.no_save and r.stdout_tail == "(0.0, 1.0, 1)"


def test_a_timeout_kills_the_script_and_its_children(unguarded, tmp_path):
    r = run(tmp_path, "import typing\nos = typing.sys.modules['os']\npid = os.fork()\n"
                      "print('CHILD' if pid == 0 else f'FORKED {pid}', flush=True)\nwhile True:\n    pass\n", timeout_s=2)
    assert not r.ok and r.timed_out and r.error.startswith("s.py ran longer than 2 s and was stopped")
    child = int(next(x for x in r.stdout_tail.splitlines() if x.startswith("FORKED")).split()[1])
    time.sleep(0.5)
    stat = Path(f"/proc/{child}/stat")
    assert not stat.exists() or stat.read_text().split(")")[1].split()[0] in {"Z", "X"}


def test_an_error_names_the_scripts_innermost_line_and_never_our_frames(tmp_path):
    r = run(tmp_path, "import math\n\ndef leg(n):\n    return math.sqrt(n)\n\nlegs = [leg(n) for n in (4, -1)]\n")
    assert not r.ok and r.error == "s.py line 4: return math.sqrt(n)\nValueError: math domain error"
    assert "Traceback" not in r.error and "runpy" not in r.error and "runner" not in r.error


def test_a_wrong_keyword_names_the_signature(tmp_path):
    r = run(tmp_path, "def box(id, size=(1, 1, 1), bevel=0):\n    return id\n\nbox('a', sise=2)\n")
    lines = r.error.split("\n")
    assert lines[0] == "s.py line 4: box('a', sise=2)" and lines[2] == "expected: box(id, size=(1, 1, 1), bevel=0)"
    assert lines[1].startswith("TypeError: box() got an unexpected keyword argument 'sise'")  # 3.13 adds "Did you mean"


def test_output_tail_is_capped_and_save_is_required(tmp_path):
    r = run(tmp_path, "for i in range(500):\n    print('line', i)\n")
    assert r.no_save and len(r.stdout_tail) <= runner.STDOUT_TAIL and r.stdout_tail.endswith("line 499")
    assert r.error == "s.py ran to the end but wrote no design: a.save() must be its last line"


def test_a_guard_refusal_never_starts_a_process(tmp_path):
    r = run(tmp_path, "import os\nos.system('touch should_not_exist')\n")
    assert r.refused and r.seconds < 0.5 and not (tmp_path / "src" / "should_not_exist").exists()
    assert not (tmp_path / "out").exists()


def test_a_child_that_leaves_the_group_cannot_hold_the_answer(unguarded, tmp_path):
    """Found by review (g4/hang_*.py): with pipes, a child that left the process group (setsid) held the script's output
    open, so a 5 s limit answered after 30 s and a script that ended at once was reported as timed out. The output now
    goes to files and the runner waits for the process."""
    head = "import typing\nos = typing.sys.modules['os']\nos.system('setsid sleep 20 > /dev/null 2>&1 < /dev/null &' if False else 'setsid sleep 20 &')\n"
    t0 = time.monotonic()
    r = run(tmp_path, head + "print('ended')\n", timeout_s=8)
    assert not r.timed_out and r.no_save and "ended" in r.stdout_tail and time.monotonic() - t0 < 6
    t0 = time.monotonic()
    r = run(tmp_path, head + "while True:\n    pass\n", timeout_s=3)
    assert r.timed_out and time.monotonic() - t0 < 6


def test_the_script_is_read_once_and_that_text_is_run_and_returned(unguarded, tmp_path):
    """Found by review (g8/overwrite_src.py): a script that wrote over its own file shipped what it wrote. The text that
    was checked is the text that runs (its frames still carry the file's name and line numbers) and is returned."""
    text = ("import typing\nos = typing.sys.modules['os']\n"
            "print(__file__.endswith('s.py'))\n"
            "os.truncate(__file__, 0)\n"
            "x = 1 / 0\n")
    r = run(tmp_path, text)
    assert r.source == text and (tmp_path / "src" / "s.py").read_text() == ""
    assert r.error == "s.py line 5: x = 1 / 0\nZeroDivisionError: division by zero" and r.stdout_tail == "True"


def test_a_syntax_error_is_reported_as_one(tmp_path):
    r = run(tmp_path, "x = 1\nfor i in range(3)\n    x += i\n")
    assert r.syntax and not r.refused and r.error.startswith("line 2: for i in range(3) (") and r.source.startswith("x = 1")
