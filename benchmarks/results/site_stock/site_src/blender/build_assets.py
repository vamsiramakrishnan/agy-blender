"""
Headless Blender 5.3 procedural asset builder for the Straitjacket 3D scroll website.
Generates five precision-machined PBR glTF Binary (.glb) assemblies:
  1. straitjacket_cinch.glb  — Iconic [ ] brackets + woven 3D amber tension straps & clasps
  2. artifact_vault.glb      — Local content-addressed storage silo with spinning optical platters
  3. evidence_prism.glb      — Four-gate mechanical iris aperture bench + Digest Slab & Needle Crystal
  4. anchor_clamp.glb        — Content-anchored caliper + articulated source-file line stack + static pointer
  5. harness_nodes.glb       — Append-only Task Ledger astrolabe disc, Steward spire, Worker Pod & Signal Capsule

Run with:
  blender -b --python blender/build_assets.py
"""

import math
import os
import bpy
import bmesh
from mathutils import Vector, Matrix, Euler

OUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public", "models"))
os.makedirs(OUT_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Scene & Material Helpers
# ---------------------------------------------------------------------------

def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.system = 'METRIC'
    scene.unit_settings.scale_length = 1.0


def make_pbr_material(
    name,
    base_color=(0.12, 0.14, 0.18, 1.0),
    metallic=0.85,
    roughness=0.28,
    emission_color=None,
    emission_strength=0.0,
    coat_weight=0.0,
    coat_roughness=0.15,
    alpha=1.0,
):
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()

    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    bsdf.inputs["Base Color"].default_value = base_color
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = roughness
    if "Coat Weight" in bsdf.inputs:
        bsdf.inputs["Coat Weight"].default_value = coat_weight
        bsdf.inputs["Coat Roughness"].default_value = coat_roughness
    if alpha < 1.0:
        bsdf.inputs["Alpha"].default_value = alpha
        mat.blend_method = 'BLEND' if hasattr(mat, 'blend_method') else 'HASHED'

    if emission_color is not None and emission_strength > 0.0:
        bsdf.inputs["Emission Color"].default_value = emission_color
        bsdf.inputs["Emission Strength"].default_value = emission_strength

    return mat


def create_shared_materials():
    return {
        # Machined dark obsidian titanium (chassis, structural ribs)
        "dark_titanium": make_pbr_material(
            "SJ_DarkTitanium",
            base_color=(0.045, 0.055, 0.072, 1.0),
            metallic=0.92,
            roughness=0.34,
            coat_weight=0.15,
            coat_roughness=0.25,
        ),
        # Brushed machined aluminum/steel (Straitjacket [ ] brackets, calipers)
        "machined_steel": make_pbr_material(
            "SJ_MachinedSteel",
            base_color=(0.68, 0.74, 0.80, 1.0),
            metallic=0.90,
            roughness=0.24,
            coat_weight=0.25,
            coat_roughness=0.12,
        ),
        # Satin gunmetal (clasps, fasteners, bolts, inner rails)
        "gunmetal": make_pbr_material(
            "SJ_Gunmetal",
            base_color=(0.14, 0.17, 0.21, 1.0),
            metallic=0.88,
            roughness=0.40,
        ),
        # Signature woven amber strap (#F0B429 linear ~ (0.86, 0.46, 0.024))
        "amber_strap": make_pbr_material(
            "SJ_AmberStrap",
            base_color=(0.86, 0.45, 0.03, 1.0),
            metallic=0.72,
            roughness=0.28,
            emission_color=(0.95, 0.56, 0.05, 1.0),
            emission_strength=0.35,
            coat_weight=0.35,
            coat_roughness=0.18,
        ),
        # Bright emissive amber core (handles, receipts, quiet needle, signals)
        "amber_glow": make_pbr_material(
            "SJ_AmberGlow",
            base_color=(1.0, 0.68, 0.12, 1.0),
            metallic=0.20,
            roughness=0.18,
            emission_color=(1.0, 0.62, 0.08, 1.0),
            emission_strength=2.8,
        ),
        # Semantic blue (bounded, addressed, controlled)
        "blue_glow": make_pbr_material(
            "SJ_BlueGlow",
            base_color=(0.18, 0.52, 1.0, 1.0),
            metallic=0.25,
            roughness=0.20,
            emission_color=(0.22, 0.58, 1.0, 1.0),
            emission_strength=2.2,
        ),
        # Deep sapphire tinted glass/optical plate
        "optical_slate": make_pbr_material(
            "SJ_OpticalSlate",
            base_color=(0.06, 0.13, 0.24, 1.0),
            metallic=0.35,
            roughness=0.12,
            emission_color=(0.08, 0.28, 0.58, 1.0),
            emission_strength=0.42,
            coat_weight=0.7,
            coat_roughness=0.06,
        ),
        # Semantic red (flood, unanchored drift, refusal)
        "red_glow": make_pbr_material(
            "SJ_RedGlow",
            base_color=(0.92, 0.18, 0.16, 1.0),
            metallic=0.25,
            roughness=0.22,
            emission_color=(0.98, 0.20, 0.18, 1.0),
            emission_strength=2.4,
        ),
        # Semantic green (verified outcome, relocated anchor)
        "green_glow": make_pbr_material(
            "SJ_GreenGlow",
            base_color=(0.14, 0.78, 0.34, 1.0),
            metallic=0.25,
            roughness=0.20,
            emission_color=(0.16, 0.85, 0.36, 1.0),
            emission_strength=2.3,
        ),
        # Matte neutral slate (code line slabs)
        "slab_neutral": make_pbr_material(
            "SJ_SlabNeutral",
            base_color=(0.11, 0.14, 0.19, 1.0),
            metallic=0.55,
            roughness=0.38,
        ),
    }


def link_mesh(name, bm, mat, location=(0, 0, 0), rotation=(0, 0, 0), parent=None, bevel_width=0.0, bevel_segs=2, smooth=False):
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    mesh = bpy.data.meshes.new(name + "_Mesh")
    bm.to_mesh(mesh)
    bm.free()

    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    obj.location = Vector(location)
    obj.rotation_euler = Euler(rotation, 'XYZ')
    if parent is not None:
        obj.parent = parent
    if mat is not None:
        obj.data.materials.append(mat)

    if bevel_width > 0.0:
        mod = obj.modifiers.new("Bevel", 'BEVEL')
        mod.width = bevel_width
        mod.segments = bevel_segs
        mod.limit_method = 'ANGLE'
        mod.angle_limit = math.radians(32)
        mod.harden_normals = False

    if smooth:
        for poly in obj.data.polygons:
            poly.use_smooth = True

    return obj


def make_empty(name, location=(0, 0, 0), parent=None):
    obj = bpy.data.objects.new(name, None)
    bpy.context.collection.objects.link(obj)
    obj.location = Vector(location)
    if parent is not None:
        obj.parent = parent
    return obj


def make_box(name, size, mat, location=(0, 0, 0), rotation=(0, 0, 0), parent=None, bevel=0.02):
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=Vector(size), verts=bm.verts)
    return link_mesh(name, bm, mat, location=location, rotation=rotation, parent=parent, bevel_width=bevel, bevel_segs=2, smooth=True)


