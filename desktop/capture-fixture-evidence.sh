#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
project_root=${script_dir:h}
app_bundle="${1:-${project_root}/ServerPilot.app}"
result_dir="${2:-${project_root}/build/native-ui-acceptance}"
executable="${app_bundle}/Contents/MacOS/ServerPilot"
fixture_dir="${script_dir}/Fixtures"

if [[ ! -x "${executable}" ]]; then
  print -u2 "Missing executable app: ${executable}"
  exit 2
fi
mkdir -p "${result_dir}/screenshots" "${result_dir}/logs" "${result_dir}/ax"
: > "${result_dir}/commands.log"

run_capture() {
  local name=$1 fixture=$2 section=$3 viewport=$4
  shift 4
  local screenshot="${result_dir}/screenshots/${name}.png"
  local command_log="${result_dir}/logs/${name}.log"
  print -r -- "fixture=${fixture} section=${section} viewport=${viewport} args=$*" >> "${result_dir}/commands.log"
  env \
    SERVERPILOT_DESKTOP_FIXTURE="${fixture}" \
    SERVERPILOT_DESKTOP_SECTION="${section}" \
    SERVERPILOT_DESKTOP_VIEWPORT="${viewport}" \
    SERVERPILOT_DESKTOP_SCREENSHOT="${screenshot}" \
    SERVERPILOT_DESKTOP_EXIT_AFTER_SCREENSHOT=1 \
    SERVERPILOT_DESKTOP_FORCE_INCREASE_CONTRAST="${SERVERPILOT_DESKTOP_FORCE_INCREASE_CONTRAST:-0}" \
    SERVERPILOT_CLI=/usr/bin/false \
    "${executable}" "$@" > "${command_log}" 2>&1
  [[ -s "${screenshot}" ]] || {
    print -u2 "Screenshot was not produced: ${screenshot}"
    exit 3
  }
}

for viewport in 1024x640 1280x800 1440x820; do
  run_capture "servers-${viewport}" resource-ownership server-pool "${viewport}"
  run_capture "usage-${viewport}" resource-ownership resource-usage "${viewport}"
  run_capture "settings-${viewport}" resource-ownership settings "${viewport}"
done
run_capture empty-1024x640 0 server-pool 1024x640
run_capture error-1024x640 error server-pool 1024x640
run_capture forced-light-1280x800 resource-ownership server-pool 1280x800 -NSRequiresAquaSystemAppearance YES
run_capture system-dark-request-1280x800 resource-ownership server-pool 1280x800 -AppleInterfaceStyle Dark
run_capture high-contrast-1280x800 resource-ownership server-pool 1280x800 -AppleIncreaseContrast YES
# Fixture-only override, because the launch argument above is inert.  This
# exercises the token wiring; the real System Settings toggle is still the only
# proof of the OS integration.
SERVERPILOT_DESKTOP_FORCE_INCREASE_CONTRAST=1 \
  run_capture high-contrast-forced-1280x800 resource-ownership server-pool 1280x800
run_capture reduce-motion-1280x800 resource-ownership server-pool 1280x800 -AppleReduceMotion YES
run_capture system-dark-high-contrast-request-1280x800 resource-ownership server-pool 1280x800 \
  -AppleInterfaceStyle Dark -AppleIncreaseContrast YES

# This baseline check intentionally uses only the Python standard library, so the
# acceptance script can run from a clean clone. It validates the repository-owned
# healthy fixture's public snapshot shape; it is not a replacement for API schema
# tests, which remain in the Python test suite.
python3 - "${fixture_dir}/contract-healthy-snapshot.json" > "${result_dir}/fixture-contract-validation.json" <<'PY'
import json
import pathlib
import sys

fixture_path = pathlib.Path(sys.argv[1])
fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
errors = []

if fixture.get("schema_version") != "v1":
    errors.append("schema_version must be v1")
if not isinstance(fixture.get("snapshot_revision"), int):
    errors.append("snapshot_revision must be an integer")

