#!/usr/bin/env bash
set -euo pipefail
test "$(uname -s)" = Linux
test "$(uname -m)" = x86_64
test "$(python -c 'import platform; print(platform.python_version())')" = 3.14.3
test "$(docker info --format '{{.Architecture}}')" = x86_64
python -B harness/verify_source.py source
python -B harness/test_fixture.py
cert_root=$(mktemp -d /tmp/fs-amd64-cert.XXXXXXXX)
pg_root=$(mktemp -d /tmp/fs-verifier-pg.cert.XXXXXXXX)
pg_bin=$(pg_config --bindir)
cleanup() {
  "$pg_bin/pg_ctl" -D "$pg_root/data" -m fast stop >/dev/null 2>&1 || true
  # Exact mktemp roots owned by this job; no source worktree removal.
  sudo rm -rf -- "$cert_root" "$pg_root"
}
trap cleanup EXIT
mkdir -p evidence "$cert_root/evidence"
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
"$cert_root/venv/bin/python" -B -m pip install --target "$cert_root/test-tools" --only-binary=:all: --no-compile --no-cache-dir pytest==8.4.2
cp -R source "$cert_root/source"
cp -R harness "$cert_root/harness"
# Only disposable job paths change ownership; the frozen checkout is untouched.
sudo chown -R 501:1000 "$cert_root"
sudo chmod -R u+rwX "$cert_root"
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
 "$cert_root/venv/bin/python" -B "$cert_root/harness/backend_gate.py" "$cert_root/source" "$cert_root/evidence"
status=$?
set -e
sudo test ! -e "$cert_root/source/Backend/database/flowsignal.db"
# Expand no wildcard as the runner: the private directory belongs to UID501.
# Copy only the explicitly created textual-evidence directory as root.
sudo cp -R "$cert_root/evidence/." evidence/
sudo chown -R "$(id -u):$(id -g)" evidence
python -B harness/verify_source.py source > evidence/checkout-after.json
exit "$status"
