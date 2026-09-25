#!/usr/bin/env python3
"""Regression: every HARD runtime third-party import in the agent is declared in requirements.

A clean seller machine failed with `ModuleNotFoundError: No module named 'cryptography'`
because `cryptography` was imported by crypto.py / task_fetcher.py but never listed in
lumaris_agent/requirements.txt (and `pip check` can't catch a dependency that was simply never
declared). This test parses the agent's RUNTIME modules, collects their top-level third-party
imports, and asserts each is satisfied by requirements.txt — so a new undeclared import fails
CI instead of a customer's install.

Run: python lumaris_agent/deps_audit_test.py
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STDLIB = set(getattr(sys, "stdlib_module_names", set()))
_fail = 0


def ok(label, cond):
    global _fail
    print(("ok  " if cond else "FAIL") + "  " + label)
    if not cond:
        _fail += 1


# Files that are NOT part of the installed runtime: test files, and build/packaging helpers
# (their deps are dev-only and not needed to RUN the agent).
EXCLUDE = {"build_exe.py", "setup.py"}
# import-name -> pip distribution name, when they differ.
ALIASES = {"dotenv": "python-dotenv"}
# Optional imports that are guarded (try/except) so the agent runs without them; not required
# in the runtime manifest.
#   * opentelemetry — best-effort tracing (agent_telemetry.py).
#   * torch — the GPU FP16 authenticity benchmark (_measure_fp16_tflops) and NCCL distributed
#     training run on the SELLER's GPU box, which ships torch itself; the agent detects its
#     absence (returns None / falls back) and never lists it as a control-plane dependency.
#   * version — a packaging-only module baked into the frozen desktop-app bundle (desktop-app/
#     version.py); agent_cli.py imports it under try/except with a hardcoded fallback, so the
#     control-plane runtime never needs it declared as a dependency.
OPTIONAL = {"opentelemetry", "torch", "version"}


def _runtime_files():
    for f in sorted(os.listdir(HERE)):
        if not f.endswith(".py"):
            continue
        if f.endswith("_test.py") or f in EXCLUDE or f == "__init__.py":
            continue
        yield f


def _local_modules():
    return {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}


def _third_party_imports():
    local = _local_modules()
    found = {}
    for f in _runtime_files():
        tree = ast.parse(open(os.path.join(HERE, f)).read(), filename=f)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for m in names:
                if m in STDLIB or m in local:
                    continue
                found.setdefault(m, set()).add(f)
    return found


def _declared_distributions():
    reqs = os.path.join(HERE, "requirements.txt")
    dists = set()
    for line in open(reqs):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # strip version specifiers / extras / markers: "python-dotenv[all]>=1.0 ; x" -> "python-dotenv"
        name = re.split(r"[<>=!~\[ ;]", line, 1)[0].strip().lower()
        if name:
            dists.add(name)
    return dists


imports = _third_party_imports()
declared = _declared_distributions()
ok("requirements.txt parsed and non-empty", bool(declared))

missing = []
for mod, files in sorted(imports.items()):
    if mod in OPTIONAL:
        continue
    dist = ALIASES.get(mod, mod).lower()
    if dist not in declared:
        missing.append(f"{mod} (dist '{dist}') used by {', '.join(sorted(files))}")

ok("cryptography is declared (the exact gap that broke a clean seller install)",
   "cryptography" in declared)
ok("every HARD runtime third-party import is declared in requirements.txt",
   not missing)
if missing:
    for m in missing:
        print("     MISSING:", m)

print(f"\n=== agent deps_audit: {'0 failures' if _fail == 0 else str(_fail) + ' FAILED'} ===")
raise SystemExit(1 if _fail else 0)
