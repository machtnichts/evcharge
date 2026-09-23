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

## The inverter is read-only, provably

The app reads the SolarEdge through the proxy and never writes to it: the house battery belongs
to the inverter, and the owner's rule is that this app does not steer it ("Ich will nicht Akku
steuern"). So the Modbus client has no write method at all and the site driver has nothing that
touches the storage registers - both were removed, and `tests/test_solaredge_decode.py` pins it
structurally: no write method on the client, nothing battery-steering on the driver, and the
control/limit register addresses must not reappear in the code.

The live canary is the proxy's own counter: `upstream_writes` must stay 0, and the proxy card
turns any value above 0 into a *bad* status ("PROXY WROTE TO THE DEVICE"). Measured: 0 writes
across 59k poll cycles. The only device this app writes to is the **wallbox** (current limit,
enable, neutral), and only through the single write chokepoint.

## Session energy, three times

The wallbox's own session figure under-reads on this plant, so the app keeps two more from the
SDM630 in the garage (`evcharge/session_meter.py`): the meter's kWh counters are latched at
plug-in, the difference is the running session, and on unplug it is frozen as "last session"
and appended to `logs/sdm_sessions.csv` - one row per session, including the go-e figure for
comparison. The SDM630 measures the garage feeder, which the garage PV also feeds into, so the
meter's figure is `import - export` and the PV share is **not** silently subtracted: import,
export and net are all recorded, and a counter that drops (device reset) rebases the session
instead of reporting a negative number.

The third figure is the meter's *corrected* one, closing the feeder's balance:

    car = import - export + garage PV during the session

so the plain SDM figure sits at the lower end and the corrected one above it. The correction
comes from the inverter's own lifetime counter (`entity_pv`, default
`sensor.garage_pv_energie`) and is only as good as that counter. Over one 12 h window the
inverter said 4.18 kWh where the meter saw 3.88 kWh leave the branch - **at most 8 %, an upper
bound rather than a measured error**: that branch permanently carries a router, the garage door
and the wallbox's standby, and the meter cannot *count* loads that small (start current 0.04 A
= ~9 VA), so its export counter under-reads the PV by exactly what they consume. The 0.30 kWh
gap is 25 W of permanent load over those 12 h, and the owner's own estimate (router ~10 W +
wallbox standby ~5 W) already covers 0.18 kWh of it. It also reports **0.00 kWh** for minutes
after every wake-up. Readings at or below zero, and any
reading below the running maximum, are therefore refused and counted (`pv_artefacts`): taken
as a *baseline*, that 0.00 would turn the next real reading into a ~279 kWh correction. If the
counter never answers (its poller sleeps at night, when the PV is genuinely 0), the correction
is 0 and the CSV row says so; if it wakes mid-session the baseline is latched late and the row
calls the correction partial.

Freshness comes from a value that moves on every poll - a phase voltage (`entity_live` in the
`sdm` block, default `sensor.sdm630_l1_spannung`) and the inverter's power
(`entity_pv_power`, default `sensor.garage_pv_leistung`). A counter that does not change is not
re-written by Home Assistant, so its own timestamp says nothing about whether the meter or the
inverter is still being read - and neither does the meter's power or current, which sit at 0 at
night and would make a perfectly healthy meter look dead. Stale readings make the session
report "waiting" rather than a fabricated 0 kWh - and any session with a stale or late baseline
says so in its CSV note.

## Related repositories

* the Modbus proxy this app reads through (one session to the inverter, many readers)
* the Home Assistant power dashboard of the same plant (Deye logger + dashboard)

The plant's own handover document (what runs, which rules must not break, what is open)
lives in the parent project directory as `STATE.md` and is not part of this repository.