def make_cylinder(name, radius, depth, segs, mat, location=(0, 0, 0), rotation=(0, 0, 0), parent=None, bevel=0.015):
    bm = bmesh.new()
    bmesh.ops.create_cone(
        bm,
        cap_ends=True,
        cap_tris=False,
        segments=segs,
        radius1=radius,
        radius2=radius,
        depth=depth,
    )
    return link_mesh(name, bm, mat, location=location, rotation=rotation, parent=parent, bevel_width=bevel, bevel_segs=2, smooth=True)


def make_ring_mesh(name, inner_r, outer_r, depth, segs, mat, location=(0, 0, 0), rotation=(0, 0, 0), parent=None, bevel=0.012):
    """Build a crisp hollow machined ring along local Z axis."""
    bm = bmesh.new()
    half_z = depth * 0.5
    rings = []
    for z in (-half_z, half_z):
        inner_loop = []
        outer_loop = []
        for i in range(segs):
            a = 2.0 * math.pi * i / segs
            ca, sa = math.cos(a), math.sin(a)
            inner_loop.append(bm.verts.new((inner_r * ca, inner_r * sa, z)))
            outer_loop.append(bm.verts.new((outer_r * ca, outer_r * sa, z)))
        rings.append((inner_loop, outer_loop))

    bm.verts.ensure_lookup_table()
    in_b, out_b = rings[0]
    in_t, out_t = rings[1]
    for i in range(segs):
        j = (i + 1) % segs
        # Bottom face
        bm.faces.new((out_b[i], in_b[i], in_b[j], out_b[j]))
        # Top face
        bm.faces.new((out_t[i], out_t[j], in_t[j], in_t[i]))
        # Outer wall
        bm.faces.new((out_b[i], out_b[j], out_t[j], out_t[i]))
        # Inner wall
        bm.faces.new((in_b[i], in_t[i], in_t[j], in_b[j]))

    return link_mesh(name, bm, mat, location=location, rotation=rotation, parent=parent, bevel_width=bevel, bevel_segs=2, smooth=True)


def export_glb(filename):
    path = os.path.join(OUT_DIR, filename)
    bpy.ops.export_scene.gltf(
        filepath=path,
        export_format='GLB',
        use_selection=False,
        export_apply=True,
        export_yup=True,
        export_normals=True,
        export_materials='EXPORT',
        export_cameras=False,
        export_lights=False,
    )
    size_kb = os.path.getsize(path) / 1024.0
    print(f"[Blender 5.3] Exported {filename} ({size_kb:.1f} KiB)")


