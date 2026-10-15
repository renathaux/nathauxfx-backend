#!/usr/bin/env bash
# Two matrix jobs invoke this independently. No registry output/tag/push exists.
set -euo pipefail
test "$(uname -s)" = Linux
test "$(uname -m)" = x86_64
test "$(docker info --format '{{.Architecture}}')" = x86_64
python3 -B harness/verify_source.py source
python3 -B harness/image/test_package.py
mkdir -p evidence images
uname -a > evidence/image-kernel.txt
docker version > evidence/docker-version.txt
docker buildx version > evidence/buildx-version.txt
python3 -B harness/image/scan_release.py source > evidence/source-credential-scan.json
python3 -B harness/image/package.py prepare source context
python3 -B harness/image/scan_release.py context/app > evidence/context-credential-scan.json
base='docker.io/library/python:3.14.3-slim-bookworm@sha256:c6f0b5b3a167963de3cc7cd97fe1a5d07105c8d27f47507ab31d648b533b05c7'
docker pull --platform linux/amd64 "$base"
docker run --rm --network none --entrypoint dpkg-query "$base" -W > evidence/base-os-packages.txt
builder="release-${GITHUB_RUN_ID}-${BUILD_NUMBER}"
pg_root=$(mktemp -d /tmp/fs-verifier-pg.image.XXXXXXXX)
pg_bin=$(pg_config --bindir)
cleanup() {
  "$pg_bin/pg_ctl" -D "$pg_root/data" -m fast stop >/dev/null 2>&1 || true
  docker buildx rm "$builder" >/dev/null 2>&1 || true
  sudo rm -rf -- "$pg_root"
}
trap cleanup EXIT
docker buildx create --name "$builder" --driver docker-container --use
docker buildx inspect --bootstrap > evidence/builder.txt
python3 -B harness/verify_source.py source
test ! -e context/app/Backend/database/flowsignal.db
test ! -e context/app/Backend/scripts/migrate_sqlite_to_neon.py
python3 -B harness/image/package.py validate context/app
docker buildx build --builder "$builder" --platform linux/amd64 --no-cache \
 --provenance=false --sbom=false --build-arg SOURCE_DATE_EPOCH=1791300799 \
 --metadata-file evidence/production-build.json \
 --output type=oci,dest=images/production.oci.tar,rewrite-timestamp=true \
 --output type=docker,dest=production.docker.tar,rewrite-timestamp=true context \
 > evidence/production-build.log 2>&1
python3 -B harness/image/package.py oci images/production.oci.tar > evidence/production-oci.json
prod=$(python3 -c "import json;print(json.load(open('evidence/production-oci.json'))['config_digest'])")
prod_manifest=$(python3 -c "import json;print(json.load(open('evidence/production-oci.json'))['image_digest'])")
docker load --input production.docker.tar
test "$(docker image inspect --format '{{.Id}}' "$prod")" = "$prod"
docker image inspect "$prod" > evidence/production-inspect.json
common=(--rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
 --user 501:1000 --tmpfs /state:rw,nosuid,nodev,noexec,uid=501,gid=1000,mode=0700 \
 --tmpfs /cache:rw,nosuid,nodev,uid=501,gid=1000,mode=0700 \
 -v "$PWD/harness/image:/cert-tools:ro" -e HOME=/nonexistent -e TZ=UTC -e LANG=C.UTF-8)
docker run "${common[@]}" --entrypoint /bin/sh "$prod" \
 -c 'mkdir /cache/tmp; exec python -B /cert-tools/package.py image' > evidence/production-runtime.json
docker run "${common[@]}" -e DATABASE_URL=sqlite:///:memory: --entrypoint /bin/sh "$prod" \
 -c 'mkdir /cache/tmp; exec python -B /cert-tools/container_smoke.py' > evidence/production-smoke.json
docker run "${common[@]}" --entrypoint python "$prod" \
 -B /cert-tools/scan_release.py /app > evidence/image-credential-scan.json
set +e
docker run "${common[@]}" -e DATABASE_URL=sqlite:///:memory: "$prod" > evidence/entrypoint-unsupported.log 2>&1
status=$?
set -e
test "$status" = 1
grep -qx RECOVERY_RELEASE_MIGRATION_FAILED evidence/entrypoint-unsupported.log
docker run "${common[@]}" -e DATABASE_URL=sqlite:///:memory: --entrypoint /bin/sh "$prod" \
 -c 'mkdir /cache/tmp; exec python -B /cert-tools/startup_smoke.py' > evidence/asgi-startup.log 2>&1
docker run --rm --network none --entrypoint dpkg-query "$prod" -W > evidence/production-os-packages.txt
cmp evidence/base-os-packages.txt evidence/production-os-packages.txt
# Derivative gets the exact locally produced OCI digest via named build context.
mkdir production-layout maintenance-context maintenance-unit
tar -xf images/production.oci.tar -C production-layout
cp harness/maintenance-inputs/Dockerfile maintenance-context/Dockerfile
cp -R harness/maintenance-inputs/maintenance maintenance-context/maintenance
cp source/maintenance/launcher.py maintenance-context/maintenance/launcher.py
cp -R maintenance-context/maintenance maintenance-unit/maintenance
cp -R harness/maintenance-inputs/tests maintenance-unit/tests
cp source/mounted_launcher_test.py mounted_launcher_test.py
python3 -B - <<'PY'
import hashlib
from pathlib import Path
pins={'maintenance/cutover_helper.py':'acae986bf2f00f1a0cfd8e583f5d005438aefb61c62d2eec76ff59dc9e0080f0',
 'tests/test_launcher.py':'4fb247f916575e8e2ff3709da6f6bda02e212e3eadc0dbbcf6656beec2470095'}
