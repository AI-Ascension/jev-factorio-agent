# Dashboard research tree

The dashboard shows where a run is on the game's research tree. It reads the tree
from the running game, so each version gets its own: Factorio 1.1 (older FLE
setups), 2.0.x, and 2.0 with Space Age. Nothing in the dashboard names a
technology.

## What it shows

- **Research strip** (top bar): the technology being researched and its
  progress, or the trigger for 2.0 trigger technologies ("Craft 10 × iron gear
  wheel"); how many technologies on the path are done per science pack; and the
  overall count to the goal, with how many are available to research now.
- **Current objective**: the controller's goals around research milestones.
  Milestones are the game's own `essential` technologies on the path (2.0+), or
  the science-pack unlocks and the goal on 1.1, where `essential` doesn't exist.

The **path** is every technology the goal depends on:

| Game | Goal | Milestones |
| --- | --- | --- |
| 1.1 | `rocket-silo` | science packs (red is "from start"), rocket silo |
| 2.0 base | `rocket-silo` | essential technologies on the path; military science is essential but not needed for the silo, so it is left out |
| 2.0 + Space Age | every essential technology | science packs, rocket silo, planet discoveries, through promethium science |

Tiers group technologies by the most advanced science pack they need. Pack order
comes from the depth of the technology that unlocks each pack. Trigger
technologies take the tier of their prerequisites. Infinite and hidden
technologies are never on the path.

## Where the data comes from

`lua/research_catalog.lua` is a read-only RCON script. It detects the Lua API
generation (1.1 `game.*_prototypes`/`global`, or 2.0 `prototypes`/`storage`/
`helpers`) and reads every member through `pcall`, because members differ between
versions. It exports the tree plus the agent force's research state as
`jev.research.v1` JSON.

It reaches the dashboard as a sidecar file, `research-catalog.json`:

- **This controller (FLE backend):** written next to `--dashboard-events` when enabled, otherwise
  next to `--log-file`, at startup. It is best effort: a failure prints a warning
  and never affects the run.
- **Any other FLE or RCON setup, including 1.1.110:** run the exporter yourself.
  `--watch` keeps the research state fresh when no controller telemetry carries
  it:

  ```
  JEV_RCON_PASSWORD=... python -m jev_factorio.research_catalog \
      --host 127.0.0.1 --port 27015 --out research-catalog.json [--watch 10]
  ```

The dashboard looks for `research-catalog.json` next to its telemetry file
(override with `--research-catalog`) and reloads it when it changes.
Controller telemetry (`researched`, and the factory's current research) is
preferred over the sidecar's own state. If telemetry names a technology the tree
doesn't have, or supplies a game version different from the catalog, the strip
is hidden ("version mismatch") and the fixed milestone list is shown instead.
Legacy telemetry without a game version retains its name-based summary with
`version_identity: unknown`; matching telemetry reports `matched`, while the
sidecar's own state reports `catalog-only`. This is display provenance, not
native compatibility or deployment acceptance.
Recorded research history retains its last observed game version across
incomplete observations and catalog replacement. A complete observation that
identifies a different version starts fresh history instead of mixing versions.

When no ordinary topic is current, an eligible trigger on the goal path is
shown with unknown progress. For telemetry, this requires a matching game
version and a mission projection bound to the current tick and session; the
sidecar's own state can also supply it. Incomplete observations and legacy
unknown-version telemetry cannot revive a trigger from the catalog. Ordinary
research takes precedence. Formula-based costs alone do not make a technology
infinite: only the exported maximum-level sentinel `4294967295` does that.
Large finite maximum levels remain part of the path.

Tick-bound research sets retain up to 5,000 valid names through event writing
and polling, with credential redaction and the event byte limit still enforced.
Malformed science-pack tables make the sidecar invalid; polling continues and
a valid replacement can recover it. Missing or null legacy pack tables remain
supported. Watch intervals must be finite and at least two seconds.

## Icons

`--icon-dir` may hold one folder per game version. The dashboard serves
`<icon-dir>/<version>/<name>.png` first, then `<icon-dir>/<name>.png`. Take icons
from the matching game install (`data/base/graphics/icons`, plus
`data/space-age/graphics/icons` for Space Age). They are Wube assets; don't
commit them. Missing icons fall back to a colour for each known pack and to text
elsewhere.