# ---------------------------------------------------------------------------
# 1. STRAITJACKET CINCH (Iconic [ ] brackets + woven 3D amber straps)
# ---------------------------------------------------------------------------

def build_extruded_bracket(name, side, mat_body, mat_accent, mat_bolt, parent):
    """
    Build a precision architectural C-bracket ('[' if side=-1, ']' if side=+1)
    in Blender coordinates (X = horizontal, Y = depth, Z = vertical -> Y-up in glTF).
    """
    grp = make_empty(name, location=(0, 0, 0), parent=parent)

    # Dimensions mirroring icon.svg proportions (64x64 grid scaled to ~3.2m tall)
    # Bracket outer x at ±1.55, stem width 0.34, arm reach 0.85, total height 2.85, thickness (Y) 0.34
    s = side  # -1 for left '[', +1 for right ']'
    ox = 1.50 * s
    ix = (1.50 - 0.34) * s
    tip_x = (1.50 - 0.92) * s
    hz = 1.42
    arm_h = 0.34
    iz = hz - arm_h
    hy = 0.18

    # 2D polygon in XZ plane (counter-clockwise for side=-1, reversed for side=+1)
    pts_2d = [
        (ox, -hz),
        (tip_x, -hz),
        (tip_x, -iz),
        (ix, -iz),
        (ix, iz),
        (tip_x, iz),
        (tip_x, hz),
        (ox, hz),
    ]
    if s > 0:
        pts_2d = list(reversed(pts_2d))

    bm = bmesh.new()
    front_v = [bm.verts.new((x, -hy, z)) for (x, z) in pts_2d]
    back_v = [bm.verts.new((x, hy, z)) for (x, z) in pts_2d]
    n = len(pts_2d)
    bm.faces.new(front_v)
    bm.faces.new(list(reversed(back_v)))
    for i in range(n):
        j = (i + 1) % n
        bm.faces.new((front_v[i], front_v[j], back_v[j], back_v[i]))

    link_mesh(f"{name}_Body", bm, mat_body, parent=grp, bevel_width=0.032, bevel_segs=3, smooth=True)

    # Outer dark titanium spine rib for industrial contrast
    make_box(
        f"{name}_SpineRib",
        (0.11, 0.42, 2.35),
        mat_bolt,
        location=((1.54) * s, 0.0, 0.0),
        parent=grp,
        bevel=0.018,
    )

    # Inner recessed blue telemetry inlay along the vertical stem interior
    make_box(
        f"{name}_TelemetryStrip",
        (0.035, 0.075, 1.92),
        mat_accent,
        location=((1.155) * s, 0.0, 0.0),
        parent=grp,
        bevel=0.008,
    )

    # Machined hex bolts on the four corners (front and back)
    for z_pos in (-1.22, 1.22):
        for y_sign in (-1, 1):
            make_cylinder(
                f"{name}_Bolt_{'T' if z_pos > 0 else 'B'}_{'F' if y_sign < 0 else 'R'}",
                radius=0.062,
                depth=0.07,
                segs=6,
                mat=mat_bolt,
                location=((1.33) * s, y_sign * (hy + 0.02), z_pos),
                rotation=(math.radians(90), 0, 0),
                parent=grp,
                bevel=0.008,
            )

    return grp