data = fixture.get("data")
if not isinstance(data, dict):
    errors.append("data must be an object")
    data = {}

summary = data.get("summary")
if not isinstance(summary, dict):
    errors.append("data.summary must be an object")

endpoints = data.get("endpoints")
if not isinstance(endpoints, list):
    errors.append("data.endpoints must be a list")
    endpoints = []
endpoint_ids = set()
for index, endpoint in enumerate(endpoints):
    if not isinstance(endpoint, dict) or not isinstance(endpoint.get("id"), str):
        errors.append(f"data.endpoints[{index}] must have a string id")
        continue
    endpoint_ids.add(endpoint["id"])

gpus = data.get("gpus")
if not isinstance(gpus, list):
    errors.append("data.gpus must be a list")
    gpus = []
for index, gpu in enumerate(gpus):
    if not isinstance(gpu, dict):
        errors.append(f"data.gpus[{index}] must be an object")
        continue
    if not isinstance(gpu.get("id"), str):
        errors.append(f"data.gpus[{index}] must have a string id")
    if gpu.get("endpoint_id") not in endpoint_ids:
        errors.append(f"data.gpus[{index}].endpoint_id must reference an endpoint")

result = {
    "fixture": str(fixture_path),
    "validator": "built-in standard-library baseline",
    "valid": not errors,
    "errors": errors,
}
print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
raise SystemExit(0 if result["valid"] else 4)
PY

{
  xcode-select -p
  xcodebuild -version
  xcrun --find xctest
} > "${result_dir}/logs/xctest-toolchain.log" 2>&1 || true

ax_dumper="${result_dir}/ax-dump"
if ! xcrun swiftc -O "${script_dir}/tools/ax-dump.swift" -o "${ax_dumper}" 2> "${result_dir}/logs/ax-dump-build.log"; then
  print -u2 "Could not build the accessibility dumper; see logs/ax-dump-build.log"
  exit 4
fi

collect_ax() {
  local name=$1 fixture=$2 section=$3
  local app_log="${result_dir}/logs/ax-${name}.app.log"
  env \
    SERVERPILOT_DESKTOP_FIXTURE="${fixture}" \
    SERVERPILOT_DESKTOP_SECTION="${section}" \
    SERVERPILOT_DESKTOP_VIEWPORT=1280x800 \
    SERVERPILOT_CLI=/usr/bin/false \
    "${executable}" > "${app_log}" 2>&1 &
  local app_pid=$!
  local ready=false
  for _attempt in {1..50}; do
    if /usr/bin/osascript -e 'on run argv' \
      -e 'tell application "System Events" to return exists (first process whose unix id is (item 1 of argv as integer))' \
      -e 'end run' \
      "${app_pid}" \
      2>/dev/null | grep -q true; then
      ready=true
      break
    fi
    sleep 0.1
  done
  if [[ "${ready}" != true ]]; then
    kill "${app_pid}" 2>/dev/null || true
    wait "${app_pid}" 2>/dev/null || true
    print -u2 "Accessibility process did not appear for ${name}"
    return 4
  fi
  sleep 0.8

  local ax_status=0
  "${ax_dumper}" "${app_pid}" > "${result_dir}/ax/${name}.txt" 2> "${result_dir}/ax/${name}.err" || true
  kill "${app_pid}" 2>/dev/null || true
  wait "${app_pid}" 2>/dev/null || true
  return "${ax_status}"
}

collect_ax servers resource-ownership server-pool
collect_ax usage resource-ownership resource-usage
collect_ax settings resource-ownership settings
collect_ax empty 0 server-pool
collect_ax error error server-pool

