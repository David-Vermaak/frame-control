// Control on the live view (tap, drag, hold, scroll, type), run in node against the real
// functions from ui/index.html with a fake canvas and a fake server.
import { readFileSync } from "fs";
const src = readFileSync(new URL("../../ui/index.html", import.meta.url), "utf8");
const grab = name => {
  const one = src.match(new RegExp(`\\n((?:async )?function ${name}\\(.*\\}\\n)`));  // one-line function
  if (one) return one[1];
  const m = src.match(new RegExp(`(?:\\nconst ${name} = [^\\n]*\\n)|((?:async )?function ${name}\\([\\s\\S]*?\\n}\\n)`));
  if (!m) throw new Error("not found: " + name);
  return m[0];
};
const NAMES = ["isMoveEvent", "TAP_MOVE", "ctrlSend", "ctrlFlush", "ctrlMoveTo", "ctrlMoveBy", "ctrlSchedule",
  "ctrlFlushMoves", "ctrlButton", "ctrlClick", "ctrlRelease", "ctrlFraction", "ctrlTouchDown", "centroid",
  "ctrlTouchMove", "ctrlTouchUp", "ctrlTap", "ctrlText"];
const code = NAMES.map(grab).join("");
const fail = msg => { console.log("FAIL " + msg); process.exit(1); };
const tick = (ms = 0) => new Promise(r => setTimeout(r, ms));

function page({ mode = "abs", rect = { left: 0, top: 0, width: 640, height: 360 }, api } = {}) {
  const ctrl = { on: true, queue: [], sending: false, state: "ready", message: "", move: null, rel: [0, 0], raf: 0,
                 held: new Set(), pointers: new Map(), g: null, retry: null };
  const sent = [];
  const canvas = { width: 1280, height: 720, getBoundingClientRect: () => rect };
  const env = {
    ctrl, $: () => canvas, ctrlMode: () => mode, ctrlShow: () => {}, toast: () => {},
    ctrlTarget: () => (mode === "abs" ? { ok: true, panel: { window: 42 } } : { ok: true }),
    api: api || (async (path, body) => { sent.push(...body.events); return { state: "ready", sent: true }; }),
    requestAnimationFrame: cb => { setTimeout(cb, 0); return 1; },
    navigator: {},
  };
  const fns = new Function(...Object.keys(env), `let ctrlWarned = false;\n${code}
    return { ctrlTouchDown, ctrlTouchMove, ctrlTouchUp, ctrlSend, ctrlText, ctrlButton, ctrlRelease, ctrlFlushMoves };`)(...Object.values(env));
  const at = (id, x, y) => ({ pointerId: id, clientX: x, clientY: y });
  return { ctrl, sent, ...fns, at };
}