def build_woven_strap(name, slope_sign, arch_sign, mat_strap, mat_buckle, parent):
    """
    Build a sculpted 3D curved industrial woven amber tension strap that wraps around
    the brackets and arches smoothly over (arch_sign=+1) or under (arch_sign=-1) at the center.
    slope_sign = +1: goes from (-x, -z) to (+x, +z)
    slope_sign = -1: goes from (-x, +z) to (+x, -z)
    """
    grp = make_empty(name, location=(0, 0, 0), parent=parent)

    # Curve in XZ from (-1.78, -1.06*slope) to (+1.78, +1.06*slope) with Y wave for real 3D weave
    segs = 44
    half_w = 0.165
    half_t = 0.048

    # Construct path points
    p_start = Vector((-1.78, 0.0, -1.05 * slope_sign))
    p_end = Vector((1.78, 0.0, 1.05 * slope_sign))
    tangent_xz = (p_end - p_start).normalized()
    perp_xz = Vector((-tangent_xz.z, 0.0, tangent_xz.x))  # width direction in XZ

    bm = bmesh.new()
    rings = []
    for i in range(segs + 1):
        u = i / segs
        s = (u - 0.5) * 2.0  # -1 .. +1
        base = p_start.lerp(p_end, u)

        # Y-profile: sits cleanly outside the brackets (y = -0.27 in front),
        # arches smoothly over/under at center (s=0), and curves slightly back at overhangs (|s|>0.82)
        weave_bump = arch_sign * 0.105 * math.exp(-((s * 3.1) ** 2))
        overhang_wrap = 0.18 * max(0.0, abs(s) - 0.78) ** 1.6 * 4.2
        y_center = -0.265 - weave_bump + overhang_wrap

        # Subtle rib modulation along the strap length (tactile braided texture in geometry)
        rib = 0.008 * math.sin(u * math.pi * 28.0)

        c = Vector((base.x, y_center, base.z))
        # Local cross-section (beveled octagon-like flat strap profile)
        w = half_w
        t = half_t + rib
        offsets = [
            perp_xz * (-w * 0.85) + Vector((0, -t, 0)),
            perp_xz * (w * 0.85) + Vector((0, -t, 0)),
            perp_xz * w + Vector((0, -t * 0.45, 0)),
            perp_xz * w + Vector((0, t * 0.45, 0)),
            perp_xz * (w * 0.85) + Vector((0, t, 0)),
            perp_xz * (-w * 0.85) + Vector((0, t, 0)),
            perp_xz * (-w) + Vector((0, t * 0.45, 0)),
            perp_xz * (-w) + Vector((0, -t * 0.45, 0)),
        ]
        ring = [bm.verts.new(c + off) for off in offsets]
        rings.append(ring)

    ring_len = len(rings[0])
    for i in range(segs):
        r0, r1 = rings[i], rings[i + 1]
        for k in range(ring_len):
            kn = (k + 1) % ring_len
            bm.faces.new((r0[k], r0[kn], r1[kn], r1[k]))
    # Cap both ends
    bm.faces.new(list(reversed(rings[0])))
    bm.faces.new(rings[-1])

    link_mesh(f"{name}_Ribbon", bm, mat_strap, parent=grp, smooth=True)

    # Also build rear return strap behind the brackets so 360-degree camera orbit looks complete!
    bm_back = bmesh.new()
    back_rings = []
    for i in range(segs + 1):
        u = i / segs
        s = (u - 0.5) * 2.0
        base = p_start.lerp(p_end, u)
        weave_bump = -arch_sign * 0.095 * math.exp(-((s * 3.1) ** 2))
        overhang_wrap = -0.18 * max(0.0, abs(s) - 0.78) ** 1.6 * 4.2
        y_center = 0.265 + weave_bump + overhang_wrap
        c = Vector((base.x, y_center, base.z))
        w = half_w * 0.92
        t = half_t * 0.88
        offsets = [
            perp_xz * (-w * 0.85) + Vector((0, -t, 0)),
            perp_xz * (w * 0.85) + Vector((0, -t, 0)),
            perp_xz * w + Vector((0, 0, 0)),
            perp_xz * (w * 0.85) + Vector((0, t, 0)),
            perp_xz * (-w * 0.85) + Vector((0, t, 0)),
            perp_xz * (-w) + Vector((0, 0, 0)),
        ]
        back_rings.append([bm_back.verts.new(c + off) for off in offsets])

    blen = len(back_rings[0])
    for i in range(segs):
        r0, r1 = back_rings[i], back_rings[i + 1]
        for k in range(blen):
            kn = (k + 1) % blen
            bm_back.faces.new((r0[k], r0[kn], r1[kn], r1[k]))
    bm_back.faces.new(list(reversed(back_rings[0])))
    bm_back.faces.new(back_rings[-1])
    link_mesh(f"{name}_BackRibbon", bm_back, mat_strap, parent=grp, smooth=True)

    # Machined tension clasps clamping the strap near each bracket shoulder
    angle_y = math.atan2(-tangent_xz.z, tangent_xz.x)
    for u_pos, tag in ((0.13, "L"), (0.87, "R")):
        pos = p_start.lerp(p_end, u_pos)
        clasp_pos = Vector((pos.x, -0.265, pos.z))
        make_box(
            f"{name}_Clasp_{tag}",
            (0.24, 0.16, 0.42),
            mat_buckle,
            location=clasp_pos,
            rotation=(0.0, angle_y, 0.0),
            parent=grp,
            bevel=0.018,
        )

    return grp


def build_straitjacket_cinch():
    reset_scene()
    mats = create_shared_materials()
    root = make_empty("StraitjacketCinch")

    build_extruded_bracket("BracketLeft", -1, mats["machined_steel"], mats["blue_glow"], mats["dark_titanium"], root)
    build_extruded_bracket("BracketRight", 1, mats["machined_steel"], mats["blue_glow"], mats["dark_titanium"], root)

    # Diagonal woven straps crossing over/under each other
    build_woven_strap("StrapOver", slope_sign=1, arch_sign=1, mat_strap=mats["amber_strap"], mat_buckle=mats["dark_titanium"], parent=root)
    build_woven_strap("StrapUnder", slope_sign=-1, arch_sign=-1, mat_strap=mats["amber_strap"], mat_buckle=mats["dark_titanium"], parent=root)

    # Central precision containment reticle ring inside the brackets
    core_grp = make_empty("CoreReticle", location=(0, 0, 0), parent=root)
    make_ring_mesh(
        "CoreReticle_Ring",
        inner_r=0.44,
        outer_r=0.52,
        depth=0.07,
        segs=48,
        mat=mats["gunmetal"],
        rotation=(math.radians(90), 0, 0),
        parent=core_grp,
        bevel=0.008,
    )
    make_ring_mesh(
        "CoreReticle_Glow",
        inner_r=0.39,
        outer_r=0.43,
        depth=0.04,
        segs=48,
        mat=mats["amber_glow"],
        rotation=(math.radians(90), 0, 0),
        parent=core_grp,
        bevel=0.004,
    )

    export_glb("straitjacket_cinch.glb")


