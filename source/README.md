# Temporary disk-maintenance image

Not a production replacement. No Render deployment is performed by this branch.
Parent is the exact certified production manifest `sha256:4f11f091d8ba77192a81569b3e85e101dcccc329c641eddeabd160b13be6fb49`.
The build adds only maintenance scripts and Render SSH metadata; protected application trees are not edited.

The entrypoint opens `/state` with O_NOFOLLOW and O_NOATIME, verifies its mount ID
against mountinfo, and adjusts only an EMPTY directory to 501:1000/0700.
For populated mounts it requires that identity/mode already match, without
repairing, opening child files, or changing any state. It checks directory entry
stability and mount identity again, then drops all supplementary groups and all saved UID/GID
authority, verifies root cannot be regained, and execs a credential-free HTTP
process. No database or broker module is imported. No recursive mutation.

Administrative SSH remains privileged, deliberately separate from the HTTP
server. `/root/.ssh` is empty mode0700. The root shadow field `NP` is a deliberately
invalid crypt hash: no password credential is installed, and it is not a locked
account marker that would prevent Render-managed public-key access. No SSH daemon
or key is included. Actual Render SSH integration remains a future deployment check.

Future operator helper (not an automatic startup action):
`/opt/venv/bin/python -I -B /usr/local/lib/flowsignal-maintenance/cutover_helper.py inventory|copy|verify ...`
Only these subcommands are accepted. The helper drops to 501:1000 before exec,
uses the existing certified module, and inherits only explicit CUTOVER_DATABASE_URL,
LANG and TZ. No DATABASE_URL fallback. Certification invokes parser help only,
not cutover operations. A test observes the real privilege drop at the exec boundary.

Network-disabled containers and disposable mounts test real startup, health,
process credentials, idempotence (including unchanged directory ctime), missing
mount, populated wrong-owner/mode rejection and read-only ownership failure. The
mounted launcher tests additionally cover populated read-only mounts, symlink
substitution, strict-atime non-mutation of all synthetic state, `.cutover`, and
`.recovery-generations`. SYS_ADMIN/SYS_PTRACE in that disposable test container
are for fixture mounts/process inspection only; HTTP runs with zero effective
capabilities. Complete file
bytes, links, ownership, modes, timestamps and PAX metadata for all three protected
trees must match the parent. Two independent OCI builds must have identical
manifest/config/layer/diffIDs before publication of the tested artifact.

Privileged operators can still override a container entrypoint or mutate state;
this image does not claim protection against its administrative root operator.
No production secrets are used in CI.

The launcher adaptation is not a new image certificate. This directory still
pins the historical production parent above. A successor maintenance build must
be derived from the newly certified production digest, preserving its protected
trees. Native AMD64 two-build, migration 0031, and termination-adapter container
certification remain required. No production invocation or publication is
authorized by these local tests. The reviewed termination API remains an
explicit administrative Python API, not a launcher action or new CLI.
