#!/usr/bin/env bash
set -euo pipefail

build_type="${1:-auto}"
if [[ "$build_type" == auto ]]; then
  if [[ "${GITHUB_REF:-}" == refs/heads/main || "${GITHUB_REF:-}" == refs/tags/v* ]]; then
    build_type=release
  else
    build_type=debug
  fi
fi
case "$build_type" in
  debug) build_task=assembleDebug; verify_task=verifyModuleMetadata ;;
  release) build_task=assembleRelease; verify_task=verifyReleaseModuleMetadata ;;
  *) echo 'Build type must be auto, debug, or release.' >&2; exit 1 ;;
esac
if [[ "${GITHUB_EVENT_NAME:-}" == pull_request && "$build_type" == release ]]; then
  echo 'Pull requests cannot request a signed release build.' >&2
  exit 1
fi

configured_secrets=0
for value in "${WECHATPAD_KEYSTORE_BASE64:-}" "${WECHATPAD_KEYSTORE_PASSWORD:-}" \
  "${WECHATPAD_KEY_ALIAS:-}" "${WECHATPAD_KEY_PASSWORD:-}"; do
  if [[ -n "$value" ]]; then configured_secrets=$((configured_secrets + 1)); fi
done
if [[ "$configured_secrets" != 0 && "$configured_secrets" != 4 ]]; then
  echo 'Configure all four signing Secrets together.' >&2
  exit 1
fi
if [[ -n "${GITHUB_EVENT_NAME:-}" && "$configured_secrets" != 0 && \
  ( "${GITHUB_EVENT_NAME:-}" == pull_request || \
    ( "${GITHUB_REF:-}" != refs/heads/main && "${GITHUB_REF:-}" != refs/tags/v* ) ) ]]; then
  echo 'Stable signing material is restricted to main and version-tag builds outside pull requests.' >&2
  exit 1
fi
if [[ "$build_type" == release && "$configured_secrets" != 4 ]]; then
  echo 'Release builds require all four stable signing Secrets; debug-key fallback is disabled.' >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo 'Build artifacts require a clean checkout so the source archive matches the APK.' >&2
  exit 1
fi

keystore_directory=''
cleanup() {
  if [[ -n "$keystore_directory" ]]; then rm -rf "$keystore_directory"; fi
}
trap cleanup EXIT
if [[ "$configured_secrets" == 4 ]]; then
  keystore_directory=$(mktemp -d)
  export WECHATPAD_KEYSTORE="$keystore_directory/impad-signing.jks"
  printf '%s' "$WECHATPAD_KEYSTORE_BASE64" | base64 --decode > "$WECHATPAD_KEYSTORE"
  test -s "$WECHATPAD_KEYSTORE"
  chmod 600 "$WECHATPAD_KEYSTORE"
fi

./gradlew --no-daemon ":app:$build_task" ":app:$verify_task"
apk="app/build/outputs/apk/$build_type/app-$build_type.apk"
version=$("$ANDROID_HOME/cmdline-tools/latest/bin/apkanalyzer" manifest version-name "$apk")
if [[ ! "$version" =~ ^[0-9A-Za-z][0-9A-Za-z.+_-]*$ ]]; then
  echo 'APK version cannot be used as an artifact filename.' >&2
  exit 1
fi
if [[ "${GITHUB_REF:-}" == refs/tags/* && "${GITHUB_REF:-}" != "refs/tags/v$version" ]]; then
  echo "Tag must match the APK version: v$version" >&2
  exit 1
fi

artifact_directory=work/module-artifact
mkdir -p "$artifact_directory"
cp "$apk" "$artifact_directory/impad-$version-$build_type.apk"
"$ANDROID_HOME/build-tools/37.0.0/apksigner" verify --verbose --print-certs "$apk" \
  > "$artifact_directory/signature.txt"
git archive --format=tar.gz --prefix="ImPad-$version/" HEAD \
  > "$artifact_directory/impad-$version-source.tar.gz"
commit=$(git rev-parse HEAD)
python3 - "$artifact_directory" "$version" "$build_type" "$commit" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

directory, version, build_type, commit = sys.argv[1:]
directory = Path(directory)
files = [directory / f'impad-{version}-{build_type}.apk', directory / f'impad-{version}-source.tar.gz']
checksums = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
(directory / 'SHA256SUMS').write_text(''.join(f'{digest}  {name}\n' for name, digest in checksums.items()))
(directory / 'build.json').write_text(json.dumps({
    'versionName': version, 'buildType': build_type, 'commit': commit, 'sha256': checksums,
}, indent=2) + '\n')
(directory / 'BUILD.md').write_text(
    f'# ImPad {version}\n\nBuild type: {build_type}\n\nSource commit: `{commit}`\n\n'
    'The source archive corresponds to this APK. Verify files against SHA256SUMS.\n'
)
PY
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  printf 'build-type=%s\nartifact-directory=%s\n' "$build_type" "$artifact_directory" >> "$GITHUB_OUTPUT"
fi