# ---------------------------------------------------------------------------
# 2. ARTIFACT VAULT (Local Content-Addressed Store)
# ---------------------------------------------------------------------------

def build_artifact_vault():
    reset_scene()
    mats = create_shared_materials()
    root = make_empty("ArtifactVault")

    # Base & Top octagonal precision collars (in Blender Z-up -> Y-up in glTF)
    make_ring_mesh("VaultBaseRing", inner_r=0.72, outer_r=1.32, depth=0.22, segs=8, mat=mats["dark_titanium"], location=(0, 0, -1.35), parent=root, bevel=0.025)
    make_ring_mesh("VaultTopRing", inner_r=0.78, outer_r=1.32, depth=0.22, segs=8, mat=mats["dark_titanium"], location=(0, 0, 1.35), parent=root, bevel=0.025)
    make_ring_mesh("VaultTopSeal", inner_r=0.66, outer_r=0.78, depth=0.08, segs=32, mat=mats["amber_glow"], location=(0, 0, 1.44), parent=root, bevel=0.006)
    make_ring_mesh("VaultBaseTrim", inner_r=1.24, outer_r=1.36, depth=0.07, segs=8, mat=mats["machined_steel"], location=(0, 0, -1.24), parent=root, bevel=0.01)

    # 8 structural vertical ribs around the perimeter
    ribs_grp = make_empty("VaultRibs", parent=root)
    for i in range(8):
        angle = i * (2.0 * math.pi / 8.0) + math.pi / 8.0
        r = 1.24
        make_box(
            f"VaultRib_{i}",
            (0.11, 0.16, 2.55),
            mats["dark_titanium"] if i % 2 == 0 else mats["gunmetal"],
            location=(r * math.cos(angle), r * math.sin(angle), 0.0),
            rotation=(0, 0, angle),
            parent=ribs_grp,
            bevel=0.016,
        )
        # Subtle vertical blue address channel on every other rib
        if i % 2 == 0:
            make_box(
                f"VaultRibLight_{i}",
                (0.03, 0.04, 2.05),
                mats["blue_glow"],
                location=((r + 0.065) * math.cos(angle), (r + 0.065) * math.sin(angle), 0.0),
                rotation=(0, 0, angle),
                parent=ribs_grp,
                bevel=0.004,
            )

    # 6 stacked optical data storage platters (separately named for staggered rotation!)
    for idx in range(6):
        z = -0.92 + idx * 0.368
        platter = make_empty(f"VaultPlatter_{idx}", location=(0, 0, z), parent=root)
        make_ring_mesh(
            f"VaultPlatterDisc_{idx}",
            inner_r=0.34,
            outer_r=1.02,
            depth=0.11,
            segs=32,
            mat=mats["optical_slate"],
            parent=platter,
            bevel=0.012,
        )
        # Rim collar with index notch tabs
        make_ring_mesh(
            f"VaultPlatterRim_{idx}",
            inner_r=0.98,
            outer_r=1.06,
            depth=0.045,
            segs=32,
            mat=mats["machined_steel"] if idx != 3 else mats["amber_strap"],
            parent=platter,
            bevel=0.005,
        )
        # 3 small radial address index pins per platter
        for p in range(3):
            a = p * (2.0 * math.pi / 3.0) + idx * 0.55
            make_box(
                f"VaultPlatterPin_{idx}_{p}",
                (0.14, 0.05, 0.06),
                mats["amber_glow"] if idx == 3 else mats["blue_glow"],
                location=(0.95 * math.cos(a), 0.95 * math.sin(a), 0.0),
                rotation=(0, 0, a),
                parent=platter,
                bevel=0.005,
            )

    # Central hexagonal content-addressed core spine
    make_cylinder("VaultCoreSpine", radius=0.28, depth=2.72, segs=6, mat=mats["blue_glow"], parent=root, bevel=0.015)

    export_glb("artifact_vault.glb")


# ---------------------------------------------------------------------------
# 3. EVIDENCE PRISM (Four-Gate Mechanical Iris Bench + Digest Slab & Needle)
# ---------------------------------------------------------------------------

