"""Petabyte Create 3D: one prompt = one commit. Runs INSIDE the Blender container:
    blender -b [base.blend] --python model3d_driver.py -- --prompt ... --out /out --llm http://llm:8080

A local LLM (the job's second container: llama.cpp, OpenAI-compatible API) writes Python that calls
Blender-MCP-style shape tools (box/cylinder/cone/sphere/torus/remove, real sizes in meters). Each code
block that runs is part of the commit's diff. Outputs in --out:
  scene.blend  scene.glb  preview.png  diff.py  commit.json  live/latest.jpg (refreshed <= every 10 s)
The container has no internet: its only network peer is the LLM container.
"""
import argparse, json, math, os, re, sys, time, traceback, urllib.request

import bpy
import mathutils

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ap = argparse.ArgumentParser()
ap.add_argument("--prompt", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--llm", default="http://llm:8080")
ap.add_argument("--max-steps", type=int, default=4)
ap.add_argument("--fresh", action="store_true", help="start from an empty scene")
args = ap.parse_args(argv)
os.makedirs(args.out, exist_ok=True)

if args.fresh:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # The empty factory scene has no World, and model code reads scene.world anyway (commit #26 died
    # on scene.world.light_settings). Give it one, as Blender's normal startup scene has.
    bpy.context.scene.world = bpy.data.worlds.new("World")


def wait_for_llm(limit_s=600):
    """The LLM container starts alongside this one and loads a ~5 GB model first."""
    end = time.time() + limit_s
    while time.time() < end:
        try:
            with urllib.request.urlopen(args.llm.rstrip("/") + "/health", timeout=5) as r:
                if r.status == 200:
                    return
        except Exception:  # noqa: BLE001 - not up yet
            pass
        time.sleep(2)
    raise SystemExit("PBERROR the local LLM did not become ready")


wait_for_llm()

SYSTEM = """You build 3D models in Blender by writing Python. Reply with ONE ```python block per turn;
it runs inside Blender and I send back the result and the scene. Use ONLY these helpers
(sizes in meters, location = CENTER of the object, Z is up, the floor is Z=0, color = (r, g, b) 0..1):
  box(name, size=(x, y, z), location=(x, y, z), color)
  cylinder(name, radius, height, location, color, rotation=(rx, ry, rz) degrees)
  cone(name, radius_bottom, radius_top, height, location, color)
  sphere(name, radius, location, color)
  torus(name, major_radius, minor_radius, location, color, rotation)
  remove(name)
An object of height h standing on something whose top is at z sits at location z + h/2.
Example, a table 1.2 x 0.8 m, 0.75 m tall, with a mug on it:
```python
wood = (0.55, 0.35, 0.18)
box("TableTop", (1.2, 0.8, 0.05), (0, 0, 0.725), wood)
for i, (x, y) in enumerate([(0.55, 0.35), (-0.55, 0.35), (0.55, -0.35), (-0.55, -0.35)]):
    box(f"Leg{i}", (0.06, 0.06, 0.70), (x, y, 0.35), wood)
cylinder("Mug", 0.04, 0.10, (0.2, 0.1, 0.75 + 0.05), (0.8, 0.1, 0.1))
torus("MugHandle", 0.03, 0.008, (0.25, 0.1, 0.80), (0.8, 0.1, 0.1), rotation=(90, 0, 0))
```
Use realistic real-world sizes and enough parts that the object is recognisable. Keep existing objects
unless asked to change them. The camera, lights, world and render settings are handled for you: never
touch them. When the request is fully done, reply with just: DONE"""



# ---- Shape helpers the model calls (Blender-MCP style: semantic, real sizes in meters) ----
def _mat(color, name=None):
    color = tuple(color) + ((1.0,) if len(color) == 3 else ())
    key = name or "pb_" + "_".join(f"{c:.2f}" for c in color[:3])
    m = bpy.data.materials.get(key) or bpy.data.materials.new(key)
    m.use_nodes = True
    m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = color
    return m


def _finish(obj, name, color, rotation):
    obj.name = name
    obj.rotation_euler = [math.radians(a) for a in rotation]
    if color is not None:
        obj.data.materials.clear()
        obj.data.materials.append(_mat(color))
    return obj


def box(name, size, location, color=(0.8, 0.8, 0.8), rotation=(0, 0, 0)):
    """size=(x, y, z) full dimensions in meters; location = center."""
    bpy.ops.mesh.primitive_cube_add(size=1, location=location)
    o = bpy.context.object
    o.scale = size
    return _finish(o, name, color, rotation)


def cylinder(name, radius, height, location, color=(0.8, 0.8, 0.8), rotation=(0, 0, 0), vertices=32):
    """Upright along Z; location = center."""
    bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=height, location=location, vertices=vertices)
    return _finish(bpy.context.object, name, color, rotation)


