#!/usr/bin/env bash
# Home Assistant add-on entrypoint.
set -euo pipefail

echo "[evcharge] starting, options:"
if [ -f /data/options.json ]; then
  # never echo the mqtt password
  python3 - <<'PY'
import json
try:
    with open("/data/options.json") as fh:
        o = json.load(fh)
except Exception as exc:
    print("[evcharge] could not parse options: %s" % exc)
else:
    o.pop("mqtt_password", None)
    print(json.dumps(o, indent=1))
PY
else
  echo "[evcharge] /data/options.json not found, using built-in defaults"
fi

cd /app
exec python3 -m evcharge.main