def build_evidence_prism():
    reset_scene()
    mats = create_shared_materials()
    root = make_empty("EvidencePrism")

    # Twin horizontal precision optical bench rails along X axis (X = -3.0 .. +3.0)
    for y_off in (-1.05, 1.05):
        make_box(
            f"BenchRail_{'L' if y_off < 0 else 'R'}",
            (5.6, 0.12, 0.14),
            mats["dark_titanium"],
            location=(0.0, y_off, -1.22),
            parent=root,
            bevel=0.015,
        )
        make_box(
            f"BenchRailTop_{'L' if y_off < 0 else 'R'}",
            (5.4, 0.05, 0.04),
            mats["machined_steel"],
            location=(0.0, y_off, -1.13),
            parent=root,
            bevel=0.006,
        )

    # 4 Concentric Mechanical Iris Gates: Birth, Entry, Residence, Emission
    gate_configs = [
        ("Gate_Birth", -2.15, 1.18, 0.86, mats["red_glow"]),
        ("Gate_Entry", -0.72, 1.08, 0.76, mats["amber_glow"]),
        ("Gate_Residence", 0.72, 0.98, 0.66, mats["blue_glow"]),
        ("Gate_Emission", 2.15, 0.88, 0.56, mats["green_glow"]),
    ]

    for gname, gx, outer_r, inner_r, accent_mat in gate_configs:
        gate = make_empty(gname, location=(gx, 0.0, 0.0), parent=root)

        # Base mounting stanchion connecting to rails
        make_box(f"{gname}_Pedestal", (0.24, 2.18, 0.24), mats["dark_titanium"], location=(0, 0, -1.16), parent=gate, bevel=0.018)

        # Rotating iris sub-group
        iris = make_empty(f"{gname}_Iris", parent=gate)

        # Outer machined housing ring (aligned perpendicular to X axis)
        make_ring_mesh(
            f"{gname}_OuterRing",
            inner_r=inner_r + 0.08,
            outer_r=outer_r,
            depth=0.20,
            segs=36,
            mat=mats["dark_titanium"],
            rotation=(0, math.radians(90), 0),
            parent=iris,
            bevel=0.016,
        )
        # Semantic inner glowing collar
        make_ring_mesh(
            f"{gname}_AccentRing",
            inner_r=inner_r,
            outer_r=inner_r + 0.065,
            depth=0.14,
            segs=36,
            mat=accent_mat,
            rotation=(0, math.radians(90), 0),
            parent=iris,
            bevel=0.006,
        )
        # 8 angled mechanical shutter vanes around the ring
        for b in range(8):
            a = b * (2.0 * math.pi / 8.0)
            r_mid = (inner_r + outer_r) * 0.51
            make_box(
                f"{gname}_Blade_{b}",
                (0.06, 0.26, 0.14),
                mats["machined_steel"],
                location=(0.09 if b % 2 == 0 else -0.09, r_mid * math.cos(a), r_mid * math.sin(a)),
                rotation=(a + 0.42, 0.0, 0.0),
                parent=iris,
                bevel=0.008,
            )

    # Emerging Digest Slab beyond Gate 4 (x = +3.35)
    digest_grp = make_empty("DigestSlab", location=(3.35, 0.0, 0.0), parent=root)
    make_box("DigestBody", (0.10, 1.08, 1.48), mats["optical_slate"], parent=digest_grp, bevel=0.022)
    # Machined steel outer frame around the digest slab
    make_box("DigestFrameTop", (0.15, 1.16, 0.09), mats["machined_steel"], location=(0, 0, 0.74), parent=digest_grp, bevel=0.01)
    make_box("DigestFrameBot", (0.15, 1.16, 0.09), mats["machined_steel"], location=(0, 0, -0.74), parent=digest_grp, bevel=0.01)
    # 5 crisp horizontal digest template bars
    for row in range(5):
        rz = 0.44 - row * 0.21
        is_needle = (row == 2)
        make_box(
            f"DigestRow_{row}",
            (0.14, 0.78 if not is_needle else 0.88, 0.065 if not is_needle else 0.095),
            mats["amber_glow"] if is_needle else mats["blue_glow"],
            location=(0.0, 0.0, rz),
            parent=digest_grp,
            bevel=0.008,
        )

    # Faceted Quiet Needle Crystal (line 14,238) floating slightly proud of the digest center
    bm_c = bmesh.new()
    bmesh.ops.create_icosphere(bm_c, subdivisions=1, radius=0.14)
    bmesh.ops.scale(bm_c, vec=Vector((0.8, 1.6, 0.65)), verts=bm_c.verts)
    link_mesh("NeedleCrystal", bm_c, mats["amber_glow"], location=(0.14, 0.0, 0.02), parent=digest_grp, smooth=False)

    export_glb("evidence_prism.glb")


# ---------------------------------------------------------------------------
# 4. ANCHOR CLAMP (Content-Anchored Caliper + File Line Column + Static Pointer)
# ---------------------------------------------------------------------------

