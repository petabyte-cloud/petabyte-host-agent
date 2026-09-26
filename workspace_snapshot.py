"""Resolve only this rental's labelled Docker workspace; never a shared cache."""
import json
import os
import re
import subprocess


def directory(task, volume):
    tid = int(task["task_id"])
    if task.get("task_type") == "template" and task.get("cache"):
        template = str(task.get("template", "tpl"))
        if not re.fullmatch(r"[a-z0-9-]+", template):
            raise ValueError("invalid template volume name")
        name = f"pb-vol-t{tid}-{template}"
        subprocess.run(["docker", "volume", "create", "--label", f"pb.task={tid}", name],
                       check=True, capture_output=True, timeout=30)
        result = subprocess.run(["docker", "volume", "inspect", name],
                                check=True, capture_output=True, text=True, timeout=15)
        info = json.loads(result.stdout)[0]
        if info.get("Labels", {}).get("pb.task") != str(tid):
            raise ValueError("workspace does not belong to this task")
        path = info.get("Mountpoint", "")
        if not os.path.isabs(path) or not os.path.isdir(path) or os.path.islink(path):
            raise ValueError("workspace has no local directory")
        return path
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", str(volume)):
        raise ValueError("invalid data volume")
    path = f"/var/lib/petabyte/vol/{volume}"
    os.makedirs(path, exist_ok=True)
    return path
