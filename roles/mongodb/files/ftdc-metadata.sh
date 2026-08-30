#!/usr/bin/env bash

set -euo pipefail

readonly ftdc_file=/ftdc/metrics
document_size="$(od -An -tu4 -N4 "$ftdc_file" | tr -d '[:space:]')"
file_size="$(stat -c %s "$ftdc_file")"

[[ "$document_size" =~ ^[0-9]+$ ]]
((document_size >= 5 && document_size <= file_size))

dd if="$ftdc_file" bs=1 count="$document_size" status=none | bsondump --quiet
