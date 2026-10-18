const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(0, 'utf8');
const key = 'roomba-cloud.customer-token';

function browser(saved, blocked = false) {
  const storage = new Map(saved ? [[key, saved]] : []);
  const elements = new Map();
  const events = {};
  const requests = [];
  let responseStatus = 200;
  let robots = [];
  let confirmed = true;
  let confirmations = 0;
  const element = selector => {
    if (!elements.has(selector)) elements.set(selector, {
      value: '', textContent: '', checked: false, hidden: false,
      addEventListener(name, handler) { this[name] = handler; },
      replaceChildren() {},
      append() {},
    });
    return elements.get(selector);
  };
  const context = vm.createContext({
    document: {querySelector: element, getElementById: id => element('#' + id),
      createElement: () => ({addEventListener(name, handler) { this[name] = handler; },
        append() {}})},
    localStorage: {
      getItem(name) { if (blocked) throw new Error('blocked'); return storage.get(name) ?? null; },
      setItem(name, value) { if (blocked) throw new Error('blocked'); storage.set(name, value); },
      removeItem(name) { if (blocked) throw new Error('blocked'); storage.delete(name); },
    },
    window: {addEventListener(name, handler) { events[name] = handler; }},
    setInterval() {},
    setTimeout(callback) { callback(); },
    confirm() { confirmations++; return confirmed; },
    AbortSignal: {timeout() {}},
    fetch: async (url, options) => {
      requests.push(options.headers.Authorization);
      return {status: responseStatus, ok: responseStatus === 200,
        json: async () => ({robots})};
    },
  });
  vm.runInContext(source, context);
  return {storage, element, context, events, requests,
    robots(value) { robots = value; },
    confirm(value) { confirmed = value; },
    confirmations() { return confirmations; },
    status(value) { responseStatus = value; },
    async settle() { await new Promise(resolve => setImmediate(resolve)); },
    async login(value) {
      element('#token').value = value;
      element('#login').submit({preventDefault() {}});
      await this.settle();
    }};
}

