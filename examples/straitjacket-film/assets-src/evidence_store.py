from agy3d import Asset

a = Asset("evidence_store", engine="web_three", max_tris=6000, art_direction="realistic",
          brief="a precision laboratory archive rack in satin charcoal powder-coated steel, open at the front, with a back panel, "
                "a plinth, eight equal bays and one wider bay with three shelves, brushed aluminium divider edges and a small "
                "brass label holder over each bay; new and clean")

steel = a.material("charcoal_steel", "#2b3038", surface="satin charcoal powder-coated steel, matte, clean, new",
                   metallic=0.0, roughness=0.62)
alu = a.material("brushed_aluminium", "#b8bcc2", surface="brushed aluminium, fine linear grain, clean, new",
                 metallic=1.0, roughness=0.38)
brass = a.material("brass_plate", "#b08a3e", surface="satin brass plate, clean, new", metallic=1.0, roughness=0.42)

# exact: outer x +-2.20, y -0.31..0.31 (back panel 0.04 at +y), inner floor z 0.16, top rail underside 1.90, top 1.98
OX, OY = 2.20, 0.31
FLOOR, RAIL0, TOP = 0.16, 1.90, 1.98
WALL = 0.04
BACK0 = OY - 0.04         # back panel y 0.27..0.31
E = 0.002                 # overlap that makes parts touch
BV = 0.003

a.box("plinth", size=(2 * OX, 2 * OY, FLOOR), bevel=0.004, bottom=(0, 0, 0), material=steel)
a.box("top_rail", size=(2 * OX, 2 * OY, TOP - RAIL0), bevel=BV, bottom=(0, 0, RAIL0), material=steel)
a.box("back_panel", size=(2 * OX - 0.004, 0.04, RAIL0 - FLOOR + 2 * E), bottom=(0, BACK0 + 0.02, FLOOR - E), material=steel)
a.box("side_wall", size=(WALL, 2 * OY, RAIL0 - FLOOR + 2 * E), bevel=BV, bottom=(OX - WALL / 2, 0, FLOOR - E),
      material=steel, mirror="x")

# dividers 0.04 thick between the bays (inner x-ranges from the brief), steel body plus a brushed aluminium front edge
div_lo = [-1.74, -1.28, -0.82, -0.36, 0.10, 0.56, 1.02, 1.48]
for i, x0 in enumerate(div_lo):
    cx = x0 + 0.02
    a.box(f"divider_{i}", size=(0.04, (BACK0 + E) - (-0.298), RAIL0 - FLOOR + 2 * E),
          at=(cx, (-0.298 + BACK0 + E) / 2, (FLOOR + RAIL0) / 2), bevel=0.002, material=steel)
    a.box(f"divider_edge_{i}", size=(0.04, 0.01, RAIL0 - FLOOR - 2 * E), bevel=0.002,
          at=(cx, -OY + 0.005, (FLOOR + RAIL0) / 2), material=alu)

# the ninth, wider bay (1.52..2.16): three shelves 0.02 thick, top faces at z 0.45, 0.95, 1.45, with an aluminium nosing
for k, ztop in enumerate((0.45, 0.95, 1.45)):
    a.box(f"shelf_{k}", size=(2.16 - 1.52 + 2 * 0.02, (BACK0 + E) - (-0.298), 0.02), bevel=0.002,
          at=(1.84, (-0.298 + BACK0 + E) / 2, ztop - 0.01), material=steel)
    a.box(f"shelf_nose_{k}", size=(2.16 - 1.52, 0.014, 0.03), bevel=0.002, at=(1.84, -OY + 0.006, ztop - 0.015), material=alu)

# nine brass label holders on the front of the top rail, one centred over each bay
bay_c = [(-2.16 - 1.74) / 2, (-1.70 - 1.28) / 2, (-1.24 - 0.82) / 2, (-0.78 - 0.36) / 2, (-0.32 + 0.10) / 2,
         (0.14 + 0.56) / 2, (0.60 + 1.02) / 2, (1.06 + 1.48) / 2, (1.52 + 2.16) / 2]
for i, cx in enumerate(bay_c):
    a.box(f"label_{i}", size=(0.28, 0.006, 0.05), bevel=0.0015, inset_faces=("-y",), inset_margin=0.008, inset_depth=0.002,
          at=(cx, -OY - 0.001, (RAIL0 + TOP) / 2), material=brass)

# brushed aluminium trim along the front edges (12 mm wide, 4 mm proud): top rail (upper and lower edge) and plinth (top, bottom)
TW, TP = 0.012, 0.004
ty = -OY - TP / 2 + 0.001
for nm, zc in (("trim_rail_top", TOP - TW / 2), ("trim_rail_bot", RAIL0 + TW / 2),
               ("trim_plinth_top", FLOOR - TW / 2), ("trim_plinth_bot", TW / 2)):
    a.box(nm, size=(2 * OX, TP + 0.002, TW), bevel=0.001, at=(0, ty, zc), material=alu)

a.save()