for name,pin in pins.items():
 assert hashlib.sha256((Path('harness/maintenance-inputs')/name).read_bytes()).hexdigest()==pin
assert Path('maintenance-context/maintenance/launcher.py').read_bytes()==Path('source/maintenance/launcher.py').read_bytes()
PY
python3 -B harness/verify_source.py source
docker buildx build --builder "$builder" --platform linux/amd64 --no-cache \
 --provenance=false --sbom=false --build-arg SOURCE_DATE_EPOCH=1791300799 \
 --build-context "certified-production=oci-layout://$PWD/production-layout@$prod_manifest" \
 --metadata-file evidence/maintenance-build.json \
 --output type=oci,dest=images/maintenance.oci.tar,rewrite-timestamp=true \
 --output type=docker,dest=maintenance.docker.tar,rewrite-timestamp=true maintenance-context \
 > evidence/maintenance-build.log 2>&1
python3 -B harness/image/package.py oci images/maintenance.oci.tar > evidence/maintenance-oci.json
maint=$(python3 -c "import json;print(json.load(open('evidence/maintenance-oci.json'))['config_digest'])")
docker load --input maintenance.docker.tar
test "$(docker image inspect --format '{{.Id}}' "$maint")" = "$maint"
python3 -B harness/image/maintenance_gate.py source "$prod" "$maint" evidence
docker run "${common[@]}" --entrypoint /bin/sh "$maint" \
 -c 'mkdir /cache/tmp; exec python -B /cert-tools/package.py image' > evidence/maintenance-runtime.json
docker run --rm --network none --entrypoint dpkg-query "$maint" -W > evidence/maintenance-os-packages.txt
cmp evidence/production-os-packages.txt evidence/maintenance-os-packages.txt
# Only a Unix socket reaches disposable PG; no network attached to any test container.
chmod 0711 "$pg_root"
"$pg_bin/initdb" -D "$pg_root/data" -U cert_user -A trust --no-locale >/dev/null
"$pg_bin/pg_ctl" -D "$pg_root/data" -l "$pg_root/server.log" \
 -o "-c listen_addresses= -k $pg_root -p 55439" start >/dev/null
"$pg_bin/createdb" -h "$pg_root" -p 55439 -U cert_user verifier_test
dsn="postgresql+psycopg2://cert_user@/verifier_test?host=$pg_root&port=55439"
for mode in production maintenance; do
  image="$prod"; test "$mode" = production || image="$maint"
  docker run "${common[@]}" -v "$pg_root:$pg_root:ro" \
   -e DATABASE_URL="$dsn" -e MIGRATION_DATABASE_URL="$dsn" \
   --entrypoint /bin/sh "$image" -c \
   'mkdir /cache/tmp; exec python -B /cert-tools/container_database_gate.py "$1"' sh "$mode" \
   > "evidence/$mode-migration-startup.log" 2>&1
  "$pg_bin/psql" -h "$pg_root" -p 55439 -U cert_user -d verifier_test -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public' >/dev/null
done
# Pytest tooling remains an external mount, never installed in the image/venv.
mkdir test-tools
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/test-tools:/tools" --entrypoint python "$base" \
 -m pip install --only-binary=:all: --no-cache-dir --no-compile --target /tools pytest==8.4.2 pluggy==1.6.0 >/dev/null
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/test-tools:/tools" -v "$PWD/harness/test-client.lock:/test-client.lock:ro" --entrypoint python "$base" \
 -m pip install --require-hashes --no-deps --only-binary=:all: --no-cache-dir --no-compile --target /tools -r /test-client.lock >/dev/null
chmod -R a+rX test-tools
docker run "${common[@]}" -v "$pg_root:$pg_root:ro" -v "$PWD/test-tools:/test-tools:ro" \
 -e DATABASE_URL="$dsn" -e MIGRATION_DATABASE_URL="$dsn" -e VERIFIER_TEST_PG_DSN="$dsn" \
 -e PYTHONPATH=/test-tools:/app/Backend --entrypoint /bin/sh "$prod" -c \
 'mkdir /cache/tmp; python -B -m pytest -q -p no:cacheprovider --rootdir=/app/Backend --junitxml=/cache/auth.xml tests/test_live_integrity_legacy_auth.py tests/test_live_integrity_preenable.py /cert-tools/test_packaged_auth.py --deselect=tests/test_live_integrity_legacy_auth.py::test_real_durable_auth_and_isolated_worker_have_no_session_side_effects; result=$?; cat /cache/auth.xml; exit "$result"' \
 > evidence/production-auth.log 2>&1
docker run "${common[@]}" -v "$pg_root:$pg_root:ro" -v "$PWD/test-tools:/test-tools:ro" \
 -e DATABASE_URL="$dsn" -e MIGRATION_DATABASE_URL="$dsn" -e VERIFIER_TEST_PG_DSN="$dsn" \
 -e PYTHONPATH=/test-tools:/app/Backend --entrypoint /bin/sh "$maint" -c \
 'mkdir /cache/tmp; python -B -m pytest -q -p no:cacheprovider --junitxml=/cache/owner.xml tests/test_owner_handoff.py tests/test_owner_handoff_submissions.py tests/test_owner_handoff_ready.py tests/test_owner_handoff_runtime.py; result=$?; cat /cache/owner.xml; exit "$result"' \
 > evidence/maintenance-owner.log 2>&1
python3 -B harness/image/finalize.py
python3 -B harness/verify_source.py source > evidence/image-source-after.json