def build_anchor_clamp():
    reset_scene()
    mats = create_shared_materials()
    root = make_empty("AnchorClamp")

    # Vertical twin precision guide rails on left & right of the code file column
    for x_off in (-1.38, 1.38):
        make_box(
            f"AnchorRail_{'L' if x_off < 0 else 'R'}",
            (0.10, 0.14, 4.60),
            mats["dark_titanium"],
            location=(x_off, 0.0, 0.0),
            parent=root,
            bevel=0.015,
        )
        make_box(
            f"AnchorRailTrack_{'L' if x_off < 0 else 'R'}",
            (0.035, 0.045, 4.40),
            mats["machined_steel"],
            location=(x_off - 0.045 * (1 if x_off > 0 else -1), 0.0, 0.0),
            parent=root,
            bevel=0.005,
        )

    # Top and bottom end-caps of the file column frame
    make_box("FileCapTop", (2.92, 0.24, 0.16), mats["dark_titanium"], location=(0, 0, 2.34), parent=root, bevel=0.02)
    make_box("FileCapBot", (2.92, 0.24, 0.16), mats["dark_titanium"], location=(0, 0, -2.34), parent=root, bevel=0.02)

    # 14 individual articulated Code Line Slabs (Z from +1.95 down to -1.95, spacing 0.30)
    # Lines 3..5 initially hold the target anchored function span!
    slabs_grp = make_empty("CodeLineSlabs", parent=root)
    for i in range(14):
        z = 1.95 - i * 0.30
        width = 1.75 + 0.32 * math.sin(i * 1.9 + 0.5)
        is_target = (3 <= i <= 5)
        mat = mats["optical_slate"] if is_target else mats["slab_neutral"]
        slab = make_box(
            f"LineSlab_{i:02d}",
            (width, 0.22, 0.17),
            mat,
            location=((width - 2.15) * 0.35, 0.0, z),
            parent=slabs_grp,
            bevel=0.016,
        )
        # Crisp left line-number LED pip
        make_box(
            f"LinePip_{i:02d}",
            (0.09, 0.25, 0.09),
            mats["amber_glow"] if is_target else mats["gunmetal"],
            location=(-width * 0.5 - 0.12, 0.0, 0.0),
            parent=slab,
            bevel=0.008,
        )

    # Static Unanchored Line Pointer (fixed on the left rail at the original line 4 position, z = +0.75)
    static_ptr = make_empty("UnanchoredPointer", location=(-1.58, 0.0, 0.75), parent=root)
    make_box("StaticPtrHousing", (0.28, 0.26, 0.56), mats["dark_titanium"], parent=static_ptr, bevel=0.02)
    make_box("StaticPtrNeedle", (0.42, 0.06, 0.06), mats["red_glow"], location=(0.24, -0.15, 0.0), parent=static_ptr, bevel=0.008)

    # Articulated Content-Anchored Caliper (slides vertically on rails to follow hash @07407f1c!)
    caliper = make_empty("AnchorCaliper", location=(0.0, 0.0, 0.75), parent=root)
    # Upper & lower machined steel clamping jaws framing the 3-line span (height ~0.92)
    make_box("CaliperUpperJaw", (2.68, 0.34, 0.10), mats["machined_steel"], location=(0, 0, 0.48), parent=caliper, bevel=0.016)
    make_box("CaliperLowerJaw", (2.68, 0.34, 0.10), mats["machined_steel"], location=(0, 0, -0.48), parent=caliper, bevel=0.016)
    # Left & right sliding rail carriages
    for sx, tag in ((-1.38, "L"), (1.38, "R")):
        make_box(f"CaliperCarriage_{tag}", (0.26, 0.38, 1.06), mats["dark_titanium"], location=(sx, 0, 0), parent=caliper, bevel=0.022)
        make_box(f"CaliperStrip_{tag}", (0.06, 0.42, 0.86), mats["amber_strap"], location=(sx + (0.12 if sx > 0 else -0.12), 0, 0), parent=caliper, bevel=0.008)
    # Right-side Optical Hash Verification Lens housing
    hash_core = make_empty("HashLens", location=(1.78, 0.0, 0.0), parent=caliper)
    make_cylinder("HashLensBody", radius=0.28, depth=0.30, segs=8, mat=mats["dark_titanium"], rotation=(math.radians(90), 0, 0), parent=hash_core, bevel=0.018)
    make_ring_mesh("HashLensRing", inner_r=0.14, outer_r=0.22, depth=0.34, segs=24, mat=mats["green_glow"], rotation=(math.radians(90), 0, 0), parent=hash_core, bevel=0.006)

    export_glb("anchor_clamp.glb")


# ---------------------------------------------------------------------------
# 5. HARNESS NODES & TASK LEDGER CONSTELLATION
# ---------------------------------------------------------------------------