(async () => {
  const mapping = browser();
  await mapping.login('token');
  for (const robot of [
    {online: false, mapping_controls: {allowed: true}},
    {online: true, mapping_controls: {allowed: false}},
    {online: true},
    {online: true, mapping_controls: {allowed: true}},
  ]) {
    mapping.robots([{robot_id:'a'.repeat(32), ...robot}]);
    await vm.runInContext('refresh()', mapping.context);
    assert.equal(vm.runInContext('mappingButtons[0].disabled', mapping.context),
      !(robot.online && robot.mapping_controls?.allowed === true));
  }
  mapping.confirm(false);
  const beforeMapping = mapping.requests.length;
  await vm.runInContext('mappingButtons[0].click()', mapping.context);
  assert.equal(mapping.requests.length, beforeMapping);
  assert.equal(mapping.confirmations(), 1);
  mapping.confirm(true);
  const commands = [];
  mapping.context.fetch = async (url, options) => {
    commands.push({url, options});
    return {ok:true, status:200};
  };
  await vm.runInContext('mappingButtons[0].click()', mapping.context);
  assert.equal(commands.length, 1);
  assert.equal(commands[0].url, '/v1/customer/robots/' + 'a'.repeat(32) + '/commands/train');
  assert.equal(commands[0].options.method, 'POST');
  await vm.runInContext('mappingButtons[0].click()', mapping.context);
  assert.equal(commands.length, 1);
  assert.match(mapping.element('#status').textContent, /not confirmed/);

  const names = browser();
  for (const invalid of ['', '   ', 'a'.repeat(65), '😀'.repeat(65), 'Kitchen\n',
    '\tKitchen', 'a\x7fb', 'a\x85b', 'a\u200bb', 'a\u202eb', 'a\u2028b',
    'a\u2029b', 'a\ud800b', '\ufeffKitchen']) {
    names.context.candidate = invalid;
    assert.throws(() => vm.runInContext('normalizeRoomName(candidate)', names.context),
      /Room names/);
  }
  for (const valid of ['  Kitchen  ', 'Chambre d’amis', '廚房', 'غرفة', 'e\u0301',
    '😀'.repeat(64), '<Kitchen & Dining>', 'a'.repeat(64)]) {
    names.context.candidate = valid;
    assert.equal(vm.runInContext('normalizeRoomName(candidate)', names.context), valid.trim());
  }
  vm.runInContext(`
    editRobot = 'robot';
    editMaps = [{id:'map', version:'1', revision:'digest', rooms:[{id:'1', name:'Old'}],
      operations:['rename'], awaiting_upload:false}];
    mapControlsAllowed = true;
  `, names.context);
  names.element('#edit-map').value = 'map';
  names.element('#edit-room').value = '1';
  names.element('#edit-operation').value = 'rename';
  names.element('#edit-enable').checked = true;
  names.element('#edit-name').value = 'bad\u200bname';
  await vm.runInContext("submitEdit('rename')", names.context);
  assert.equal(names.requests.length, 0);
  assert.match(names.element('#edit-status').textContent, /invisible formatting/);
  assert.equal(vm.runInContext('activeMap().awaiting_upload', names.context), false);
  const renames = [];
  names.context.fetch = async (url, options) => {
    renames.push({url, options});
    return {status: 200, ok: true, json: async () => options.method === 'POST'
      ? {transaction_id: 7} : {status: 'confirmed', message: 'Accepted'}};
  };
  names.element('#edit-name').value = '  廚房 & Dining  ';
  await vm.runInContext("submitEdit('rename')", names.context);
  assert.equal(JSON.parse(renames[0].options.body).name, '廚房 & Dining');
  const first = browser();
  await first.login('test-key');
  assert.equal(first.storage.get(key), 'test-key');
  assert.equal(first.element('#token').value, '');
  const reloaded = browser(first.storage.get(key));
  await reloaded.settle();
  assert.deepEqual(reloaded.requests, ['Bearer test-key']);
  assert.equal(reloaded.element('#dashboard').hidden, false);
  reloaded.status(503);
  await vm.runInContext('refresh()', reloaded.context);
  assert.equal(reloaded.storage.get(key), 'test-key');
  reloaded.status(401);
  await vm.runInContext('refresh()', reloaded.context);
  assert.equal(reloaded.storage.has(key), false);
  assert.equal(reloaded.element('#login').hidden, false);
  first.element('#logout').click();
  assert.equal(first.storage.has(key), false);
  const invalid = browser();
  invalid.status(401);
  await invalid.login('bad-key');
  assert.equal(invalid.storage.has(key), false);
  const blocked = browser('', true);
  assert.match(blocked.element('#storage-status').textContent, /unavailable/);
  await blocked.login('test-key');
  assert.equal(blocked.element('#dashboard').hidden, false);
  assert.match(blocked.element('#storage-status').textContent, /cannot be remembered/);
  blocked.element('#logout').click();
  assert.match(blocked.element('#storage-status').textContent, /Clear this site/);
  const otherTab = browser('test-key');
  await otherTab.settle();
  otherTab.events.storage({
    storageArea: otherTab.context.localStorage, key, newValue: null,
  });
  assert.equal(otherTab.element('#login').hidden, false);
  assert.equal(otherTab.storage.has(key), false);
  const deletion = browser('test-key');
  await deletion.settle();
  const sent = [];
  deletion.context.fetch = async (url, options) => {
    sent.push({url, options});
    return {status: 200, ok: true, json: async () => options.method === 'POST'
      ? {transaction_id: 5} : {status: 'deleted', message: 'Robot confirms deletion.'}};
  };
  vm.runInContext(`
    editRobot = 'robot';
    editMaps = [{id:'target-map', version:'1', revision:'digest', rooms:[],
      operations:['delete'], awaiting_upload:false}];
    mapControlsAllowed = true;
  `, deletion.context);
  deletion.element('#edit-map').value = 'target-map';
  deletion.element('#edit-enable').checked = true;
  let confirmation;
  deletion.context.confirm = message => { confirmation = message; return false; };
  await vm.runInContext("submitEdit('delete')", deletion.context);
  assert.equal(sent.length, 0);
  assert.match(confirmation, /Permanently delete map target-map/);
  deletion.context.confirm = () => true;
  await vm.runInContext("mapControlsAllowed = false; submitEdit('delete')", deletion.context);
  assert.equal(sent.length, 0);
  await vm.runInContext("mapControlsAllowed = true; submitEdit('delete')", deletion.context);
  assert.deepEqual(JSON.parse(sent[0].options.body), {
    map_id: 'target-map', version: '1', revision: 'digest', operation: 'delete',
  });
  assert.equal(sent.length, 2);
  assert.equal(vm.runInContext('editMaps.length', deletion.context), 0);
  assert.equal(deletion.element('#edit-fields').disabled, true);
  assert.equal(deletion.element('#edit-delete').disabled, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
