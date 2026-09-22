# evcharge — solar-surplus charging controller

The charging controller for a specific hybrid plant (SolarEdge inverter with a house
battery + go-e Charger HOME+ wallbox). It reads the site (PV, grid, battery) through the
single-client Modbus proxy, decides what the wallbox should do, and drives it over the
go-e HTTP API v1. It also exposes a small web UI, a REST API and optional MQTT discovery
so Home Assistant can show and steer it.

Deliberately plant-specific, not a general-purpose tool: the rules come from this plant -
settled by measuring it, and by studying how a mature open-source controller solves the
same problems - while the code is written from scratch for this installation.

## Layout

```
evcharge/            the application
  controller.py      decision logic (pure: state in -> decision out, no hardware)
  main.py            service loop, web UI, REST API, settings, persistence
  proxy.py           reads the Modbus proxy's /status (display only)
  ha.py              small Home Assistant state reader (env token, never in a URL)
  mqtt.py            optional MQTT publishing + HA discovery
  drivers/goe.py     go-e wallbox (HTTP API v1, measured `nrg` offsets)
  drivers/modbus.py  site reader against the proxy
  drivers/solaredge.py SunSpec decode of inverter + meter windows
config.example.json example config; copy to config.json (which is git-ignored)
config.yaml          Home Assistant add-on options (supervisor owns the real one)
run.sh, Dockerfile   start paths (systemd unit, HA add-on container)
systemd/             user unit used in production
tests/               hand-rolled suites, plain `python3 tests/<name>.py`
tools/               probes and one-off helpers used on the live plant
```

## Running

```sh
python3 -m evcharge.main --config config.json     # or ./run.sh
curl -s 127.0.0.1:7080/api/state                  # live state as JSON
```

No third-party Python packages: standard library only, so the service survives a system
update without a virtualenv.

## Tests

```sh
for t in tests/test_*.py; do python3 "$t"; done
```

Each suite prints one line per check and exits non-zero on failure. They cover the
decision logic (including the timing/dwell paths and the tariff window), the drivers
against synthetic register/HTTP fixtures, the phase policy, the charge-switching safety
counter, MQTT framing, and a service smoke test that builds the app the way `main()` does.

## Modes

* `pv` — follow the PV surplus; never charges from the grid deliberately
* `minpv` — start at the minimum current as soon as there is minimum surplus
* `cheap_hours` — inside the configured tariff window charge from the grid at maximum
  current, outside it behave like `pv`
* `now` — charge at maximum now, no meter needed
* `manual` — handover: the app reads and reports, and writes nothing at all
* `off`

## Session energy, twice

The wallbox's own session figure under-reads on this plant, so the app keeps a second one
from the SDM630 in the garage (`evcharge/session_meter.py`): the meter's kWh counters are
latched at plug-in, the difference is the running session, and on unplug it is frozen as
"last session" and appended to `logs/sdm_sessions.csv` - one row per session, including the
go-e figure for comparison. The SDM630 measures the garage feeder, which the garage PV also
feeds into, so the figure is the *meter's* view of the session and the PV share is
deliberately **not** subtracted: import, export and net are all recorded, and a counter that
drops (device reset) rebases the session instead of reporting a negative number.

Freshness comes from a value that moves on every poll - a phase voltage (`entity_live` in the
`sdm` block, default `sensor.sdm630_l1_spannung`). A counter that does not change is not
re-written by Home Assistant, so its own timestamp says nothing about whether the meter is
still being read - and neither does the meter's power or current, which sit at 0 at night and
would make a perfectly healthy meter look dead. Stale readings make the session report "waiting"
rather than a fabricated 0 kWh - and any session with a stale or late baseline says so in
its CSV note.

## Related repositories

* the Modbus proxy this app reads through (one session to the inverter, many readers)
* the Home Assistant power dashboard of the same plant (Deye logger + dashboard)

The plant's own handover document (what runs, which rules must not break, what is open)
lives in the parent project directory as `STATE.md` and is not part of this repository.
