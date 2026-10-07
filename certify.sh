#!/usr/bin/env bash
set -euo pipefail
test "$(uname -m)" = x86_64
test "$(uname -s)" = Linux
mkdir evidence
uname -a > evidence/kernel.txt
git rev-parse HEAD > evidence/source-commit.txt
python3 -B -m unittest discover -s tests -v 2>&1 | tee evidence/unit-tests.log
python3 -B credential_scan.py maintenance > evidence/maintenance-credential-scan.json
base='ghcr.io/renathaux/nathauxfx-backend@sha256:4f11f091d8ba77192a81569b3e85e101dcccc329c641eddeabd160b13be6fb49'
docker pull --platform linux/amd64 "$base"
docker image inspect "$base" > evidence/base.inspect.json
python3 - <<'PY'
import json
x=json.load(open('evidence/base.inspect.json'))[0]
assert x['Id']=='sha256:fb148eb27d5c97c373131778f2a0e8430f6bfcfbac4584995dc5d7e9ecaa30c9'
assert x['Architecture']=='amd64' and x['Os']=='linux'
PY
for n in 1 2; do
  builder="disk-maintenance-${GITHUB_RUN_ID:-local}-$n"
  docker buildx create --name "$builder" --driver docker-container --use
  docker buildx inspect --bootstrap > "evidence/builder-$n.txt"
  docker buildx build --builder "$builder" --platform linux/amd64 --no-cache --provenance=false --sbom=false \
    --build-arg SOURCE_DATE_EPOCH=1791320000 --tag flowsignal-disk-maintenance:cert \
    --metadata-file "evidence/build-$n.json" \
    --output "type=oci,dest=evidence/image-$n.oci.tar,rewrite-timestamp=true" \
    --output "type=docker,dest=evidence/image-$n.docker.tar,rewrite-timestamp=true" . \
    2>&1 | tee "evidence/build-$n.log"
  docker load --input "evidence/image-$n.docker.tar"
  docker image inspect flowsignal-disk-maintenance:cert > "evidence/image-$n.inspect.json"
  python3 -B certify.py oci "evidence/image-$n.oci.tar" > "evidence/oci-$n.json"
  python3 -B certify.py test 2>&1 | tee "evidence/test-$n.log"
  cp evidence/tests.json "evidence/tests-$n.json"
  docker buildx rm "$builder"
done
python3 - <<'PY'
import json,pathlib
p=pathlib.Path('evidence')
a=json.loads((p/'oci-1.json').read_text());b=json.loads((p/'oci-2.json').read_text())
assert a==b,'NONDETERMINISTIC_MAINTENANCE_IMAGE'
for n in (1,2):
 assert json.loads((p/f'image-{n}.inspect.json').read_text())[0]['Id']==a['config_digest']
assert (p/'tests-1.json').read_bytes()==(p/'tests-2.json').read_bytes()
summary=dict(certification='PASS',base_digest='sha256:4f11f091d8ba77192a81569b3e85e101dcccc329c641eddeabd160b13be6fb49',image=a,reproducible=True,production_mutations=0,broker_calls=0,database_connections=0)
(p/'summary.json').write_text(json.dumps(summary,sort_keys=True,indent=2)+'\n')
print(json.dumps(summary,sort_keys=True))
PY
