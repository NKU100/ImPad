#!/usr/bin/env bash
set -euo pipefail

magisk_path="${IMPAD_MAGISK_PATH:-/debug_ramdisk/magisk}"
evidence=work/root-verification/evidence
mkdir -p "$evidence"
trap 'adb logcat -d -v threadtime > "$evidence/logcat.txt" 2>&1 || true; adb shell getprop > "$evidence/device-properties.txt" 2>&1 || true' EXIT
exec > >(tee "$evidence/root-check.txt") 2>&1

read_shell_uid() {
  local uid
  uid="$(timeout 10s adb shell id -u)" || return $?
  uid="${uid//$'\r'/}"
  uid="${uid//$'\n'/}"
  printf '%s' "$uid"
}

transition_adbd() {
  local mode="$1"
  local expected_uid="$2"
  local attempt command_status command_output uid

  for attempt in 1 2 3; do
    if command_output="$(timeout 10s adb "$mode" 2>&1)"; then
      command_status=0
    else
      command_status=$?
    fi
    printf 'adb %s attempt %s/3 exited %s: %s\n' \
      "$mode" "$attempt" "$command_status" "$command_output"

    if timeout 20s adb wait-for-device; then
      if uid="$(read_shell_uid 2>&1)"; then
        if [[ "$uid" == "$expected_uid" ]]; then
          if (( command_status != 0 )); then
            printf 'Accepting adb %s transport error because shell UID is %s\n' "$mode" "$expected_uid"
          fi
          return 0
        fi
        printf 'adb %s attempt %s reached shell UID %s; expected %s\n' \
          "$mode" "$attempt" "$uid" "$expected_uid"
      else
        printf 'adb %s attempt %s could not verify shell UID: %s\n' \
          "$mode" "$attempt" "$uid"
      fi
    else
      printf 'adb %s attempt %s did not reconnect before timeout\n' "$mode" "$attempt"
    fi

    if (( attempt < 3 )); then
      sleep 1
    fi
  done

  printf 'adb %s transition failed: shell UID did not reach %s after 3 attempts\n' \
    "$mode" "$expected_uid" >&2
  return 1
}

initial_uid="$(read_shell_uid)"
if [[ "$initial_uid" != 2000 ]]; then
  printf 'Expected initial adbd shell UID 2000, got %s\n' "$initial_uid" >&2
  exit 1
fi
adb shell id
adb shell ${magisk_path} -v
adb shell pidof magiskd

# Authorize shell in this disposable test device, then restore ordinary adbd.
transition_adbd root 0
adb shell "${magisk_path} --sqlite \"REPLACE INTO policies (uid,policy,until,logging,notification) VALUES (2000,2,0,1,0)\""
transition_adbd unroot 2000
adb shell id | tee "$evidence/shell-id.txt"
grep -F 'uid=2000(shell)' "$evidence/shell-id.txt"
adb shell ${magisk_path} su -c id | tee "$evidence/su-id.txt"
grep -F 'uid=0(root)' "$evidence/su-id.txt"
grep -F 'context=u:r:magisk:s0' "$evidence/su-id.txt"
adb shell ${magisk_path} su -c "${magisk_path} -v; ${magisk_path} -V" | tee "$evidence/magisk-version.txt"
grep -Fx '31000' "$evidence/magisk-version.txt"
touch "$evidence/root-verified.txt"
