#!/bin/sh
# Screenshots every mock in docs/mocks with headless Edge (Windows, Git Bash).
cd "$(dirname "$0")/../../docs/mocks" || exit 1
EDGE="/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
URL="file:///$(cygpath -m "$PWD")"
shoot() {  # file width height
    out="$(cygpath -w "$PWD/${1%.html}.png")"
    "$EDGE" --headless --disable-gpu --hide-scrollbars --window-size="$2,$3" --virtual-time-budget=3000 \
        --screenshot="$out" "$URL/$1" >/dev/null 2>&1
}
for f in firmware/*.html; do shoot "$f" 500 900; done
for f in server/*.html; do
    case "$f" in
        *activity*.html) h=1900 ;;
        *users.html|*updates.html) h=1500 ;;
        *sign-in*|*not-allowed*) h=640 ;;
        *) h=1000 ;;
    esac
    shoot "$f" 1180 "$h"
done
ls firmware/*.png server/*.png | wc -l
