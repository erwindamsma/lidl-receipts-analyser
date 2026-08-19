#!/usr/bin/env bash
# Print analysis/report.html to analysis/report.pdf.
#
# There is no PDF toolchain on the Linux side of this WSL install, but Chrome
# on the Windows side prints headless just fine. The catch is the file path:
# Chrome is a Windows process, so it cannot open /home/... -- the HTML has to
# be handed over somewhere both sides can see, which is what the copy through
# the Windows temp directory is for.
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f analysis/report.html ] || {
    echo "analysis/report.html ontbreekt -- draai eerst:" >&2
    echo "  python3 analysis/spending.py > analysis/spending.json" >&2
    echo "  python3 analysis/report.py   > analysis/report.html" >&2
    exit 1
}

CHROME=""
for candidate in \
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe" \
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe" \
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" \
    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe"; do
    [ -x "$candidate" ] && { CHROME="$candidate"; break; }
done
[ -n "$CHROME" ] || { echo "Geen Chrome of Edge gevonden op de Windows-kant." >&2; exit 1; }

WIN_TMP=$(cmd.exe /c 'echo %TEMP%' 2>/dev/null | tr -d '\r')
LNX_TMP=$(wslpath -u "$WIN_TMP")
URL_TMP=$(echo "$WIN_TMP" | tr '\\' '/')

cp analysis/report.html "$LNX_TMP/lidl-rapport.html"
"$CHROME" --headless=new --disable-gpu --no-pdf-header-footer \
    --run-all-compositor-stages-before-draw --virtual-time-budget=10000 \
    --print-to-pdf="$WIN_TMP\\lidl-rapport.pdf" \
    "file:///$URL_TMP/lidl-rapport.html" 2>&1 | tail -1
cp "$LNX_TMP/lidl-rapport.pdf" analysis/report.pdf
rm -f "$LNX_TMP/lidl-rapport.html" "$LNX_TMP/lidl-rapport.pdf"

echo "analysis/report.pdf: $(du -h analysis/report.pdf | cut -f1)"
