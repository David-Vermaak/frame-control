// The page's input queue, run in node against the real functions from ui/index.html.
import { readFileSync } from "fs";
const src = readFileSync(new URL("../../ui/index.html", import.meta.url), "utf8");
const grab = name => src.match(new RegExp(`(?:const ${name} = [^\\n]*\\n)|((?:async )?function ${name}\\([\\s\\S]*?\\n}\\n)`))[0];
const code = ["isMove", "padSend", "padKeep", "padFlush"].map(grab).join("");
const fail = msg => { console.log("FAIL " + msg); process.exit(1); };

function page(api) {
  const pad = { state: "ready", queue: [], sending: false };
  const shown = [];
  const fns = new Function("pad", "api", "padShow", code + "; return { padSend, padFlush };")(pad, api, s => { shown.push(s); pad.state = s.state; });
  return { pad, shown, ...fns };
}

// While a request is in flight, events queue by these rules.
{
  const { pad, padSend } = page(() => new Promise(() => {}));  // a request that never returns
  const send = (state, e) => { pad.state = state; padSend(e); };
  send("ready", { singlehold: true });     // goes out, and stays in flight
  send("error", { dx: 3 });                // dropped: a stale move while broken
  send("error", { singlerelease: true });  // kept
  send("off", { singlerelease: true });    // off: nothing was held
  send("pairing", { key: "a" });           // kept while reconnecting
  send("pairing", { dx: 1 });              // dropped
  for (let i = 0; i < 250; i++) send("pairing", { key: "x" });
  send("pairing", { singlerelease: true }); // kept past the 200 cap
  const q = pad.queue;
  if (!(q[0].singlerelease && q[1].key === "a" && q.at(-1).singlerelease && !q.some(e => "dx" in e)
        && q.filter(e => e.singlerelease).length === 2)) fail("queueing " + JSON.stringify(q.slice(0, 3)) + " len " + q.length);
}

// A request that fails, or reaches a link that had dropped, keeps its keys and releases.
for (const [label, api] of [["request failed", async () => { throw new Error("offline"); }],
                            ["not sent", async () => ({ state: "starting", sent: false })]]) {
  const { pad, padSend, shown } = page(api);
  padSend({ dx: 5 });  // sent and lost: a move, fine to drop
  await new Promise(r => setTimeout(r, 0));
  pad.state = "ready";
  padSend({ singlerelease: true });
  padSend({ dx: 7 });  // queued while that request is in flight: stale once it fails
  padSend({ key: "b" });
  await new Promise(r => setTimeout(r, 0));
  const q = pad.queue;
  if (!(q.some(e => e.singlerelease) && q.some(e => e.key === "b") && !q.some(e => "dx" in e)))
    fail(label + ": " + JSON.stringify(q));
  if (!shown.length || shown.at(-1).state === "ready") fail(label + ": state not shown");
}
console.log("queue rules ok");
