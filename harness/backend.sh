#!/usr/bin/env bash
set -euo pipefail
gate=${CERTIFICATION_GATE:-backend_gate}
python -B harness/test_auth_overlay.py
case "$gate" in backend_gate|broad_gate|diagnostic_gate|logging_probe_gate|native_reference_gate) ;; *) exit 1 ;; esac
test "$(uname -s)" = Linux
test "$(uname -m)" = x86_64
test "$(python -c 'import platform; print(platform.python_version())')" = 3.14.3
test "$(docker info --format '{{.Architecture}}')" = x86_64
python -B harness/verify_source.py source
python -B harness/test_fixture.py
python -B harness/test_git_shim.py
cert_root=$(mktemp -d /tmp/fs-amd64-cert.XXXXXXXX)
pg_root=$(mktemp -d /tmp/fs-verifier-pg.cert.XXXXXXXX)
pg_bin=$(pg_config --bindir)
identity_created=0
group_created=0
sudoers_created=0
cleanup() {
  original_status=$?
  trap - EXIT
  set +e
  cleanup_status=0
  if [ "$sudoers_created" = 1 ]; then
    sudo rm -f -- /etc/sudoers.d/flowsignal-cert-501 /etc/sudoers.d/.flowsignal-cert-501.pending || cleanup_status=1
    sudo test ! -e /etc/sudoers.d/flowsignal-cert-501 || cleanup_status=1
    sudo test ! -e /etc/sudoers.d/.flowsignal-cert-501.pending || cleanup_status=1
  fi
  if [ "$identity_created" = 1 ]; then
    if getent passwd flowsignal-cert-501 >/dev/null; then
      sudo userdel flowsignal-cert-501 || cleanup_status=1
    fi
    if getent passwd 501 >/dev/null; then cleanup_status=1; fi
    if getent passwd flowsignal-cert-501 >/dev/null; then cleanup_status=1; fi
  fi
  if [ "$group_created" = 1 ]; then
    if getent group flowsignal-cert-1000 >/dev/null; then
      sudo groupdel flowsignal-cert-1000 || cleanup_status=1
    fi
    if getent group 1000 >/dev/null; then cleanup_status=1; fi
  fi
  "$pg_bin/pg_ctl" -D "$pg_root/data" -m fast stop >/dev/null 2>&1 || true
  # Exact mktemp roots owned by this job; no source worktree removal.
  sudo rm -rf -- "$cert_root" "$pg_root" || cleanup_status=1
  python -B harness/verify_source.py source > evidence/checkout-after-cleanup.json || cleanup_status=1
  python -B - "$identity_created" "$group_created" "$cleanup_status" <<'PY' > evidence/identity-cleanup.json
import json,sys
print(json.dumps({'temporary_identity_created':sys.argv[1]=='1',
 'temporary_group_created':sys.argv[2]=='1','cleanup_passed':sys.argv[3]=='0'}))
PY
  if [ "$cleanup_status" != 0 ]; then exit 1; fi
  exit "$original_status"
}
trap cleanup EXIT
mkdir -p evidence "$cert_root/evidence"
# Reject collisions before any passwd/sudoers mutation. No existing identity is reused.
if getent passwd 501 >/dev/null || getent passwd flowsignal-cert-501 >/dev/null; then
  echo UNEXPECTED_PREEXISTING_UID501_IDENTITY >&2; exit 1
fi
sudo test ! -e /etc/sudoers.d/flowsignal-cert-501
sudo test ! -e /etc/sudoers.d/.flowsignal-cert-501.pending
test ! -e /nonexistent
if ! getent group 1000 >/dev/null; then
  if getent group flowsignal-cert-1000 >/dev/null; then exit 1; fi
  group_created=1
  sudo groupadd --gid 1000 flowsignal-cert-1000
fi
identity_created=1
sudo useradd --system --uid 501 --gid 1000 --no-create-home --no-user-group --no-log-init \
 --home-dir /nonexistent --shell /usr/sbin/nologin flowsignal-cert-501
sudo visudo -cf harness/uid501.sudoers
sudoers_created=1
# Dotted names are ignored by sudo's includedir. Validate before atomic activation.
sudo install -o 0 -g 0 -m 0440 harness/uid501.sudoers /etc/sudoers.d/.flowsignal-cert-501.pending
sudo visudo -cf /etc/sudoers.d/.flowsignal-cert-501.pending
sudo mv -- /etc/sudoers.d/.flowsignal-cert-501.pending /etc/sudoers.d/flowsignal-cert-501
test "$(sudo stat -c '%u:%g:%a' /etc/sudoers.d/flowsignal-cert-501)" = 0:0:440
python -B - "$group_created" <<'PY' > evidence/identity-setup.json
import json,sys
print(json.dumps({'preexisting_uid501':False,'gid1000':'TEMPORARY' if sys.argv[1]=='1' else 'PREEXISTING',
 'sudoers_validated':True,'sudo_authorization':'CHOWN_AND_UNSHARE_ONLY'}))
