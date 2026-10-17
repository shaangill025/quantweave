# Sandbox qualification (QUAL17) — owner runbook

QUAL17 ("gVisor and single-machine isolation") is qualified only by a run on the
owner's reference Linux host. The script does not mark QUAL17: the owner pastes its
evidence block back, it is recorded in `docs/receipts/T007.md`, and QUAL17 changes only
by owner decision on that evidence. Until then the sandbox gate (`sandbox_unqualified`,
G11) stays blocked. A development VM run is not a qualification.

## Prerequisites
- The reference host (x86_64 or aarch64) with sudo. Kernel, distro, CPU and RAM are
  recorded, not checked against a list.
- cgroup v2 with the `cpu`, `memory` and `pids` controllers, or cgroup v1 with the
  `memory`, `pids`, `cpu` and `cpuacct` hierarchies (`stat -fc %T /sys/fs/cgroup`
  prints `cgroup2fs` on v2). The adapter's v2 path is unit-tested only, so this run is
  its first real exercise.
- Swap off, or swap accounting on; otherwise every sandbox run fails closed.
- A clean checkout of the commit to qualify, with no modified or untracked files, and
  `uv sync` already run as your normal user.
- runsc: the official gVisor release. Without `QW_SANDBOX_RUNSC`, the script prints the
  manual steps (with `sha512sum -c`). Alternatively `--fetch-runsc` downloads release
  `20261005.0` (`--runsc-release` overrides), verifies its sha512 and prints the
  version. That flag is the only network access. runsc must be owned by root or you,
  and must not be group- or world-writable.
- The rootfs: the pinned `busybox:1.37.0@sha256:bdf57e52…5013f82e` exported to a
  directory (the script prints the steps). Only its tree digest is **recorded**. Pass
  `--expect-rootfs-digest <sha256_tree>` to require a known digest: a mismatch fails.
  The digest (`quantweave.rootfs_tree/3`, from `tools/candidate_rootfs.py`, as in CI)
  of the T007 busybox export is `80e3966b…bf4d22b9` (earlier values are superseded,
  see the receipt). Busybox stays the qualification rootfs: two real-runtime tests
  probe with its `wget`, `nslookup` and `mount`.

## Commands
```sh
git status --short --untracked-files=all    # must print nothing
sudo env "PATH=$PATH" \
  QW_SANDBOX_RUNSC=/var/tmp/qw-sandbox/gvisor/runsc \
  QW_SANDBOX_ROOTFS=/var/tmp/qw-sandbox/rootfs-busybox \
  uv run --frozen --no-sync python tools/qualify_sandbox.py
```
The work dir (`/var/tmp/qw-sandbox`, or `--work-dir`) must be a real directory owned by
root or you that is not group- or world-writable; otherwise the run is refused. Each
run writes its logs and downloads to a fresh private `run-*` dir inside it. The run
takes a few minutes: it runs the sandbox test files with `QW_REQUIRE_GVISOR=1`, first
on systrap and then on ptrace. Keep any custom `--work-dir`, and the directory that holds runsc, under parents only you can write to, such as the default under the sticky `/var/tmp`. The script checks the directory itself, not its ancestors.

## The result
`"host_passes_sandbox_suite": true` only if all of these hold:
- on at least one platform, pytest printed its summary with exit code 0 and zero
  failed, errors or skipped, and every collected real-runtime (`gvisor`) test passed;
- cgroup support is available;
- the script ran as root, with a safe work dir and a usable runsc and rootfs (and the
  expected rootfs digest, if one was given);
- the tree was clean at a known commit.

Otherwise it is false and `reasons` says why. The exit code is 0 or 1 to match.

## What to paste back, and how it is recorded
Paste everything from `----- BEGIN QW SANDBOX QUALIFICATION EVIDENCE -----` to
`----- END … -----`, plus the summary lines after it. If a platform failed, also attach
its `pytest-<platform>.log`. The block holds no secrets, but check the paths in it
before sharing. The orchestrator appends it verbatim to the T007 receipt, and a
cross-model reviewer checks it. A passing block is evidence, not authority: G11, hosted
workers and the other open T007 items still need their own evidence.

## The Python candidate rootfs (owner decision 13)
CI jobs `python-base-image` and `candidate-rootfs` build it from `python:3.13-slim`,
pinned in `tools/candidate_rootfs.json` by its linux/amd64 manifest digest (the same
bytes on Docker Hub and in the GHCR mirror). While a pin is `null` the job prints both
values and fails; it may fail at the audit first, so fix the manifest, never the audit.
The pin commit changes only `base_manifest_digest`, `tree_sha256` and `pinned_from`.
The new GHCR `python` package may need the one-time "Manage Actions access" grant that
`postgres` needed, or the pull returns 403. Docker-injected stubs (such as `etc/mtab`)
can differ between Docker versions, so confirm the pin by a local x86_64 rebuild:
```sh
digest=$(python3 tools/candidate_rootfs.py show base_manifest_digest)
dir=/var/tmp/qw-sandbox/rootfs-python && sudo mkdir -m 755 "$dir"  # root mode counts
cid=$(docker create --platform linux/amd64 "docker.io/library/python@$digest")
docker export "$cid" | sudo tar -xpf - --numeric-owner -C "$dir" && docker rm "$cid"
for c in prune audit; do sudo python3 tools/candidate_rootfs.py $c "$dir"; done
sudo python3 tools/candidate_rootfs.py digest "$dir" --expect "$(python3 \
  tools/candidate_rootfs.py show tree_sha256)"
```
Pass the same value as `--expect-rootfs-digest`. The audit forbids `wget` and
`nslookup`, so the egress probe test fails on this rootfs; QUAL17 stays on busybox.
