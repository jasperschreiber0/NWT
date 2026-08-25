#!/usr/bin/env python3
"""
scripts/verify_deployment.py

Deterministic deployment verification for the execution engine. Answers,
without relying on anyone's memory:

  1. What commit is deployed (this checkout's HEAD)?
  2. What branch is this checkout on?
  3. Is this working tree dirty?
  4. What does crontab actually invoke, and does it point at THIS directory?
  5. Is a second execution_engine cron entry (or process) pointing anywhere
     else, i.e. two competing production paths?
  6. Is there a stray execution_engine process not matching this commit?

Exit code 0 = all checks pass. Non-zero = at least one check failed, with
every failure printed (not just the first). Run from the production
checkout root, e.g.:

    python3 scripts/verify_deployment.py
    python3 scripts/verify_deployment.py --expect-commit 3733362...

This is deliberately dependency-free (stdlib only) so it can run anywhere
without needing the project's own venv/requirements first.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path


def run(cmd: list) -> tuple:
    """Run a command, return (returncode, stdout, stderr). Never raises."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out: {' '.join(cmd)}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-commit", default=None,
                        help="Fail if HEAD does not start with this SHA prefix.")
    parser.add_argument("--expect-branch", default=None,
                        help="Fail if the current branch name differs (informational for detached HEAD).")
    args = parser.parse_args()

    failures = []
    repo_root = Path(__file__).resolve().parent.parent

    print(f"Checking deployment at: {repo_root}")

    # --- 1/3. Commit + dirty tree ---
    rc, head, err = run(["git", "-C", str(repo_root), "rev-parse", "HEAD"])
    if rc != 0:
        failures.append(f"Could not read HEAD commit: {err}")
        head = None
    else:
        print(f"HEAD commit:       {head}")

    rc, branch, _ = run(["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"])
    print(f"Branch:             {branch}")
    if branch == "HEAD":
        print("  (detached HEAD — expected for a deploy checkout built by cherry-pick, not a symptom by itself)")
    elif args.expect_branch and branch != args.expect_branch:
        failures.append(f"Branch mismatch: expected '{args.expect_branch}', got '{branch}'")

    rc, dirty, _ = run(["git", "-C", str(repo_root), "status", "--porcelain"])
    if dirty:
        failures.append(f"Working tree is DIRTY ({len(dirty.splitlines())} changed path(s)):\n" +
                        "\n".join(f"    {line}" for line in dirty.splitlines()))
    else:
        print("Working tree:       clean")

    if args.expect_commit and head and not head.startswith(args.expect_commit):
        failures.append(f"Deployed commit does not match expectation: "
                        f"HEAD={head}, expected prefix={args.expect_commit}")

    # --- 4/5. Crontab points here, and only here ---
    rc, crontab_out, crontab_err = run(["crontab", "-l"])
    if rc != 0:
        failures.append(f"Could not read crontab: {crontab_err or 'unknown error'}")
        crontab_out = ""

    engine_lines = [l for l in crontab_out.splitlines() if "engine.py" in l and "execution_engine" in l]
    if not engine_lines:
        failures.append("No execution_engine cron entry found at all — nothing is scheduled to run it.")
    elif len(engine_lines) > 1:
        failures.append(f"Multiple execution_engine cron entries found ({len(engine_lines)}) — "
                        f"competing production paths:\n" +
                        "\n".join(f"    {l}" for l in engine_lines))
    else:
        line = engine_lines[0]
        m = re.search(r"cd\s+(\S+?)/execution\s*&&", line)
        cron_dir = m.group(1) if m else None
        if cron_dir is None:
            failures.append(f"Could not parse target directory out of cron line: {line}")
        elif Path(cron_dir).resolve() != repo_root.resolve():
            failures.append(f"Cron points at '{cron_dir}', not this checkout ({repo_root}) — "
                            f"this script and the running cron job disagree about what production is.")
        else:
            print(f"Cron target:        {cron_dir} (matches this checkout)")

    # --- 6. No stray process running a different checkout's engine.py ---
    rc, ps_out, _ = run(["ps", "-eo", "pid,args"])
    engine_procs = [l for l in ps_out.splitlines() if "engine.py" in l and "grep" not in l]
    if engine_procs:
        other = [l for l in engine_procs if str(repo_root) not in l]
        if other:
            failures.append(f"Found execution_engine process(es) NOT running from this checkout:\n" +
                            "\n".join(f"    {l}" for l in other))
        for l in engine_procs:
            print(f"Running process:    {l.strip()}")
    else:
        print("Running process:    none right now (expected between cron ticks — engine.py is a "
              "one-shot process, not a daemon)")

    print()
    if failures:
        print(f"DEPLOYMENT VERIFICATION FAILED ({len(failures)} issue(s)):")
        for i, f in enumerate(failures, 1):
            print(f"  {i}. {f}")
        return 1

    print("DEPLOYMENT VERIFICATION PASSED — this checkout is the single, unambiguous production path.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