// A tap lands where it was tapped: pointer there first, then the click.
{
  const p = page();
  p.ctrlTouchDown(p.at(1, 320, 90)); p.ctrlTouchUp(p.at(1, 320, 90));
  await tick(10);
  const [move, down, up] = p.sent;
  if (!(move.fx === 0.5 && Math.abs(move.fy - 0.25) < 1e-9 && move.window === 42)) fail("tap position " + JSON.stringify(move));
  if (!(down.button === "left" && down.down && up.button === "left" && up.down === false && p.sent.length === 3))
    fail("tap click " + JSON.stringify(p.sent));
}
// The panel is letterboxed in a taller view: taps map inside it, taps on the bars do nothing.
{
  const p = page({ rect: { left: 0, top: 0, width: 640, height: 480 } });  // 640x360 picture, 60px bars
  p.ctrlTouchDown(p.at(1, 320, 150)); p.ctrlTouchUp(p.at(1, 320, 150));
  p.ctrlTouchDown(p.at(2, 320, 10)); p.ctrlTouchUp(p.at(2, 320, 10));
  await tick(10);
  if (!(p.sent.length === 3 && Math.abs(p.sent[0].fy - 0.25) < 1e-9)) fail("letterbox " + JSON.stringify(p.sent));
}
// A drag is a mouse drag: down where it started, moves, up at the end.
{
  const p = page();
  p.ctrlTouchDown(p.at(1, 100, 100));
  p.ctrlTouchMove(p.at(1, 140, 100)); await tick(5);
  p.ctrlTouchMove(p.at(1, 200, 120)); await tick(5);
  p.ctrlTouchUp(p.at(1, 200, 120)); await tick(10);
  const kinds = p.sent.map(e => "fx" in e ? "move" : `${e.button}-${e.down ? "down" : "up"}`);
  if (!(kinds[0] === "move" && kinds[1] === "left-down" && kinds.at(-1) === "left-up" && kinds.includes("move", 2)))
    fail("drag " + kinds.join(","));
  if (Math.abs(p.sent[0].fx - 100 / 640) > 1e-9) fail("drag starts where the finger went down");
  if (p.ctrl.held.size) fail("drag left the button held");
}
// Press and hold is a right-click where the finger is.
{
  const p = page();
  p.ctrlTouchDown(p.at(1, 64, 36));
  await tick(620);
  p.ctrlTouchUp(p.at(1, 64, 36)); await tick(10);
  const buttons = p.sent.filter(e => "button" in e).map(e => `${e.button}-${e.down}`);
  if (buttons.join() !== "right-true,right-false") fail("hold " + buttons.join());
  if (!(p.sent[0].fx === 0.1)) fail("hold position " + JSON.stringify(p.sent[0]));
}
// Two fingers scroll, and the content follows them (fingers up scrolls down).
{
  const p = page();
  p.ctrlTouchDown(p.at(1, 100, 200)); p.ctrlTouchDown(p.at(2, 200, 200));
  p.ctrlTouchMove(p.at(1, 100, 180)); p.ctrlTouchMove(p.at(2, 200, 180));
  p.ctrlTouchUp(p.at(1, 100, 180)); p.ctrlTouchUp(p.at(2, 200, 180));
  await tick(10);
  const dy = p.sent.filter(e => e.scroll).reduce((a, e) => a + e.scroll[1], 0);
  if (!(dy === 40 && !p.sent.some(e => "button" in e))) fail("scroll " + JSON.stringify(p.sent));
}
// On the headset view it's a trackpad: drags move the pointer, taps click where it is.
{
  const p = page({ mode: "rel" });
  p.ctrlTouchDown(p.at(1, 100, 100));
  p.ctrlTouchMove(p.at(1, 110, 100)); p.ctrlTouchMove(p.at(1, 120, 105));
  p.ctrlTouchUp(p.at(1, 120, 105)); await tick(10);
  p.ctrlTouchDown(p.at(1, 50, 50)); p.ctrlTouchUp(p.at(1, 50, 50)); await tick(10);
  const moved = p.sent.filter(e => "dx" in e).reduce((a, e) => [a[0] + e.dx, a[1] + e.dy], [0, 0]);
  if (!(moved[0] === 32 && moved[1] === 8)) fail("trackpad move " + JSON.stringify(moved));
  if (p.sent.some(e => "fx" in e)) fail("trackpad sent an absolute position");
  if (p.sent.filter(e => e.button === "left").length !== 2) fail("trackpad tap " + JSON.stringify(p.sent));
}
// Text: plain ASCII only, in chunks the server takes.
{
  const p = page();
  p.ctrlText("héllo " + "x".repeat(600));
  await tick(10);
  const text = p.sent.map(e => e.text).join("");
  if (!(text === "hllo " + "x".repeat(600) && p.sent.every(e => e.text.length <= 500))) fail("text " + text.length);
}
// Not connected yet, or the request fails: clicks and keys wait, stale moves don't; a release is never lost.
{
  let calls = 0;
  const p = page({ api: async () => { calls++; if (calls === 1) return { state: "starting", sent: false }; throw new Error("offline"); } });
  p.ctrlSend([{ fx: 0.1, fy: 0.1, window: 42 }, { button: "left", down: true }, { key: 30, down: true }]);
  await tick(5);
  const q = p.ctrl.queue;
  if (!(q.length === 2 && q[0].button === "left" && q[1].key === 30)) fail("kept while starting " + JSON.stringify(q));
  clearTimeout(p.ctrl.retry);
  p.ctrl.queue = [];
  p.ctrlSend([{ button: "left", down: false }, { key: 31, down: true }, { dx: 3, dy: 1 }]);
  await tick(5);
  if (!(p.ctrl.queue.length === 1 && p.ctrl.queue[0].button === "left" && p.ctrl.queue[0].down === false))
    fail("release kept on failure " + JSON.stringify(p.ctrl.queue));
  if (calls !== 2) fail("retried in a tight loop: " + calls + " requests");
  clearTimeout(p.ctrl.retry);
}
// Turning Control off (or the view losing focus) lets go of anything held.
{
  const p = page();
  p.ctrlButton("left", true);
  p.ctrlRelease(); await tick(10);
  if (!(p.sent.at(-1).button === "left" && p.sent.at(-1).down === false && !p.ctrl.held.size)) fail("release " + JSON.stringify(p.sent));
}
console.log("control gestures ok");
