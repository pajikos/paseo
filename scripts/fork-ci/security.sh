#!/usr/bin/env bash
set -euo pipefail
if [[ $# != 1 || $(uname -s) != Linux || $(id -u) == 0 ]]; then
  echo 'Usage on a non-root Linux runner: bash scripts/fork-ci/security.sh <output-dir>' >&2
  exit 2
fi
out=$(realpath -m -- "$1")
mkdir -p "$out"
printf '%s\n' 'INCOMPLETE: dependency scanner did not finish initialization.' > "$out/security-summary.txt"
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
commit=$(git rev-parse HEAD)
git archive "$commit" package.json package-lock.json plugins/package.json \
  ':(glob)packages/*/package.json' > "$scratch/manifests.tar"
git show "$commit:scripts/fork-ci/security.py" > "$scratch/security.py"
git show "$commit:scripts/fork-ci/security-runtime.cjs" > "$scratch/security-runtime.cjs"
mkdir "$scratch/docker"
export DOCKER_CONFIG="$scratch/docker"
image=node@sha256:e13726ca604184d7a91fcacab1fc09eefd642dc28e79ac34e85540dd93b78cfb
docker pull --platform linux/arm64 "$image" || exit 2
status=0
docker run --rm --platform linux/arm64 --user "$(id -u):$(id -g)" --cap-drop ALL \
  --security-opt no-new-privileges --read-only --pids-limit 128 --memory 4g \
  --tmpfs '/tmp:rw,nosuid,nodev,mode=1777,size=768m' --env HOME=/tmp/home \
  --mount "type=bind,src=$scratch/manifests.tar,dst=/inputs.tar,readonly" \
  --mount "type=bind,src=$scratch/security.py,dst=/security.py,readonly" \
  --mount "type=bind,src=$scratch/security-runtime.cjs,dst=/security-runtime.cjs,readonly" \
  --mount "type=bind,src=$out,dst=/reports" --workdir /tmp \
  "$image" python3 -I /security.py "$commit" || status=$?
case "$(tail -n 1 -- "$out/security-summary.txt"):$status" in
  'Gate PASSED:0') exit 0 ;;
  'Gate BLOCKED:1') exit 1 ;;
  *) exit 2 ;;
esac