PY
uname -a > evidence/kernel.txt
docker info --format '{{.OSType}}/{{.Architecture}}' > evidence/docker-arch.txt
python -B harness/verify_source.py source > evidence/source-before.json
python -m venv "$cert_root/venv"
"$cert_root/venv/bin/python" -B -m pip install --require-hashes --only-binary=:all: --no-deps --no-compile --no-cache-dir -r source/Backend/requirements.production.lock
"$cert_root/venv/bin/python" -B -m pip check
"$cert_root/venv/bin/python" -B - <<'PY' > evidence/profile.json
import importlib.metadata as m,json,re
norm=lambda n:re.sub('[-_.]+','-',n).lower()
expected={norm(p['name']):p['version'] for p in json.load(open('source/Backend/requirements.production.metadata.json'))['packages']}
actual={norm(d.metadata['Name']):d.version for d in m.distributions()}
tooling={k:actual.pop(k) for k in ('pip',) if k in actual}
assert actual==expected and len(actual)==39 and actual['greenlet']=='3.5.6'
print(json.dumps({'packages':actual,'installer_tooling':tooling},sort_keys=True))
PY
"$cert_root/venv/bin/python" -B -m pip install --target "$cert_root/test-tools" --only-binary=:all: --no-compile --no-cache-dir pytest==8.4.2 pluggy==1.6.0
"$cert_root/venv/bin/python" -B -m pip install --target "$cert_root/test-tools" --require-hashes --no-deps --only-binary=:all: --no-compile --no-cache-dir -r harness/test-client.lock
if [ "$gate" = diagnostic_gate ] || [ "$gate" = logging_probe_gate ] || [ "$gate" = native_reference_gate ]; then
  PYTHONPATH="$cert_root/test-tools" "$cert_root/venv/bin/python" -B harness/test_diagnostic_contract.py
  PYTHONPATH="$cert_root/test-tools" "$cert_root/venv/bin/python" -B harness/test_reference_compare.py
  PYTHONPATH="$cert_root/test-tools" "$cert_root/venv/bin/python" -B harness/test_native_reference_gate.py
fi
cp -R source "$cert_root/source"
cp -R harness "$cert_root/harness"
# Only disposable job paths change ownership; the frozen checkout is untouched.
sudo chown -R 501:1000 "$cert_root"
sudo chmod -R u+rwX "$cert_root"
host_net=$(readlink /proc/self/ns/net)
sudo unshare --net -- setpriv --reuid=501 --regid=1000 --clear-groups env -i \
 HOME=/nonexistent PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
 "$cert_root/venv/bin/python" -B "$cert_root/harness/identity_probe.py" "$cert_root" "$host_net" \
 > evidence/identity-probe.json
chmod 0711 "$pg_root"
"$pg_bin/initdb" -D "$pg_root/data" -U cert_user -A trust --no-locale >/dev/null
"$pg_bin/pg_ctl" -D "$pg_root/data" -l "$pg_root/server.log" -o "-c listen_addresses= -k $pg_root -p 55439" start >/dev/null
"$pg_bin/createdb" -h "$pg_root" -p 55439 -U cert_user verifier_test
dsn="postgresql+psycopg2://cert_user@/verifier_test?host=$pg_root&port=55439"
set +e
sudo unshare --net -- setpriv --reuid=501 --regid=1000 --clear-groups env -i \
 HOME=/nonexistent PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 \
 DATABASE_URL="$dsn" MIGRATION_DATABASE_URL="$dsn" VERIFIER_TEST_PG_DSN="$dsn" \
 PYTHONPATH="$cert_root/test-tools:$cert_root/harness" \
 FIXTURE_EVIDENCE="$cert_root/evidence/fixture.jsonl" \
 "$cert_root/venv/bin/python" -B "$cert_root/harness/$gate.py" "$cert_root/source" "$cert_root/evidence"
status=$?
set -e
sudo test ! -e "$cert_root/source/Backend/database/flowsignal.db"
# Expand no wildcard as the runner: the private directory belongs to UID501.
# Copy only the explicitly created textual-evidence directory as root.
sudo cp -R "$cert_root/evidence/." evidence/
sudo chown -R "$(id -u):$(id -g)" evidence
python -B harness/verify_source.py source > evidence/checkout-after.json
exit "$status"