def cone(name, radius_bottom, radius_top, height, location, color=(0.8, 0.8, 0.8), rotation=(0, 0, 0), vertices=32):
    bpy.ops.mesh.primitive_cone_add(radius1=radius_bottom, radius2=radius_top, depth=height, location=location, vertices=vertices)
    return _finish(bpy.context.object, name, color, rotation)


def sphere(name, radius, location, color=(0.8, 0.8, 0.8), segments=32):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=radius, location=location, segments=segments, ring_count=segments // 2)
    return _finish(bpy.context.object, name, color, (0, 0, 0))


def torus(name, major_radius, minor_radius, location, color=(0.8, 0.8, 0.8), rotation=(0, 0, 0)):
    bpy.ops.mesh.primitive_torus_add(major_radius=major_radius, minor_radius=minor_radius, location=location)
    return _finish(bpy.context.object, name, color, rotation)


def remove(name):
    o = bpy.data.objects.get(name)
    if o:
        bpy.data.objects.remove(o, do_unlink=True)


NS = {}   # one namespace per commit: a fix-up block can reuse variables from earlier blocks


def scene_objects():
    out = []
    for o in bpy.context.scene.objects:
        mat = o.active_material.name if getattr(o, "active_material", None) else None
        out.append({"name": o.name, "type": o.type, "location": [round(v, 3) for v in o.location],
                    "rotation": [round(v, 3) for v in o.rotation_euler], "scale": [round(v, 3) for v in o.scale],
                    "dimensions": [round(v, 3) for v in o.dimensions], "material": mat})
    return out


SCENE_MAX_CHARS = 6000   # the scene as the LLM sees it: keeps each request inside its 8k-token context


def scene_info(limit=SCENE_MAX_CHARS):
    """The scene for the LLM, capped: a big scene drops the less useful fields, then the last objects."""
    objs = scene_objects()
    s = json.dumps(objs)
    if len(s) <= limit:
        return s
    slim = [{"name": o["name"], "location": o["location"], "dimensions": o["dimensions"]} for o in objs]
    while slim and len(json.dumps(slim)) > limit - 40:
        slim.pop()
    return json.dumps(slim) + f" (+{len(objs) - len(slim)} more objects)"


def run_code(code):
    import io, contextlib
    buf = io.StringIO()
    import bmesh
    ns = NS
    if not ns:
        ns.update(bpy=bpy, bmesh=bmesh, mathutils=mathutils, math=math, Vector=mathutils.Vector, box=box,
                  cylinder=cylinder, cone=cone, sphere=sphere, torus=torus, remove=remove)
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(code, "<llm>", "exec"), ns)
        return True, (buf.getvalue()[-2000:] or "ok")
    except Exception:
        return False, (buf.getvalue()[-500:] + traceback.format_exc(limit=3))[-2000:]


