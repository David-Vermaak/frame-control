#!/usr/bin/env python3
"""Frame-side: manage non-Steam shortcuts through the Steam client's CEF debug
port (127.0.0.1:8080, target SharedJSContext), without restarting Steam.
Python stdlib only; the Mac runs it with `ssh frame python3 - <args> < this`.

  steam_shortcuts.py add NAME EXE START_DIR [ICON]  -> prints the shortcut app id
  steam_shortcuts.py list                           -> JSON [{appid, name, exe}]
  steam_shortcuts.py configure APPID NAME EXE START_DIR ICON VR ARTWORK_JSON
  steam_shortcuts.py stop APPID
  steam_shortcuts.py remove APPID
"""
import base64, json, os, re, socket, struct, sys, urllib.request

DEVTOOLS = 'http://127.0.0.1:8080/json'


def target_ws():
    for t in json.load(urllib.request.urlopen(DEVTOOLS, timeout=5)):
        if t.get('title') == 'SharedJSContext':
            return t['webSocketDebuggerUrl']
    sys.exit('SharedJSContext not found: is the Steam client running?')


class WS:
    """Just enough RFC 6455 for one CDP request/response on loopback."""

    def __init__(self, url, timeout=20):
        host_port, path = url[len('ws://'):].split('/', 1)
        host, port = host_port.split(':')
        self.s = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f'GET /{path} HTTP/1.1\r\nHost: {host_port}\r\nUpgrade: websocket\r\n'
                        f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n'
                        'Sec-WebSocket-Version: 13\r\n\r\n').encode())
        buf = b''
        while b'\r\n\r\n' not in buf:
            buf += self.s.recv(4096)
        if b' 101 ' not in buf.split(b'\r\n', 1)[0]:
            sys.exit('websocket handshake failed')
        self.rest = buf.split(b'\r\n\r\n', 1)[1]

    def _read(self, n):
        while len(self.rest) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise EOFError
            self.rest += chunk
        out, self.rest = self.rest[:n], self.rest[n:]
        return out

    def send(self, text):
        data = text.encode()
        mask = os.urandom(4)
        n = len(data)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else
                                bytes([0x80 | 126]) + struct.pack('>H', n) if n < 65536 else
                                bytes([0x80 | 127]) + struct.pack('>Q', n))
        self.s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        msg = b''
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7f
            if n == 126:
                n = struct.unpack('>H', self._read(2))[0]
            elif n == 127:
                n = struct.unpack('>Q', self._read(8))[0]
            msg += self._read(n)
            if b0 & 0x80:
                return msg.decode()


def evaluate(js, timeout=20):
    ws = WS(target_ws(), timeout)
    ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate', 'params': {
        'expression': js, 'awaitPromise': True, 'returnByValue': True}}))
    while True:
        r = json.loads(ws.recv())
        if r.get('id') == 1:
            break
    res = r.get('result', {})
    if 'exceptionDetails' in res:
        sys.exit('JS error: ' + json.dumps(res['exceptionDetails'])[:500])
    return res.get('result', {}).get('value')


# Steam's ELibraryAssetType (Capsule, Hero, Logo, Header, Icon).
ASSETS = {'grid': 0, 'hero': 1, 'logo': 2, 'wide': 3, 'icon': 4}


def collections_js(appid, wanted=()):
    wanted = list(wanted)
    return f'''async function syncCollections() {{
      const wanted = {json.dumps(wanted)};
      if (typeof collectionStore === "undefined" ||
          typeof collectionStore.GetUserCollectionsByName !== "function" ||
          typeof collectionStore.NewUnsavedCollection !== "function" ||
          typeof collectionStore.SaveCollection !== "function")
        return ["Steam collections API unavailable"];
      const app = {{appid: {appid}}};
      const warnings = [];
      for (const name of ["Android", "Android VR", "Sideloaded"]) {{
        const matches = collectionStore.GetUserCollectionsByName(name);
        let collection = matches.find(c => !c.bIsDynamic && c.bAllowsDragAndDrop);
        if (wanted.includes(name)) {{
          if (!collection && matches.length) {{
            warnings.push(name + " is an existing dynamic or read-only collection");
            continue;
          }}
          if (!collection) {{
            collection = collectionStore.NewUnsavedCollection(name, undefined, [app]);
          }} else {{
            collection.AsDragDropCollection().AddApps([app]);
          }}
          await collectionStore.SaveCollection(collection);
        }} else if (collection) {{
          collection.AsDragDropCollection().RemoveApps([app]);
          await collectionStore.SaveCollection(collection);
        }}
      }}
      return warnings;
    }}'''



