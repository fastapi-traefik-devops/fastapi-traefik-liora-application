#!/usr/bin/env python3
"""Validate application CI inputs without contacting a Kubernetes cluster."""
import ast
import json
from pathlib import Path
import re
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[1]


def main():
    for folder in ("backend/app", "ci", "scripts"):
        for path in (ROOT / folder).rglob("*.py"):
            ast.parse(path.read_text(), filename=str(path))
    # CI runs in Jenkins; any remaining GitHub workflow must stay read-only.
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        assert workflow.get("jobs"), f"Workflow has no jobs: {path}"
        assert workflow.get("permissions", {}).get("contents") == "read", f"Workflow is not read-only: {path}"
    for name in ("Jenkinsfile", "Jenkinsfile.promote"):
        text = (ROOT / name).read_text()
        assert not re.search(r"agent\s*\{\s*kubernetes\b", text), name
        assert not re.search(r"\b(kubectl|helm|argocd|terraform)\s", text), name
    for image in json.loads((ROOT / "ci/images.lock.json").read_text()).values():
        assert re.search(r"@sha256:[0-9a-f]{64}$", image), image
    for path in [*list((ROOT / "ci").glob("*.sh")), *list((ROOT / "backend/scripts").glob("*.sh")), *list((ROOT / "scripts").glob("*.sh"))]:
        shell = "bash" if "bash" in path.read_text().splitlines()[0] else "sh"
        subprocess.run([shell, "-n", str(path)], check=True)
    print("PASS: application Python, shell, read-only workflows and pinned CI images")


if __name__ == "__main__":
    main()
