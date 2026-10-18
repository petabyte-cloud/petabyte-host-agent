"""Scene planning and evidence checks shared by the sandboxed Blender driver and its tests."""
import json
import math
import re


PLAN_SYSTEM = """Plan a complete Blender scene from the user's prompt. Return ONLY a JSON object:
{"requirements":[{"id":"short_snake_case","description":"visible subject and its required details",
"min_parts":12}],"composition":"camera position, lens, lighting, palette and relative subject placement"}.
Use 2-7 requirements for a complex scene, one per major subject/environment group. Include every
explicitly requested subject, landscape feature, distant/background element and visual composition.
Requirements are physical mesh assemblies. Camera, lighting and material-only work belongs in
the composition field, not a requirement with no geometry.
Do not replace a detailed scene with a generic object. Set min_parts to a realistic assembly size
(1-40), e.g. articulated people and mechanical vehicles need many separate parts. Requirements
will be built in order, so put terrain first and detailed foreground subjects last. Keep it concise.
"""

BUILD_SYSTEM = """You are a skilled Blender environment artist. Build the requested complete scene,
with recognisable detailed subjects, coherent scale, foreground depth and the requested atmosphere.
Use Python with bpy, bmesh, mathutils, math, random and Vector, which are already imported. Reply
with ONE complete ```python block. Code runs inside an isolated Blender container. No internet,
external assets, subprocesses, package installs or file access. Use procedural meshes and materials.
Do not simplify a complex prompt to a cube. Build the current requirement thoroughly; retain earlier
requirements. Use stable, descriptive object names; calling a helper with the same name replaces it.
Available helpers (units meters, Z up, location = center, rotation in degrees):
box(name,size,location,color=(r,g,b),rotation=(rx,ry,rz))
cylinder(name,radius,height,location,color,rotation=(rx,ry,rz),vertices=32)
cone(name,radius_bottom,radius_top,height,location,color,rotation=(rx,ry,rz))
sphere(name,radius,location,color,segments=32)
torus(name,major_radius,minor_radius,location,color,rotation=(rx,ry,rz))
beam(name,start,end,radius,color) creates a cylinder joining two 3D points.
ellipsoid(name,size,location,color,rotation=(0,0,0)) uses full dimensions.
material(name,color,metallic=0,roughness=0.5,emission=None,strength=1) returns a PBR material.
assign(obj,mat) assigns a material. Mesh helpers return the new object.
terrain(name,size=40,resolution=80,height=2,seed=1,color=(.4,.15,.07)) creates displaced rocky ground.
camera(location,target,lens=24) sets the final artistic camera. light(name,location,color,energy,size=5)
adds an area light. world(color,strength=.3) sets ambient light. remove(name) removes a part.
You may use bpy directly for meshes, curves, bevels, modifiers, material nodes, lights and camera.
Follow the requested lighting, sky, fog, reflections, lens and composition with procedural Blender
equivalents. Protect the foreground composition: distant planets and scenery must not force the
camera to frame the entire world. Use a deliberate camera looking at the main subjects.
No text labels or captions as substitutes for geometry. Do not alter render resolution, engine,
sample counts or output paths; the driver controls rendering and exports. Keep each block complete
and bounded (under 350 lines). Never output DONE instead of building an unfinished requirement.
"""


def parse_plan(text, prompt):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    plan = json.loads(text)
    if not isinstance(plan, dict):
        raise ValueError("The local model did not supply a scene plan object")
    rows = plan.get("requirements")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 7:
        raise ValueError("The local model did not supply a complete scene plan")
    clean, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("The local model supplied an invalid scene requirement")
        key = row.get("id", "")
        description = row.get("description", "")
        minimum = row.get("min_parts", 1)
        if (not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", key)
                or key in seen or not isinstance(description, str) or not description.strip()
                or type(minimum) is not int or not 1 <= minimum <= 40):
            raise ValueError("The local model supplied an invalid scene requirement")
        seen.add(key)
        clean.append({"id": key, "description": description[:1200], "min_parts": minimum})
    return {"requirements": clean, "composition": str(plan.get("composition", ""))[:1800],
            "request": prompt}


def evidence(objects, requirements):
    result = []
    for requirement in requirements:
        parts = [o for o in objects if o.get("requirement") == requirement["id"] and o.get("type") == "MESH"
                 and len(o.get("dimensions", ())) == 3
                 and all(math.isfinite(x) for x in o["dimensions"])
                 and sum(abs(x) for x in o["dimensions"]) > 0.001]
        result.append({"id": requirement["id"], "parts": len(parts),
                       "minimum": requirement["min_parts"], "complete": len(parts) >= requirement["min_parts"]})
    return result


def assembly_summary(objects):
    """Keep every assembly's position visible even when the detailed object list is truncated."""
    groups = {}
    for obj in objects:
        key = obj.get("requirement")
        position, dimensions = obj.get("location", ()), obj.get("dimensions", ())
        if not key or obj.get("type") != "MESH" or len(position) != 3 or len(dimensions) != 3:
            continue
        group = groups.setdefault(key, {"parts": 0, "min": [float("inf")] * 3,
                                        "max": [float("-inf")] * 3, "examples": []})
        group["parts"] += 1
        if len(group["examples"]) < 6:
            group["examples"].append(obj.get("name"))
        for i in range(3):
            group["min"][i] = round(min(group["min"][i], position[i] - dimensions[i] / 2), 3)
            group["max"][i] = round(max(group["max"][i], position[i] + dimensions[i] / 2), 3)
    return groups