def notes_js(name, details):
    filename = 'notes_shortcut_' + re.sub(r'[!-/:-@ \[\\\]\^`]', '_', name.strip())
    content = '\n'.join(str(details[k]) for k in ('package', 'version', 'source') if details.get(k))
    return f'''if (SteamClient.GameNotes && typeof SteamClient.GameNotes.GetNotes === "function" &&
                   typeof SteamClient.GameNotes.SaveNotes === "function") {{
      try {{
        const file = {json.dumps(filename)};
        const previous = await SteamClient.GameNotes.GetNotes(file, file + "_images/");
        if (previous.result !== 1 && previous.result !== 9) throw Error("read " + previous.result);
        const data = previous.result === 1 ? JSON.parse(previous.notes) : {{notes: [], shortcut_name: {json.dumps(name)}}};
        if (!Array.isArray(data.notes)) throw Error("unexpected notes format");
        const id = "frame-control-library", now = Math.floor(Date.now()/1000);
        const old = data.notes.find(n => n.id === id);
        const note = {{id, shortcut_name: {json.dumps(name)}, title: "Installation details",
                      content: {json.dumps(content)}, ordinal: old ? old.ordinal : data.notes.length,
                      time_created: old ? old.time_created : now, time_modified: now}};
        data.notes = data.notes.filter(n => n.id !== id).concat([note]);
        const result = await SteamClient.GameNotes.SaveNotes(file, JSON.stringify(data));
        if (result !== 1) throw Error("save " + result);
      }} catch (e) {{ warnings.push("Steam notes: " + String(e)); }}
    }}'''


MAX_ART = 12 * 1024 * 1024  # Steam's custom artwork limit per slot


def render(plan):
    with open(plan) as f:
        source = json.load(f)
    images = {}
    for slot, path in source['images'].items():
        ext = os.path.splitext(path)[1][1:]
        with open(path, 'rb') as f:
            data = f.read(MAX_ART + 1)
        if len(data) > MAX_ART:
            raise ValueError('source artwork too large')
        images[slot] = [ext, base64.b64encode(data).decode()]
    renderer = globals().get('ART_RENDERER')
    if renderer is None:
        with open(os.path.join(os.path.dirname(__file__), 'library_artwork.js')) as f:
            renderer = f.read()
    try:
        return _render(plan, renderer, source['label'], images)
    except (ValueError, OSError, EOFError, SystemExit) as e:
        # Generated art from the icon alone always fits; a photo that didn't must not fail the install.
        result = _render(plan, renderer, source['label'], {k: v for k, v in images.items() if k == 'icon'})
        result['warnings'].insert(0, 'Source artwork could not be rendered (' + str(e)[:120] + '); generated art used')
        return result


def _render(plan, renderer, label, images):
    # A 4K photo takes seconds to decode and encode on the Frame; allow well beyond that.
    result = evaluate(renderer + '\nrenderLibraryArtwork(' + json.dumps({'label': label, 'images': images}) + ')',
                      timeout=75)
    if not isinstance(result, dict) or set(result.get('images', {})) != set(ASSETS):
        raise ValueError('incomplete artwork render')
    paths = {}
    for slot, (ext, encoded) in result['images'].items():
        data = base64.b64decode(encoded, validate=True)
        signature = {'png': b'\x89PNG\r\n\x1a\n', 'jpg': b'\xff\xd8\xff'}.get(ext)
        if not signature or not data.startswith(signature) or len(data) > MAX_ART:
            raise ValueError(slot + ' render is ' + str(len(data)) + ' bytes of ' + str(ext))
        paths[slot] = os.path.join(os.path.dirname(plan), slot + '.' + ext)
        with open(paths[slot] + '.tmp', 'wb') as f:
            f.write(data)
    for slot, path in paths.items():
        os.replace(path + '.tmp', path)
        for stale in ('png', 'jpg'):
            other = os.path.join(os.path.dirname(plan), slot + '.' + stale)
            if other != path and os.path.exists(other):
                os.remove(other)
    return {'paths': paths, 'warnings': list(result.get('warnings', []))}


