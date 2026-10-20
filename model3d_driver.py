"""Petabyte Create 3D: one prompt = one commit. Runs INSIDE the Blender container:
    blender -b [base.blend] --python model3d_driver.py -- --prompt ... --out /out --llm http://llm:8080

A local LLM (the job's second container: llama.cpp, OpenAI-compatible API) writes Python that calls
Blender-MCP-style shape tools (box/cylinder/cone/sphere/torus/remove, real sizes in meters). Each code
block that runs is part of the commit's diff. Outputs in --out:
  scene.blend  scene.glb  preview.png  diff.py  commit.json  live/latest.jpg (refreshed <= every 10 s)
The container has no internet: its only network peer is the LLM container.
"""
import argparse, json, math, os, re, sys, time, traceback, urllib.request
import urllib.error

import bpy
import mathutils
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model3d_scene import (BUILD_SYSTEM, request_plan, evidence, assembly_summary,
                           runtime_api_description, validate_render_settings, completion_blocks,
                           sampling_options, execution_hint, validate_observation_names,
                           validate_material_assignments, validate_reserved_definitions)

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ap = argparse.ArgumentParser()
ap.add_argument("--prompt", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--llm", default="http://llm:8080")
ap.add_argument("--max-steps", type=int, default=4)
ap.add_argument("--staged", action="store_true", help="plan and verify every requested scene component")
ap.add_argument("--max-tokens", type=int, default=3072)
ap.add_argument("--context", type=int, default=16384)
ap.add_argument("--render-size", choices=("720p", "1080p"), default="720p")
ap.add_argument("--model", default="local")
ap.add_argument("--gpu-render", action="store_true")
ap.add_argument("--fresh", action="store_true", help="start from an empty scene")
args = ap.parse_args(argv)
if not args.prompt.strip():
    raise SystemExit("PBERROR the 3D task has no scene request; execution was refused")
os.makedirs(args.out, exist_ok=True)

if args.fresh:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    # The empty factory scene has no World, and model code reads scene.world anyway (commit #26 died
    # on scene.world.light_settings). Give it one, as Blender's normal startup scene has.
    bpy.context.scene.world = bpy.data.worlds.new("World")


def _llm_peer_opener():
    """Talk directly to the job's coder or agent relay, without environment proxy routing."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


_llm_http = _llm_peer_opener()


def _llm_health_failure(error):
    """Classify a probe failure without copying URLs, credentials, bodies or exception messages."""
    if isinstance(error, urllib.error.HTTPError) and isinstance(error.code, int):
        return f"HTTP{error.code}"[:120]
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    label = type(error).__name__
    if reason is not error:
        label += "/" + type(reason).__name__
    if isinstance(reason, OSError) and isinstance(reason.errno, int):
        label += f" errno={reason.errno}"
    # The original exception retains standard-library frames even when urllib wraps it.
    # Emit only a fixed phase label, never frame locals, source text, addresses or messages.
    phase, tb = None, reason.__traceback__ if isinstance(reason, BaseException) else None
    while tb is not None:
        module, function = tb.tb_frame.f_globals.get("__name__"), tb.tb_frame.f_code.co_name
        if module == "socket" and function == "getaddrinfo":
            phase = "dns"
            break
        if module == "socket" and function == "create_connection":
            phase = "connect"
        elif module == "http.client" and function == "_read_status":
            phase = "response"
        tb = tb.tb_next
    if phase:
        label += f" phase={phase}"
    return label[:120]


def wait_for_llm(limit_s=600):
    """The LLM container starts alongside this one and loads a ~5 GB model first."""
    end = time.time() + limit_s
    attempts, last = 0, "no response"
    while time.time() < end:
        attempts += 1
        try:
            with _llm_http.open(args.llm.rstrip("/") + "/health", timeout=5) as r:
                if r.status == 200:
                    return
                last = f"HTTP{r.status}" if isinstance(r.status, int) else "non-HTTP response"
        except Exception as error:  # noqa: BLE001 - not up yet; retain only safe classification
            last = _llm_health_failure(error)
        time.sleep(2)
    raise SystemExit(f"PBERROR the local LLM did not become ready (health attempts={attempts}; last={last})")


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
math and random are already imported. Calling a helper again with an existing name replaces that part.
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
touch them in this compatibility mode. Preserve every physical subject and landscape feature in the request.
When the request is fully done, reply with just: DONE (never put DONE inside a code block)"""



# ---- Shape helpers the model calls (Blender-MCP style: semantic, real sizes in meters) ----
def _mat(color, name=None):
    color = tuple(color) + ((1.0,) if len(color) == 3 else ())
    key = name or "pb_" + "_".join(f"{c:.2f}" for c in color[:3])
    m = bpy.data.materials.get(key) or bpy.data.materials.new(key)
    m.use_nodes = True
    m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = color
    return m


def _finish(obj, name, color, rotation):
    # Same name = the same part: replace it, so re-sending a whole corrected block (what the model
    # is told to do after an error) rebuilds the scene instead of duplicating every part as .001.
    old = bpy.data.objects.get(name)
    if old is not None and old is not obj:
        bpy.data.objects.remove(old, do_unlink=True)
    obj.name = name
    obj.rotation_euler = [math.radians(a) for a in rotation]
    if color is not None:
        obj.data.materials.clear()
        obj.data.materials.append(_mat(color))
    return obj


# Commit #43: the model passed a tuple where cylinder() takes one number, Blender answered "Converting
# py args to operator properties" from inside the driver, and the model repeated the same call 4 times.
# Every argument is checked here and a wrong one names the argument and the right call.
SIG = {"box": "box(name, size=(x, y, z), location=(x, y, z), color=(r, g, b), rotation=(rx, ry, rz))",
       "cylinder": "cylinder(name, radius, height, location=(x, y, z), color=(r, g, b), rotation=(rx, ry, rz))",
       "cone": "cone(name, radius_bottom, radius_top, height, location=(x, y, z), color=(r, g, b), rotation=(rx, ry, rz))",
       "sphere": "sphere(name, radius, location=(x, y, z), color=(r, g, b))",
       "ellipsoid": "ellipsoid(name, size=(x, y, z), location=(x, y, z), color=(r, g, b), rotation=(rx, ry, rz))",
       "torus": "torus(name, major_radius, minor_radius, location=(x, y, z), color=(r, g, b), rotation=(rx, ry, rz))"}


def _num(v, fn, arg):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TypeError(f"{fn}(): {arg} must be ONE number in meters, got {v!r}. Call it as {SIG[fn]}")
    return float(v)


def _vec(v, fn, arg):
    try:
        t = tuple(float(x) for x in v)
    except (TypeError, ValueError):
        t = ()
    if len(t) != 3:
        raise TypeError(f"{fn}(): {arg} must be 3 numbers like (x, y, z), got {v!r}. Call it as {SIG[fn]}")
    return t


def _color(v, fn):
    if v is None:
        return None
    try:
        t = tuple(float(x) for x in v)
    except (TypeError, ValueError):
        t = ()
    if len(t) not in (3, 4):
        raise TypeError(f"{fn}(): color must be (r, g, b) numbers 0..1, got {v!r}. Call it as {SIG[fn]}")
    return tuple(c / 255 for c in t) if max(t) > 1 else t      # 0..255 colours are common: accept them


def box(name, size, location, color=(0.8, 0.8, 0.8), rotation=(0, 0, 0)):
    """size=(x, y, z) full dimensions in meters (one number = a cube); location = center."""
    size = (_num(size, "box", "size"),) * 3 if isinstance(size, (int, float)) else _vec(size, "box", "size")
    location, rotation, color = _vec(location, "box", "location"), _vec(rotation, "box", "rotation"), _color(color, "box")
    bpy.ops.mesh.primitive_cube_add(size=1, location=location)
    o = bpy.context.object
    o.scale = size
    return _finish(o, name, color, rotation)


def cylinder(name, radius, height, location, color=(0.8, 0.8, 0.8), rotation=(0, 0, 0), vertices=32):
    """Upright along Z; location = center."""
    radius, height = _num(radius, "cylinder", "radius"), _num(height, "cylinder", "height")
    location, rotation = _vec(location, "cylinder", "location"), _vec(rotation, "cylinder", "rotation")
    color = _color(color, "cylinder")
    bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=height, location=location, vertices=int(vertices))
    return _finish(bpy.context.object, name, color, rotation)


