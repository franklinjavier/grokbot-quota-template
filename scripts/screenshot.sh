#!/bin/sh
# Full-page captures of the dashboard (headless Chrome, no desktop).
# No argument: rebuild the fake demo and capture that.
# --real-db PATH: anonymize that quota database into docs/demo/dashboard.html, then capture.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$ROOT"
HTML="$ROOT/docs/demo/dashboard.html"
REAL_DB=""
NO_BUILD=""
while [ $# -gt 0 ]; do
  case "$1" in
    --real-db)
      if [ $# -lt 2 ]; then
        echo "missing path after --real-db" >&2
        exit 1
      fi
      REAL_DB=$2
      shift 2
      ;;
    --no-build)
      NO_BUILD=1
      shift
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 1
      ;;
  esac
done
if [ -n "$NO_BUILD" ]; then
  :
elif [ -n "$REAL_DB" ]; then
  python3 -m quota.dashboard --db "$REAL_DB" --anonymize --out "$HTML"
else
  python3 -m quota.demo --seed 1 --out "$HTML"
fi
URI=$(python3 -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).resolve().as_uri())' "$HTML")

capture() {
  # Separate names: /bin/sh functions share variables, and `dest` is the final path.
  cap_w=$1
  cap_h=$2
  cap_dest=$3
  udd=$(mktemp -d)
  google-chrome \
    --headless=new \
    --disable-gpu \
    --no-sandbox \
    --hide-scrollbars \
    --disable-dev-shm-usage \
    --force-device-scale-factor=2 \
    --user-data-dir="$udd" \
    --window-size="${cap_w},${cap_h}" \
    --screenshot="$cap_dest" \
    "$URI" >/dev/null 2>&1 || {
      rm -rf "$udd"
      echo "chrome failed for ${cap_w}x${cap_h} -> $cap_dest" >&2
      exit 1
    }
  rm -rf "$udd"
  if [ ! -s "$cap_dest" ]; then
    echo "empty screenshot: $cap_dest" >&2
    exit 1
  fi
}

# Print the CSS-pixel height of the page painted into a screenshot.
# Arg 1 is the PNG. Arg 2 is the requested window width (to recover scale).
content_height() {
  python3 - "$1" "$2" <<'PY'
import struct
import sys
import zlib

path, requested = sys.argv[1], int(sys.argv[2])

def paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c

data = open(path, "rb").read()
if data[:8] != b"\x89PNG\r\n\x1a\n":
    raise SystemExit(f"not a png: {path}")
pos = 8
width = height = color = None
idat = []
while pos + 8 <= len(data):
    length = struct.unpack(">I", data[pos:pos + 4])[0]
    kind = data[pos + 4:pos + 8]
    chunk = data[pos + 8:pos + 8 + length]
    pos += 12 + length
    if kind == b"IHDR":
        width, height, bit, color, comp, filt, inter = struct.unpack(">IIBBBBB", chunk)
        if bit != 8 or inter != 0 or color not in (2, 6):
            raise SystemExit(f"unsupported png {bit=} {color=} {inter=}")
    elif kind == b"IDAT":
        idat.append(chunk)
    elif kind == b"IEND":
        break
channels = 4 if color == 6 else 3
stride = width * channels
raw = zlib.decompress(b"".join(idat))
rows = []
i = 0
prev = bytearray(stride)
for _y in range(height):
    filt = raw[i]
    i += 1
    row = bytearray(raw[i:i + stride])
    i += stride
    for x in range(stride):
        left = row[x - channels] if x >= channels else 0
        up = prev[x]
        ul = prev[x - channels] if x >= channels else 0
        if filt == 0:
            pred = 0
        elif filt == 1:
            pred = left
        elif filt == 2:
            pred = up
        elif filt == 3:
            pred = (left + up) // 2
        elif filt == 4:
            pred = paeth(left, up, ul)
        else:
            raise SystemExit(f"png filter {filt}")
        row[x] = (row[x] + pred) & 255
    prev = row
    rows.append(row)

scale = width / requested
if scale <= 0:
    raise SystemExit("bad scale")
bg = rows[min(2, height - 1)][:3]

def content(row):
    # Sample across the row. A painted row differs from the page background.
    step = max(1, width // 80) * channels
    for x in range(0, stride, step):
        if abs(row[x] - bg[0]) + abs(row[x + 1] - bg[1]) + abs(row[x + 2] - bg[2]) > 18:
            return True
    return False

last = 0
for y, row in enumerate(rows):
    if content(row):
        last = y
clipped = last >= height - 2
css = int(round((last + 1) / scale))
print(f"{css} {1 if clipped else 0} {width} {height}")
PY
}

fit() {
  width=$1
  dest=$2
  guess=4200
  if [ "$width" -lt 500 ]; then
    guess=9000
  fi
  tmp=$(mktemp --suffix=.png)
  while true; do
    capture "$width" "$guess" "$tmp"
    # shellcheck disable=SC2046
    set -- $(content_height "$tmp" "$width")
    css_bottom=$1
    clipped=$2
    if [ "$clipped" -eq 0 ]; then
      break
    fi
    guess=$((guess + 2500))
    if [ "$guess" -gt 20000 ]; then
      echo "page taller than 20000px at width $width" >&2
      exit 1
    fi
  done
  # A little background under the last painted pixel, still inside the page.
  height=$((css_bottom + 20))
  if [ "$height" -lt 600 ]; then
    height=600
  fi
  capture "$width" "$height" "$dest"
  rm -f "$tmp"
  echo "wrote $dest (${width}x${height})"
}

fit 1280 "$ROOT/docs/dashboard.png"
fit 390 "$ROOT/docs/dashboard-mobile.png"
