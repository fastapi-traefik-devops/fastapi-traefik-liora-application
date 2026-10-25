#!/usr/bin/env python3
"""Docker-only CI. No cluster credentials, deployment commands or registry writes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
# Read-only source of the deployment overlays checked by the "gitops" gate.
INFRASTRUCTURE_REPO = "https://github.com/fastapi-traefik-devops/fastapi-traefik-liora-infrastructure.git"
STEPS = (
    "config", "secrets", "backend-dependencies", "backend-lint", "backend-unit",
    "backend-integration", "frontend-dependencies", "frontend-lint", "frontend-build",
    "frontend-tests", "backend-security", "frontend-security", "backend-image",
    "frontend-image", "backend-image-smoke", "frontend-image-smoke",
    "backend-image-security", "frontend-image-security", "attest",
)
# The 18 gates an attestation can record; "attest" itself and "gitops" are not image gates.
GATES = tuple(step for step in STEPS if step != "attest")
PROFILES = {
    # Functional-only gates. Publication requires a separate explicit candidate export.
    "migration": (
        "config", "backend-dependencies", "backend-lint", "backend-unit",
        "backend-integration", "frontend-dependencies", "frontend-lint",
        "frontend-build", "frontend-tests", "backend-image", "frontend-image",
        "backend-image-smoke", "frontend-image-smoke",
    ),
    # Pull requests and the nightly run: every gate plus GitOps manifest and CI-script checks.
    "full": (*GATES, "gitops"),
    # Merges to main: re-verify the image that gets published and refresh vulnerability data.
    # Source-level tests already passed on the pull request for the same tree.
    "release": (
        "config", "secrets", "backend-dependencies", "frontend-dependencies",
        "backend-security", "frontend-security", "backend-image", "frontend-image",
        "backend-image-smoke", "frontend-image-smoke", "backend-image-security",
        "frontend-image-security", "attest",
    ),
}
# TEMPORARY (2026-10-24): pull requests skip the slow integration and e2e tests to speed up CI
# setup; the nightly "full" run still executes them. Remove "pr" to return PRs to "full".
SKIPPED_ON_PR = ("backend-integration", "frontend-tests")
PROFILES["pr"] = tuple(step for step in PROFILES["full"] if step not in SKIPPED_ON_PR)
# Manual DEV build of any branch: same gates as a main release (source tests belong to the PR).
PROFILES["dev"] = PROFILES["release"]
# The publisher refuses an attestation that lacks any of these.
RELEASE_GATES = frozenset(PROFILES["release"]) - {"attest"}
TEST_REPORTS = {"backend-unit": "backend-unit.xml", "backend-integration": "backend-integration.xml",
                "frontend-tests": "frontend-tests.xml"}


def run(*args: str, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def output(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


def file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class CI:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()
        self.sha = output("git", "-C", str(self.root), "rev-parse", "HEAD")
        if not re.fullmatch(r"[0-9a-f]{40}", self.sha):
            raise ValueError("Full Git SHA required")
        suffix = re.sub(r"[^a-zA-Z0-9-]", "-", os.environ.get("BUILD_TAG", "local"))[-24:]
        self.name = f"phase1-{self.sha[:12]}-{suffix}".lower()
        self.private = self.root / ".ci-private" / self.name
        self.reports = self.root / ".ci-output"
        self.private.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.reports.mkdir(exist_ok=True)
        self.private.chmod(0o700)
        self.state_file = self.private / "state.json"
        self.state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {"passed": []}
        self.images = json.loads((self.root / "infra/jenkins-host/images.lock.json").read_text())
        self.env_file = self.private / "test.env"
        if not self.env_file.exists():
            env = {
                "ENVIRONMENT": "local", "PROJECT_NAME": "Phase 1 CI",
                "POSTGRES_SERVER": "db", "POSTGRES_PORT": "5432", "POSTGRES_DB": "app_ci",
                "POSTGRES_USER": "postgres", "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
                "SECRET_KEY": secrets.token_urlsafe(32), "FIRST_SUPERUSER": "ci-admin@example.com",
                "FIRST_SUPERUSER_PASSWORD": secrets.token_urlsafe(24), "EMAIL_TEST_USER": "test@example.com",
                "EMAILS_FROM_EMAIL": "ci@example.com", "EMAILS_FROM_NAME": "CI",
                "SMTP_HOST": "mail", "SMTP_PORT": "1025", "SMTP_TLS": "false", "SMTP_SSL": "false",
                "FRONTEND_HOST": "http://localhost:5173", "BACKEND_CORS_ORIGINS": "http://localhost:5173",
            }
            fd = os.open(self.env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write("".join(f"{key}={value}\n" for key, value in env.items()))

    def tool(self, name: str) -> str:
        local = self.root / ".tools/phase1" / name
        return str(local) if local.is_file() else name

    def image(self, prefix: str) -> str:
        candidates = [value for key, value in self.images.items() if key.startswith(prefix)]
        if len(candidates) != 1:
            raise ValueError(f"Expected one pinned image for {prefix}")
        return candidates[0]

    @property
    def test_image(self) -> str:
        return f"phase1/backend-test:{self.name}"

    def container(self, image: str, *command: str, network: bool = True, memory: str = "1g", work: str = "/app"):
        args = ["docker", "run", "--rm", "--name", self.name + "-tool", "--memory", memory,
                "--memory-swap", memory, "--shm-size", "256m", "--cpus", "2" if memory == "2g" else "1", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--user", f"{os.getuid()}:{os.getgid()}", "--env-file", str(self.env_file),
                "-e", "HOME=/tmp", "-e", "CI=true", "-e", "PYTHONDONTWRITEBYTECODE=1",
                "-e", "NODE_OPTIONS=--max-old-space-size=1536", "-e", "PLAYWRIGHT_JUNIT_OUTPUT_NAME=/reports/frontend-tests.xml",
                "-e", "VITE_API_URL=http://backend:8000", "-e", "MAILPIT_HOST=http://mail:8025",
                "-e", "COVERAGE_FILE=/reports/.coverage", "-e", "RUFF_CACHE_DIR=/reports/ruff-cache",
                "-v", f"{self.reports}:/reports", "-w", work]
        if network:
            self.ensure_network()
            args.extend(["--network", self.name])
        if image != self.test_image:
            args.extend(["-v", f"{self.root / 'frontend'}:/app"])
        private_values = [line.split("=", 1)[1] for line in self.env_file.read_text().splitlines()
                          if line.startswith(("SECRET_KEY=", "POSTGRES_PASSWORD=", "FIRST_SUPERUSER_PASSWORD="))]
        process = subprocess.Popen([*args, image, *command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for line in process.stdout:
                for value in private_values:
                    line = line.replace(value, "<redacted-ci-secret>")
                line = re.sub(r'(fill\(["\']).*?(["\']\))', r'\1<redacted>\2', line)
                print(line, end="", flush=True)
            status = process.wait()
        finally:
            for report in [*self.reports.glob("*.xml"), *self.reports.glob("*.json")]:
                contents = report.read_text()
                for value in private_values:
                    contents = contents.replace(value, "[REDACTED_CI_SECRET]")
                report.write_text(contents)
        if status:
            raise subprocess.CalledProcessError(status, ["docker", "run", image, *command])

    def ensure_network(self):
        if subprocess.run(["docker", "network", "inspect", self.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            run("docker", "network", "create", "--label", "phase1.ci=true", self.name, stdout=subprocess.DEVNULL)

    def database(self):
        self.ensure_network()
        name = self.name + "-db"
        if subprocess.run(["docker", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            run("docker", "run", "--rm", "-d", "--name", name, "--network", self.name, "--network-alias", "db",
                "--memory", "256m", "--user", "70:70", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--tmpfs", "/var/lib/postgresql/data:uid=70,gid=70,mode=0700", "--env-file", str(self.env_file),
                self.image("postgres:"), stdout=subprocess.DEVNULL)
        for _ in range(60):
            if subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres", "-d", "app_ci"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                return
            time.sleep(1)
        raise RuntimeError("Disposable CI database did not become ready")

    def backend_tests(self, unit: bool):
        if not unit:
            self.database()
            self.container(self.test_image, "bash", "scripts/prestart.sh")
        selected = ["app/tests/scripts"] if unit else ["app/tests/api", "app/tests/crud", "app/tests/integration"]
        args = ["coverage", "run", "--source=app", "--omit=app/tests/*"]
        if not unit:
            args.append("--append")
        self.container(self.test_image, *args, "-m", "pytest", *selected,
                       "-p", "no:cacheprovider", f"--junitxml=/reports/backend-{'unit' if unit else 'integration'}.xml")
        if not unit:
            self.container(self.test_image, "coverage", "report", "--show-missing")
            self.container(self.test_image, "coverage", "xml", "-o", "/reports/backend-coverage.xml")

    def frontend_tests(self):
        self.database()
        # Start a fresh database for E2E after pytest's cleanup and recreate seed data.
        self.container(self.test_image, "bash", "scripts/prestart.sh")
        self.ensure_network()
        for suffix, image, args in [
            ("mail", self.image("axllent/mailpit:"), []),
            ("backend", self.test_image, ["fastapi", "run", "--workers", "1", "app/main.py"]),
        ]:
            run("docker", "run", "--rm", "-d", "--name", self.name + "-" + suffix,
                "--network", self.name, "--network-alias", suffix, "--memory", "384m",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--env-file", str(self.env_file),
                image, *args, stdout=subprocess.DEVNULL)
        # Run the browser and Vite in the same Docker container; no host ports are published.
        self.container(self.image("mcr.microsoft.com/playwright:"), "bash", "-ec",
                       "npm run test:e2e -- --workers=1 --reporter=line,junit", memory="2g")

    def secret_scan(self):
        scanner = self.tool("gitleaks")
        common = ["--redact=100", "--no-banner", "--ignore-gitleaks-allow", "--config", str(self.root / ".gitleaks.toml")]
        run(scanner, "git", str(self.root), *common, "--report-format", "json", "--report-path", str(self.reports / "secrets-history.json"))
        # Respect Git's ignored local private/cache directories without blanket scanner exclusions.
        with tempfile.TemporaryDirectory(prefix="phase1-secret-tree-") as directory:
            selected = subprocess.check_output(["git", "-C", str(self.root), "ls-files", "-z", "-co", "--exclude-standard"]).split(b"\0")
            for raw in set(selected):
                if not raw:
                    continue
                relative = Path(os.fsdecode(raw))
                source = self.root / relative
                if source.is_file() and not source.is_symlink():
                    target = Path(directory) / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
            run(scanner, "dir", directory, *common, "--report-format", "json", "--report-path", str(self.reports / "secrets-tree.json"))

    def scan_image(self, component: str):
        archive = self.reports / f"{component}-ci.tar"
        run("docker", "save", "-o", str(archive), f"phase1/{component}:{self.name}")
        report = self.reports / f"{component}-image-security.json"
        args = [self.tool("trivy"), "image", "--input", str(archive), "--scanners", "vuln,secret",
                "--severity", "HIGH,CRITICAL", "--exit-code", "1", "--no-progress", "--timeout", "10m",
                "--format", "json", "--output", str(report)]
        result = subprocess.run(args)
        if report.exists():
            data = json.loads(report.read_text())
            for target in data.get("Results", []):
                for finding in target.get("Secrets") or []:
                    for key in ("Match", "Code"):
                        if key in finding:
                            finding[key] = "[REDACTED]"
            report.write_text(json.dumps(data, indent=2) + "\n")
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, args)

    def smoke_image(self, component: str):
        image = f"phase1/{component}:{self.name}"
        expected_user = "10001:10001" if component == "backend" else "101:101"
        actual_user = output("docker", "image", "inspect", image, "--format", "{{.Config.User}}")
        if actual_user != expected_user:
            raise RuntimeError(f"{component} runtime image has unexpected user: {actual_user}")
        self.ensure_network()
        name = self.name + "-" + component + "-smoke"
        args = ["docker", "run", "--rm", "-d", "--name", name, "--network", self.name,
                "--memory", "512m" if component == "backend" else "128m",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true"]
        if component == "backend":
            args.extend(["--env-file", str(self.env_file)])
            probe = ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/utils/health-check/', timeout=3)"]
        else:
            probe = ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1/healthz"]
        run(*args, image, stdout=subprocess.DEVNULL)
        try:
            for _ in range(60):
                if subprocess.run(["docker", "exec", name, *probe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                    return
                time.sleep(1)
            raise RuntimeError(f"{component} runtime image failed its health endpoint")
        finally:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def step(self, name: str):
        print(f"CI step: {name}", flush=True)
        # A repeated or interrupted gate must not retain a prior PASS verdict.
        if name in self.state["passed"]:
            self.state["passed"].remove(name)
            self.state_file.write_text(json.dumps(self.state))
        node = self.image("node:")
        if name == "config":
            run("python3", str(self.root / "ci/validate_phase1.py"))
            run("python3", "-B", "-m", "unittest", "discover", "-s", str(self.root / "ci"), "-p", "test_registry.py")
        elif name == "secrets":
            self.secret_scan()
        elif name == "backend-dependencies":
            run("docker", "build", "--target", "test", "--tag", self.test_image, str(self.root / "backend"))
        elif name == "backend-lint":
            self.container(self.test_image, "bash", "-ec", "mypy --cache-dir=/reports/mypy-cache app && ruff check app && ruff format app --check")
        elif name in ("backend-unit", "backend-integration"):
            self.backend_tests(unit=name == "backend-unit")
        elif name == "frontend-dependencies":
            self.container(node, "npm", "ci", "--no-audit", memory="2g")
        elif name == "frontend-lint":
            self.container(node, "npm", "run", "lint")
        elif name == "frontend-build":
            self.container(node, "npm", "run", "build", memory="2g")
        elif name == "frontend-tests":
            self.frontend_tests()
        elif name == "backend-security":
            self.container(self.test_image, "bash", "-ec",
                           "uv export --locked --all-groups --no-emit-project --no-hashes -o /reports/backend-requirements.txt >/dev/null && pip-audit --disable-pip --no-deps -r /reports/backend-requirements.txt --format json -o /reports/backend-dependencies.json")
        elif name == "frontend-security":
            self.container(node, "bash", "-ec", "npm audit --audit-level=high --json > /reports/frontend-dependencies.json")
        elif name in ("backend-image", "frontend-image"):
            component = name.removesuffix("-image")
            run("docker", "build", "--target", "runtime", "--tag", f"phase1/{component}:{self.name}", str(self.root / component))
        elif name in ("backend-image-smoke", "frontend-image-smoke"):
            self.smoke_image(name.removesuffix("-image-smoke"))
        elif name in ("backend-image-security", "frontend-image-security"):
            self.scan_image(name.removesuffix("-image-security"))
        elif name == "gitops":
            # Deployment overlays live in the infrastructure repository. Validate its current main
            # with this change's promotion validator, so new operator kinds fail here, not in STAGE.
            with tempfile.TemporaryDirectory(prefix="phase1-infra-") as infra:
                run("git", "clone", "--quiet", "--depth", "1", "--branch", "main", INFRASTRUCTURE_REPO, infra)
                run("sh", str(self.root / "ci/validate.sh"), cwd=Path(infra))
            run("sh", str(self.root / "ci/validate-platform.sh"), cwd=self.root)
            run("python3", "-B", "-m", "unittest", "discover", "-s", str(self.root / "ci"), "-p", "test_*.py")
        elif name == "attest":
            if not RELEASE_GATES.issubset(self.state["passed"]):
                raise RuntimeError("Cannot attest incomplete or failed CI gates")
            # Record every gate that passed, so a full run still lists all 18.
            passed = [gate for gate in GATES if gate in self.state["passed"]]
            for gate, name_xml in TEST_REPORTS.items():
                if gate not in passed:
                    continue
                suites = ET.parse(self.reports / name_xml).getroot()
                cases = suites.findall(".//testcase")
                if suites.tag == "testcase":
                    cases.append(suites)
                if not cases or any(case.find("failure") is not None or case.find("error") is not None for case in cases):
                    raise RuntimeError(f"Missing or failing test results: {name_xml}")
            for report in ("secrets-history.json", "secrets-tree.json"):
                if json.loads((self.reports / report).read_text()):
                    raise RuntimeError(f"Secret findings present: {report}")
            for component in ("backend", "frontend"):
                findings = json.loads((self.reports / f"{component}-image-security.json").read_text())
                if any(result.get("Vulnerabilities") or result.get("Secrets") for result in findings.get("Results", [])):
                    raise RuntimeError(f"Image findings present: {component}")
            manifest = {"version": 1, "sha": self.sha, "gates": sorted(passed), "archives": {}}
            for component in ("backend", "frontend"):
                manifest["archives"][component] = file_sha(self.reports / f"{component}-ci.tar")
            (self.reports / "gates.json").write_text(json.dumps(manifest, indent=2) + "\n")
            (self.reports / "source-sha.txt").write_text(self.sha + "\n")
        else:
            raise ValueError(name)
        if name not in self.state["passed"]:
            self.state["passed"].append(name)
        self.state_file.write_text(json.dumps(self.state))

    def migration_report(self):
        expected = PROFILES["migration"]
        manifest = {
            "profile": "migration", "sha": self.sha, "passed": self.state["passed"],
            "expected": expected,
            "not_completed": [step for step in expected if step not in self.state["passed"]],
            "deferred": [step for step in (*STEPS, "gitops") if step not in expected],
            "publishable": False,
        }
        (self.reports / "migration-result.json").write_text(json.dumps(manifest, indent=2) + "\n")

    def export_images(self):
        # Export exactly the runtime images that passed the functional smoke gates.
        from candidate import write_candidate
        expected = set(PROFILES["migration"])
        if not expected <= set(self.state["passed"]):
            raise RuntimeError("All functional gates must pass before exporting images")
        for component in ("backend", "frontend"):
            run("docker", "save", "-o", str(self.reports / f"{component}-ci.tar"),
                f"phase1/{component}:{self.name}")
        write_candidate(self.reports, self.sha, self.state["passed"],
                        os.environ["JOB_NAME"], os.environ["BUILD_NUMBER"])

    def clean(self):
        # Only resources with this exact build identifier, never unrelated containers/volumes.
        for suffix in ("tool", "backend", "mail", "db", "backend-smoke", "frontend-smoke"):
            subprocess.run(["docker", "rm", "-f", self.name + "-" + suffix], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "network", "rm", self.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(self.private, ignore_errors=True)
        self.prune_images()

    def prune_images(self):
        # Built images leave CI only as archived tarballs, so drop every phase1/* tag on the
        # isolated CI daemon, including leftovers of interrupted builds. Pinned base images
        # (Playwright, Node, PostgreSQL) stay cached; build cache unused for 3 days is dropped.
        quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        listed = subprocess.run(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}",
                                 "--filter", "reference=phase1/*"], capture_output=True, text=True)
        tags = [tag for tag in listed.stdout.split() if tag.startswith("phase1/")]
        if tags:
            subprocess.run(["docker", "image", "rm", "-f", *tags], **quiet)
        subprocess.run(["docker", "image", "prune", "-f"], **quiet)
        subprocess.run(["docker", "builder", "prune", "-f", "--filter", "until=72h"], **quiet)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=[*STEPS, "gitops", "all", "cleanup", "steps", "migration-report", "export-images"])
    parser.add_argument("--profile", choices=sorted(PROFILES), default="full",
                        help="gate set for 'all' and 'steps' (default: full)")
    args = parser.parse_args()
    if args.step == "steps":
        # Lets the Jenkinsfile select its stages from the same definition.
        print("\n".join(PROFILES[args.profile]))
        return
    ci = CI()
    if args.step == "cleanup":
        ci.clean()
    elif args.step == "export-images":
        ci.export_images()
    elif args.step == "migration-report":
        ci.migration_report()
    elif args.step == "all":
        try:
            for step in PROFILES[args.profile]:
                ci.step(step)
        finally:
            ci.clean()
    else:
        ci.step(args.step)


if __name__ == "__main__":
    main()