def cone(name, radius_bottom, radius_top, height, location, color=(0.8, 0.8, 0.8), rotation=(0, 0, 0), vertices=32):
    r1, r2 = _num(radius_bottom, "cone", "radius_bottom"), _num(radius_top, "cone", "radius_top")
    height, location = _num(height, "cone", "height"), _vec(location, "cone", "location")
    rotation = _vec(rotation, "cone", "rotation")
    color = _color(color, "cone")
    bpy.ops.mesh.primitive_cone_add(radius1=r1, radius2=r2, depth=height, location=location, vertices=int(vertices))
    return _finish(bpy.context.object, name, color, rotation)


def sphere(name, radius, location, color=(0.8, 0.8, 0.8), segments=32):
    radius, location = _num(radius, "sphere", "radius"), _vec(location, "sphere", "location")
    segments = int(segments)
    color = _color(color, "sphere")
    bpy.ops.mesh.primitive_uv_sphere_add(radius=radius, location=location, segments=segments, ring_count=segments // 2)
    return _finish(bpy.context.object, name, color, (0, 0, 0))


def torus(name, major_radius, minor_radius, location, color=(0.8, 0.8, 0.8), rotation=(0, 0, 0)):
    major, minor = _num(major_radius, "torus", "major_radius"), _num(minor_radius, "torus", "minor_radius")
    location, rotation = _vec(location, "torus", "location"), _vec(rotation, "torus", "rotation")
    color = _color(color, "torus")
    bpy.ops.mesh.primitive_torus_add(major_radius=major, minor_radius=minor, location=location)
    return _finish(bpy.context.object, name, color, rotation)


def remove(name):
    o = bpy.data.objects.get(name)
    if o:
        bpy.data.objects.remove(o, do_unlink=True)


def material(name, color=None, metallic=0, roughness=0.5, emission=None, strength=1):
    mat = bpy.data.materials.get(name)
    if color is None:
        if mat is None:
            raise ValueError(f"material({name!r}): no existing material; first create it with material(name, (r, g, b))")
        if metallic != 0 or roughness != 0.5 or emission is not None or strength != 1:
            raise ValueError("material(): supply color=(r, g, b) when changing material properties")
        return mat
    mat = mat or bpy.data.materials.new(name)
    mat.use_nodes = True
    shader = mat.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = tuple(color[:3]) + (1,)
    shader.inputs["Metallic"].default_value = max(0, min(1, float(metallic)))
    shader.inputs["Roughness"].default_value = max(0.02, min(1, float(roughness)))
    if emission is not None:
        shader.inputs["Emission Color"].default_value = tuple(emission[:3]) + (1,)
        shader.inputs["Emission Strength"].default_value = float(strength)
    return mat


def assign(obj, mat):
    if isinstance(mat, str):
        mat = material(mat)
    if not isinstance(mat, bpy.types.Material):
        raise TypeError("assign(): pass a material object or the name of an existing material")
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    return obj


def beam(name, start, end, radius, color=(0.7, 0.7, 0.7)):
    start, end = mathutils.Vector(start), mathutils.Vector(end)
    direction = end - start
    obj = cylinder(name, radius, direction.length, (start + end) / 2, color)
    obj.rotation_euler = direction.to_track_quat("Z", "Y").to_euler()
    return obj


def ellipsoid(name, size, location, color=(0.8, 0.8, 0.8), rotation=(0, 0, 0)):
    # Validate the wrapper's inputs before sphere() allocates anything. A failed color or
    # transform must not leave an unnamed mesh for the next generated repair to inherit.
    size, location = _vec(size, "ellipsoid", "size"), _vec(location, "ellipsoid", "location")
    rotation, color = _vec(rotation, "ellipsoid", "rotation"), _color(color, "ellipsoid")
    obj = sphere(name, 1, location, color)
    obj.scale = [x / 2 for x in size]
    obj.rotation_euler = [math.radians(x) for x in rotation]
    return obj


def terrain(name, size=40, resolution=80, height=2, seed=1, color=(.4, .15, .07)):
    import random
    resolution = max(8, min(160, int(resolution)))
    rng = random.Random(seed)
    phase = rng.random() * 10
    vertices, faces = [], []
    for y in range(resolution + 1):
        for x in range(resolution + 1):
            px, py = (x / resolution - .5) * size, (y / resolution - .5) * size
            z = height * (.3 * math.sin(px * .22 + phase) * math.cos(py * .19)
                          + .12 * math.sin(px * .8 + py * .4) + .04 * rng.random())
            vertices.append((px, py, z))
    for y in range(resolution):
        for x in range(resolution):
            a = y * (resolution + 1) + x
            faces.append((a, a + 1, a + resolution + 2, a + resolution + 1))
    old = bpy.data.objects.get(name)
    if old is not None:
        bpy.data.objects.remove(old, do_unlink=True)
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    assign(obj, _mat(color))
    obj["pb_background"] = True
    return obj


def camera(location, target, lens=24):
    for fallback in ("pb_sun", "pb_cam"):
        old = bpy.data.objects.get(fallback)
        if old is not None:
            bpy.data.objects.remove(old, do_unlink=True)
    obj = bpy.data.objects.get("pb_artist_camera")
    if obj is None:
        obj = bpy.data.objects.new("pb_artist_camera", bpy.data.cameras.new("pb_artist_camera"))
        bpy.context.scene.collection.objects.link(obj)
    obj.location = location
    obj.rotation_euler = (mathutils.Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()
    obj.data.lens = max(12, min(120, float(lens)))
    obj.data.clip_end = 5000
    obj["pb_target"] = list(target)
    bpy.context.scene.camera = obj
    return obj


def light(name, location, color=(1, 1, 1), energy=1000, size=5):
    obj = bpy.data.objects.get(name)
    if obj is None:
        obj = bpy.data.objects.new(name, bpy.data.lights.new(name, "AREA"))
        bpy.context.scene.collection.objects.link(obj)
    obj.location, obj.data.color = location, color[:3]
    obj.data.energy, obj.data.shape, obj.data.size = float(energy), "DISK", float(size)
    obj.rotation_euler = (-mathutils.Vector(location)).to_track_quat("-Z", "Y").to_euler()
    return obj


def world(color=(0.05, 0.06, 0.08), strength=.3):
    if bpy.context.scene.world is None:
        bpy.context.scene.world = bpy.data.worlds.new("pb_world")
    bg_world = bpy.context.scene.world
    bg_world.use_nodes = True
    background = bg_world.node_tree.nodes.get("Background")
    background.inputs[0].default_value = tuple(color[:3]) + (1,)
    background.inputs[1].default_value = float(strength)


RUNTIME_API = runtime_api_description(bpy)
NS = {}   # one namespace per commit: a fix-up block can reuse variables from earlier blocks


def scene_objects():
    out = []
    for o in bpy.context.scene.objects:
        mat = o.active_material.name if getattr(o, "active_material", None) else None
        matrix = getattr(o, "matrix_world", None)
        location = matrix.translation if matrix is not None else o.location
        out.append({"name": o.name, "type": o.type, "location": [round(v, 3) for v in location],
                    "rotation": [round(v, 3) for v in o.rotation_euler], "scale": [round(v, 3) for v in o.scale],
                    "dimensions": [round(v, 3) for v in o.dimensions], "material": mat,
                    "requirement": getattr(o, "get", lambda key, default: default)("pb_requirement", "")})
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
    import bmesh, random
    ns = NS
    # Keep model variables, but never inherit a poisoned helper/import binding from an earlier
    # block (e.g. terrain = terrain(...)). Commit52 repeated 'Object is not callable' on repair.
    bindings = dict(bpy=bpy, bmesh=bmesh, mathutils=mathutils, math=math, random=random, Vector=mathutils.Vector,
                    box=box, cylinder=cylinder, cone=cone, sphere=sphere, torus=torus, remove=remove,
                    beam=beam, ellipsoid=ellipsoid, terrain=terrain, material=material, assign=assign,
                    camera=camera, light=light, world=world)
    ns.update(bindings)
    try:
        validate_reserved_definitions(code, bindings)
        validate_observation_names(code)
        validate_material_assignments(code)
        validate_render_settings(code, [name for name, value in ns.items()
                                        if value is bpy.context.scene])
        with contextlib.redirect_stdout(buf):
            exec(compile(code, "<llm>", "exec"), ns)
        return True, (buf.getvalue()[-2000:] or "ok")
    except Exception as e:
        # Only the model's own line + the error, on one line: a traceback through driver internals
        # (and Blender's multi-line operator errors) told it nothing it could act on (commit #43).
        frames = [f for f in traceback.extract_tb(e.__traceback__) if f.filename == "<llm>"]
        line = frames[-1].lineno if frames else getattr(e, "lineno", None)
        msg = " ".join(f"{type(e).__name__}: {e}".split())
        source = code.splitlines()
        failing = (f" Code: {source[line - 1].strip()[:200]}" if line and 0 < line <= len(source) else "")
        rebound = [name for name, value in bindings.items() if ns.get(name) is not value]
        hint = (" Reserved helper/import names were overwritten: " + ", ".join(rebound)
                + ". Rename object/material variables with _obj or _mat; keep helper names callable."
                if rebound else "")
        api_hint = execution_hint(msg, source[line - 1] if line and 0 < line <= len(source) else "")
        return False, (buf.getvalue()[-500:] + (f"line {line} of your code: " if line else "")
                       + msg + failing + hint + api_hint)[-2000:]
    finally:
        ns.update(bindings)


def fits_context(messages):
    """Conservative byte-token bound for the pinned Qwen byte-fallback tokenizers.

    Reserve chat-template framing and the requested completion as well as every message's
    UTF-8 bytes. Optional repair history is kept whole or omitted, never cut inside code.
    """
    prompt_bound = 256 + sum(64 + len(message["content"].encode("utf-8")) for message in messages)
    return prompt_bound + max(1024, min(6144, args.max_tokens)) <= args.context


def scene_messages(prefix, suffix, failure=None):
    """Keep every mandatory instruction; budget runtime guidance and object detail as optional.

    A runtime socket catalogue must not make a request that fit the base instruction fail.
    Prefer the catalogue with less object detail, then omit it whole if the mandatory request
    and completion fit only without it. chat_live still validates the exact final messages.
    """
    for runtime in (RUNTIME_API, ""):
        for limit in (SCENE_MAX_CHARS, 3000, 1000, 0):
            messages = [{"role": "system", "content": BUILD_SYSTEM + ("\n" + runtime if runtime else "")},
                        {"role": "user", "content": prefix + "\nCurrent scene: " + scene_info(limit) + suffix}]
            if failure is not None:
                messages.append(failure)
            if fits_context(messages):
                return messages
    return messages  # chat_live reports the mandatory instruction's explicit budget failure.


_observed_local_peers = []


def chat(messages):
    body = json.dumps({"model": args.model, "messages": messages,
                       **sampling_options(args.model),
                       "max_tokens": max(1024, min(6144, args.max_tokens))}).encode()
    req = urllib.request.Request(args.llm.rstrip("/") + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + os.environ["PB_MODEL3D_LLM_KEY"]}
                                             if os.getenv("PB_MODEL3D_LLM_KEY") else {})})
    with _llm_http.open(req, timeout=600) as r:
        reply = json.load(r)
        choice = reply["choices"][0]
        peer = reply.get("petabyte_local_peer")
        if isinstance(peer, dict) and peer not in _observed_local_peers:
            _observed_local_peers.append(peer)
        return {**choice["message"], "finish_reason": choice.get("finish_reason")}


