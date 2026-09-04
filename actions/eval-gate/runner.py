import json
import os
import sys
import urllib.parse
import urllib.request
from enum import Enum
from typing import Dict, List, Optional

BASE = os.environ["CONFIDENT_BASE_URL"].rstrip("/")
API_KEY = os.environ.get("CONFIDENT_API_KEY", "")
# Fallback-only since v2: the serve-config endpoint is authoritative for which
# gates run (and which dataset the metrics gate pulls); these YAML inputs keep
# older/on-prem backends without that endpoint working unchanged.
ALIAS = os.environ.get("DATASET_ALIAS") or ""
VERSION = os.environ.get("DATASET_VERSION") or "latest"
PROVIDER = os.environ.get("CONFIDENT_PROVIDER") or "GITHUB"


class RefType(str, Enum):
    PULL_REQUEST = "PULL_REQUEST"
    BRANCH = "BRANCH"


def headers() -> Dict[str, str]:
    return {"Content-Type": "application/json", "confident-api-key": API_KEY}


def git_context() -> Dict[str, object]:
    repo = os.environ.get("REPO", "/")
    owner, _, name = repo.partition("/")
    pr = os.environ.get("PR_NUMBER") or ""
    return {
        "repoOwner": owner,
        "repoName": name,
        "repoId": int(os.environ.get("REPO_ID") or 0),
        "refType": (RefType.PULL_REQUEST if pr else RefType.BRANCH).value,
        "prNumber": int(pr) if pr else None,
        "headSha": os.environ.get("HEAD_SHA") or "",
        "baseBranch": os.environ.get("BASE_BRANCH") or "",
    }


