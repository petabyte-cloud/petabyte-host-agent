"""Scene planning and evidence checks shared by the sandboxed Blender driver and its tests."""
import ast
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
will be built in order. Establish essential supports and the main subjects' shared pose/scale early;
leave repetitive background decoration until after the main subjects. Keep it concise.
Use valid JSON literals only: double-quoted strings, integer min_parts, no Python expressions,
comments or trailing commas. Keep composition a concise string, not Blender code.
"""

BUILD_SYSTEM = """You are a skilled Blender environment artist. Build the requested scene in small passes,
with recognisable detailed subjects, coherent scale, foreground depth and the requested atmosphere.
Use Python with bpy, bmesh, mathutils, math, random and Vector, which are already imported. Reply
with ONE complete ```python block. Code runs inside an isolated Blender container. No internet,
external assets, subprocesses, package installs or file access. Use procedural meshes and materials.
Do not simplify a complex prompt to a cube. In an assembly pass, build ONLY the named requirement.
Add a manageable batch of detailed parts; further passes can finish that same assembly. Retain all
existing parts and earlier requirements. Never clear the scene or build other subjects in this pass.
Use stable, descriptive object names; calling a helper with the same name replaces it.
Available helpers (units meters, Z up, location = center, rotation in degrees):
box(name,size,location,color=(r,g,b),rotation=(rx,ry,rz))
cylinder(name,radius,height,location,color,rotation=(rx,ry,rz),vertices=32)
cone(name,radius_bottom,radius_top,height,location,color,rotation=(rx,ry,rz))
sphere(name,radius,location,color,segments=32)
torus(name,major_radius,minor_radius,location,color,rotation=(rx,ry,rz))
beam(name,start,end,radius,color) creates a cylinder joining two 3D points.
ellipsoid(name,size,location,color,rotation=(0,0,0)) uses full dimensions.
Mesh helper color arguments are numeric RGB, not material objects; assign PBR materials afterward.
material(name,color,metallic=0,roughness=0.5,emission=None,strength=1) returns a PBR material.
material(name) retrieves an existing material unchanged; a new material always needs numeric RGB.
assign(obj,mat) accepts a material object or an existing material's name. Mesh helpers return the new
object, already linked to the scene.
Do not link helper objects again. There is no predefined scene variable; scene.objects.link is an
obsolete Blender API. Only a brand-new bpy.data object needs bpy.context.collection.objects.link(obj).
Helper/import names are reserved: never assign to box, terrain, material, camera, random, etc.
Use names like terrain_obj, ground_mat and scene_camera for their returned objects/materials.
Measured extents and part counts are read-only text, not Python variables. Never use assembly_bounds
or write completion/count metadata; the driver computes that evidence from actual meshes.
terrain(name,size=40,resolution=80,height=2,seed=1,color=(.4,.15,.07)) creates displaced rocky ground.
camera(location,target,lens=24) sets the final artistic camera. light(name,location,color,energy,size=5)
adds an area light. world(color,strength=.3) sets ambient light. remove(name) removes a part.
You may use bpy directly for meshes, curves, bevels, modifiers, material nodes, lights and camera.
Follow the requested lighting, sky, fog, reflections, lens and composition with procedural Blender
equivalents. Protect the foreground composition: distant planets and scenery must not force the
camera to frame the entire world. Use a deliberate camera looking at the main subjects.
No text labels or captions as substitutes for geometry. Do not alter render resolution, engine,
sample counts or output paths; never call render or export operators. The driver renders and exports.
Keep each reply compact (at most 80 lines), finish the Python block and close its fence. Prefer helpers
and loops over repeated boilerplate. An assembly pass must defer camera, lights and world to the final
composition pass. Never output DONE instead of building an unfinished requirement.
"""


def sampling_options(model):
    """Trusted local sampler defaults; generated code cannot choose unbounded penalty windows."""
    if model.lower().rsplit("/", 1)[-1] == "qwen2.5-coder-7b-instruct":
        # Qwen's generation_config.json specifies these values (the GGUF does not apply it).
        # llama.cpp calls repetition_penalty repeat_penalty; use a bounded recent-token window.
        # https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct/blob/main/generation_config.json
        return {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0,
                "repeat_penalty": 1.1, "repeat_last_n": 256}
    return {"temperature": 0.3}


def completion_blocks(reply, label):
    """Reject incomplete output as a whole; never execute a prefix of a cut-off response."""
    text = reply.get("content") or ""
    fences = re.findall(r"(?m)^[ \t]*```[^\n]*$", text)
    if reply.get("finish_reason") == "length" or len(fences) % 2:
        return [], ("Incomplete Python response for " + label + ": the completion ended before the reply was finished. "
                    "No code from this reply ran. Return one compact, closed Python block for ONLY the current pass; "
                    "use helpers and loops and leave remaining parts for another pass.")
    blocks = re.findall(r"```(?:python|py)?[ \t]*\n(.*?)```", text, re.S)
    return blocks, "" if blocks else "No executable Blender code was returned for " + label


