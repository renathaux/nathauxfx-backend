#!/usr/bin/env bash
set -euo pipefail
test "$(uname -m)" = x86_64
test "$(uname -s)" = Linux
base='docker.io/library/python:3.14.3-slim-bookworm@sha256:c6f0b5b3a167963de3cc7cd97fe1a5d07105c8d27f47507ab31d648b533b05c7'
mkdir evidence
uname -a > evidence/kernel.txt
docker version > evidence/docker-version.txt
docker buildx version > evidence/buildx-version.txt
python3 packaging/test_package.py
python3 packaging/package.py prepare source context
python3 packaging/credential_scan.py source > evidence/source-credential-scan.json
python3 packaging/credential_scan.py context/app > evidence/context-credential-scan.json
docker pull --platform linux/amd64 "$base"
docker image inspect "$base" > evidence/base-inspect.json
docker run --rm --network none --entrypoint python "$base" -c 'import platform; assert platform.python_version()=="3.14.3"; assert platform.machine()=="x86_64"; print(platform.platform())' > evidence/base-runtime.txt
# Test tooling is not installed in either final image or production venv.
mkdir test-tools
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/test-tools:/tools" --entrypoint python "$base" -m pip install --no-cache-dir --only-binary=:all: --target /tools/site pytest==8.4.2
chmod -R a+rX test-tools
epoch=1791300799
for n in 1 2; do
  python3 packaging/package.py validate context/app
  builder="image-cert-${GITHUB_RUN_ID:-local}-$n"
  docker buildx create --name "$builder" --driver docker-container --use
  docker buildx inspect --bootstrap > "evidence/builder-$n.txt"
  docker buildx build --builder "$builder" --platform linux/amd64 --no-cache --provenance=false --sbom=false \
    --build-arg SOURCE_DATE_EPOCH="$epoch" --tag flowsignal-cert:immutable \
    --metadata-file "evidence/build-$n.json" \
    --output "type=oci,dest=evidence/image-$n.oci.tar,rewrite-timestamp=true" \
    --output "type=docker,dest=evidence/image-$n.docker.tar,rewrite-timestamp=true" context \
    2>&1 | tee "evidence/build-$n.log"
  docker load --input "evidence/image-$n.docker.tar"
  docker image inspect flowsignal-cert:immutable > "evidence/image-$n.inspect.json"
  python3 packaging/package.py oci "evidence/image-$n.oci.tar" > "evidence/oci-$n.json"
  # No TCP/UDP network namespace, no writable app/identity mount, no privileges.
  common=(--rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /state:rw,nosuid,nodev,noexec,uid=501,gid=1000,mode=0700 \
    --tmpfs /cache:rw,nosuid,nodev,uid=501,gid=1000,mode=0700 \
    -v "$PWD/packaging:/cert-tools:ro" \
    -e DATABASE_URL=sqlite:///:memory: -e TZ=UTC -e LANG=C.UTF-8)
  docker run "${common[@]}" --entrypoint /bin/sh flowsignal-cert:immutable \
    -c 'mkdir /cache/tmp; exec python -B /cert-tools/package.py image' > "evidence/runtime-$n.json"
  docker run "${common[@]}" --entrypoint /bin/sh flowsignal-cert:immutable \
    -c 'mkdir /cache/tmp; exec python -B /cert-tools/container_smoke.py' > "evidence/smoke-$n.log"
  docker run "${common[@]}" --entrypoint python flowsignal-cert:immutable \
    -B /cert-tools/credential_scan.py /app > "evidence/image-credential-scan-$n.json"
  # Exercise the unmodified production entrypoint: unsupported test SQLite must
  # stop at the real migration guard, before Uvicorn/ownership/broker activity.
  set +e
  docker run "${common[@]}" flowsignal-cert:immutable > "evidence/entrypoint-$n.log" 2>&1
  entry_status=$?
  set -e
  test "$entry_status" = 1
  grep -qx 'RECOVERY_RELEASE_MIGRATION_FAILED' "evidence/entrypoint-$n.log"
  # Separately run actual Uvicorn and ASGI lifespan with an unsupported, empty,
  # disposable DB. The real recovery boundary must withhold all authority.
  docker run "${common[@]}" --entrypoint /bin/sh flowsignal-cert:immutable \
    -c 'mkdir /cache/tmp; exec python -B /cert-tools/startup_smoke.py' > "evidence/asgi-startup-$n.log" 2>&1
  # Missing writable state fails rather than creating state under source.
  if docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
     --entrypoint python flowsignal-cert:immutable -B -c 'import paths; paths.ensure_runtime_dirs()' \
     > "evidence/missing-state-$n.log" 2>&1; then
    echo 'MISSING_WRITABLE_STATE_DID_NOT_BLOCK'; exit 1
  fi
  if test "$n" = 1; then
    # Bind external test tools, retain all selected application tests/assertions.
    docker run "${common[@]}" -v "$PWD/test-tools/site:/test-site:ro" \
      -e PYTHONPATH=/test-site:/cert-tools:/app/Backend \
      --entrypoint /bin/sh flowsignal-cert:immutable -c \
      'mkdir /cache/tmp; python -B -m pytest -q -p no:cacheprovider -p certification_plugin --junitxml=/cache/result.xml tests/test_immutable_build_identity.py tests/test_runtime_paths.py tests/test_recovery_configuration.py tests/test_cutover_manifest.py; result=$?; cat /cache/result.xml; exit "$result"' \
      > evidence/container-tests.log 2>&1
    python3 - <<'PY'
import pathlib,re,xml.etree.ElementTree as ET
text=pathlib.Path('evidence/container-tests.log').read_text()
raw=text[text.index('<?xml'):]
root=ET.fromstring(raw)
suites=list(root.iter('testsuite'))
assert suites and sum(int(s.get('tests','0')) for s in suites)>67
assert all(int(s.get(k,'0'))==0 for s in suites for k in ('errors','failures','skipped'))
pathlib.Path('evidence/container-tests.xml').write_text(raw)
PY
  fi
  # Compare installed distro packages with the immutable base, no apt additions.
  docker run --rm --network none --entrypoint dpkg-query flowsignal-cert:immutable -W > "evidence/os-packages-$n.txt"
  docker buildx rm "$builder"
done
docker run --rm --network none --entrypoint dpkg-query "$base" -W > evidence/base-os-packages.txt
cmp evidence/base-os-packages.txt evidence/os-packages-1.txt
cmp evidence/os-packages-1.txt evidence/os-packages-2.txt
python3 - <<'PY'
import json,pathlib
p=pathlib.Path('evidence')
a=json.loads((p/'runtime-1.json').read_text());b=json.loads((p/'runtime-2.json').read_text())
assert a==b,'APPLICATION_OR_IDENTITY_NONDETERMINISM'
one=json.loads((p/'oci-1.json').read_text());two=json.loads((p/'oci-2.json').read_text())
assert one==two,'IMAGE_DIGEST_NONDETERMINISM_REQUIRES_EXPLANATION'
for n,oci in ((1,one),(2,two)):
    inspected=json.loads((p/f'image-{n}.inspect.json').read_text())[0]
    assert inspected['Id']==oci['config_digest'],'OCI_AND_TESTED_IMAGE_DIFFER'
summary=dict(packaging_certification='PASS',image_digests=[one,two],build_identity=a['build']['build_identity'],
    application_filesystem_reproducible=True,build_identity_reproducible=True,full_image_digest_reproducible=True,
    real_broker_orders_sent=0,registry_push=False,production_secrets_used=False)
(p/'summary.json').write_text(json.dumps(summary,sort_keys=True,indent=2)+'\n')
print(json.dumps(summary,sort_keys=True))
PY
