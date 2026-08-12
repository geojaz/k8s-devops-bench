# Integration branch

`ehole/integration` is a local integration of unmerged work, built while
upstream approvers are away for a week. It is a working branch, not a PR:
do not push it upstream. The individual branches below each still have
their own PR against `geojaz/k8s-devops-bench` and should be reviewed and
merged there in the normal way; this branch just lets development continue
on top of all of them at once in the meantime.

## Base

Built from `main` at `da52ebc` (`tests: restore the package logger between
tests`), which is `upstream/main` plus 8 local commits not yet merged
upstream.

## Branches merged, in order

| # | Branch | PR | Description |
|---|--------|----|----|
| 1 | `ehole/subprocess-stdin-devnull` | #71 | Never let a child process inherit the operator's stdin. |
| 2 | `ehole/agent-sandbox` | #72 | Optionally run CLI agents in a container with a scoped cluster identity. |
| 3 | `ehole/gitignore-terraform-artifacts` | #73 | Ignore local OpenTofu state and working directories. |
| 4 | `ehole/preserve-parallel-child-results` | #68 | Preserve a parallel child's observed result at the deadline instead of discarding it. |
| 5 | `ehole/verification-hold-mode` | #59 | Implement hold mode for verification entries. |
| 6 | `ehole/verifier-http-probe` | #55 | Add the http_probe verifier and the run_pod kubectl helper. |
| 7 | `ehole/verifier-external-http-probe` | #54 | Add the external_http_probe verifier. |
| 8 | `ehole/verifier-git-repo-sync` | #57 | Add the git_repo_sync verifier. |
| 9 | `ehole/opa-repo-remediated-objective` | (none) | Re-enable the repo-remediated objective in the opa-remediation task, built on top of #57. |
| 10 | `ehole/minted-scratch-root` | #60 | Mint the GitOps repo under an owned scratch root instead of a free-form path. |

Each merge used `git merge --no-ff` so the composition stays visible in the
log. One additional commit follows the merges:

- `verification: pin the new probe verifiers to the run's cluster too` --
  the three new verifiers (http_probe, external_http_probe, git_repo_sync)
  were written before main's cluster-pinning fix (`9a099e7`) and did not
  forward the `context` field. This commit closes that gap so the new
  verifiers pin to the run's cluster the same way the rest of the harness
  does.

## Excluded on purpose

`ehole/deterministic-verification-mvp` is deliberately **not** included.
PR #47 was already squash-merged into upstream `main` as `0022cd6`
(`feat(verification): deterministic verification scores beside the LLM
judge`), so the branch is superseded, not pending.

## How to refresh

1. Fetch and merge the latest `upstream/main` into this branch:
   ```
   git fetch geojaz main
   git merge geojaz/main
   ```
2. For any of the 10 branches above that picked up new commits since this
   was built, re-merge it the same way:
   ```
   git merge --no-ff ehole/<branch-name>
   ```
3. Re-run `uv run pytest tests/ -q` after each merge and resolve conflicts
   using the same reasoning applied the first time: keep both sides'
   substance rather than picking one, since these branches are independent
   features landing in the same files.
