#!/usr/bin/env bash
# Build the committed fork revision on a Linux ARM64 host.
set -euo pipefail

if [[ $# != 1 || $(uname -sm) != 'Linux aarch64' ]]; then
  echo 'Usage on Linux ARM64: bash scripts/fork-ci/build.sh <output-dir>' >&2
  exit 2
fi

upstream=2ee30f4164af42ba209236ab3a31bf9532592fc3
image=node@sha256:e13726ca604184d7a91fcacab1fc09eefd642dc28e79ac34e85540dd93b78cfb
fork=$(git rev-parse HEAD)
git merge-base --is-ancestor "$upstream" "$fork"
lock_digest=$(git show "$fork:package-lock.json" | sha256sum | cut -d' ' -f1)
[[ $lock_digest == 1e5d5af05e99d9ea4d5e97aa446a7f75d4da4bd55b1acd41810e88a74037b07b ]]

output=$(realpath -m -- "$1")
scratch=$(mktemp -d)
volume=''
cleanup() {
  if [[ -n $volume ]]; then docker volume rm "$volume" >/dev/null; fi
  rm -rf -- "$scratch"
}
trap cleanup EXIT
mkdir -p "$scratch/docker" "$output"
git archive "$fork" > "$scratch/source.tar"
git archive "$fork" package.json package-lock.json plugins/package.json \
  ':(glob)packages/*/package.json' > "$scratch/manifests.tar"

# Keep host Docker client configuration (including any proxy credentials) out of the containers.
export DOCKER_CONFIG="$scratch/docker"
docker pull --platform linux/arm64 "$image"
volume=$(docker volume create)
run=(docker run --rm --platform linux/arm64 --user node --read-only
  --cap-drop ALL --security-opt no-new-privileges --memory 10g --cpus 3 --pids-limit 1024
  --tmpfs '/tmp:rw,nosuid,nodev,size=1536m,mode=1777' --workdir /home/node
  --env HOME=/home/node --mount "type=volume,src=$volume,dst=/home/node")

# The only checkout inputs exposed during network access are npm manifests and the lockfile.
git show "$fork:scripts/fork-ci/build-fetch.sh" | "${run[@]}" -i \
  --mount "type=bind,src=$scratch/manifests.tar,dst=/manifests.tar,readonly" \
  "$image" bash -seu -o pipefail

"${run[@]}" --network none \
  --mount "type=bind,src=$scratch/source.tar,dst=/source.tar,readonly" \
  "$image" bash -euc '
    tar xf /source.tar -C src
    tar xf /source.tar -C target
    bash src/scripts/fork-ci/build-package.sh
  '

archive=Paseo-mac-arm64.tar.gz
"${run[@]}" --network none "$image" cat "/home/node/$archive" > "$scratch/$archive"
mv -- "$scratch/$archive" "$output/$archive"
(cd "$output" && sha256sum "$archive" > SHA256SUMS && sha256sum -c SHA256SUMS)
cat > "$output/provenance.json" <<EOF
{
  "upstreamTag": "v0.16.2",
  "upstreamCommit": "$upstream",
  "forkCommit": "$fork",
  "buildImage": "$image",
  "packageLockSha256": "$lock_digest"
}
EOF