# Full Keyboard Access is a global setting.  Passing -AppleKeyboardUIMode as a
# launch argument only populates the app's own NSArgumentDomain; AppKit reads
# the real NSGlobalDomain value, so the argument alone leaves Tab traversal off
# and the check below then reports "not measured" rather than a verdict.
#
# Writing the global default is a change to the operator's Mac, so it happens
# only when SERVERPILOT_AX_SET_KEYBOARD_MODE=1 is set explicitly.  The previous
# value is captured first and restored on any exit path, including a signal.
keyboard_mode_restored=0
restore_keyboard_mode() {
  [ "${keyboard_mode_restored}" = "1" ] && return 0
  keyboard_mode_restored=1
  case "${keyboard_mode_previous-unset}" in
    unset) ;;
    absent) defaults delete NSGlobalDomain AppleKeyboardUIMode 2>/dev/null || true ;;
    *) defaults write NSGlobalDomain AppleKeyboardUIMode -int "${keyboard_mode_previous}" 2>/dev/null || true ;;
  esac
}
if [ "${SERVERPILOT_AX_SET_KEYBOARD_MODE:-0}" = "1" ]; then
  if keyboard_mode_previous=$(defaults read NSGlobalDomain AppleKeyboardUIMode 2>/dev/null); then
    :
  else
    keyboard_mode_previous=absent
  fi
  trap restore_keyboard_mode EXIT INT TERM
  defaults write NSGlobalDomain AppleKeyboardUIMode -int 3
fi

env \
  SERVERPILOT_DESKTOP_FIXTURE=resource-ownership \
  SERVERPILOT_DESKTOP_SECTION=server-pool \
  SERVERPILOT_DESKTOP_VIEWPORT=1280x800 \
  SERVERPILOT_CLI=/usr/bin/false \
  "${executable}" > "${result_dir}/logs/keyboard.app.log" 2>&1 &
keyboard_pid=$!
for _attempt in {1..50}; do
  /usr/bin/osascript -e 'on run argv' \
    -e 'tell application "System Events" to return exists (first process whose unix id is (item 1 of argv as integer))' \
    -e 'end run' \
    "${keyboard_pid}" \
    2>/dev/null | grep -q true && break
  sleep 0.1
done
sleep 0.8
defaults read NSGlobalDomain AppleKeyboardUIMode 2>/dev/null \
  > "${result_dir}/ax/keyboard-uimode.txt" || echo absent > "${result_dir}/ax/keyboard-uimode.txt"
/usr/bin/osascript - "${keyboard_pid}" > "${result_dir}/ax/keyboard-focus.txt" 2> "${result_dir}/ax/keyboard-focus.err" <<'APPLESCRIPT'
on run argv
set targetPID to item 1 of argv as integer
tell application "System Events"
  tell (first process whose unix id is targetPID)
    set frontmost to true
    set output to ""
    repeat with stepIndex from 1 to 12
      key code 48
      delay 0.04
      set focusedSummary to "none"
      try
        set itemRef to value of attribute "AXFocusedUIElement"
        set focusedRole to (role of itemRef) as text
        try
          set focusedHelp to (value of attribute "AXHelp" of itemRef) as text
        on error
          set focusedHelp to "?"
        end try
        try
          set focusedValue to (value of attribute "AXValue" of itemRef) as text
        on error
          set focusedValue to "?"
        end try
        set focusedSummary to focusedRole & tab & focusedHelp & tab & focusedValue
      end try
      set output to output & stepIndex & tab & focusedSummary & linefeed
    end repeat
    keystroke "r" using command down
    delay 0.1
    return output & "command-r=sent" & linefeed
  end tell
end tell
end run
APPLESCRIPT
kill "${keyboard_pid}" 2>/dev/null || true
wait "${keyboard_pid}" 2>/dev/null || true
restore_keyboard_mode

python3 - "${project_root}" "${app_bundle}" "${result_dir}" <<'PY'
import json
import subprocess
import pathlib
import re
import struct
import sys

project_root, app_bundle, result_dir = map(pathlib.Path, sys.argv[1:])
screenshots = []
pixels_by_name = {}
for path in sorted((result_dir / "screenshots").glob("*.png")):
    data = path.read_bytes()
    pixels_by_name[path.stem] = data
    size = struct.unpack(">II", data[16:24]) if data.startswith(b"\x89PNG\r\n\x1a\n") else None
    screenshots.append({
        "name": path.stem,
        "path": str(path),
        "pixels": list(size) if size else None,
    })