def build_harness_nodes():
    reset_scene()
    mats = create_shared_materials()
    root = make_empty("HarnessConstellation")

    # 1) Central Append-Only Task Ledger Disc (horizontal astrolabe in XY plane, Z-up)
    ledger = make_empty("LedgerDisc", location=(0, 0, 0), parent=root)
    make_ring_mesh("LedgerOuterTrack", inner_r=1.55, outer_r=1.88, depth=0.14, segs=64, mat=mats["dark_titanium"], parent=ledger, bevel=0.014)
    make_ring_mesh("LedgerMidTrack", inner_r=1.08, outer_r=1.42, depth=0.11, segs=64, mat=mats["gunmetal"], parent=ledger, bevel=0.012)
    make_ring_mesh("LedgerAmberTrack", inner_r=1.45, outer_r=1.52, depth=0.08, segs=64, mat=mats["amber_glow"], parent=ledger, bevel=0.006)
    make_ring_mesh("LedgerBlueTrack", inner_r=0.96, outer_r=1.03, depth=0.08, segs=64, mat=mats["blue_glow"], parent=ledger, bevel=0.006)

    # 12 radial append-only ledger entry ticks around the disc
    for i in range(12):
        a = i * (2.0 * math.pi / 12.0)
        make_box(
            f"LedgerTick_{i}",
            (0.26, 0.05, 0.16),
            mats["machined_steel"],
            location=(1.70 * math.cos(a), 1.70 * math.sin(a), 0.02),
            rotation=(0, 0, a),
            parent=ledger,
            bevel=0.006,
        )

    # Central Deterministic Steward Spire (model-free handback router)
    spire = make_empty("StewardSpire", location=(0, 0, 0), parent=root)
    make_cylinder("StewardBase", radius=0.62, depth=0.24, segs=6, mat=mats["dark_titanium"], location=(0, 0, 0.06), parent=spire, bevel=0.02)
    make_cylinder("StewardColumn", radius=0.26, depth=0.72, segs=6, mat=mats["machined_steel"], location=(0, 0, 0.44), parent=spire, bevel=0.018)
    make_ring_mesh("StewardHalo", inner_r=0.40, outer_r=0.52, depth=0.08, segs=24, mat=mats["amber_strap"], location=(0, 0, 0.56), parent=spire, bevel=0.008)
    # Crown octahedral crystal
    bm_s = bmesh.new()
    bmesh.ops.create_icosphere(bm_s, subdivisions=1, radius=0.20)
    bmesh.ops.scale(bm_s, vec=Vector((1.0, 1.0, 1.55)), verts=bm_s.verts)
    link_mesh("StewardBeacon", bm_s, mats["amber_glow"], location=(0, 0, 0.98), parent=spire, smooth=False)

    # 2) Modular Sculpted Harness Worker Pod prototype (positioned off-origin for Three.js cloning into 7 orbiting pods!)
    pod = make_empty("WorkerPodTemplate", location=(0, 0, 0), parent=root)
    make_cylinder("PodCoreShell", radius=0.46, depth=0.28, segs=6, mat=mats["dark_titanium"], parent=pod, bevel=0.024)
    make_cylinder("PodTopVisor", radius=0.32, depth=0.10, segs=6, mat=mats["optical_slate"], location=(0, 0, 0.16), parent=pod, bevel=0.014)
    make_ring_mesh("PodAccentRing", inner_r=0.38, outer_r=0.48, depth=0.055, segs=6, mat=mats["blue_glow"], location=(0, 0, 0.06), parent=pod, bevel=0.006)
    make_cylinder("PodMcpPort", radius=0.20, depth=0.14, segs=12, mat=mats["machined_steel"], location=(0, 0, -0.18), parent=pod, bevel=0.01)
    # Twin docking stabilizer fins
    for sgn in (-1, 1):
        make_box(
            f"PodFin_{'L' if sgn < 0 else 'R'}",
            (0.24, 0.08, 0.22),
            mats["machined_steel"],
            location=(sgn * 0.52, 0, -0.02),
            rotation=(0, sgn * 0.24, 0),
            parent=pod,
            bevel=0.012,
        )

    # 3) Sleek Address Signal Capsule prototype (flies along relay arcs)
    capsule = make_empty("SignalCapsuleTemplate", location=(0, 0, 0), parent=root)
    make_cylinder("CapsuleShell", radius=0.08, depth=0.36, segs=12, mat=mats["machined_steel"], rotation=(0, math.radians(90), 0), parent=capsule, bevel=0.015)
    make_ring_mesh("CapsuleGlowBand", inner_r=0.06, outer_r=0.105, depth=0.10, segs=16, mat=mats["amber_glow"], rotation=(0, math.radians(90), 0), parent=capsule, bevel=0.008)

    export_glb("harness_nodes.glb")


if __name__ == "__main__":
    build_straitjacket_cinch()
    build_artifact_vault()
    build_evidence_prism()
    build_anchor_clamp()
    build_harness_nodes()
    print("[Blender 5.3] All 5 Straitjacket GLB assets built successfully.")
