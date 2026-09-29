# Power, heat and what you can control

What the Frame spends power on while it's on, where the heat comes from, and
which controls exist. Everything here was **read** on the Frame on
2026-09-29 (BUILD_ID 20260925.6191901). Nothing was changed. Numbers under
load haven't been measured yet.

## Sensors you can read without sudo

| What | Where |
|---|---|
| Power per rail (W ×10⁶) | hwmon `max34417_10`: `vph` (whole system), `s1c`, `s3c`, `s6c`. `max34417_12`: `apc0`/`apc1`/`apc2` (CPU clusters), `nsp1`. `max34417_1a`: `gfx` (GPU), `nsp2`, `bob`. Each `powerN_input` has a `powerN_label` |
| Board temperatures (m°C) | `/sys/bus/iio/devices/iio:device0/in_temp_*_input`: battery, left and right display, heatsink fins, fan exhaust, Wi-Fi, flash, 40-pin connector, nRF radio, PMIC and charger die |
| CPU, GPU and modem zones | `/sys/class/thermal/thermal_zone*/{type,temp}` (per-core top and bottom, `gpuss-*`, `nsp*`, `video`) |
| Charger input and charge current | `iio:device0/in_current_pm8550b_{iin,ichg}_fb_input` (µA) |
| Battery | `/sys/class/power_supply/max1720x_bat_7-36/uevent` (cycle count, health, current) |
| Fan | hwmon `slg4ax46073v`: `fan1_input` (RPM), `pwm1` (%) |
| Proximity (worn or not) | `/sys/bus/iio/devices/iio:device2/in_proximity_raw` |
| Why the fan ramped | `journalctl -u deckard-fan-control` (`C3 temperature of 95.36 greater than max 95! Setting fan to max speed.`) |

The right display thermistor reads −16 °C, so it's absent or broken. Ignore it.

## Idle baseline (2026-09-29 08:05)

Conditions:
- On the 12 V USB-C charger, battery 100%, 5 cycles.
- Headset off-head, SteamVR in standby, backlight 0.
- Steam, SteamVR, the tracking service and one Chromium (Mac view) running.
- `pauseCompositorOnStandby` = false, set by another session this morning for testing.

30 s average:

| Rail | W | Notes |
|---|---|---|
| `vph` (everything) | **4.71** | |
| CPU `apc0+1+2` | 0.61 | 91% idle overall |
| GPU `gfx` | 0.25 | GPU at 366 of 903 MHz |
| NSP `nsp1+2` | 0.08 | Neural and DSP processors |
| `s1c` + `s3c` + `s6c` + `bob` | 1.01 | SoC, memory and peripheral supplies (which rail is which isn't documented) |
| Unmetered remainder | ~2.8 | Cameras, display link, Wi-Fi, fan, sensors, conversion losses (inferred) |

Temperatures:

| Sensor | °C |
|---|---|
| CPU cores | 40–45 |
| Board (heatsink, Wi-Fi, flash) | 34–37 |
| Left display thermistor | 46 (the warmest) |
| Charger IC | 37 |
| Battery | 23 |

The fan ran at about 8,350 RPM at `pwm1` 41.

What was running while idle (`top`):
- The compositor was at about 14% of one core.
- Steam was at about 7%.
- The tracking service (XRService) was at about 4%, and still had 4 camera nodes open (`/dev/video0,3,9,13`).
- vrserver and gamescope were at about 3% each.

## Why it's warm while "doing nothing"

- **The compositor keeps rendering in standby** when
  `power.pauseCompositorOnStandby` is false. The default is true. Check
  `~/.config/openvr/config/steamvr.vrsettings`.
- **The tracking cameras stay open in standby.** XRService holds them so
  tracking resumes instantly.
- **The fan never goes below 40% on the charger.** That's by design in
  `/usr/share/deckard-fan-control/deckard-config.yaml`:
  `fan_charging_min_speed: 40`, against `fan_min_speed: 30` on battery.
  Charging, including topping up at 100%, heats the charger IC and battery
  area.
- **The display link stays up at backlight 0.** The DSI connector reports
  `dpms=On` while the backlight is 0, so the panels are dark but still driven.
- **Heavy load reaches the throttle limit.** On 2026-09-28 at 22:09–22:12,
  cores C3, C5 and C7 hit 95 °C and the fan went to max. The cause wasn't
  investigated. It came around a Steam restart after the reboot.

## Controls

No sudo needed:

| Control | How | Effect | Caveat |
|---|---|---|---|
| Steam idle sleep | `scripts/keep-awake.sh`, `system_idle_suspend_{ac,battery}_sec` | When it sleeps | Sleep has no remote wake |
| Compositor pause in standby | `vrcmd --set-settings-bool power.pauseCompositorOnStandby 1` | Stops rendering while off-head | Other sessions toggle it for testing, so coordinate |
| Screens-off delay | `vrcmd --set-settings-float power.turnOffScreensTimeout <s>` | Backlight off sooner or later | Default 5 s |
| Stop the whole VR stack | `systemctl --user stop steamvr.service` | Cameras, tracking and compositor off | Also stops the gamescope VR session and Steam (seen 2026-09-28), so panels and Mac view go too. The restart cost hasn't been measured |
| Background apps | Close Mac-view Chromium, stop Lepton containers (`podman`) | Less CPU and memory | Mac view needs relaunching |
| Brightness | Steam settings | Panel power while worn | — |

Needs root (reset at reboot unless made persistent):

| Control | Where | Effect |
|---|---|---|
| CPU governor and max frequency per cluster | `/sys/devices/system/cpu/cpufreq/policy{0,2,5,7}/scaling_{governor,max_freq}` (`powersave`, `conservative`, `schedutil`, …) | Caps CPU power and heat |
| Take cores offline | `/sys/devices/system/cpu/cpuN/online` | Fewer active cores |
| GPU max frequency | `/sys/class/devfreq/3d00000.gpu/max_freq` | Caps GPU power (it hurts VR smoothness when worn) |
| Fan curve | The service reads `/usr/share/…/deckard-config.yaml`, which is on the read-only rootfs. It could be overridden with a systemd drop-in pointing at a copy in `/etc` | Quieter fan while charging, but hotter parts |
| Charge current | `pm8550b-charger` `constant_charge_current` (1.0 A, max 1.2 A) | Writability unverified. There's **no** charge-limit or end-threshold file, so the battery sits at 100% on the charger |

None of these have been tried yet.

## Plan: leave it on, but cheaply

Goal: the Frame stays on the charger, reachable over SSH, drawing as little
power and making as little heat, fan wear and battery wear as possible while
nobody wears it. When someone puts it on, everything comes back quickly.
Sleeping isn't an option until remote wake works (see
[open-questions.md](open-questions.md#remote-wake-2026-09-28)).

Rules for every step:
- Change one thing at a time. Snapshot the setting first, measure 5 minutes
  (`vph` plus temperatures and fan), then restore it unless it's being kept.
- Freeze the Steam and SteamVR health-check trackers first
  ([frame-doctor.md](frame-doctor.md) §4).
- Anything that could leave the Frame unreachable, and every root change, waits
  until someone is home to recover it.
- `power.pauseCompositorOnStandby` belongs to another session's testing. Ask
  before touching it.

### 1. Measure (read-only, safe remotely)

- [x] Idle baseline off-head on the charger (above).
- [ ] A sampler script (`scripts/frame-power-sample.sh`) that logs `vph`, the
      CPU/GPU rails, key temperatures, fan RPM, the proximity sensor and the
      charger current every few seconds to a CSV. Every later step uses it.
- [ ] The same baseline worn, idle in the home space.
- [ ] Under load: Mac view streaming, and one VR game.
- [ ] On battery (unplugged) to separate charging heat from everything else.
- [ ] Find what caused the 95 °C spike on 2026-09-28 22:09–22:12 (journal and
      process history around the Steam restart).

### 2. Settings without sudo (one at a time, measured)

- [ ] Compositor pause in standby (after asking the owning session).
- [ ] Shorter screens-off delay.
- [ ] Stop `steamvr.service` while off-head. Measure the saving and how long it
      takes to come back, since it also stops the gamescope session and Steam.
- [ ] Close Mac-view Chromium and stop idle Lepton containers.
- [ ] Steam's "never sleep on AC" (`system_idle_suspend_ac_sec = 0`), keeping
      the battery timer. Confirm the displays go dark.

### 3. Root settings (at home, with approval)

- [ ] CPU: `powersave` or a lower max frequency while off-head.
- [ ] Fan: a copy of the fan config with a lower charging minimum, through a
      systemd drop-in. Only if temperatures in steps 1–2 leave headroom.
- [ ] Battery: check whether charge current is writable. There's no charge
      limit, so the fallback is a Home Assistant smart plug that lets the
      battery cycle between roughly 80% and 100%.
- [ ] Decide which root changes to make persistent (drop-ins in `/etc`, which
      survive SteamOS updates, unlike `/usr`).

### 4. A quiet mode

- [ ] Put the kept settings behind one switch: off-head for N minutes → quiet
      mode; on-head (proximity sensor) or a Frame Control request → normal.
- [ ] Run it from Frame Control / keep-awake, not a hand-edited setting, so it
      can always be undone.
- [ ] Add a doctor check that reports whether quiet mode is on and that it
      restores cleanly.

### 5. Remote wake (at home)

- [ ] Charger wake: during confirmed sleep, plug in, unplug, and switch the
      charger's AC off and on, one at a time.
- [ ] Controller-button wake.
- [ ] RTC dark wake (root timer that wakes, checks for queued work, sleeps).
- [ ] Re-test WoWLAN after each SteamOS or kernel update.

### 6. Doctor script

- [ ] Turn [frame-doctor.md](frame-doctor.md) into `scripts/frame-doctor.sh`:
      read-only checks by default, fixes only with a flag, and **ask** fixes
      never automatic. Add the sensor reads from this page.
