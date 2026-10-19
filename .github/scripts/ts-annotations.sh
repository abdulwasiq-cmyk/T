#!/usr/bin/env bash
# Print one slice (10 errors) of the saved tsc output as GitHub annotations.
# GitHub caps annotations at 10 per step, so CI calls this once per slice.
# Usage: ts-annotations.sh <slice-number starting at 0>
set -euo pipefail

slice="${1:?slice number required}"
input="${RUNNER_TEMP}/tsc.txt"
[ -f "$input" ] || exit 0

awk -v slice="$slice" '
  function flush() {
    if (rec != "") {
      n++
      if (n > slice * 10 && n <= slice * 10 + 10) print rec
    }
    rec = ""
  }
  /^[^ ].*\([0-9]+,[0-9]+\): error TS/ { flush(); rec = $0; next }
  /^ +/ { if (rec != "") rec = rec " " $0; next }
  END { flush() }
' "$input" | while IFS= read -r line; do
  file="${line%%(*}"
  rest="${line#*(}"
  pos="${rest%%)*}"
  msg="${line#*: error }"
  msg="${msg//%/%25}"
  echo "::error file=frontend/${file},line=${pos%%,*},col=${pos##*,}::${msg:0:400}"
done
