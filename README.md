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

## PV forecast (step 1: evidence, not control)

The house battery fills by 10-11:00 in summer, and the only flexible consumer left is the car.
Which of the two should get the surplus is a *weather* question, so the controller will need a
forecast of the rest of the day. Step 1 - what is built here - **displays and logs that forecast
and steers nothing**, because the owner wants to judge it against his own roof first.

* `evcharge/pv_forecast.py`: Open-Meteo `global_tilted_irradiance` per roof plane (no key, one
  request per plane per hour), `GTI x kWp` per hour = the DC estimate, `x PR` = the AC estimate.
* Config block `forecast`: coordinates, planes (`kwp`/`azimuth`/`tilt`), `pr`, `every_s`,
  `write_s`, `house_reserve_kwh`, `margin`, and the three file paths. With `enabled: false` the
  module is not even constructed.
* **The factor is computed and logged, but it was disproved as a control input.** The idea was
  `measured today / predicted for exactly this window`, pulling a dull morning's forecast down
  with it. Seven days of 15-minute portal exports killed it: 08.09. had a dead morning (2.56 of
  6.88 kWh predicted) followed by a strong afternoon, 10.09. was the mirror image (morning
  exactly as predicted, 0.98) with the afternoon collapsing to 0.51. The morning therefore says
  nothing about the afternoon, in either direction - a noon factor would have told the owner "all
  good" on 10.09. and spent the battery on the car. It keeps a basis requirement (below 0.05 kWh
  measured there is no factor; outside 0.25-1.60 it is refused with a warning) and stays in the
  record, where it can be compared against the rule below.
* **The rule that survived is seasonal-relative** - the owner's own idea, sharpened by the same
  seven days: a day total near the top of what this season can do cannot happen without a strong
  afternoon, so a high forecast may hand the morning to the car. Measured: at 88-93 % of the
  season's best day the afternoon was strong every time (17.9-19.2 kWh in 12-18 h), at 71-78 %
  mediocre every time (8.9-14.1 kWh); the two groups do not overlap. So the reference is the
  plant's OWN best complete day of the last 30 (`rolling_best`, read back from the day records so
  a restart cannot wipe it) with a **90 % threshold** (`rule_by_best`). Against all seven days:
  **7 of 7 classified correctly**. Days before this app existed are seeded from the inverter's own
  export (`logs/pv_days_seed.csv`), because a reference without history is a guess.
* **Evidence, all of it on disk** (a restart must not cost the history): `logs/pv_forecast_today.json`
  (rewritten every `write_s`, resumed after a restart) and one row per finished day in
  `logs/pv_forecast.csv` - the forecast both as a total and split into morning/midday/afternoon/
  evening, the measured figures (AC side and array side), both factors, house/car/battery energy,
  SOC range, `samples`, and the rule columns (`best30_kwh`, `best30_threshold_kwh`, `best30_pct`,
  `rule_best_says`, `rule_margin_says`). Both rules are written down every day precisely so the
  history can later decide which one was right. The measured figures are a zero-order hold of the
  app's own reads, so `samples` is part of the record; a stale reading is never integrated.
* **The day's production comes from the inverter's own counter**, not from an integral: SunSpec
  model 101 `WH` sits inside the window this driver reads anyway, so it costs **no extra Modbus
  traffic** (pinned by a test: still three reads, 103 registers). It is exact, it survives a
  service outage, and it counts the battery's later discharge with it - which is what the
  owner's monitoring app reports as production, and it is fair (energy into the battery is
  counted once, when it comes out). Verified live: over the same 3.5 minutes the counter
  advanced 0.0200 kWh and the app's AC integral advanced 0.0200 kWh. Compare the two as
  **deltas over the same window**, never as totals right after a restart: the integral resumes
  from the day's file, the counter's baseline only if the file has one.
* The UI rows say "PV forecast today", "forecast rest of day", "factor today", "forecast vs best
  day (30d)", "rule A (factor+margin) would say" and "rule B (season) would say". The two rule
  rows are *displayed* verdicts that nothing acts on, each with its inputs in the tooltip so
  neither can be mistaken for a decision.
* **Pinned by tests** (`tests/test_pv_forecast.py`): the module has no actuator vocabulary and
  `evcharge/controller.py` does not contain the word "forecast" at all. That is what makes "step
  1 steers nothing" checkable rather than promised - and it stays that way until step 2 is
  deliberately built.

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
