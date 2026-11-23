from agy3d import Asset

a = Asset("console", engine="web_three", max_tris=3000, art_direction="realistic",
          brief="a precision laboratory console cabinet in satin charcoal powder-coated steel: a recessed dark screen panel on the "
                "front upper half, a keyboard shelf, vent slots, four feet and an aluminium outlet port on the right side; "
                "new and clean")

steel = a.material("charcoal_steel", "#2b3038", surface="satin charcoal powder-coated steel, matte, clean, new",
                   metallic=0.0, roughness=0.62)
alu = a.material("brushed_aluminium", "#b8bcc2", surface="brushed aluminium, fine linear grain, clean, new",
                 metallic=1.0, roughness=0.38)
dark = a.material("screen_black", "#0b0d10", surface="black display glass and dark vent inlay, smooth, clean, new",
                  metallic=0.0, roughness=0.25)

E = 0.002
BV = 0.003
HW, HD = 0.5, 0.375            # footprint 1.0 x 0.75
FOOT = 0.06
LOW_TOP = 0.80                 # lower cabinet top
TRAY_TOP = 0.83
TOP = 1.55

# four feet, aluminium
for sx in (-1, 1):
    for sy in (-1, 1):
        a.box(f"foot_{'r' if sx > 0 else 'l'}{'b' if sy > 0 else 'f'}", size=(0.09, 0.09, FOOT + E), bevel=0.003,
              bottom=(sx * 0.42, sy * 0.29, 0), material=alu)

# lower cabinet with a recessed front door panel
a.box("lower", size=(2 * HW, 2 * HD, LOW_TOP - FOOT), bevel=BV, inset_faces=("-y",), inset_margin=0.05, inset_depth=0.005,
      bottom=(0, 0, FOOT), material=steel)
# vent slots: dark inlays in the front door panel and in the left side
for i in range(9):
    a.box(f"vent_f{i}", size=(0.34, 0.004, 0.012), at=(0, -HD + 0.005 + 0.0005, 0.11 + i * 0.026), material=dark)
for i in range(9):
    a.box(f"vent_s{i}", size=(0.004, 0.34, 0.012), at=(-HW - 0.0005 + 0.002, 0.0, 0.11 + i * 0.026), material=dark)

# keyboard shelf (tray) with a key plate and keys
a.box("tray", size=(0.88, 0.24, TRAY_TOP - (LOW_TOP - 0.005)), bevel=BV, bottom=(0, -0.25, LOW_TOP - 0.005), material=steel)
a.box("key_plate", size=(0.56, 0.17, 0.008 + E), bevel=0.002, bottom=(0, -0.255, TRAY_TOP - E), material=alu)
kz = TRAY_TOP + 0.008
for r, ky in enumerate((-0.31, -0.27, -0.23)):
    for c in range(12):
        a.box(f"key_{r}_{c}", size=(0.034, 0.032, 0.008 + E), bottom=(-0.236 + c * 0.043, ky, kz - E), material=dark)
a.box("key_space", size=(0.24, 0.032, 0.008 + E), bottom=(0, -0.19, kz - E), material=dark)

# upper housing with the recessed screen panel on the front (-y)
HY0, HY1 = -0.15, 0.37
UW = 0.49
a.box("housing", size=(2 * UW, HY1 - HY0, TOP - (LOW_TOP - E)), bevel=BV, inset_faces=("-y",), inset_margin=0.06, inset_depth=0.012,
      at=(0, (HY0 + HY1) / 2, (LOW_TOP - E + TOP) / 2), material=steel)
# the screen: dark glass in the recess (recess floor at y = -0.138)
a.box("screen", size=(0.80, 0.008, 0.56), bevel=0.002, at=(0, HY0 + 0.012 - 0.002, 1.19), material=dark)
# aluminium trim round the screen recess: 12 mm wide, 4 mm proud, on the housing front face outside the recess
RX0, RZ0, RZ1 = 0.43, 0.89, 1.49
ty = HY0 - 0.002 + 0.001
for nm, zc in (("trim_scr_top", RZ1 + 0.006), ("trim_scr_bot", RZ0 - 0.006)):
    a.box(nm, size=(2 * RX0 + 0.024, 0.006, 0.012), bevel=0.001, at=(0, ty, zc), material=alu)
a.box("trim_scr_side", size=(0.012, 0.0055, RZ1 - RZ0), bevel=0.001, at=(RX0 + 0.006, ty, (RZ0 + RZ1) / 2), material=alu, mirror="x")

# top vent slots on the housing
for i in range(8):
    a.box(f"vent_t{i}", size=(0.5, 0.012, 0.004), at=(0.0, 0.10 + i * 0.03, TOP + 0.0005 - 0.001), material=dark)

# outlet port on the +x side: a recessed rectangular opening 0.16 (y) x 0.07 (z), centred z = 1.30, y = 0,
# in a short aluminium lip that stands out to x = 0.58
a.box("outlet_lip", size=(0.58 - (UW - E), 0.20, 0.11), bevel=0.002, inset_faces=("+x",), inset_margin=0.018, inset_depth=0.07,
      at=((0.58 + UW - E) / 2, 0.0, 1.30), material=alu)

a.save()
