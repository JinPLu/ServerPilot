#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
project_root=${script_dir:h}
final_app_bundle="${project_root}/ServerPilot.app"
staging_root="$(mktemp -d /tmp/serverpilot-macos-app.XXXXXX)"
trap 'rm -rf "${staging_root}"' EXIT
app_bundle="${staging_root}/ServerPilot.app"
macos_dir="${app_bundle}/Contents/MacOS"
resources_dir="${app_bundle}/Contents/Resources"
core_dir="${script_dir}/ServerPilotCore"
core_sources=("${core_dir}/Sources/ServerPilotCore"/*.swift(N))
swift_sources=("${core_sources[@]}" "${script_dir}"/*.swift(N))
deployment_target="14.0"
target_arch="$(uname -m)"

case "${target_arch}" in
  arm64|x86_64) ;;
  *)
    print -u2 "Unsupported macOS architecture: ${target_arch}"
    exit 1
    ;;
esac
target_triple="${target_arch}-apple-macosx${deployment_target}"

if [[ "${1:-}" == "test" ]]; then
  cd "${core_dir}"
  # The tests use swift-testing, not XCTest, so they need no Xcode: the Command
  # Line Tools ship Testing.framework and its interop library, just not on the
  # default search paths. Passing them here is what lets a machine with only
  # the Command Line Tools run the desktop suite at all.
  developer_frameworks="$(xcode-select -p)/Library/Developer/Frameworks"
  developer_lib="$(xcode-select -p)/Library/Developer/usr/lib"
  if [[ -d "${developer_frameworks}" ]]; then
    swift test \
      -Xswiftc -F"${developer_frameworks}" \
      -Xlinker -F"${developer_frameworks}" \
      -Xlinker -rpath -Xlinker "${developer_frameworks}" \
      -Xlinker -rpath -Xlinker "${developer_lib}"
  else
    swift test
  fi
  exit 0
fi

mkdir -p "${macos_dir}" "${resources_dir}/configs"
cp "${script_dir}/Info.plist" "${app_bundle}/Contents/Info.plist"
release_version="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "${project_root}/src/serverpilot/__init__.py")"
plutil -replace CFBundleShortVersionString -string "${release_version}" "${app_bundle}/Contents/Info.plist"
cp "${script_dir}/assets/ServerPilot.icns" "${resources_dir}/ServerPilot.icns"
# daemon install --source-root still seeds Application Support from this file.
# The GUI is not a second backend: it only needs the inventory, not a runtime.
cp "${project_root}/configs/inventory.yaml" "${resources_dir}/configs/inventory.yaml"
if [[ -d "${script_dir}/Fixtures" ]]; then
  mkdir -p "${resources_dir}/Fixtures"
  cp "${script_dir}/Fixtures"/*.json(N) "${resources_dir}/Fixtures/"
fi
plutil -lint "${app_bundle}/Contents/Info.plist" >/dev/null

xcrun --sdk macosx swiftc \
  -target "${target_triple}" \
  -parse-as-library \
  -D DESKTOP_FIXTURES \
  -framework AppKit \
  -framework SwiftUI \
  "${swift_sources[@]}" \
  -o "${macos_dir}/ServerPilot"
xattr -cr "${app_bundle}"
xattr -d com.apple.FinderInfo "${app_bundle}" 2>/dev/null || true
xattr -d 'com.apple.fileprovider.fpfs#P' "${app_bundle}" 2>/dev/null || true
codesign --force --deep --sign - "${app_bundle}"
codesign --verify --deep --strict "${app_bundle}"
mkdir -p "${project_root}"
if [[ -e "${final_app_bundle}" || -L "${final_app_bundle}" ]]; then
  existing_bundle_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "${final_app_bundle}/Contents/Info.plist" 2>/dev/null || true)"
  if [[ "${existing_bundle_id}" != "local.serverpilot.desktop" &&
        "${existing_bundle_id}" != "local.gpu-broker.desktop" ]]; then
    print -u2 "Refusing to replace an unrelated app: ${final_app_bundle}"
    exit 1
  fi
  rm -rf "${final_app_bundle}"
fi
ditto --norsrc "${app_bundle}" "${final_app_bundle}"
xattr -cr "${final_app_bundle}"
xattr -rd com.apple.FinderInfo "${final_app_bundle}" 2>/dev/null || true
xattr -rd 'com.apple.fileprovider.fpfs#P' "${final_app_bundle}" 2>/dev/null || true
# Finder can immediately reattach FileProvider metadata to a bundle stored in
# the project folder. Verify an ephemeral, metadata-free copy of the canonical
# root bundle so signature checking remains deterministic without leaving a
# second app behind.
verification_bundle="${staging_root}/verified/ServerPilot.app"
mkdir -p "${verification_bundle:h}"
COPYFILE_DISABLE=1 ditto --norsrc "${final_app_bundle}" "${verification_bundle}"
xattr -cr "${verification_bundle}"
codesign --verify --deep --strict "${verification_bundle}"

remove_legacy_app() {
  local entry="$1"
  [[ -e "${entry}" || -L "${entry}" ]] || return 0
  if [[ -L "${entry}" ]]; then
    unlink "${entry}"
    return 0
  fi
  local bundle_id
  bundle_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "${entry}/Contents/Info.plist" 2>/dev/null || true)"
  if [[ "${bundle_id}" != "local.serverpilot.desktop" &&
        "${bundle_id}" != "local.gpu-broker.desktop" ]]; then
    print -u2 "Refusing to remove unrelated app: ${entry}"
    exit 1
  fi
  rm -rf "${entry}"
}

# Before the root-bundle rule, builds installed a second copy in ~/Applications
# and exposed aliases under dist/. Remove only verified ServerPilot bundles or
# symlinks after the new canonical bundle has passed signature verification.
remove_legacy_app "${HOME}/Applications/ServerPilot.app"
legacy_dist_entries=("${project_root}/dist"/ServerPilot*.app(N))
for legacy_entry in "${legacy_dist_entries[@]}"; do
  remove_legacy_app "${legacy_entry}"
done

echo "Built ${final_app_bundle}"