def configure(appid, name, exe, start_dir, icon, vr, artwork, options=None):
    options = options or {}
    category = options.get('category', 'Android')
    if set(artwork) != set(ASSETS):
        raise ValueError('all five Steam artwork slots are required')
    images = []
    for slot, path in artwork.items():
        if slot not in ASSETS:
            raise ValueError('unknown artwork slot')
        ext = os.path.splitext(path)[1][1:]
        if ext not in ('png', 'jpg'):
            raise ValueError('artwork must be PNG or JPEG')
        with open(path, 'rb') as f:
            data = f.read(MAX_ART + 1)
        if len(data) > MAX_ART:
            raise ValueError('artwork is too large')
        if slot != 'icon':  # Frame's custom-art API maps type 4 to Header; use SetShortcutIcon.
            images.append([ASSETS[slot], ext, base64.b64encode(data).decode()])
    return evaluate(f'''(async () => {{
      const id = {int(appid)}, warnings = [];
      SteamClient.Apps.SetShortcutName(id, {json.dumps(name)});
      if ({json.dumps(exe)}) SteamClient.Apps.SetShortcutExe(id, {json.dumps(exe)});
      if ({json.dumps(start_dir)}) SteamClient.Apps.SetShortcutStartDir(id, {json.dumps(start_dir)});
      if (typeof SteamClient.Apps.SetShortcutSortAs === "function")
        SteamClient.Apps.SetShortcutSortAs(id, {json.dumps(name)});
      SteamClient.Apps.SetShortcutIcon(id, {json.dumps(icon)});
      // null (devkit titles): leave the VR flag as Steam registered it.
      if ({json.dumps(vr)} !== null) {{
        if (typeof SteamClient.Apps.SetShortcutIsVR === "function")
          SteamClient.Apps.SetShortcutIsVR(id, {json.dumps(vr)});
        else warnings.push("Steam VR shortcut flag API unavailable");
      }}
      if (typeof SteamClient.Apps.SetCustomArtworkForApp === "function") {{
        for (const [type, ext, data] of {json.dumps(images)}) {{
          // Steam keeps a slot's PNG and JPEG side by side; clear it so a stale one can't win.
          if (typeof SteamClient.Apps.ClearCustomArtworkForApp === "function")
            try {{ await SteamClient.Apps.ClearCustomArtworkForApp(id, type); }} catch (e) {{}}
          await SteamClient.Apps.SetCustomArtworkForApp(id, data, ext, type);
        }}
      }} else throw new Error("Steam artwork API unavailable; installation is incomplete");
      {collections_js(int(appid), [category] + (['Android VR'] if vr and category == 'Android' else []))}
      try {{ warnings.push(...await syncCollections()); }}
      catch (e) {{ warnings.push("Steam collections: " + String(e)); }}
      {notes_js(name, options.get('details', {}))}
      return {{warnings}};
    }})()''', timeout=60)


def remove(appid):
    # Collections and artwork are tidy-up: only a missing RemoveShortcut may fail the removal.
    return evaluate(f'''(async () => {{
      const id = {int(appid)}, warnings = [];
      {collections_js(int(appid))}
      try {{ warnings.push(...await syncCollections()); }}
      catch (e) {{ warnings.push("Steam collections: " + String(e)); }}
      if (typeof SteamClient.Apps.ClearCustomArtworkForApp === "function") {{
        for (const type of [0, 1, 2, 3]) {{
          try {{ await SteamClient.Apps.ClearCustomArtworkForApp(id, type); }}
          catch (e) {{ warnings.push("Steam artwork " + type + ": " + String(e)); }}
        }}
      }} else warnings.push("Steam artwork removal API unavailable");
      SteamClient.Apps.RemoveShortcut(id);
      return {{warnings}};
    }})()''')


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == 'add':
        name, exe, start_dir = args[:3]
        icon = args[3] if len(args) > 3 else ''
        js = f'''(async () => {{
          const id = await SteamClient.Apps.AddShortcut({json.dumps(name)}, {json.dumps(exe)}, "", "");
          SteamClient.Apps.SetShortcutName(id, {json.dumps(name)});
          SteamClient.Apps.SetShortcutStartDir(id, {json.dumps(start_dir)});
          if ({json.dumps(icon)}) SteamClient.Apps.SetShortcutIcon(id, {json.dumps(icon)});
          return id;
        }})()'''
        print(evaluate(js))
    elif cmd == 'list':
        # Overviews carry no exe or devkit id (checked 2026-09-28); app details do, once registered.
        js = '''(async () => Promise.all(appStore.allApps.filter(a => a.app_type === 1073741824).map(async a => {
          let d = typeof appDetailsStore !== "undefined" && appDetailsStore.GetAppDetails(a.appid);
          if (!d && typeof SteamClient.Apps.RegisterForAppDetails === "function") d = await new Promise(ok => {
            let reg;
            const timer = setTimeout(() => { if (reg) reg.unregister(); ok(null); }, 3000);
            reg = SteamClient.Apps.RegisterForAppDetails(a.appid, x => {
              clearTimeout(timer); setTimeout(() => reg && reg.unregister()); ok(x); });
          });
          return {appid: a.appid, name: a.display_name, devkit_gameid: a.devkit_gameid,
                  exe: d ? d.strShortcutExe || "" : "", start_dir: d ? d.strShortcutStartDir || "" : ""};
        })))()'''
        print(json.dumps(evaluate(js)))
    elif cmd == 'render':
        print(json.dumps(render(args[0])))
    elif cmd == 'configure':
        vr = {'1': True, '0': False}.get(args[5])  # '' leaves Steam's VR flag alone
        print(json.dumps(configure(int(args[0]), *args[1:5], vr, json.loads(args[6]),
                                   json.loads(args[7]) if len(args) > 7 else None)))
    elif cmd == 'stop':
        evaluate(f'SteamClient.Apps.TerminateApp({json.dumps(str((int(args[0]) << 32) | 0x02000000))}, false)')
        print('stopping')
    elif cmd == 'remove':
        print(json.dumps(remove(int(args[0]))))
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