expected_names = {
    *(f"{surface}-{viewport}" for surface in ("servers", "usage", "settings") for viewport in ("1024x640", "1280x800", "1440x820")),
    "empty-1024x640",
    "error-1024x640",
    "forced-light-1280x800",
    "system-dark-request-1280x800",
    "high-contrast-1280x800",
    "reduce-motion-1280x800",
    "system-dark-high-contrast-request-1280x800",
}
by_name = {item["name"]: item for item in screenshots}
missing = sorted(expected_names - by_name.keys())
dimension_errors = []
for name, item in by_name.items():
    match = re.search(r"(1024x640|1280x800|1440x820)$", name)
    if not match:
        continue
    expected = [int(value) for value in match.group(1).split("x")]
    if item["pixels"] != expected:
        dimension_errors.append({"name": name, "expected": expected, "actual": item["pixels"]})

ui_test_source = project_root / "desktop/UITests/FixtureModeUITests.swift"
declared_tests = re.findall(r"\bfunc\s+(test[A-Za-z0-9_]+)\s*\(", ui_test_source.read_text(encoding="utf-8"))
ax_files = sorted(str(path) for path in (result_dir / "ax").glob("*.txt"))
ax_text = {
    name: (result_dir / "ax" / f"{name}.txt").read_text(encoding="utf-8", errors="replace")
    for name in ("servers", "usage", "settings", "empty", "error")
}
semantic_checks = {
    "servers_navigation_and_sort_ax": all(
        token in ax_text["servers"]
        for token in (
            "服务器", "使用情况", "设置", "测试数据不能刷新",
            "按GPU 利用率排序", "按显存占用率排序", "按CPU 负载排序", "按内存占用率排序"
        )
    ),
    "usage_project_task_ax": all(
        token in ax_text["usage"]
        for token in ("使用情况", "vision-lab", "4 个任务", "当前使用")
    ),
    # The settings page groups its facts into cards, so the group headings are
    # asserted alongside the facts: a card that loses its heading loses the only
    # thing that says what its rows are about.
    "settings_contract_ax": all(
        token in ax_text["settings"]
        for token in (
            "设置", "本机服务", "服务地址", "版本",
            "数据状态", "连接", "快照", "清单", "资源变更",
            "数据采集", "数据采集间隔",
        )
    ),
    "empty_state_ax": "暂无端点" in ax_text["empty"],
    "connection_error_ax": any(
        token in ax_text["error"]
        for token in ("连接失败", "连接或更新超时", "更新中断")
    ),
    "no_internal_agent_identity_ax": not any(
        token in "\n".join(ax_text.values())
        for token in ("Agent", "agent-trainer", "__serverpilot_system__")
    ),
}
keyboard_text = (result_dir / "ax/keyboard-focus.txt").read_text(encoding="utf-8", errors="replace")
keyboard_rows = [line for line in keyboard_text.splitlines() if re.match(r"^\d+\t", line)]
keyboard_focus_summaries = {line.split("\t", 1)[1] for line in keyboard_rows if not line.endswith("\tnone")}
# Read back what was in force *during* the capture: the harness restores the
# operator's original value before these checks run, so probing the live
# setting here would describe the restored state, not the measured one.
full_keyboard_access = (result_dir / "ax/keyboard-uimode.txt").read_text(
    encoding="utf-8", errors="replace"
).strip()
if full_keyboard_access not in {"2", "3"}:
    # Without Full Keyboard Access, .focusable() views legitimately take no Tab
    # stop, so a verdict here would describe System Settings, not this app.
    semantic_checks["keyboard_focus_traversal"] = None
    semantic_checks["keyboard_focus_traversal_note"] = (
        "not measured: Full Keyboard Access is off, so .focusable() views take no "
        "Tab stop and any verdict here would describe System Settings.  Re-run with "
        "SERVERPILOT_AX_SET_KEYBOARD_MODE=1 to let this script set and restore it, or "
        "enable System Settings > Keyboard > Keyboard navigation."
    )
