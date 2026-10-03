"""Merges what the Frame's Steam client reports (frame_steam.py live) into
/api/status. Runs on your computer, not the Frame. Stdlib only.

- battery.percent becomes Steam's figure, the one the headset shows; the
  kernel's own reading stays as battery.rawPercent.
- controllers: SteamVR only lists a controller once it has connected since
  SteamVR started, so a sleeping one vanishes after a restart. Each one seen
  is remembered per headset (by serial, in controllers.json) and listed as
  not connected, with its last battery level, while it's gone.
"""
import json
import os
import threading
import time

import frame_host

_lock = threading.Lock()


def _store():
    return frame_host.data_dir("controllers.json")


def _load():
    try:
        data = json.loads(_store().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data):
    path = _store()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def _without_seen(known):
    return {s: {**k, "lastSeen": None} for s, k in known.items()}


def _newest(known):
    return max((k.get("lastSeen") or 0 for k in known.values()), default=0)


def controllers(headset, devices, now=None):
    """Live controllers plus remembered ones that aren't listed, connected first."""
    now = time.time() if now is None else now
    out, seen = [], set()
    with _lock:
        data = _load()
        known = data.setdefault(headset, {})
        before = {s: dict(k) for s, k in known.items()}
        for d in devices or []:
            entry = {"model": d.get("model"), "kind": d.get("kind"), "battery": d.get("battery"),
                     "charging": bool(d.get("charging")), "connected": d.get("connected") is not False}
            serial = d.get("serial")
            if serial:
                seen.add(serial)
                if entry["connected"]:
                    entry["lastSeen"] = now
                    known[serial] = {k: entry[k] for k in ("model", "kind", "battery", "lastSeen")}
                else:
                    entry["lastSeen"] = known.get(serial, {}).get("lastSeen")
            out.append(entry)
        for serial, k in known.items():
            if serial not in seen:
                out.append({"model": k.get("model"), "kind": k.get("kind"), "battery": k.get("battery"),
                            "charging": False, "connected": False, "lastSeen": k.get("lastSeen"),
                            "remembered": True})
        # lastSeen alone changes on every poll; only write that every few minutes.
        if _without_seen(known) != _without_seen(before) or _newest(known) - _newest(before) > 300:
            try:
                _save(data)
            except OSError:
                pass  # remembering is a nicety; never fail the status over it
    out.sort(key=lambda c: (not c["connected"], c.get("model") or ""))
    return out


def merge(status, live):
    """Fold frame_steam.py live's answer (or None when Steam didn't answer) into status."""
    if not isinstance(live, dict):
        status["steam"] = None
        return status
    battery, steam_battery = status.get("battery"), live.get("battery")
    if isinstance(battery, dict) and isinstance(steam_battery, dict) and steam_battery.get("percent") is not None:
        battery["rawPercent"] = battery.get("percent")
        battery["percent"] = max(0, min(100, int(steam_battery["percent"])))
    status["steam"] = {
        "downloads": live.get("downloads"),
        "running": live.get("running"),
        "controllers": controllers(status.get("hostname") or "frame", live.get("devices")),
    }
    return status
