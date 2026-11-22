import pytest

from antigravity3d.adapters.blender_5_3 import shim_5_3 as S


def test_normalisers():
    assert S.normalize_engine_id("BLENDER_EEVEE_NEXT") == "BLENDER_EEVEE"
    assert S.normalize_engine_id("CYCLES") == "CYCLES"
    assert S.normalize_boolean_solver("FAST") == "FLOAT"
    assert S.normalize_boolean_solver("EXACT") == "EXACT"
    assert S.normalize_pass_name("Depth") == "Depth"
    assert S.normalize_pass_name("Diffuse Color") == "Diffuse Color"  # current names pass through
    assert S.normalize_pass_name("DiffCol") == "Diffuse Color" and S.normalize_pass_name("Z") == "Depth"  # pre-5.0 names are mapped
    assert S.normalize_pass_name("AO") == "Ambient Occlusion" and S.normalize_pass_name("Emit") == "Emission"
    assert S.normalize_pass_name("Custom") == "Custom"


def test_split_properties():
    rna, idp = S.split_properties({"a": 1, "b": 2, "c": 3}, {"a", "c"})
    assert rna == {"a": 1, "c": 3} and idp == {"b": 2}


def test_readonly_name_guard():
    assert S.is_readonly_propgroup_assignment("name")
    assert not S.is_readonly_propgroup_assignment("value")


def test_rewrite_source():
    src = (
        "scene.render.engine = 'BLENDER_EEVEE_NEXT'\n"
        "m.solver = 'FAST'\n"
        "m2 = obj.modifiers.new('b', 'BOOLEAN')\n"
        "x = bpy.types.AssetHandle\n"
        "tex = img.bindcode\n"
        "keep = 'FAST'\n"
    )
    out, changes = S.rewrite_source(src)
    assert "BLENDER_EEVEE_NEXT" not in out and "'BLENDER_EEVEE'" in out
    assert "m.solver = 'FLOAT'" in out
    assert "AssetRepresentation" in out and "AssetHandle" not in out
    assert "gpu.texture.from_image(img)" in out and "bindcode" not in out
    assert "keep = 'FAST'" in out
    assert len(changes) == 4


def test_rewrite_leaves_bgl_and_bad_syntax():
    assert S.rewrite_source("import bgl\n") == ("import bgl\n", [])
    assert S.rewrite_source("def (:") == ("def (:", [])


def test_rewritten_source_passes_deprecation_lint():
    from antigravity3d.adapters.blender_5_3.linter import lint_script
    out, _ = S.rewrite_source("import bpy\nbpy.context.scene.render.engine = 'BLENDER_EEVEE_NEXT'\nm.solver = 'FAST'\n")
    assert lint_script(out) == []


def test_install_with_fake_bpy():
    import types
    bpy = types.SimpleNamespace(types=types.SimpleNamespace(AssetRepresentation=object))
    S.install(bpy)
    scene = types.SimpleNamespace(render=types.SimpleNamespace(engine=""))
    bpy.agy_compat.set_engine(scene, "BLENDER_EEVEE_NEXT")
    assert scene.render.engine == "BLENDER_EEVEE"
    assert bpy.agy_compat.pass_name("Z") == "Depth"


def test_rewrite_imported_asset_representation_and_bindcode():
    code, changes = S.rewrite_source(
        'from __future__ import annotations\nfrom bpy.types import AssetHandle as AH\nthumb = img.bindcode')
    assert "from bpy.types import AssetRepresentation as AH" in code and "import gpu.texture" in code
    assert code.index("gpu.init()") < code.index("gpu.texture.from_image")  # in a background Blender, from_image without gpu.init() crashes
    compile(code, "<shim>", "exec")


def test_rewritten_bindcode_source_passes_the_gpu_init_lint():
    from antigravity3d.adapters.blender_5_3.linter import lint_script
    out, _ = S.rewrite_source("import bpy\nimg = bpy.data.images['x']\nthumb = img.bindcode\n")
    assert lint_script(out) == []