def chat(messages):
    body = json.dumps({"model": "local", "messages": messages,
                       "temperature": 0.2, "max_tokens": 2048}).encode()
    req = urllib.request.Request(args.llm.rstrip("/") + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["choices"][0]["message"]


def frame_camera():
    """Camera + light that frame every mesh, so each commit gets a comparable preview."""
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not meshes:
        return
    pts = [o.matrix_world @ mathutils.Vector(c) for o in meshes for c in o.bound_box]
    lo = mathutils.Vector([min(p[i] for p in pts) for i in range(3)])
    hi = mathutils.Vector([max(p[i] for p in pts) for i in range(3)])
    center, size = (lo + hi) / 2, max((hi - lo).length, 0.5)
    for n in ("pb_cam", "pb_sun"):
        if n in bpy.data.objects:
            bpy.data.objects.remove(bpy.data.objects[n], do_unlink=True)
    cam = bpy.data.objects.new("pb_cam", bpy.data.cameras.new("pb_cam"))
    bpy.context.scene.collection.objects.link(cam)
    cam.location = center + mathutils.Vector((1.0, -1.3, 0.8)) * size * 1.1
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    sun = bpy.data.objects.new("pb_sun", bpy.data.lights.new("pb_sun", "SUN"))
    sun.data.energy = 3.0
    sun.rotation_euler = (math.radians(50), 0, math.radians(30))
    bpy.context.scene.collection.objects.link(sun)
    bpy.context.scene.camera = cam


SNAP_EVERY = 10   # seconds between live views at most
LIVE = os.path.join(args.out, "live")
os.makedirs(LIVE, exist_ok=True)
_snap = {"at": 0.0, "dirty": True, "n": 0}


def snapshot(force=False):
    """Cheap live view (~1 s): 320x240, 4 samples, written as live/latest.jpg + a numbered copy.
    Only when the scene changed and at most every SNAP_EVERY seconds; the seller agent uploads
    latest.jpg on the same cadence, so the buyer watches progress without a remote desktop."""
    if not (_snap["dirty"] or force) or (not force and time.time() - _snap["at"] < SNAP_EVERY):
        return
    if not any(o.type == "MESH" for o in bpy.context.scene.objects):
        return
    frame_camera()
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = 4
    sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = 320, 240, 100
    sc.render.image_settings.file_format = "JPEG"
    _snap["n"] += 1
    sc.render.filepath = os.path.join(LIVE, f"snap_{_snap['n']:03d}.jpg")
    bpy.ops.render.render(write_still=True)
    try:
        import shutil
        shutil.copyfile(sc.render.filepath, os.path.join(LIVE, "latest.jpg"))
    except OSError:
        pass
    sc.render.image_settings.file_format = "PNG"
    _snap.update(at=time.time(), dirty=False)


def chat_live(messages):
    """The LLM call runs in a thread; the main thread (the only one allowed to touch bpy) keeps
    taking snapshots on the 10 s cadence while it waits."""
    import threading
    box = {}

    def work():
        try:
            box["r"] = chat(messages)
        except Exception as e:  # noqa: BLE001
            box["e"] = e

    th = threading.Thread(target=work, daemon=True)
    th.start()
    while th.is_alive():
        th.join(1.0)
        snapshot()
    if "e" in box:
        raise box["e"]
    return box["r"]


messages = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Current scene: {scene_info()}\n\nRequest: {args.prompt}"}]
diff, log, summary, error, step, t0 = [], [], "", "", 0, time.time()
FENCE = re.compile(r"```(?:python|py)?[ \t]*\n(.*?)```", re.S)
try:
    for step in range(args.max_steps):
        # The system prompt, the request and only the LATEST exchange: the whole history (every code
        # block plus a scene dump per step) overflowed the 8k context on a long prompt (commit #26).
        text = chat_live(messages[:2] + messages[2:][-2:]).get("content") or ""
        messages.append({"role": "assistant", "content": text})
        blocks = FENCE.findall(text)
        if not blocks:
            summary = text.strip()[:300]
            log.append({"step": step, "done": True, "text": summary})
            print("PBSTEP", step, "done:", summary[:200].replace("\n", " | "), flush=True)
            break
        results = []
        for code in blocks:                      # each block = one execute_blender_code call
            ok, res = run_code(code)
            if ok:
                diff.append(code)
                _snap["dirty"] = True
                snapshot()
            log.append({"step": step, "tool": "execute_blender_code", "ok": ok, "result": res[:300]})
            print("PBSTEP", step, "code", "ok" if ok else "ERR", res[:300].replace("\n", " | "), flush=True)
            results.append(("OK: " if ok else "ERROR: ") + res[:800])
        messages.append({"role": "user", "content": "\n".join(results) + f"\nScene now: {scene_info()}"
                         "\nFix any error with another code block, add anything still missing, or reply DONE."})
except Exception as e:  # noqa: BLE001 — the LLM call or a live view failed: keep what was built
    error = f"{type(e).__name__}: {e}"[:300]
    log.append({"step": step, "error": error})
    print("PBERROR", error.replace("\n", " | "), flush=True)

# Commit artifacts
frame_camera()
has_mesh = any(o.type == "MESH" for o in bpy.context.scene.objects)
sc = bpy.context.scene
sc.render.engine = "CYCLES"
sc.cycles.device = "CPU"
sc.cycles.samples = 24
sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = 640, 480, 100
if sc.world is None:
    sc.world = bpy.data.worlds.new("pb_world")
sc.world.use_nodes = True
bg = sc.world.node_tree.nodes.get("Background")
if bg:
    bg.inputs[0].default_value = (0.05, 0.06, 0.08, 1)
sc.render.filepath = os.path.join(args.out, "preview.png")
if has_mesh:
    bpy.ops.render.render(write_still=True)
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(args.out, "scene.blend"))
try:
    bpy.ops.export_scene.gltf(filepath=os.path.join(args.out, "scene.glb"), export_format="GLB")
except Exception as e:  # noqa: BLE001
    log.append({"export_error": str(e)[:300]})
with open(os.path.join(args.out, "diff.py"), "w") as f:
    f.write(f"# prompt: {args.prompt}\n\n" + "\n\n# ---\n".join(diff) + "\n")
with open(os.path.join(args.out, "commit.json"), "w") as f:
    json.dump({"prompt": args.prompt, "ok": bool(diff), "error": error, "summary": summary, "steps": log,
               "seconds": round(time.time() - t0, 1), "objects": scene_objects()}, f, indent=1)
print("PBCOMMIT", json.dumps({"ok": bool(diff), "error": error, "summary": summary,
                              "seconds": round(time.time() - t0, 1)}))