else:
    semantic_checks["keyboard_focus_traversal"] = (
        len(keyboard_focus_summaries) >= 3 and "command-r=sent" in keyboard_text
    )

appearance_checks = {
    "light_only_policy_ignores_system_dark_request": pixels_by_name.get("forced-light-1280x800") == pixels_by_name.get("system-dark-request-1280x800"),
    # -AppleIncreaseContrast YES does not drive
    # NSWorkspace.accessibilityDisplayShouldIncreaseContrast: measured on this
    # machine it stays false, so a pixel comparison here reports the launch
    # argument's inertness, not the app.  Verifying this needs the real System
    # Settings > Accessibility > Display > Increase contrast toggle.
    "high_contrast_injection_changed_static_pixels": None,
    "high_contrast_note": (
        "not measured: the launch argument does not set the accessibility flag; "
        "toggle System Settings > Accessibility > Display > Increase contrast, then re-run"
    ),
    # What this does prove: the Increase Contrast branch reaches the rendered
    # tokens.  What it does not prove: that macOS delivers the flag to the app.
    "forced_high_contrast_changed_static_pixels": (
        pixels_by_name.get("servers-1280x800") is not None
        and pixels_by_name.get("high-contrast-forced-1280x800") is not None
        and pixels_by_name.get("servers-1280x800") != pixels_by_name.get("high-contrast-forced-1280x800")
    ),
    "reduce_motion_static_screenshot_is_not_behavioral_proof": None,
}
payload = {
    "artifact_integrity_ok": not missing and not dimension_errors,
    "evidence_scope": "fake desktop fixtures only",
    "app_bundle": str(app_bundle),
    "fixture_contract_validation": str(result_dir / "fixture-contract-validation.json"),
    "screenshots": screenshots,
    "missing_screenshots": missing,
    "dimension_errors": dimension_errors,
    "appearance_variants": {
        "light_policy": "the app must stay Aqua/light even when launched with -AppleInterfaceStyle Dark",
        "forced_light_reference": "captured with -NSRequiresAquaSystemAppearance YES",
        "system_dark_request": "captured with -AppleInterfaceStyle Dark; this is a resistance test, not dark-mode support",
        "high_contrast": "requested with -AppleIncreaseContrast YES",
        "reduce_motion": "requested with -AppleReduceMotion YES",
    },
    "appearance_checks": appearance_checks,
    "accessibility_dumps": ax_files,
    "semantic_checks": semantic_checks,
    "acceptance_gaps": [
        "No XCTest/XCUITest execution or xcresult is possible without full Xcode.",
        "A static screenshot cannot prove Reduce Motion behavior.",
        "High Contrast is not accepted unless the recorded pixel comparison changes or Accessibility Inspector evidence is added.",
        "Confirmation dialogs are mutation-gated and are not exercised by the read-only fixture provider.",
        "Detail/inspector interaction remains declared in XCUITest but is not executed without Xcode.",
    ],
    "declared_xcui_test_count": len(declared_tests),
    "declared_xcui_tests": declared_tests,
    "xctest_execution": {
        "executed": 0,
        "reason": "Full Xcode/xcodebuild and xctest are unavailable; see logs/xctest-toolchain.log",
    },
}
(result_dir / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

print(json.dumps({"artifact_integrity_ok": payload["artifact_integrity_ok"], "screenshots": len(screenshots), "declared_xcui_tests": len(declared_tests), "missing": missing, "dimension_errors": dimension_errors, "semantic_checks": semantic_checks, "appearance_checks": appearance_checks}, ensure_ascii=False))
raise SystemExit(0 if payload["artifact_integrity_ok"] else 5)
PY
