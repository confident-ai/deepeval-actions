# Post-verdict risk-gate code scan (fail-open, always exits 0): waits for the verdict, scans
# the diff with DeepTeam for the regressed types, posts findings. --engine runs the DeepTeam
# half of this file inside an isolated venv so it never touches the customer's environment.
import asyncio
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, NamedTuple, Optional, Set, Tuple

POLL_INTERVAL_SECONDS = 20
DEFAULT_MAX_WAIT_MINUTES = 30
HTTP_TIMEOUT_SECONDS = 30
INSTALL_TIMEOUT_SECONDS = 10 * 60
ENGINE_TIMEOUT_SECONDS = 15 * 60
MAX_FILES = 50
MAX_CHUNKS = 40
DEEPTEAM_VERSION = "1.0.9"
# deepteam imports sentry_sdk at module load but never declares it; it arrived via
# deepeval, which dropped the dependency in 4.x, so the venv must add it explicitly.
UNDECLARED_DEEPTEAM_DEPS = ["sentry-sdk"]
GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)

# The scan reads its own key, never the app's: a job-level OPENAI_API_KEY must not
# start paying for scans nobody opted into.
SCAN_API_KEY_ENV = "CONFIDENT_SCAN_API_KEY"
# deepeval is DeepTeam's built-in OpenAI judge: no extra install, no agent CLI.
DEFAULT_PROVIDER = "deepeval"
PROVIDER_KEY_ENV = {
    "deepeval": "OPENAI_API_KEY",
    "codex": "OPENAI_API_KEY",
    "claude-code": "ANTHROPIC_API_KEY",
    "cursor": "CURSOR_API_KEY",
}
PROVIDER_PIP_EXTRA = {
    "deepeval": None,
    "codex": "codex",
    "claude-code": "claude-code",
    "cursor": "cursor",
}
# The backend re-uses one gate run per head sha, so a re-run legitimately finds the
# section already there; only "failed" means the findings did not reach the PR.
OUTCOME_MESSAGE = {
    "posted": "posted to the pull request",
    "already_posted": "already posted by an earlier run, nothing to do",
    "stale": "a newer commit superseded this run, not posted",
    "failed": "the backend could not post them",
}
DEFAULT_EXCLUDES = [
    "tests/*",
    "test/*",
    "*/tests/*",
    "*/test/*",
    "*test_*",
    "*.test.*",
    "*.spec.*",
    "*/migrations/*",
]


def log(message: str) -> None:
    print("eval-gate: " + message, file=sys.stderr, flush=True)


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def ci_context() -> Dict[str, str]:
    if env("GITLAB_CI"):
        return {
            "root": env("CI_PROJECT_DIR") or os.getcwd(),
            "scratch": tempfile.gettempdir(),
            "merge_base_sha": env("CI_MERGE_REQUEST_DIFF_BASE_SHA"),
        }
    return {
        "root": env("GITHUB_WORKSPACE") or os.getcwd(),
        "scratch": env("RUNNER_TEMP") or tempfile.gettempdir(),
        "merge_base_sha": "",
    }


