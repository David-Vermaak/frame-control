"""Run the actual shared page's comfort renderer against a minimal DOM/bridge."""
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), 'Node exercises the shared page JS')
class ComfortUI(unittest.TestCase):
    def test_notification_failure_survives_poll_until_success(self):
        page = (ROOT / 'ui/index.html').read_text(encoding='utf-8')
        code = page[page.index('let comfortBusy ='):page.index('async function pollComfort()')]
        setup = r'''
const assert = require('node:assert/strict');
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, {textContent:'', hidden:true, disabled:false, type: 'number'});
  return elements.get(id);
};
let denied = 0;
const window = {frameApp:{notify:async()=>{denied++;throw Error('permission denied');}}};
const log = ()=>{}, toast = ()=>{};
'''
        checks = r'''
(async()=>{
  const active = {id:'session-one',active:true,time:100,remaining:120,
    options:{minutes:2,breakMinutes:1,stillMinutes:1,batteryAlert:true,heatAlert:true},
    events:[{id:'event-one',kind:'battery',time:99,message:'Low battery'}]};
  renderComfort(active); // initial history must not replay even a fresh event
  assert.equal(denied,0);
  assert.equal($('comfortAnnouncement').textContent,'');
  active.events.push({id:'event-two',kind:'break',time:100,message:'Take a break'});
  renderComfort(active);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(denied,1);
  assert.equal($('comfortAnnouncement').textContent,'Take a break');
  assert.equal($('comfortNotificationStatus').hidden,false);
  assert.match($('comfortNotificationStatus').textContent,/notification settings/);
  renderComfort({...active,time:105}); // the next normal poll must not erase failure
  assert.equal($('comfortNotificationStatus').hidden,false);
  assert.equal($('sessionStart').disabled,true);
  assert.equal($('sessionMinutes').disabled,true);
  assert.equal($('sessionCancel').disabled,false);
  let requests=0;
  window.frameApp.notify=async()=>{requests++;};
  renderComfort({...active,time:106});
  assert.equal($('comfortAnnouncement').textContent,'Take a break');
  assert.equal(requests,0); // polling does not replay an already-seen event
  await localNotification('test',true);
  assert.equal($('comfortNotificationStatus').hidden,true);
  renderComfort({...active,active:false});
  assert.equal($('sessionStart').disabled,false);
  assert.equal($('sessionMinutes').disabled,false);
  assert.equal($('sessionCancel').disabled,true);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', setup + code + checks], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_finished_session_leaves_the_defaults(self):
        """A one-minute test session that has ended mustn't fill the form with 1 / 0 / 0."""
        page = (ROOT / 'ui/index.html').read_text(encoding='utf-8')
        code = page[page.index('let comfortBusy ='):page.index('async function pollComfort()')]
        setup = r'''
const assert = require('node:assert/strict');
const elements = new Map();
const $ = id => {
  // The form's own defaults, as in the page's HTML.
  const value = {sessionMinutes:'30', breakMinutes:'20', stillMinutes:'30'}[id] || '';
  if (!elements.has(id)) elements.set(id, {textContent:'', hidden:true, disabled:false, value, defaultValue:value,
    checked:true, defaultChecked:true, type: /Alert$/.test(id) ? 'checkbox' : 'number'});
  return elements.get(id);
};
const window = {frameApp:{notify:async()=>{}}};
const log = ()=>{}, toast = ()=>{};
'''
        checks = r'''
renderComfort({id:'old', active:false, time:100, options:{minutes:1,breakMinutes:0,stillMinutes:0,batteryAlert:false,heatAlert:false},
  events:[{id:'old:1',kind:'finished',time:90,message:'Session ended'}]});
assert.equal($('sessionMinutes').value, '30');
assert.equal($('breakMinutes').value, '20');
assert.equal($('batteryAlert').checked, true);
assert.match($('comfortEvents').textContent, /Session ended/);
assert.notEqual($('comfortEvents').textContent, 'Session ended');  // it carries a time
renderComfort({id:'new', active:true, time:200, remaining:600, options:{minutes:45,breakMinutes:15,stillMinutes:20,batteryAlert:true,heatAlert:false}, events:[]});
assert.equal($('sessionMinutes').value, 45);  // a running session shows its own settings
assert.equal($('heatAlert').checked, false);
// The page stays open while that session ends: the next one starts from the defaults again.
renderComfort({id:'new', active:false, time:900, options:{minutes:45,breakMinutes:15,stillMinutes:20,batteryAlert:true,heatAlert:false},
  events:[{id:'new:1',kind:'finished',time:899,message:'Session ended'}]});
assert.equal($('sessionMinutes').value, '30');
assert.equal($('breakMinutes').value, '20');
assert.equal($('heatAlert').checked, true);
renderComfort({id:'new', active:false, time:905, options:{minutes:45}, events:[]});
$('sessionMinutes').value = '50';  // what's typed for the next session
renderComfort({id:'new', active:false, time:910, options:{minutes:45}, events:[]});
assert.equal($('sessionMinutes').value, '50');  // later polls leave it alone
'''
        result = subprocess.run(['node', '-e', setup + code + checks], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