def post(payload: Dict[str, object]) -> Dict[str, object]:
    req = urllib.request.Request(
        BASE + "/v1/eval-gate/runs",
        data=json.dumps(payload).encode(),
        headers=headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def fetch_gates() -> List[Dict[str, object]]:
    query = urllib.parse.urlencode(
        {"provider": PROVIDER, "repoId": git_context()["repoId"]}
    )
    req = urllib.request.Request(
        BASE + "/v1/eval-gate/config?" + query, headers=headers(), method="GET"
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.loads(r.read().decode())
    body = body.get("data", body)
    gates = body.get("gates")
    if not isinstance(gates, list):
        raise ValueError("malformed config response")
    return gates


def pull_goldens(alias: str, version: str) -> List[Dict[str, object]]:
    url = BASE + "/v1/datasets/" + alias + "?version=" + version
    req = urllib.request.Request(url, headers=headers(), method="GET")
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.loads(r.read().decode())
    body = body.get("data", body)
    return body.get("goldens", [])


def report_gate_crash(kind: Optional[str], error: str) -> None:
    print("eval-gate: app failure: " + str(error), file=sys.stderr)
    payload: Dict[str, object] = {
        "git": git_context(),
        "execution": {"crashed": True, "error": str(error)},
    }
    if kind is not None:
        payload["kind"] = kind
    try:
        post(payload)
    except Exception as e:
        print("eval-gate: failed to report crash: " + str(e), file=sys.stderr)


def build_metrics_test_cases(run, goldens) -> List[Dict[str, object]]:
    test_cases = []
    for golden in goldens:
        inp = golden.get("input")
        output = str(run(inp))
        case = {"input": inp, "actualOutput": output}
        if golden.get("expectedOutput") is not None:
            case["expectedOutput"] = golden["expectedOutput"]
        if golden.get("retrievalContext") is not None:
            case["retrievalContext"] = golden["retrievalContext"]
        if golden.get("context") is not None:
            case["context"] = golden["context"]
        test_cases.append(case)
    return test_cases


def run_metrics_gate(run, gate: Dict[str, object]) -> bool:
    alias = str(gate.get("datasetAlias") or ALIAS)
    version = str(gate.get("datasetVersion") or "latest")
    try:
        goldens = pull_goldens(alias, version)
    except Exception as e:
        report_gate_crash("METRICS", "could not pull dataset: " + str(e))
        return False

    try:
        test_cases = build_metrics_test_cases(run, goldens)
    except Exception as e:
        report_gate_crash("METRICS", "app raised while producing outputs: " + str(e))
        return False

    try:
        resp = post(
            {"kind": "METRICS", "git": git_context(), "llmTestCases": test_cases}
        )
    except Exception as e:
        print("eval-gate: failed to submit metrics results: " + str(e), file=sys.stderr)
        return False
    print(json.dumps(resp))
    return True


def record_risk_run_id(resp: Dict[str, object]) -> None:
    # Hands the run id to scan.py (same job) for PR runs only: branch baselines
    # never scan, so scan.py must not wait on them.
    path = os.environ.get("CONFIDENT_RISK_RUN_ID_FILE")
    if not path or git_context()["prNumber"] is None:
        return
    try:
        data = resp.get("data")
        run_id = data.get("evalGateRunId") if isinstance(data, dict) else None
        if run_id:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(str(run_id))
    except Exception as e:
        print("eval-gate: could not record risk run id: " + str(e), file=sys.stderr)


def run_risk_gate(run, gate: Dict[str, object]) -> bool:
    # An attack that makes run() raise becomes an errored entry, not a CI
    # failure — the backend excludes errored attacks from pass rates.
    attack_responses = []
    for attack in gate.get("attacks") or []:
        entry: Dict[str, object] = {
            "attackId": attack.get("id"),
            "order": attack.get("order"),
        }
        try:
            entry["actualOutput"] = str(run(attack.get("input")))
        except Exception as e:
            entry["error"] = str(e)
        attack_responses.append(entry)

    try:
        resp = post(
            {
                "kind": "RISK_ASSESSMENT",
                "git": git_context(),
                "riskSubmission": {
                    "attackSuiteId": gate.get("attackSuiteId"),
                    "attackResponses": attack_responses,
                },
            }
        )
    except Exception as e:
        print("eval-gate: failed to submit risk results: " + str(e), file=sys.stderr)
        return False
    print(json.dumps(resp))
    record_risk_run_id(resp)
    return True


def legacy_report_crash(error: str) -> None:
    report_gate_crash(None, error)
    sys.exit(1)


def legacy_main() -> None:
    """The pre-serve-config flow, byte-identical payloads (no `kind`): pull the
    YAML-pinned dataset, run every golden, submit. Used when the backend has no
    /v1/eval-gate/config endpoint (older or lagging on-prem deployments)."""
    try:
        from confident_eval import run
    except Exception as e:
        legacy_report_crash("could not import confident_eval.run: " + str(e))
        return

    try:
        goldens = pull_goldens(ALIAS, VERSION)
    except Exception as e:
        legacy_report_crash("could not pull dataset: " + str(e))
        return

    try:
        test_cases = build_metrics_test_cases(run, goldens)
    except Exception as e:
        legacy_report_crash("app raised while producing outputs: " + str(e))
        return

    try:
        resp = post({"git": git_context(), "llmTestCases": test_cases})
    except Exception as e:
        legacy_report_crash("failed to submit results: " + str(e))
        return
    print(json.dumps(resp))


def main() -> None:
    sys.path.insert(0, os.getcwd())

    try:
        gates = fetch_gates()
    except Exception as e:
        print(
            "eval-gate: config endpoint unavailable (" + str(e) + ")",
            file=sys.stderr,
        )
        if ALIAS:
            print("eval-gate: falling back to the dataset pinned in the workflow")
            legacy_main()
            return
        print(
            "eval-gate: no dataset_alias input to fall back to; cannot run",
            file=sys.stderr,
        )
        sys.exit(1)

    if not gates:
        # Config state must never fail a customer's CI.
        print("eval-gate: no gates configured for this repository; nothing to run")
        return

    try:
        from confident_eval import run
    except Exception as e:
        error = "could not import confident_eval.run: " + str(e)
        print("eval-gate: app failure: " + error, file=sys.stderr)
        for gate in gates:
            report_gate_crash(str(gate.get("kind")), error)
        sys.exit(1)

    # Each gate submits independently: a failure in one must not eat the other.
    any_failed = False
    for gate in gates:
        kind = gate.get("kind")
        if kind == "METRICS":
            if not run_metrics_gate(run, gate):
                any_failed = True
        elif kind == "RISK_ASSESSMENT":
            if not run_risk_gate(run, gate):
                any_failed = True
        else:
            print("eval-gate: skipping unknown gate kind: " + str(kind))

    if any_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
