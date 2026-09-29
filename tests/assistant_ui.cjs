// Run the actual page script with a tiny DOM/fetch fixture; no browser dependency.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
const events = new Map();
const requests = [];
const element = id => {
  if (!elements.has(id)) elements.set(id, {value:'', checked:false, disabled:false, textContent:'',
    addEventListener(){}, reset(){}});
  return elements.get(id);
};
const context = {
  document:{getElementById:element}, location:{hash:''}, URLSearchParams,
  window:{addEventListener:(name, fn) => events.set(name, fn)},
  fetch:(path, options) => new Promise(resolve => requests.push({path, options, resolve})),
};
const html = fs.readFileSync(process.argv[2], 'utf8');
vm.runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('__FRAME_KEY__', '"test"'), context);
const answer = (index, data) => requests[index].resolve({ok:true,json:async () => data});
(async () => {
  context.location.hash = '#confirm=first';
  const first = events.get('hashchange')();
  context.location.hash = '#confirm=second';
  const second = events.get('hashchange')();
  answer(1, {action:{name:'second'},approved:false});
  await second;
  answer(0, {action:{name:'first'},approved:false});
  await first;
  assert.match(element('action').textContent, /second/);
  assert.doesNotMatch(element('action').textContent, /first/);
  const approved = element('approve').onclick();
  assert.equal(JSON.parse(requests[2].options.body).confirmation, 'second');
  context.location.hash = '#confirm=third';
  const third = events.get('hashchange')();
  answer(3, {action:{name:'third'},approved:false});
  await third;
  answer(2, {message:'Approved for one use'});
  await approved;
  assert.equal(element('approval-status').textContent, '');
  assert.match(element('action').textContent, /third/);
  console.log('Approval navigation races: pass');
})().catch(error => { console.error(error); process.exitCode=1; });