def execution_hint(message, source):
    """Correct observed Blender API mistakes at the failing line, without changing/executing code."""
    if ("scene.objects.link" in source and
            (message.startswith("NameError:") or message.startswith("AttributeError:"))):
        return (" Remove scene.objects.link(...): mesh helpers already link their objects and scene is not a "
                "predefined variable. Only brand-new bpy.data objects need bpy.context.collection.objects.link(obj).")
    if ".objects.link(" in source and "already in collection" in message:
        return " Do not link that object again: helper meshes are already in the scene collection."
    if ("bmesh.ops.bevel" in source and message.startswith("TypeError:") and "thickness" in message):
        return (" bmesh.ops.bevel does not accept thickness. Use a mesh BEVEL modifier with width and segments; "
                "apply the mesh's scale for a consistent bevel width.")
    if "primitive_ellipsoid_add" in source and message.startswith("AttributeError:"):
        return (" Blender has no primitive_ellipsoid_add operator. Use the provided "
                "ellipsoid(name, size, location, color=(r,g,b)) helper, which returns a linked mesh.")
    if message.startswith("ValueError:") and "no existing material" in message and "material(" in source:
        return (" Material names are not predefined colors. First create the material with numeric RGB, "
                "then use its returned material object or look it up by that same name.")
    if message.startswith("TypeError:") and "color must be" in message and "bpy.data.materials[" in message:
        return (" A material object is not an RGB color. Pass numeric RGB to the mesh helper, then "
                "apply the material with assign(the_returned_mesh, the_material).")
    return ""


def validate_observation_names(code):
    """Reject invented bookkeeping state before any statement in this block runs.

    The prompt's measured extents are text, not a mutable object in the execution namespace.
    This narrowly checks a known mistaken name; it is not a Python security sandbox.
    """
    for node in ast.walk(ast.parse(code, filename="<llm>", mode="exec")):
        if isinstance(node, ast.Name) and node.id == "assembly_bounds":
            raise ValueError(f"assembly_bounds is read-only observation text, not a Python variable "
                             f"(line {node.lineno}). Remove that bookkeeping code; build actual meshes. "
                             "The driver measures extents and counts parts itself. No code in this block ran.")


def validate_material_assignments(code):
    """Reject known-invalid literal colors at every assign call before scene mutation.

    This is a narrow helper-contract check, not a type checker or a Python security sandbox.
    Material objects, existing names and computed values retain their runtime validation.
    """
    lines = set()
    for node in ast.walk(ast.parse(code, filename="<llm>", mode="exec")):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "assign"):
            continue
        values = ([node.args[1]] if len(node.args) > 1 else [])
        values += [kw.value for kw in node.keywords if kw.arg == "mat"]
        if any(isinstance(value, (ast.Tuple, ast.List)) for value in values):
            lines.add(node.lineno)
    if lines:
        ordered = sorted(lines)
        label = ", ".join(str(line) for line in ordered[:8])
        if len(ordered) > 8:
            label += f" (+{len(ordered) - 8} more)"
        raise ValueError("assign() needs a material object or an existing material name, not RGB tuples/lists "
                         f"(lines {label}). Fix ALL these calls: create a material with "
                         "material('name', (r,g,b)), then pass its returned handle to assign(obj, mat). "
                         "Mesh-helper color arguments still take numeric RGB. No code in this block ran.")


def runtime_api_description(bpy):
    """Describe sockets from this container, rather than guessing a Blender release's API."""
    version = getattr(bpy.app, "version_string", "unknown")
    version = version if isinstance(version, str) else "unknown"
    lines = ["Running Blender: " + version]
    probe = bpy.data.materials.new("pb_api_contract_probe")
    try:
        probe.use_nodes = True
        for kind in ("ShaderNodeBsdfPrincipled", "ShaderNodeVolumePrincipled"):
            node = probe.node_tree.nodes.new(kind)
            for direction in ("inputs", "outputs"):
                sockets = [f"{socket.name} ({socket.type})" for socket in getattr(node, direction)]
                lines.append(kind + "." + direction + ": " + ", ".join(sockets))
    finally:
        bpy.data.materials.remove(probe)
    lines.append("Use actual socket names, never numeric indices; inspect node.inputs.keys() "
                 "before linking/setting sockets. Do not assume an older Blender API. "
                 "Use string values for STRING sockets, scalar numbers for VALUE sockets, "
                 "and RGBA tuples for RGBA sockets. Render/Cycles settings, including denoising, "
                 "are driver-owned; omit them from generated code.")
    return "\n".join(lines)


