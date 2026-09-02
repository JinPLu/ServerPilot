#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
project_root=${script_dir:h}
app_bundle="${1:-${project_root}/ServerPilot.app}"
frontend="${app_bundle}/Contents/MacOS/ServerPilot"
inventory="${app_bundle}/Contents/Resources/configs/inventory.yaml"
runtime_root="${app_bundle}/Contents/Resources/ServerPilotRuntime"
info_plist="${app_bundle}/Contents/Info.plist"

if [[ ! -e "${frontend}" ]]; then
  print -u2 "Missing standalone app resource: ${frontend}"
  exit 1
fi
if [[ ! -e "${inventory}" ]]; then
  print -u2 "Missing bundled inventory seed: ${inventory}"
  exit 1
fi
if [[ ! -x "${frontend}" ]]; then
  print -u2 "Bundled frontend is not executable"
  exit 1
fi
if [[ -e "${runtime_root}" ]]; then
  print -u2 "App must not bundle ServerPilotRuntime; the installed CLI is the only backend"
  exit 1
fi

bundle_version="$(plutil -extract CFBundleShortVersionString raw "${info_plist}")"
if ! python3 "${project_root}/scripts/release_metadata.py" check-tag "v${bundle_version}"; then
  print -u2 "Bundled CFBundleShortVersionString ${bundle_version} does not match __version__"
  exit 1
fi

xattr -d com.apple.FinderInfo "${app_bundle}" 2>/dev/null || true
xattr -d 'com.apple.fileprovider.fpfs#P' "${app_bundle}" 2>/dev/null || true
(
  signature_check_root="$(mktemp -d /tmp/serverpilot-signature-check.XXXXXX)"
  trap 'rm -rf "${signature_check_root}"' EXIT
  signature_check_bundle="${signature_check_root}/ServerPilot.app"
  COPYFILE_DISABLE=1 ditto --norsrc "${app_bundle}" "${signature_check_bundle}"
  xattr -cr "${signature_check_bundle}"
  codesign --verify --deep --strict "${signature_check_bundle}"
)

external_links="$(otool -L "${frontend}" | tail -n +2 | awk '{print $1}' | grep -Ev '^(/System/|/usr/lib/)' || true)"
if [[ -n "${external_links}" ]]; then
  print -u2 "Frontend links non-system libraries:"
  print -u2 -- "${external_links}"
  exit 1
fi

resolve_installed_cli() {
  local candidates=()
  if [[ -n "${SERVERPILOT_CLI:-}" ]]; then
    candidates+=("${SERVERPILOT_CLI}")
  fi
  candidates+=("${HOME}/.local/share/uv/tools/serverpilot/bin/serverpilot")
  local from_path
  from_path="$(command -v serverpilot || true)"
  if [[ -n "${from_path}" ]]; then
    candidates+=("${from_path}")
  fi
  candidates+=("/opt/homebrew/bin/serverpilot" "/usr/local/bin/serverpilot")
  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -x "${candidate}" ]]; then
      print -- "${candidate}"
      return 0
    fi
  done
  return 1
}

cli="$(resolve_installed_cli || true)"
if [[ -z "${cli}" ]]; then
  print -u2 "No installed ServerPilot CLI found. Install or upgrade with: uv tool install --force ."
  exit 1
fi
"${cli}" --help >/dev/null

print "Standalone macOS app verification: PASS"
