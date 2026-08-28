#!/bin/sh
set -e
VENV=/var/lib/beschaffung/venv
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q -e /opt/beschaffung
cd /opt/beschaffung
exec "$VENV/bin/python" -m app
