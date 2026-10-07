# Temporary disk-maintenance image

Not a production replacement. No Render deployment is performed by this branch.
Parent is the exact certified production manifest `sha256:4f11f091d8ba77192a81569b3e85e101dcccc329c641eddeabd160b13be6fb49`.
The build adds only maintenance scripts and Render SSH metadata; protected application trees are not edited.

The entrypoint opens `/state` without following symlinks, verifies its mount ID
against mountinfo, rejects any directory entries, adjusts only that directory
to 501:1000/0700, verifies it, drops all supplementary groups and all saved UID/GID
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
mount, unexpected-file rejection and read-only ownership failure. Complete file
bytes, links, ownership, modes, timestamps and PAX metadata for all three protected
trees must match the parent. Two independent OCI builds must have identical
manifest/config/layer/diffIDs before publication of the tested artifact.

Privileged operators can still override a container entrypoint or mutate state;
this image does not claim protection against its administrative root operator.
Do not run against a populated disk. No production secrets are used in CI.