def validate_render_settings(code, scene_aliases=()):
    """Reject direct renderer writes before a block can partially modify the scene.

    This is an ownership check for generated scene code, not a Python security sandbox.
    Track common aliases so `sc = bpy.context.scene; sc.cycles.samples = ...` gets the same
    actionable feedback as the fully qualified form.
    """
    aliases = {"bpy": ("bpy",), **{name: ("bpy", "context", "scene") for name in scene_aliases}}

    def path(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, ())
        if isinstance(node, ast.Attribute):
            parent = path(node.value)
            return (*parent, node.attr) if parent else ()
        return ()

    def check(target, attribute=None):
        if isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                check(item)
            return
        if attribute is None and not isinstance(target, ast.Attribute):
            return  # Rebinding/deleting a local alias never writes to its referenced object.
        parts = path(target)
        if (parts[:3] == ("bpy", "context", "scene")
                and ((len(parts) > 3 and parts[3] in ("render", "cycles"))
                     or (len(parts) == 3 and attribute in ("render", "cycles")))):
            raise ValueError(f"Render/Cycles settings are driver-owned (line {target.lineno}). "
                             "Remove all render/Cycles assignments from this block; the driver "
                             "sets the requested resolution, Cycles engine, samples, denoising and output paths.")

    class Writes(ast.NodeVisitor):
        def visit_Call(self, node):
            parts = path(node.func)
            if parts[:2] == ("bpy", "ops") and len(parts) > 2 and parts[2] in ("render", "export_scene", "export_mesh"):
                raise ValueError(f"Rendering and exports are driver-owned (line {node.lineno}). "
                                 "Remove render/export calls; return only the current scene-building pass.")
            if isinstance(node.func, ast.Name) and node.func.id in ("setattr", "delattr") and len(node.args) >= 2:
                attribute = node.args[1].value if isinstance(node.args[1], ast.Constant) else ""
                check(node.args[0], attribute=attribute)
            self.generic_visit(node)

        def visit_Assign(self, node):
            for target in node.targets:
                check(target)
            self.visit(node.value)
            value = path(node.value)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if value:
                        aliases[target.id] = value
                    else:
                        aliases.pop(target.id, None)

        def visit_AnnAssign(self, node):
            self.visit_Assign(ast.Assign(targets=[node.target], value=node.value or ast.Constant(None)))

        def visit_AugAssign(self, node):
            check(node.target)
            self.visit(node.value)
            if isinstance(node.target, ast.Name):
                aliases.pop(node.target.id, None)

        def visit_Delete(self, node):
            for target in node.targets:
                check(target)
                if isinstance(target, ast.Name):
                    aliases.pop(target.id, None)

        def visit_FunctionDef(self, node):
            saved = aliases.copy()
            # A function's parameter may shadow a scene alias from the outer block.
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                             *([node.args.vararg] if node.args.vararg else []),
                             *([node.args.kwarg] if node.args.kwarg else [])):
                aliases.pop(argument.arg, None)
            self.generic_visit(node)
            aliases.clear()
            aliases.update(saved)

    Writes().visit(ast.parse(code, filename="<llm>", mode="exec"))


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
    setup = sorted(seen.intersection({"camera_setup", "lighting_setup"}))
    if setup:
        raise ValueError("Camera and lighting setup belong in composition, not physical mesh requirements: "
                         + ", ".join(setup) + ". Keep every requested physical subject as a requirement; "
                         "the final composition pass handles camera and lighting after those meshes exist.")
    return {"requirements": clean, "composition": str(plan.get("composition", ""))[:1800],
            "request": prompt}


def request_plan(prompt, ask, fits_context, attempts):
    """Repair at most three invalid plans in the existing job; never execute a plan reply."""
    base = [{"role": "system", "content": PLAN_SYSTEM}, {"role": "user", "content": prompt}]
    previous, error = None, ""
    for attempt in range(3):
        messages = base
        if error:
            feedback = {"role": "user", "content": (
                "The previous scene plan was invalid: " + error
                + "\nReturn the WHOLE corrected JSON object, with all requested subjects."
                + " Use only requirements (id, description, integer min_parts) and a concise composition string."
                + " No code, expressions, comments or trailing commas. Finish the JSON object.")}
            messages = [*base, feedback]
            history = [*base, {"role": "assistant", "content": previous}, feedback]
            # Previous output is optional. Keep the full request, diagnostic and completion budget.
            if fits_context(history):
                messages = history
        reply = ask(messages)  # chat_live checks the exact final context before making the call.
        text = reply.get("content") or ""
        record = {"attempt": attempt, "response": text, "finish_reason": reply.get("finish_reason"), "ok": False}
        attempts.append(record)
        try:
            if reply.get("finish_reason") == "length":
                raise ValueError("The plan completion was cut off; return a shorter complete plan")
            if not isinstance(text, str):
                raise ValueError("The plan response must be JSON text")
            plan = parse_plan(text, prompt)
        except ValueError as exc:
            error = f"{type(exc).__name__}: {exc}"
            record["error"] = error
            previous = text if isinstance(text, str) else ""
            continue
        record["ok"] = True
        return plan
    raise RuntimeError("Scene planning failed after 3 attempts: " + error)


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
