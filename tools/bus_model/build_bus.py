"""The twin's bus: a minimal, low-poly 12 m city bus, built and exported by Blender.

It follows the buses Cyprus Public Transport runs in Nicosia (King Long U12 and B12, Yutong E12):
a tall box with a continuous dark window band, a cap over the front and rear of the roof, a
lower panel that rises toward the back, a long pod on the roof, a big windscreen under a
destination display, and the doors on the left (Cyprus drives on the left). Logos, lettering and
the operator's own colours stay out: on the map the body takes its line's colour.

Run it headless from the repository root (Blender 4.2 or later; 5.0 is what built the file):

    "C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" --background --factory-startup \
        --python tools/bus_model/build_bus.py -- [--out PATH.glb] [--previews DIR] [--no-previews]

It writes chatbot/web/src/assets/models/bus.glb and, unless told not to, preview renders in
docs/screenshots/bus-models/.

The model, as the map reads it (chatbot/web/src/lib/mobility/bus-model.ts):
- glTF axes: +Y up, +Z forward (the front of the bus), -X to the bus's right. In Blender the
  front faces -Y and the doors are on +X.
- Metres, true size: 12.0 m long, 2.55 m wide, 3.05 m to the roof (3.33 m over the roof pod).
- The origin is on the ground under the middle of the bus.
- Flat shaded, no textures, one mesh. Its materials, by name:
    body, accent    tinted with the line's colour (accent is the darker caps and lower panel)
    glass, trim, tyre, hub, roof    constant
    lights, tail    drawn unlit, so they read at night
- The shell (body, accent, glass, trim, lights, tail) is convex: a renderer may draw it on its
  own for a silhouette, without the wheels and the roof pods, and get no overlaps.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import bmesh
import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "chatbot" / "web" / "src" / "assets" / "models" / "bus.glb"
PREVIEWS = ROOT / "docs" / "screenshots" / "bus-models"

# -- dimensions (metres; Blender axes: X across, Y along with the front at -Y, Z up) --------------
L, W = 12.0, 2.55
HW = W / 2
YF, YR = -L / 2, L / 2  # front and rear faces
ZB, ZT = 0.32, 3.05  # underside of the skirt, roof
CF, CR = 0.30, 0.14  # front and rear vertical corners, cut off
TF, TR = 0.20, 0.12  # front and rear roof edges, cut off
WIN_LO, WIN_HI = 1.10, 2.60  # the side window band
FRONT_AXLE, REAR_AXLE = YF + 2.75, YR - 3.15
WHEEL_R, WHEEL_W = 0.50, 0.30
DOORS = [(YF + 0.30, YF + 1.45), (-0.35, 0.95)]  # front and middle doors, on the left (+X)
CAP_FRONT, CAP_REAR = YF + 1.60, YR - 0.55  # where the roof caps end (the front one is longer)
SWOOSH = ((-0.6, ZB), (YR - 0.5, WIN_LO))  # the lower panel rises from here to there
# Bands across the front (bumper, lamps, windscreen, a band, the destination display) and the
# rear (bumper, the lower panel with the lamps, the rear window); the face ends at ZT - TF / TR.
FRONT_BANDS = (0.46, 0.58, 0.82, 0.98, 2.58, 2.66, 2.83)
REAR_BANDS = (0.46, 0.60, 1.30, 1.98, 2.70)

# Materials: sRGB colour, then how the map treats it (documented above).
MATERIALS = {
    "body": "#ffffff",  # tinted: white takes the line's colour as it is
    "accent": "#9aa1ab",  # tinted: a darker shade of the line's colour
    "glass": "#1b2535",
    "trim": "#262c35",
    "lights": "#fff1c9",
    "tail": "#e0362c",
    "tyre": "#1a1c20",
    "hub": "#b8c0ca",
    "roof": "#d5dbe3",
}
SHELL = ("body", "accent", "glass", "trim", "lights", "tail")

# Preview colours: the operator's own look (white with blue), for comparing with photos, and
# three line colours as the map tints them.
CPT = {"body": "#f4f6f8", "accent": "#1f4fa8"}
LINES = ["#ff5a4e", "#2fb36d", "#4f8dff"]


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgba(hexstr: str) -> tuple[float, float, float, float]:
    h = hexstr.lstrip("#")
    r, g, b = (srgb_to_linear(int(h[i:i + 2], 16) / 255) for i in (0, 2, 4))
    return (r, g, b, 1.0)


def args() -> dict:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    out = {"out": OUT, "previews": PREVIEWS, "render": True}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--out":
            out["out"] = Path(argv[i + 1]); i += 1
        elif a == "--previews":
            out["previews"] = Path(argv[i + 1]); i += 1
        elif a == "--no-previews":
            out["render"] = False
        i += 1
    return out


# -- materials --------------------------------------------------------------------------------

def make_materials() -> dict[str, bpy.types.Material]:
    mats = {}
    for name, hexstr in MATERIALS.items():
        m = bpy.data.materials.new(name)
        m.use_nodes = True
        bsdf = m.node_tree.nodes.get("Principled BSDF")
        bsdf.inputs["Base Color"].default_value = rgba(hexstr)
        bsdf.inputs["Roughness"].default_value = 0.25 if name == "glass" else 0.7
        bsdf.inputs["Metallic"].default_value = 0.0
        m.diffuse_color = rgba(hexstr)  # what Workbench shows
        mats[name] = m
    return mats


def recolour(mats: dict, colours: dict[str, str]) -> None:
    for name, hexstr in colours.items():
        m = mats[name]
        m.node_tree.nodes.get("Principled BSDF").inputs["Base Color"].default_value = rgba(hexstr)
        m.diffuse_color = rgba(hexstr)


# -- geometry ---------------------------------------------------------------------------------

def hull(bm: bmesh.types.BMesh, points: list[tuple[float, float, float]]) -> None:
    verts = [bm.verts.new(p) for p in points]
    res = bmesh.ops.convex_hull(bm, input=verts)
    bmesh.ops.delete(bm, geom=[g for g in res["geom_interior"] + res["geom_unused"]
                               if isinstance(g, bmesh.types.BMVert)], context="VERTS")
    # The hull comes as triangles: merge the coplanar ones back into flat faces.
    bmesh.ops.dissolve_limit(bm, angle_limit=math.radians(0.5), verts=bm.verts[:], edges=bm.edges[:])


def shell_points() -> list[tuple[float, float, float]]:
    pts = []
    for sx in (-1, 1):
        for z in (ZB, ZT - TF):
            pts += [(sx * HW, YF + CF, z), (sx * (HW - CF), YF, z)]
        pts += [(sx * HW, YF + CF + TF, ZT), (sx * (HW - CF), YF + TF, ZT)]
        for z in (ZB, ZT - TR):
            pts += [(sx * HW, YR - CR, z), (sx * (HW - CR), YR, z)]
        pts += [(sx * HW, YR - CR - TR, ZT), (sx * (HW - CR), YR - TR, ZT)]
    return pts


def faces_where(bm, test) -> list:
    bm.faces.ensure_lookup_table()
    return [f for f in bm.faces if test(f.calc_center_median(), f.normal)]


def cut(bm, test, co, no) -> None:
    """Split the faces that pass `test` along a plane (only those, so cuts stay where needed)."""
    faces = faces_where(bm, test)
    if not faces:
        return
    edges = list({e for f in faces for e in f.edges})
    verts = list({v for f in faces for v in f.verts})
    bmesh.ops.bisect_plane(bm, geom=faces + edges + verts, plane_co=co, plane_no=no, dist=1e-5)


def is_side(c, n):
    return abs(n.x) > 0.99


def is_left(c, n):
    return n.x > 0.99


def is_front(c, n):
    return n.y < -0.3 and n.z < 0.5  # the front face and its corners


def is_rear(c, n):
    return n.y > 0.3 and n.z < 0.5


def is_roof(c, n):
    return n.z > 0.5


def build_shell(mats) -> bpy.types.Object:
    bm = bmesh.new()
    hull(bm, shell_points())
    bmesh.ops.delete(bm, geom=faces_where(bm, lambda c, n: n.z < -0.9), context="FACES_ONLY")
    up, along, across = Vector((0, 0, 1)), Vector((0, 1, 0)), Vector((1, 0, 0))

    # Sides: the window band; the roof caps above it; the doors and the rising panel below it.
    for z in (WIN_LO, WIN_HI):
        cut(bm, is_side, (0, 0, z), up)
    cut(bm, lambda c, n: (is_side(c, n) and c.z > WIN_HI) or is_roof(c, n), (0, CAP_FRONT, 0), along)
    cut(bm, lambda c, n: (is_side(c, n) and c.z > WIN_HI) or is_roof(c, n), (0, CAP_REAR, 0), along)
    for y0, y1 in DOORS:
        for y in (y0, y1):
            cut(bm, lambda c, n: is_left(c, n) and c.z < WIN_LO, (0, y, 0), along)
    (y0, z0), (y1, z1) = SWOOSH
    slope = Vector((0, -(z1 - z0), y1 - y0)).normalized()  # normal of the rising line, in YZ
    cut(bm, lambda c, n: is_side(c, n) and c.z < WIN_LO, (0, y0, z0), slope)
    cut(bm, lambda c, n: is_side(c, n) and c.z < WIN_LO, (0, YF + 1.0, 0), along)  # front corner panel

    # Front: bumper, lamps, windscreen, a band, the destination display.
    for z in FRONT_BANDS:
        cut(bm, is_front, (0, 0, z), up)
    # Rear: bumper, the lower panel with the lamps, the rear window.
    for z in REAR_BANDS:
        cut(bm, is_rear, (0, 0, z), up)

    me = bpy.data.meshes.new("bus")
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("bus", me)
    bpy.context.collection.objects.link(ob)
    for name in SHELL:
        me.materials.append(mats[name])
    idx = {name: i for i, name in enumerate(SHELL)}

    def door(y):
        return any(a <= y <= b for a, b in DOORS)

    def below_swoosh(c):
        (ya, za), (yb, zb) = SWOOSH
        return c.z < za + (c.y - ya) * (zb - za) / (yb - ya)

    for p in me.polygons:
        c, n = p.center, p.normal
        m = "body"
        if is_roof(c, n):
            corner = abs(n.y) > 0.3  # the cut-off roof edges
            m = "accent" if corner or c.y < CAP_FRONT or c.y > CAP_REAR else "body"
        elif is_side(c, n):
            if WIN_LO < c.z < WIN_HI:
                m = "glass"
            elif c.z > WIN_HI:
                m = "accent" if c.y < CAP_FRONT or c.y > CAP_REAR else "body"
            elif n.x > 0 and door(c.y):
                m = "glass"
            elif below_swoosh(c) or c.y < YF + 1.0:
                m = "accent"
        elif is_front(c, n):
            corner = abs(n.x) > 0.3
            if c.z < 0.46:
                m = "trim"
            elif c.z < 0.58:
                m = "accent"
            elif c.z < 0.82:
                m = "lights" if corner else "body"
            elif c.z < 0.98:
                m = "body"
            elif c.z < 2.58:
                m = "glass"
            elif c.z < 2.66:
                m = "accent"
            elif c.z < 2.83:
                m = "glass" if not corner else "accent"
            else:
                m = "accent"
        elif is_rear(c, n):
            corner = abs(n.x) > 0.3
            if c.z < 0.46:
                m = "trim"
            elif c.z < 0.60:
                m = "accent"
            elif c.z < 1.30:
                m = "tail" if corner else "accent"
            elif c.z < 1.98:
                m = "body"
            elif c.z < 2.70:
                m = "glass" if not corner else "body"
            else:
                m = "accent"
        p.material_index = idx[m]
        p.use_smooth = False
    return ob


def build_wheels(mats) -> bpy.types.Object:
    bm = bmesh.new()
    for y in (FRONT_AXLE, REAR_AXLE):
        for sx in (-1, 1):
            res = bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=8,
                                        radius1=WHEEL_R, radius2=WHEEL_R, depth=WHEEL_W)
            verts = res["verts"]
            # Lay it on its side (its axis across the bus), the outer face 1.5 cm proud of the body.
            for v in verts:
                x, y2, z = v.co
                v.co = Vector((z, x, y2)) + Vector((sx * (HW + 0.015 - WHEEL_W / 2), y, WHEEL_R))
            bm.normal_update()
            # The inner cap never shows; the outer cap gets a hub.
            caps = [f for f in {f for v in verts for f in v.link_faces} if abs(f.normal.x) > 0.99]
            inner = [f for f in caps if f.normal.x * sx < 0]
            outer = [f for f in caps if f.normal.x * sx > 0]
            bmesh.ops.delete(bm, geom=inner, context="FACES_ONLY")
            res = bmesh.ops.inset_individual(bm, faces=outer, thickness=0.17, depth=0.012)
            for f in outer:
                f.material_index = 1  # hub
    me = bpy.data.meshes.new("wheels")
    bm.to_mesh(me)
    bm.free()
    for p in me.polygons:
        p.use_smooth = False
    me.materials.append(mats["tyre"])
    me.materials.append(mats["hub"])
    ob = bpy.data.objects.new("wheels", me)
    bpy.context.collection.objects.link(ob)
    return ob


def pod_points(y0, y1, hw, z0, z1, chamfer):
    pts = []
    for sx in (-1, 1):
        for y in (y0, y1):
            pts.append((sx * hw, y, z0))
            pts.append((sx * hw, y + (chamfer if y == y0 else -chamfer), z1 - chamfer))
            pts.append((sx * (hw - chamfer), y + (chamfer if y == y0 else -chamfer), z1))
    return pts


def build_roof(mats) -> bpy.types.Object:
    bm = bmesh.new()
    hull(bm, pod_points(-3.55, 0.35, 0.92, ZT, ZT + 0.28, 0.10))  # the air conditioning
    hull(bm, pod_points(3.35, 4.55, 0.70, ZT, ZT + 0.16, 0.05))  # a smaller unit at the back
    bmesh.ops.delete(bm, geom=faces_where(bm, lambda c, n: n.z < -0.9), context="FACES_ONLY")
    me = bpy.data.meshes.new("roof")
    bm.to_mesh(me)
    bm.free()
    for p in me.polygons:
        p.use_smooth = False
    me.materials.append(mats["roof"])
    ob = bpy.data.objects.new("roof", me)
    bpy.context.collection.objects.link(ob)
    return ob


def join(objs) -> bpy.types.Object:
    for o in bpy.context.selected_objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.object.join()
    ob = bpy.context.view_layer.objects.active
    ob.name = "bus"
    return ob


def triangles(ob) -> int:
    return sum(len(p.vertices) - 2 for p in ob.data.polygons)


# -- export -----------------------------------------------------------------------------------

def export(ob, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for o in bpy.context.selected_objects:
        o.select_set(False)
    ob.select_set(True)
    bpy.ops.export_scene.gltf(
        filepath=str(path), export_format="GLB", use_selection=True, export_apply=True,
        export_yup=True, export_normals=True, export_texcoords=False, export_materials="EXPORT",
        export_extras=False, export_animations=False, export_cameras=False, export_lights=False,
    )


# -- previews ---------------------------------------------------------------------------------

def look_at(cam, target: Vector) -> None:
    cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()


def setup_render(res=(900, 560)) -> bpy.types.Object:
    scene = bpy.context.scene
    try:
        scene.render.engine = "BLENDER_WORKBENCH"
    except TypeError:
        scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x, scene.render.resolution_y = res
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "JPEG"
    scene.render.image_settings.quality = 88
    sh = scene.display.shading
    sh.light = "STUDIO"
    sh.color_type = "MATERIAL"
    sh.show_object_outline = True
    sh.object_outline_color = (0.05, 0.07, 0.1)
    sh.show_cavity = False
    sh.show_shadows = True
    sh.shadow_intensity = 0.35
    scene.display.render_aa = "8"
    if scene.world is None:
        scene.world = bpy.data.worlds.new("world")
    scene.world.color = (srgb_to_linear(0x15 / 255), srgb_to_linear(0x23 / 255), srgb_to_linear(0x3a / 255))
    cam_data = bpy.data.cameras.new("cam")
    cam = bpy.data.objects.new("cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    # A ground plane, so the bus stands on something.
    bpy.ops.mesh.primitive_plane_add(size=60, location=(0, 0, 0))
    ground = bpy.context.active_object
    gm = bpy.data.materials.new("ground")
    gm.diffuse_color = rgba("#0e1828")
    ground.data.materials.append(gm)
    return cam


def shoot(cam, path: Path, loc, target=(0, 0, 1.4), ortho: float | None = None, lens=50) -> None:
    cam.location = Vector(loc)
    look_at(cam, Vector(target))
    if ortho:
        cam.data.type = "ORTHO"
        cam.data.ortho_scale = ortho
    else:
        cam.data.type = "PERSP"
        cam.data.lens = lens
    bpy.context.scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)


def previews(ob, mats, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    cam = setup_render()
    recolour(mats, CPT)
    shoot(cam, out / "blender-three-quarter.jpg", (14.5, -16.5, 7.0), lens=55)  # front, left (doors)
    shoot(cam, out / "blender-side.jpg", (30, 0, 1.6), target=(0, 0, 1.6), ortho=13.5)
    shoot(cam, out / "blender-rear.jpg", (-12.0, 17.0, 6.0), lens=55)
    shoot(cam, out / "blender-top.jpg", (0, 0, 40), target=(0, 0, 0), ortho=13.5)
    cam.rotation_euler = (0, 0, math.radians(90))  # the top view lies along the picture
    bpy.ops.render.render(write_still=True)
    # As the map tints it: three buses in line colours, seen from the height of a pitched map.
    scene = bpy.context.scene
    copies = []
    for i, colour in enumerate(LINES):
        c = ob.copy()
        c.data = ob.data.copy()
        c.location = (-(i - 1) * 4.2, (i - 1) * 2.5, 0)
        scene.collection.objects.link(c)
        for slot in c.material_slots:
            if slot.material and slot.material.name.split(".")[0] in ("body", "accent"):
                m = slot.material.copy()
                base = rgba(colour)
                k = 1.0 if m.name.startswith("body") else 0.6  # accent: the darker shade
                col = (base[0] * k ** 2.2, base[1] * k ** 2.2, base[2] * k ** 2.2, 1.0)
                m.diffuse_color = col
                slot.link = "OBJECT"
                slot.material = m
        copies.append(c)
    ob.hide_render = True
    shoot(cam, out / "blender-lines.jpg", (19, -21, 14), target=(0, 0, 1.0), lens=50)
    ob.hide_render = False
    for c in copies:
        bpy.data.objects.remove(c)
    recolour(mats, {k: MATERIALS[k] for k in ("body", "accent")})


def main() -> None:
    a = args()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    mats = make_materials()
    parts = [build_shell(mats), build_wheels(mats), build_roof(mats)]
    counts = {o.name: triangles(o) for o in parts}
    ob = join(parts)
    export(ob, a["out"])
    size = a["out"].stat().st_size
    print(f"BUS: {triangles(ob)} triangles {counts}, {len(ob.data.materials)} materials, "
          f"{size / 1024:.1f} KB -> {a['out']}")
    if a["render"]:
        previews(ob, mats, a["previews"])
        print(f"BUS: previews in {a['previews']}")


if __name__ == "__main__":
    main()