def frame_camera():
    """Camera + light that frame every mesh, so each commit gets a comparable preview."""
    if getattr(bpy.context.scene.camera, "name", None) == "pb_artist_camera":
        return
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
    if not fits_context(messages):
        size = sum(len(message["content"].encode("utf-8")) for message in messages)
        raise RuntimeError(f"Model context exceeded: {size} UTF-8 prompt bytes plus framing and "
                           f"completion do not fit the configured {args.context} token context")
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
# Qwen 7B often ends its code with a bare DONE line, inside the block: that NameError'd every block of
# commit #27, so a scene the model had fully built read as "the AI model's code never ran". A DONE line
# is stripped before the code runs, and ends the build once that step's code has run cleanly.
DONE_LINE = re.compile(r"^[ \t]*DONE\.?[ \t]*$", re.M)
last_err = ""
plan, checks, plan_attempts = None, [], []
RERUN_HINT = ("\nThat block stopped at the error: nothing after that line ran, so any variable it would have set "
              "later (loop counters like i, lists, positions) does not exist. Send the WHOLE corrected block "
              "again, not just the fix; parts with the same name are replaced, not duplicated.")
try:
    if args.staged:
        plan = request_plan(args.prompt, chat_live, fits_context, plan_attempts)
        print("PBSTEP plan", json.dumps(plan["requirements"]), flush=True)
        world()
        for requirement in plan["requirements"]:
            previous_attempt, feedback, failures = None, None, 0
            while failures < 3:
                if step >= args.max_steps:
                    raise RuntimeError("Scene build budget ended before all requirements were verified")
                before = {getattr(obj, "session_uid", obj.as_pointer()) for obj in bpy.context.scene.objects}
                parts_before = evidence(scene_objects(), [requirement])[0]["parts"]
                prefix = ("Full request: " + args.prompt + "\nPlan: " + json.dumps(plan["requirements"])
                          + "\nComposition: " + plan["composition"])
                suffix = ("\nMeasured extents (read-only text, not Python state): " + json.dumps(assembly_summary(scene_objects()))
                          + "\nBuild ONLY this requirement now: " + json.dumps(requirement)
                          + f"\nThis assembly currently has {parts_before} verified mesh parts."
                          + " Add a compact batch of missing details; another pass can finish it."
                          + " Keep all existing assemblies. Defer camera, lights, world and all other subjects."
                          + " Do not render, export or write render/Cycles settings.")
                failure = {"role": "user", "content": feedback} if feedback else None
                build_messages = scene_messages(prefix, suffix, failure)
                if previous_attempt is not None:
                    retry = [*build_messages[:-1], {"role": "assistant", "content": previous_attempt}, failure]
                    # The instruction/scene, full failure and next completion take priority over
                    # optional history. An oversized response must not break the next repair.
                    build_messages = retry if fits_context(retry) else build_messages
                reply = chat_live(build_messages)
                text = reply.get("content") or ""
                previous_attempt = text
                blocks, last_err = completion_blocks(reply, requirement["id"])
                if not blocks:
                    log.append({"step": step, "requirement": requirement["id"], "ok": False,
                                "result": last_err, "response": text,
                                "finish_reason": reply.get("finish_reason")})
                for code in blocks:
                    code = DONE_LINE.sub("", code)
                    ok, res = run_code(code)
                    bpy.context.view_layer.update()
                    for obj in bpy.context.scene.objects:
                        if obj.type == "MESH" and (getattr(obj, "session_uid", obj.as_pointer()) not in before
                                                  or obj.get("pb_requirement") == requirement["id"]):
                            obj["pb_requirement"] = requirement["id"]
                    log.append({"step": step, "requirement": requirement["id"], "ok": ok, "result": res[:2000],
                                "code": code, "finish_reason": reply.get("finish_reason")})
                    print("PBSTEP", step, requirement["id"], "ok" if ok else "ERR", res[:300], flush=True)
                    if ok:
                        diff.append(code)
                        _snap["dirty"] = True
                    else:
                        last_err = res
                step += 1
                checks = evidence(scene_objects(), plan["requirements"])
                current = next(c for c in checks if c["id"] == requirement["id"])
                successful = bool(blocks) and all(item.get("ok") for item in log if item.get("step") == step - 1)
                if successful and current["complete"]:
                    snapshot()
                    last_err = ""
                    break
                if successful and current["parts"] > parts_before:
                    # A bounded reply may build only part of a detailed subject. Keep that real
                    # progress without spending a repair attempt; the global step budget still caps it.
                    feedback = (f"Assembly progress: {current['parts']} of at least {current['minimum']} parts exist. "
                                "Keep those parts. Add the remaining details for ONLY this requirement in a compact block.")
                    previous_attempt = None
                    continue
                if blocks and not current["complete"]:
                    missing = f"Only {current['parts']} parts exist; build at least {current['minimum']} detailed parts"
                    last_err = missing + ("\nExecution failure in this attempt: " + last_err if last_err else "")
                failures += 1
                feedback = "Previous failure: " + last_err + RERUN_HINT
            else:
                raise RuntimeError("Incomplete scene requirement " + requirement["id"] + ": " + last_err)
        # Composition is an explicit final pass, after all subjects exist. It must not delete them.
        composition_reply = chat_live(scene_messages("Full request: " + args.prompt,
            "\nMeasured extents (read-only text, not Python state): " + json.dumps(assembly_summary(scene_objects()))
            + "\nSet the final camera, PBR materials, lights and world for this composition: " + plan["composition"]
            + "\nKeep all existing mesh objects. Use camera(location,target,lens=24) to frame the main subjects."
            + " Return a Python code block; do not change render settings."))
        composition_blocks, composition_error = completion_blocks(composition_reply, "scene composition")
        if not composition_blocks:
            raise RuntimeError(composition_error)
        for code in composition_blocks:
            ok, res = run_code(DONE_LINE.sub("", code))
            if not ok:
                raise RuntimeError("Scene composition failed: " + res)
            diff.append(code)
        bpy.context.view_layer.update()
        checks = evidence(scene_objects(), plan["requirements"])
        if not all(c["complete"] for c in checks):
            raise RuntimeError("Composition removed required scene geometry")
        if getattr(bpy.context.scene.camera, "name", None) != "pb_artist_camera":
            raise RuntimeError("The scene has no deliberate composition camera")
        summary = "All planned scene components built; artistic quality awaits buyer review"
    for step in range(0 if args.staged else args.max_steps):
        # The system prompt, the request and only the LATEST exchange: the whole history (every code
        # block plus a scene dump per step) overflowed the 8k context on a long prompt (commit #26).
        text = chat_live(messages[:2] + messages[2:][-2:]).get("content") or ""
        messages.append({"role": "assistant", "content": text})
        blocks = FENCE.findall(text)
        finished = bool(DONE_LINE.search(text))
        if not blocks:
            summary = text.strip()[:300]
            log.append({"step": step, "done": True, "text": summary})
            print("PBSTEP", step, "done:", summary[:200].replace("\n", " | "), flush=True)
            break
        results = []
        for code in blocks:                      # each block = one execute_blender_code call
            code = DONE_LINE.sub("", code)
            ok, res = run_code(code)
            if ok:
                diff.append(code)
                _snap["dirty"] = True
                snapshot()
            else:
                finished = False                 # let the model fix it first
                last_err = (res.strip().splitlines() or [""])[-1][:300]
            log.append({"step": step, "tool": "execute_blender_code", "ok": ok, "result": res[:300],
                        **({} if ok else {"code": code[:1500]})})   # a failed block is otherwise lost
            print("PBSTEP", step, "code", "ok" if ok else "ERR", res[:300].replace("\n", " | "), flush=True)
            # Commit #41: after a NameError the model sent only a "fix" fragment that used a loop variable
            # from the failed block, which never ran -> NameError on every step. Say so, and ask for the
            # whole block again (same names replace the parts that did get built).
            results.append("OK: " + res[:800] if ok else "ERROR: " + res[:800] + RERUN_HINT)
        if finished:
            summary = "DONE"
            print("PBSTEP", step, "done: DONE", flush=True)
            break
        messages.append({"role": "user", "content": "\n".join(results) + f"\nScene now: {scene_info()}"
                         "\nFix any error with another code block, add anything still missing, or reply DONE."})