class Backend:
    def __init__(self) -> None:
        self.base = env("CONFIDENT_BASE_URL").rstrip("/")
        self.api_key = env("CONFIDENT_API_KEY")

    def request(
        self, method: str, path: str, payload: Optional[Dict[str, object]] = None
    ) -> Tuple[int, Dict[str, object]]:
        payload_bytes = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=payload_bytes,
            method=method,
            headers={
                "Content-Type": "application/json",
                "confident-api-key": self.api_key,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as r:
                body = json.loads(r.read().decode() or "{}")
                data = body.get("data", body) if isinstance(body, dict) else {}
                return r.status, data if isinstance(data, dict) else {}
        except urllib.error.HTTPError as e:
            return e.code, {}


def read_run_id() -> Optional[str]:
    path = env("CONFIDENT_RISK_RUN_ID_FILE")
    if not path or not os.path.isfile(path):
        return None
    with open(path) as f:
        return f.read().strip() or None


def resolve_provider_config() -> Optional[Tuple[str, str, str]]:
    provider = env("CONFIDENT_SCAN_PROVIDER") or DEFAULT_PROVIDER
    if provider not in PROVIDER_KEY_ENV:
        log("unknown CONFIDENT_SCAN_PROVIDER '" + provider + "' — skipping code scan")
        return None
    api_key = env(SCAN_API_KEY_ENV)
    if not api_key:
        log(SCAN_API_KEY_ENV + " is not set — skipping code scan")
        return None
    return provider, env("CONFIDENT_SCAN_MODEL"), api_key


def poll_scan_spec(
    backend: Backend, run_id: str, on_first_pending: Callable[[], None]
) -> Optional[Dict[str, object]]:
    max_wait_minutes = env("CONFIDENT_SCAN_MAX_WAIT_MINUTES") or str(
        DEFAULT_MAX_WAIT_MINUTES
    )
    deadline = time.monotonic() + float(max_wait_minutes) * 60
    pending_seen = False
    while True:
        try:
            status, data = backend.request(
                "GET", "/v1/eval-gate/runs/" + run_id + "/scan-spec"
            )
        except (urllib.error.URLError, OSError, ValueError) as e:
            status, data = 0, {}
            log("scan-spec unreachable (" + str(e) + "); retrying")
        if 400 <= status < 500:
            log("scan-spec answered " + str(status) + " — skipping code scan")
            return None
        spec_status = data.get("status") if status == 200 else None
        if spec_status == "SKIPPED":
            log("nothing to scan (" + str(data.get("reason", "")) + ")")
            return None
        if spec_status == "READY":
            return data
        if not pending_seen:
            pending_seen = True
            on_first_pending()
        if time.monotonic() >= deadline:
            log("verdict not available within " + max_wait_minutes + " minutes — skipping code scan")
            return None
        time.sleep(POLL_INTERVAL_SECONDS)


def pick_interpreter() -> Optional[str]:
    if (3, 10) <= sys.version_info[:2] < (3, 14):
        return sys.executable
    for name in ("python3.12", "python3.11", "python3.13", "python3.10"):
        found = shutil.which(name)
        if found:
            return found
    return None


def venv_python(venv_dir: str) -> str:
    return os.path.join(venv_dir, "Scripts" if os.name == "nt" else "bin", "python")


def start_venv_install(python: str, venv_dir: str, provider: str) -> subprocess.Popen:
    extra = PROVIDER_PIP_EXTRA[provider]
    default_spec = "deepteam" + ("[" + extra + "]" if extra else "") + "==" + DEEPTEAM_VERSION
    spec = env("CONFIDENT_SCAN_DEEPTEAM_SPEC") or default_spec
    shutil.rmtree(venv_dir, ignore_errors=True)
    script = (
        shlex.quote(python) + " -m venv " + shlex.quote(venv_dir)
        + " && " + shlex.quote(venv_python(venv_dir))
        + " -m pip install -q --disable-pip-version-check --no-input "
        + " ".join(shlex.quote(p) for p in [spec] + UNDECLARED_DEEPTEAM_DEPS)
    )
    return subprocess.Popen(
        script,
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PIP_NO_PYTHON_VERSION_WARNING": "1"},
    )


def is_valid_git_ref_name(ref: str) -> bool:
    return (
        bool(ref)
        and not ref.startswith(("-", "/"))
        and not ref.endswith(("/", ".", ".lock"))
        and ".." not in ref
        and "@{" not in ref
        and "//" not in ref
        and re.search(r"[\s~^:?*\[\\\x00-\x1f\x7f]", ref) is None
    )


def git(root: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", root, *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError("git " + " ".join(args) + " failed: " + result.stderr.strip())
    return result.stdout


def strip_dot_slash(path: str) -> str:
    return path[2:] if path.startswith("./") else path


def parse_hunks(diff_text: str) -> Dict[str, Set[int]]:
    """New-side line numbers per file from a -U0 diff."""
    hunks: Dict[str, Set[int]] = {}
    current: Optional[str] = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path == "/dev/null":
                current = None
                continue
            current = path[2:] if path.startswith("b/") else path
            hunks.setdefault(current, set())
        elif line.startswith("@@") and current is not None:
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if not m:
                continue
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            hunks[current].update(range(start, start + count))
    return hunks


class Diff(NamedTuple):
    scan_root: str
    base_ref: str
    head_ref: str
    files: List[str]
    hunks: Dict[str, Set[int]]


def prepare_diff(ctx: Dict[str, str], spec: Dict[str, object]) -> Diff:
    """Fetches what a shallow checkout lacks and resolves the base/head to diff."""
    root = ctx["root"]
    base_branch = str(spec["baseBranch"])
    head_sha = str(spec["headSha"])
    # Both come from the backend and git reads a leading "-" as an option: never pass them raw.
    if not GIT_SHA_PATTERN.match(head_sha) or not is_valid_git_ref_name(base_branch):
        raise RuntimeError("scan-spec returned an unexpected head sha or base branch; refusing to run git")
    merge_base_sha = ctx["merge_base_sha"] if GIT_SHA_PATTERN.match(ctx["merge_base_sha"]) else ""

    git(root, "fetch", "--depth=1", "origin",
        "+refs/heads/" + base_branch + ":refs/remotes/origin/" + base_branch)
    base_ref = "origin/" + base_branch
    # GitLab exposes the merge base, which excludes changes the target branch picked up since
    # the branch point; GitHub's merge-ref checkout gets the same from the name intersection.
    if merge_base_sha:
        try:
            git(root, "fetch", "--depth=1", "origin", merge_base_sha)
            base_ref = merge_base_sha
        except RuntimeError as e:
            log("merge-base fetch failed (" + str(e) + "); diffing against " + base_ref)

    # Scan the head commit itself so line numbers are head-side even when CI checked out the
    # PR merge commit.
    scan_root, head_ref = root, "HEAD"
    try:
        git(root, "fetch", "--depth=1", "origin", head_sha)
        worktree = os.path.join(ctx["scratch"], "confident-scan-head")
        subprocess.run(["git", "-C", root, "worktree", "remove", "--force", worktree],
                       capture_output=True)
        git(root, "worktree", "add", "--detach", worktree, head_sha)
        scan_root, head_ref = worktree, head_sha
    except RuntimeError as e:
        log("could not check out " + head_sha[:7] + " separately (" + str(e) + "); scanning the "
            "checkout — line numbers may be offset if the base branch touched the same files")

    changed_files_head = set(git(root, "diff", "--name-only", base_ref + ".." + head_ref).splitlines())
    changed_files_net = set(git(root, "diff", "--name-only", base_ref + "..HEAD").splitlines())
    files = (
        sorted(changed_files_head & changed_files_net)
        if head_ref != "HEAD"
        else sorted(changed_files_head)
    )
    hunks = parse_hunks(git(root, "diff", "-U0", base_ref + ".." + head_ref, "--", *files)) if files else {}
    return Diff(scan_root, base_ref, head_ref, files, hunks)


def build_instruction(spec: Dict[str, object]) -> str:
    def rate(value: object) -> str:
        return "n/a" if value is None else str(round(float(value) * 100)) + "%"

    lines = [
        "- " + str(r["vulnerability"]) + " / " + str(r["vulnerabilityType"])
        + ": pass rate " + rate(r["baselineRate"]) + " -> " + rate(r["currentRate"])
        for r in spec["regressedTypes"]
    ]
    return (
        "This change regressed the following adversarial attack categories in a "
        "live red-team gate (pass rate against the same frozen attack suite, "
        "before -> after). Look specifically for code changes that could explain "
        "these regressions and only report the listed subtypes:\n" + "\n".join(lines)
    )


def run_engine(
    python: str, diff: Diff, spec: Dict[str, object], provider: str, model: str,
    api_key: str,
) -> Dict[str, object]:
    payload: Dict[str, object] = {
        "root": diff.scan_root,
        "base": diff.base_ref,
        "head": diff.head_ref,
        "files": diff.files,
        "spec": spec,
        "provider": provider,
        "model": model,
        "min_severity": env("CONFIDENT_SCAN_MIN_SEVERITY") or "low",
    }
    result = subprocess.run(
        [python, os.path.abspath(__file__), "--engine"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=ENGINE_TIMEOUT_SECONDS,
        cwd=diff.scan_root,
        env={**os.environ, PROVIDER_KEY_ENV[provider]: api_key},
    )
    if result.stderr.strip():
        log("engine: " + result.stderr.strip()[-2000:])
    if result.returncode != 0:
        raise RuntimeError("scan engine exited " + str(result.returncode))
    return json.loads(result.stdout or "{}")


def post_findings(
    backend: Backend, run_id: str, findings: List[Dict[str, object]], provider: str,
    model: str, error: Optional[str],
) -> None:
    payload: Dict[str, object] = {"findings": findings, "provider": provider}
    if model:
        payload["model"] = model
    if error:
        payload["error"] = error[:1500]
    status, data = backend.request(
        "POST", "/v1/eval-gate/runs/" + run_id + "/scan-findings", payload
    )
    if status == 200:
        log(
            "submitted " + str(len(findings)) + " finding(s) — "
            + OUTCOME_MESSAGE.get(str(data.get("outcome")), "unknown outcome")
        )
    else:
        log("scan-findings answered " + str(status))


def main() -> None:
    run_id = read_run_id()
    if not run_id:
        # Metrics-only gate, branch push, or the runner didn't submit.
        return
    provider_config = resolve_provider_config()
    if not provider_config:
        return
    provider, model, api_key = provider_config
    backend = Backend()
    if not backend.base or not backend.api_key:
        log("CONFIDENT_BASE_URL/CONFIDENT_API_KEY missing — skipping code scan")
        return

    ctx = ci_context()
    venv_dir = os.path.join(ctx["scratch"], "confident-scan")
    install_proc: Optional[subprocess.Popen] = None

    def start_install() -> None:
        nonlocal install_proc
        python = pick_interpreter()
        if not python:
            log("code scan needs Python 3.10-3.13 (found "
                + str(sys.version_info[0]) + "." + str(sys.version_info[1]) + ") — skipping")
            return
        install_proc = start_venv_install(python, venv_dir, provider)

    # The install overlaps the verdict wait; a SKIPPED first answer never creates the venv.
    spec = poll_scan_spec(backend, run_id, start_install)
    if not spec:
        return
    log("risk gate regressed on " + ", ".join(spec["scan"]["vulnerabilityTypes"])
        + " — scanning the diff")

    try:
        if install_proc is None:
            start_install()
        if install_proc is None:
            return
        _, stderr = install_proc.communicate(timeout=INSTALL_TIMEOUT_SECONDS)
        if install_proc.returncode != 0:
            raise RuntimeError("deepteam install failed: " + (stderr or "").strip()[-1500:])

        diff = prepare_diff(ctx, spec)
        if not diff.files:
            log("no changed files to scan")
            post_findings(backend, run_id, [], provider, model, None)
            return
        truncation = None
        if len(diff.files) > MAX_FILES:
            truncation = "scan truncated to " + str(MAX_FILES) + " of " + str(len(diff.files)) + " changed files"
            diff = diff._replace(files=diff.files[:MAX_FILES])

        result = run_engine(venv_python(venv_dir), diff, spec, provider, model, api_key)
        findings = result.get("findings") or []
        findings_in_diff = [
            f for f in findings
            if f.get("lineStart") in diff.hunks.get(strip_dot_slash(str(f.get("filePath", ""))), set())
        ]
        if len(findings_in_diff) < len(findings):
            log(str(len(findings) - len(findings_in_diff)) + " finding(s) outside the diff dropped")
        errors = [e for e in (result.get("error"), truncation) if e]
        post_findings(backend, run_id, findings_in_diff, provider, model, "; ".join(errors) or None)
    except Exception as e:  # fail-open by design
        log("code scan failed: " + str(e))
        try:
            post_findings(backend, run_id, [], provider, model, str(e))
        except Exception as post_error:
            log("could not report the failure: " + str(post_error))


def engine_main() -> None:
    """Runs inside the venv: stdin = payload, stdout = one JSON object."""
    payload = json.load(sys.stdin)
    stdout, sys.stdout = sys.stdout, sys.stderr  # keep library chatter off the JSON channel
    os.environ.setdefault("CODE_CONTEXT_LIMIT", "40000")
    from deepteam.code_scanner import (
        CodeScanner,
        KNOWN_VULNERABILITIES,
        build_engine,
        collect_changed_files,
        filter_by_severity,
    )

    spec = payload["spec"]
    categories = [v for v in spec["scan"]["vulnerabilities"] if v in KNOWN_VULNERABILITIES]
    if not categories:
        stdout.write(json.dumps({
            "findings": [],
            "error": "no scannable vulnerability categories: " + str(spec["scan"]["vulnerabilities"]),
        }))
        return

    # include= is glob-matched, so a literal path like app/[id]/route.ts must be escaped.
    chunks = collect_changed_files(
        payload["root"], base=payload["base"], head=payload["head"],
        include=[glob.escape(f) for f in payload["files"]], exclude=DEFAULT_EXCLUDES,
    )
    error = None
    if len(chunks) > MAX_CHUNKS:
        error = "scan truncated to " + str(MAX_CHUNKS) + " of " + str(len(chunks)) + " code chunks"
        chunks = chunks[:MAX_CHUNKS]
    findings = []
    if chunks:
        engine = build_engine(payload["provider"], payload["model"] or None)
        # build_engine returns None for the deepeval provider — it judges with its
        # own model, which build_engine drops, so the override is passed here.
        judge_model = (payload["model"] or None) if engine is None else None
        scanner = CodeScanner(
            model=judge_model,
            vulnerabilities=categories,
            instruction=build_instruction(spec),
            engine=engine,
            max_concurrent=4,
        )
        findings = filter_by_severity(asyncio.run(scanner.a_scan(chunks)), payload["min_severity"])
    # codeSnippet stays on the runner: only a finding's location and text leave it.
    stdout.write(json.dumps({
        "findings": [f.model_dump(exclude_none=True, exclude={"codeSnippet"}) for f in findings],
        "error": error,
    }))


if __name__ == "__main__":
    if "--engine" in sys.argv[1:]:
        engine_main()
    else:
        try:
            main()
        except Exception as e:  # fail-open by design
            print("eval-gate: unexpected error: " + str(e), file=sys.stderr)
        sys.exit(0)
