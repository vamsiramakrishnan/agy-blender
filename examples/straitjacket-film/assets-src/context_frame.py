from agy3d import Asset

a = Asset("context_frame", engine="web_three", max_tris=2500, art_direction="realistic",
          brief="a precision laboratory window frame in satin charcoal powder-coated steel, open through front to back, "
                "with brushed aluminium corner brackets and bolt heads on the front face, a small brass label holder on the "
                "top bar near the left end, and two wide feet; new and clean")

steel = a.material("charcoal_steel", "#23272e", surface="satin charcoal powder-coated steel, matte, clean, new",
                   metallic=0.0, roughness=0.62)
alu = a.material("brushed_aluminium", "#b8bcc2", surface="brushed aluminium, fine linear grain, clean, new",
                 metallic=1.0, roughness=0.38)
brass = a.material("brass_plate", "#b08a3e", surface="satin brass plate, clean, new", metallic=1.0, roughness=0.42)

# inner opening (exact): x -1.28..1.28, z 0.18..1.78, y -0.33..0.33; bars 0.12
IX, IZ0, IZ1, IY = 1.28, 0.18, 1.78, 0.33
BAR = 0.12
OX = IX + BAR            # 1.40
ZB0, ZT1 = IZ0 - BAR, IZ1 + BAR   # 0.06, 1.90
BV = 0.003

# side bars (one side, mirrored), full height
a.box("side_bar", size=(BAR, 2 * IY, ZT1 - ZB0), bevel=BV, bottom=(IX + BAR / 2, 0, ZB0), material=steel, mirror="x")
# top and bottom bars run into the side bars by 2 mm (never into the opening)
a.box("top_bar", size=(2 * IX + 0.004, 2 * IY, BAR), bevel=BV, bottom=(0, 0, IZ1), material=steel)
a.box("bottom_bar", size=(2 * IX + 0.004, 2 * IY, BAR), bevel=BV, bottom=(0, 0, ZB0), material=steel)

# feet: wider in y than the frame so it stands
a.box("foot", size=(0.24, 0.90, ZB0 + 0.002), bevel=BV, bottom=(0.95, 0, 0), material=steel, mirror="x")

# corner brackets on the front face (y = -0.33): two flat arms per corner, kept on the bar strips (never over the opening)
T = 0.012
AW = 0.07     # arm width
AL = 0.26     # arm length
yb = -IY - T / 2 + 0.002
for sx in (-1, 1):
    for sz, zc in ((1, IZ1 + BAR / 2), (-1, IZ0 - BAR / 2)):
        tag = f"{'r' if sx > 0 else 'l'}{'t' if sz > 0 else 'b'}"
        cx = sx * (IX + BAR / 2)
        # horizontal arm along the top/bottom bar, vertical arm along the side bar
        a.box(f"brk_h_{tag}", size=(AL, T, AW), bevel=0.002, at=(cx - sx * (AL / 2 - BAR / 2 - 0.0), yb, zc), material=alu)
        a.box(f"brk_v_{tag}", size=(AW, T, AL), bevel=0.002, at=(cx, yb, zc - sz * (AL / 2 - BAR / 2)), material=alu)
        # bolt heads: corner, and one at each arm end
        pts = [(cx, zc), (cx - sx * (AL - BAR / 2 - 0.03), zc), (cx, zc - sz * (AL - BAR / 2 - 0.03))]
        for k, (bx, bz) in enumerate(pts):
            a.cylinder(f"bolt_{tag}_{k}", radius=0.0105, height=0.009, segments=6, axis="y",
                       at=(bx, -IY - T - 0.0025 + 0.002, bz), material=alu)

# brass label holder on the front of the top bar, near the left end (about 0.30 x 0.05)
a.box("label_holder", size=(0.30, 0.008, 0.05), bevel=0.0015, at=(-0.93, -IY - 0.004 + 0.002, IZ1 + BAR / 2), material=brass)
a.box("label_slot", size=(0.26, 0.004, 0.026), bevel=0.001, at=(-0.93, -IY - 0.008 - 0.002 + 0.002, IZ1 + BAR / 2), material=steel)

a.save()