except Exception as e:  # noqa: BLE001 — the LLM call or a live view failed: keep what was built
    error = f"{type(e).__name__}: {e}"[:300]
    log.append({"step": step, "error": error})
    print("PBERROR", error.replace("\n", " | "), flush=True)

if not diff and not error:
    error = last_err                         # every block failed: say on what (not "never ran")

# Commit artifacts
frame_camera()
has_mesh = any(o.type == "MESH" for o in bpy.context.scene.objects)
sc = bpy.context.scene
sc.render.engine = "CYCLES"
sc.cycles.device = "CPU"
if args.gpu_render:
    try:
        preferences = bpy.context.preferences.addons["cycles"].preferences
        preferences.compute_device_type = "CUDA"
        preferences.get_devices()
        cuda_devices = [device for device in preferences.devices if device.type == "CUDA"]
        for device in preferences.devices:
            device.use = device.type == "CUDA"
        if cuda_devices:
            sc.cycles.device = "GPU"
    except Exception as exc:
        log.append({"gpu_render_unavailable": type(exc).__name__})
sc.cycles.samples = 48 if args.staged else 24
sc.cycles.use_denoising = True
size = (1920, 1080, 100) if args.render_size == "1080p" else ((1280, 720, 100) if args.staged else (640, 480, 100))
sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = size
if sc.world is None:
    sc.world = bpy.data.worlds.new("pb_world")
sc.world.use_nodes = True
bg = sc.world.node_tree.nodes.get("Background")
if bg and not args.staged:
    bg.inputs[0].default_value = (0.05, 0.06, 0.08, 1)
sc.render.filepath = os.path.join(args.out, "preview.png")
if has_mesh:
    bpy.ops.render.render(write_still=True)
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(args.out, "scene.blend"))
try:
    bpy.ops.export_scene.gltf(filepath=os.path.join(args.out, "scene.glb"), export_format="GLB")
except Exception as e:  # noqa: BLE001
    log.append({"export_error": str(e)[:300]})
    if args.staged:
        error = "The completed scene could not be exported to the 3D viewer: " + str(e)[:200]
if args.staged and not os.path.isfile(os.path.join(args.out, "scene.glb")):
    error = error or "The scene has no exported GLB for the 3D viewer"
with open(os.path.join(args.out, "diff.py"), "w") as f:
    f.write(f"# prompt: {args.prompt}\n\n" + "\n\n# ---\n".join(diff) + "\n")
viewer = None
if args.staged and getattr(sc.camera, "name", None) == "pb_artist_camera":
    target = mathutils.Vector(sc.camera.get("pb_target", (0, 0, 0)))
    offset = sc.camera.location - target
    radius = offset.length
    if radius > .01:
        # Blender resolves AUTO/HORIZONTAL/VERTICAL sensor fit and pixel aspect for this scene.
        frame = sc.camera.data.view_frame(scene=sc)
        vertical_fov = 2 * math.atan((max(v.y for v in frame) - min(v.y for v in frame))
                                     / (2 * abs(frame[0].z)))
        # Blender is Z-up; glTF/model-viewer are Y-up: (x,y,z) -> (x,z,-y).
        viewer = {"target": [float(target.x), float(target.z), float(-target.y)],
                  "orbit": [math.degrees(math.atan2(offset.x, -offset.y)),
                            math.degrees(math.acos(max(-1, min(1, offset.z / radius)))), radius],
                  "fov": math.degrees(vertical_fov)}
with open(os.path.join(args.out, "commit.json"), "w") as f:
    json.dump({"prompt": args.prompt, "ok": bool(diff) and (not args.staged or not error), "error": error, "summary": summary, "steps": log,
               "model": args.model, "local_peer_responses": _observed_local_peers,
               "plan": plan, "plan_attempts": plan_attempts,
               "requirements": checks, "viewer": viewer,
               "render_size": f"{size[1]}p", "resolution": list(size[:2]),
               "seconds": round(time.time() - t0, 1), "objects": scene_objects()}, f, indent=1)
print("PBCOMMIT", json.dumps({"ok": bool(diff) and (not args.staged or not error), "error": error, "summary": summary,
                              "seconds": round(time.time() - t0, 1)}))
