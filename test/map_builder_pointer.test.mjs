// Browser regression: start Vite and an isolated Chrome with remote debugging, then run:
// MAP_BUILDER_BROWSER_URL=http://127.0.0.1:9233 node --test test/map_builder_pointer.test.mjs
// Override MAP_BUILDER_VITE_URL if Vite is not on port 5188. No production data is used.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { writeFile, unlink } from 'node:fs/promises';

const browser = process.env.MAP_BUILDER_BROWSER_URL;
const vite = process.env.MAP_BUILDER_VITE_URL ?? 'http://127.0.0.1:5188';
const fixture = new URL('../frontend/__builder_pointer_test.html', import.meta.url);
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function connect(url) {
  const socket = new WebSocket(url);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve); socket.addEventListener('error', reject); });
  let id = 0;
  const pending = new Map();
  socket.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    const request = pending.get(message.id);
    if (!request) return;
    pending.delete(message.id);
    if (message.error) request.reject(message.error); else request.resolve(message.result);
  });
  return {
    call: (method, params = {}) => new Promise((resolve, reject) => {
      pending.set(++id, { resolve, reject });
      socket.send(JSON.stringify({ id, method, params }));
    }),
    close: () => socket.close(),
  };
}

async function verifyMapNavigation(evaluate, viewport, world) {
  const transform = () => evaluate(`document.querySelector(${JSON.stringify(world)}).style.transform`);
  const before = await transform();
  const canceled = await evaluate(`(()=>{
    const el=document.querySelector(${JSON.stringify(viewport)}), r=el.getBoundingClientRect();
    window.mapWheelEscaped=false;
    const outside=()=>{window.mapWheelEscaped=true};
    document.addEventListener('wheel',outside);
    const event=new WheelEvent('wheel',{deltaX:7,deltaY:9,clientX:r.left+r.width/2,clientY:r.top+r.height/2,bubbles:true,cancelable:true});
    el.dispatchEvent(event);document.removeEventListener('wheel',outside);
    return event.defaultPrevented && !window.mapWheelEscaped;
  })()`);
  assert.ok(canceled, 'map scroll is canceled locally and does not reach the page');
  await delay(100);
  assert.notEqual(await transform(), before, 'scroll pans the map');
  const panned = await transform();
  assert.ok(await evaluate(`(()=>{
    const el=document.querySelector(${JSON.stringify(viewport)}),r=el.getBoundingClientRect();
    const event=new WheelEvent('wheel',{deltaY:-4,ctrlKey:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2,bubbles:true,cancelable:true});
    el.dispatchEvent(event);return event.defaultPrevented;
  })()`));
  await delay(100);
  assert.notEqual(await transform(), panned, 'trackpad pinch zooms the map');
  const pinched = await transform();
  assert.ok(await evaluate(`(()=>{
    const el=document.querySelector(${JSON.stringify(viewport)});
    return ['gesturestart','gesturechange','gestureend'].every(type=>{
      const event=new Event(type,{bubbles:true,cancelable:true});
      Object.defineProperty(event,'scale',{value:type==='gesturechange'?1.05:1});
      el.dispatchEvent(event);return event.defaultPrevented;
    });
  })()`), 'Safari gestures are canceled locally');
  await delay(100);
  assert.notEqual(await transform(), pinched, 'Safari pinch zooms the map');
  assert.equal(await evaluate('window.visualViewport.scale'), 1, 'page zoom is unchanged');
}

test('Builder selection takes precedence over an interrupted pan; groups move, delete and undo', { skip: !browser, timeout: 20000 }, async () => {
  const metadata = await (await fetch(`${browser}/json/version`)).json();
  const manager = await connect(metadata.webSocketDebuggerUrl);
  const { targetId } = await manager.call('Target.createTarget', { url: 'about:blank' });
  let page;
  try {
    const tabs = await (await fetch(`${browser}/json`)).json();
    page = await connect(tabs.find((tab) => tab.id === targetId).webSocketDebuggerUrl);
    const evaluate = async (expression) => {
      const result = await page.call('Runtime.evaluate', { expression, returnByValue: true });
      assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    const doc = { width: 1000, height: 800, joints: { a: { x: 100, y: 100 }, b: { x: 300, y: 100 }, c: { x: 300, y: 300 }, d: { x: 100, y: 300 } }, borders: { ab: { a: 'a', b: 'b' }, bc: { a: 'b', b: 'c' }, cd: { a: 'c', b: 'd' }, da: { a: 'd', b: 'a' } }, labels: { first: { x: 175, y: 175, display_name: 'First region' }, second: { x: 225, y: 225, display_name: 'Second region' } } };
    const store = JSON.stringify({ currentId: 'test', drafts: [{ id: 'test', name: 'Pointer regression', updated: 1, doc }] });
    await writeFile(fixture, `<html><body><div id="root"></div><script type="module">
      import React from 'react'; import { createRoot } from 'react-dom/client';
      import { MapBuilder } from '/src/pages/admin/MapBuilder.tsx'; import '/src/pages/Admin.css';
      window.fetch = async () => new Response(JSON.stringify({maps:[]}), {headers:{'Content-Type':'application/json'}});
      localStorage.setItem('admin_map_builder_drafts_v1', ${JSON.stringify(store)});
      createRoot(document.getElementById('root')).render(React.createElement(MapBuilder));
      </script><style>.mapbuilder{height:95vh!important}.mapbuilder__main{height:90vh!important}.mapbuilder__viewport{height:65vh!important;flex:none!important;min-height:400px!important}</style></body></html>`);
    await page.call('Emulation.setDeviceMetricsOverride', { width: 1400, height: 1100, deviceScaleFactor: 1, mobile: false });
    await page.call('Page.navigate', { url: `${vite}/__builder_pointer_test.html` });
    for (let attempt = 0; attempt < 60; attempt++) {
      if (await evaluate("!!document.querySelector('.mapbuilder__joint')")) break;
      await delay(100);
    }
    await delay(1000); // Let initial fit and Vite file-watcher updates settle.
    const state = () => evaluate(`({selected:document.querySelectorAll('.mapbuilder__joint--selected').length,nodes:document.querySelectorAll('.mapbuilder__joint').length,box:!!document.querySelector('rect[stroke="#4173aa"]'),transform:document.querySelector('.mapbuilder__world').style.transform})`);
    const point = (x, y) => evaluate(`(()=>{const r=document.querySelector('.mapbuilder__svg').getBoundingClientRect();return {x:r.left+${x}*r.width/1000,y:r.top+${y}*r.height/800}})()`);
    const drag = async (from, to, button = 'left', during) => {
      const buttons = button === 'left' ? 1 : 4;
      await page.call('Input.dispatchMouseEvent', { type: 'mousePressed', ...from, button, buttons, clickCount: 1 });
      await page.call('Input.dispatchMouseEvent', { type: 'mouseMoved', ...to, button, buttons });
      await delay(70);
      if (during) await during();
      await page.call('Input.dispatchMouseEvent', { type: 'mouseReleased', ...to, button, buttons: 0, clickCount: 1 });
      await delay(70);
    };
    await verifyMapNavigation(evaluate, '.mapbuilder__viewport', '.mapbuilder__world');
    const original = (await state()).transform;
    const click = async (position) => {
      await page.call('Input.dispatchMouseEvent', { type: 'mousePressed', ...position, button: 'left', buttons: 1, clickCount: 1 });
      await page.call('Input.dispatchMouseEvent', { type: 'mouseReleased', ...position, button: 'left', buttons: 0, clickCount: 1 });
      await delay(70);
    };
    const remove = async (key = 'Delete') => {
      await page.call('Input.dispatchKeyEvent', { type: 'keyDown', key, code: key, windowsVirtualKeyCode: key === 'Delete' ? 46 : 8 });
      await delay(70);
    };
    const undo = async () => {
      await evaluate("window.dispatchEvent(new KeyboardEvent('keydown',{key:'z',ctrlKey:true,bubbles:true}))");
      await delay(70);
    };
    const edgePoint = await point(200, 100);
    await click(edgePoint);
    await page.call('Input.dispatchMouseEvent', { type: 'mousePressed', ...edgePoint, button: 'left', buttons: 1, clickCount: 2 });
    await page.call('Input.dispatchMouseEvent', { type: 'mouseReleased', ...edgePoint, button: 'left', buttons: 0, clickCount: 2 });
    await delay(70);
    assert.equal((await state()).nodes, 5);
    const split = await evaluate(`(()=>{const n=document.querySelector('.mapbuilder__joint--selected');return {x:+n.getAttribute('cx'),y:+n.getAttribute('cy'),edges:[...document.querySelectorAll('.mapbuilder__border')].map(l=>[+l.getAttribute('x1'),+l.getAttribute('y1'),+l.getAttribute('x2'),+l.getAttribute('y2')])}})()`);
    assert.ok(Math.abs(split.x - 200) < 1 && Math.abs(split.y - 100) < 1);
    assert.equal(split.edges.filter(([x1,y1,x2,y2]) => (Math.abs(x1-200)<1 && y1===100) || (Math.abs(x2-200)<1 && y2===100)).length, 2);
    await undo();
    assert.equal((await state()).nodes, 4);
    assert.equal(await evaluate("document.querySelectorAll('.mapbuilder__border').length"), 4);
    const selectRegion = async () => {
      const position = await point(250, 250);
      await click(position);
      await page.call('Input.dispatchMouseEvent', { type: 'mousePressed', ...position, button: 'left', buttons: 1, clickCount: 2 });
      await page.call('Input.dispatchMouseEvent', { type: 'mouseReleased', ...position, button: 'left', buttons: 0, clickCount: 2 });
      await delay(70);
      assert.equal((await state()).selected, 4);
    };
    const firstNode = () => evaluate("({x:+document.querySelector('.mapbuilder__joint').getAttribute('cx'),y:+document.querySelector('.mapbuilder__joint').getAttribute('cy')})");
    const firstLabel = () => evaluate("({x:+document.querySelector('[data-label-id=first] circle').getAttribute('cx'),y:+document.querySelector('[data-label-id=first] circle').getAttribute('cy')})");
    await selectRegion();
    await page.call('Input.dispatchKeyEvent', { type: 'keyDown', key: 'ArrowRight', code: 'ArrowRight' });
    await page.call('Input.dispatchKeyEvent', { type: 'keyDown', key: 'ArrowRight', code: 'ArrowRight', autoRepeat: true });
    await page.call('Input.dispatchKeyEvent', { type: 'keyUp', key: 'ArrowRight', code: 'ArrowRight' });
    await delay(70);
    assert.deepEqual(await firstNode(), { x: 102, y: 100 });
    assert.deepEqual(await firstLabel(), { x: 177, y: 175 });
    await undo();
    assert.deepEqual(await firstNode(), { x: 100, y: 100 });
    assert.deepEqual(await firstLabel(), { x: 175, y: 175 });
    await selectRegion();
    await page.call('Input.dispatchKeyEvent', { type: 'keyDown', key: 'ArrowDown', code: 'ArrowDown', modifiers: 8 });
    await page.call('Input.dispatchKeyEvent', { type: 'keyUp', key: 'ArrowDown', code: 'ArrowDown' });
    await delay(70);
    assert.deepEqual(await firstNode(), { x: 100, y: 110 });
    assert.deepEqual(await firstLabel(), { x: 175, y: 185 });
    await undo();
    await selectRegion();
    await drag(await point(100, 100), await point(110, 110));
    assert.ok(Math.abs((await firstNode()).x - 110) < 1);
    assert.ok(Math.abs((await firstLabel()).x - 185) < 1);
    await undo();
    // Two preserved labels can occupy one face after a border is removed. A dot
    // must select its own label, rather than whichever label the face search finds first.
    await click(await point(225, 225));
    assert.equal(await evaluate("document.querySelector('.mapbuilder__label--selected')?.dataset.labelId"), 'second');
    await remove();
    assert.equal(await evaluate("document.querySelectorAll('.mapbuilder__label').length"), 1);
    assert.equal(await evaluate("!!document.querySelector('[data-label-id=first]')"), true);
    assert.equal(await evaluate("document.querySelectorAll('.mapbuilder__border').length"), 4);
    await undo();
    // Removing a boundary opens the face but keeps both labels. The orphaned dot
    // must still be selectable and deletable, with undo restoring it independently.
    await click(await point(200, 100));
    await remove();
    assert.equal(await evaluate("document.querySelectorAll('.mapbuilder__label').length"), 2);
    await click(await point(225, 225));
    assert.equal(await evaluate("document.querySelector('.mapbuilder__label--selected')?.dataset.labelId"), 'second');
    await remove('Backspace');
    assert.equal(await evaluate("document.querySelectorAll('.mapbuilder__label').length"), 1);
    await undo();
    await undo();
    assert.equal(await evaluate("document.querySelectorAll('.mapbuilder__label').length"), 2);
    const from = await point(50, 50), to = await point(350, 350);
    await drag(from, to, 'left', async () => assert.equal((await state()).box, true));
    assert.equal((await state()).selected, 4);
    assert.equal((await state()).transform, original);
    // Simulate a pan whose release was lost before a new left-button selection starts.
    await evaluate(`document.querySelector('.mapbuilder__viewport').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:1,button:1,buttons:4,clientX:${from.x},clientY:${from.y}}))`);
    await drag(from, to, 'left', async () => assert.equal((await state()).box, true));
    assert.equal((await state()).selected, 4);
    assert.equal((await state()).transform, original, 'an old pan must not move the map during selection');
    await drag(await point(100, 100), await point(150, 150));
    const positions = JSON.parse(await evaluate("JSON.stringify([...document.querySelectorAll('.mapbuilder__joint')].map(n=>[+n.getAttribute('cx'),+n.getAttribute('cy')]))"));
    assert.ok(Math.abs(positions[0][0] - 150) < 1);
    assert.equal(positions[1][0] - positions[0][0], 200);
    assert.equal((await state()).transform, original);
    await page.call('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Delete', code: 'Delete', windowsVirtualKeyCode: 46 });
    await delay(70);
    assert.equal((await state()).nodes, 0);
    await evaluate("window.dispatchEvent(new KeyboardEvent('keydown',{key:'z',ctrlKey:true,bubbles:true}))");
    await delay(70);
    assert.equal((await state()).nodes, 4);
    await drag(await point(50, 50), await point(80, 80), 'middle');
    assert.notEqual((await state()).transform, original, 'explicit middle-button pan still works');
    const beforeSpace = (await state()).transform;
    await page.call('Input.dispatchKeyEvent', { type: 'keyDown', key: ' ', code: 'Space', windowsVirtualKeyCode: 32 });
    await drag(await point(50, 50), await point(80, 80));
    await page.call('Input.dispatchKeyEvent', { type: 'keyUp', key: ' ', code: 'Space', windowsVirtualKeyCode: 32 });
    assert.notEqual((await state()).transform, beforeSpace, 'explicit Space-drag pan still works');
  } finally {
    page?.close();
    await manager.call('Target.closeTarget', { targetId });
    manager.close();
    await unlink(fixture).catch(() => {});
  }
});

test('Seeds uses matching admin styles and a top-right Close on every tab', { skip: !browser, timeout: 20000 }, async () => {
  const metadata = await (await fetch(`${browser}/json/version`)).json();
  const manager = await connect(metadata.webSocketDebuggerUrl);
  const { targetId } = await manager.call('Target.createTarget', { url: 'about:blank' });
  const seedsFixture = new URL('../frontend/__seeds_ui_test.html', import.meta.url);
  let page;
  try {
    const tabs = await (await fetch(`${browser}/json`)).json();
    page = await connect(tabs.find((tab) => tab.id === targetId).webSocketDebuggerUrl);
    const evaluate = async (expression) => {
      const result = await page.call('Runtime.evaluate', { expression, returnByValue: true });
      assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    await writeFile(seedsFixture, `<html><body><div class="admin-page" id="root"></div><script type="module">
      import React from 'react'; import {createRoot} from 'react-dom/client';
      import {ScenarioLibraryPanel} from '/src/pages/admin/ScenarioLibraryPanel.tsx';
      import '/src/index.css'; import '/src/pages/Admin.css';
      window.library={units:{guard:{id:'guard',display_name:'Guard',faction:'good',archetype:'infantry',attack:5,defense:3,movement:1,health:1,cost:{power:3},specials:[]}},
        factions:{good:{id:'good',display_name:'Allies',alliance:'good',capital:'shire',color:'#31572c'}},
        territories:{shire:{id:'shire',display_name:'The Shire',produces:{power:2},ownable:true,is_stronghold:false}},
        maps:{world:{shire:{id:'shire',terrain_type:'plains',adjacent:[]}}}};
      window.fetch=async(input,init={})=>{const url=String(input);let data={};
        if(url.includes('/scenario-library/preview')){window.reviewPayload=JSON.parse(init.body);data={changed_setups:['alpha'],changes:{alpha:{territories:{shire:{produces:{power:4}}}}}};}
        else if(url.includes('/scenario-library')){if(init.method==='PUT'){window.library=JSON.parse(init.body);data={changed_setups:['alpha'],changes:{}};}else data=window.library;}
        else if(url.includes('/catalog'))data={terrain_types:['plains'],archetypes:['infantry'],specials:[]};
        return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});};
      createRoot(document.getElementById('root')).render(React.createElement(ScenarioLibraryPanel,{onClose:()=>{window.seedClosed=true},onSaved:()=>{window.seedSaved=true}}));
      </script></body></html>`);
    await page.call('Emulation.setDeviceMetricsOverride', { width: 1400, height: 1100, deviceScaleFactor: 1, mobile: false });
    await page.call('Page.navigate', { url: `${vite}/__seeds_ui_test.html` });
    for (let attempt = 0; attempt < 60; attempt++) {
      if (await evaluate("!!document.querySelector('.admin-entity__item')")) break;
      await delay(100);
    }
    const button = (name) => `[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===${JSON.stringify(name)})`;
    for (const name of ['Units', 'Factions', 'Territories']) {
      await evaluate(`${button(name)}.click()`);
      await delay(100);
      const styles = await evaluate(`(()=>{
        const close=${button('Close')}, title=document.getElementById('admin-seeds-title');
        const reference=document.querySelector('.admin-entity__toolbar button');
        const r=close.getBoundingClientRect(), t=title.getBoundingClientRect();
        const actions=[...document.querySelectorAll('.admin-modal__actions button')];
        return {right:r.left>t.right,top:Math.abs(r.top-t.top)<20,labels:actions.map(b=>b.textContent.trim()),
          family:getComputedStyle(reference).fontFamily,closeFamily:getComputedStyle(close).fontFamily,
          actionFamilies:actions.map(b=>getComputedStyle(b).fontFamily),styled:actions.every(b=>b.classList.contains('admin-page__btn'))};})()`);
      assert.deepEqual(styles.labels, ['Review', 'Save and Rebuild', 'Export']);
      assert.ok(styles.right && styles.top, `${name}: Close must stay at the top right`);
      assert.ok(styles.styled);
      assert.equal(styles.closeFamily, styles.family);
      assert.ok(styles.actionFamilies.every((family) => family === styles.family));
      assert.ok(styles.family.includes('IM Fell English'), styles.family);
    }
    assert.equal(await evaluate("[...document.querySelectorAll('.admin-form__label')].some(l=>l.textContent==='Terrain type')"), true);
    await evaluate(`(()=>{const row=[...document.querySelectorAll('.admin-form__row')].find(r=>r.querySelector('label')?.textContent==='Power production');
      const input=row.querySelector('input');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'4');input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
    await delay(70);
    assert.equal(await evaluate(`${button('Save and Rebuild')}.disabled`), true);
    await evaluate(`${button('Review')}.click()`);
    await delay(100);
    assert.equal(await evaluate('window.reviewPayload.territories.shire.produces.power'), 4);
    assert.equal(await evaluate('window.library.territories.shire.produces.power'), 2, 'Review must not save');
    await evaluate(`${button('Save and Rebuild')}.click()`);
    await delay(100);
    assert.equal(await evaluate('window.library.territories.shire.produces.power'), 4);
    assert.equal(await evaluate('window.seedSaved'), true);
    const screenshot = await page.call('Page.captureScreenshot', { format: 'png' });
    await writeFile('/private/tmp/baggins-seeds-territories.png', Buffer.from(screenshot.data, 'base64'));
    await evaluate(`${button('Close')}.click()`);
    assert.equal(await evaluate('window.seedClosed'), true);
  } finally {
    page?.close();
    await manager.call('Target.closeTarget', { targetId });
    manager.close();
    await unlink(seedsFixture).catch(() => {});
  }
});

test('Landscape paints the clicked shared border and Graph has independent selection and edge tools', { skip: !browser, timeout: 20000 }, async () => {
  const metadata = await (await fetch(`${browser}/json/version`)).json();
  const manager = await connect(metadata.webSocketDebuggerUrl);
  const { targetId } = await manager.call('Target.createTarget', { url: 'about:blank' });
  const landscapeFixture = new URL('../frontend/__landscape_border_test.html', import.meta.url);
  let page;
  try {
    const tabs = await (await fetch(`${browser}/json`)).json();
    page = await connect(tabs.find((tab) => tab.id === targetId).webSocketDebuggerUrl);
    const evaluate = async (expression) => {
      const result = await page.call('Runtime.evaluate', { expression, returnByValue: true });
      assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 200"><path id="a" d="M20 20H140V180H20Z"/><path id="b" d="M140 20H260V180H140Z"/><path id="c" d="M260 20H380V180H260Z"/></svg>';
    await writeFile(landscapeFixture, `<html><body><div id="root"></div><script type="module">
      import React from 'react'; import {createRoot} from 'react-dom/client';
      import {MapOverridesPane} from '/src/pages/admin/MapOverridesPane.tsx'; import '/src/pages/Admin.css';
      const svg=${JSON.stringify(svg)};
      window.fetch=async (input,init={})=>{
        const url=String(input); let data={};
        if(init.method==='PUT'){window.saved=JSON.parse(init.body);data={overrides:window.saved};}
        else if(url.includes('.svg'))return new Response(svg,{headers:{'Content-Type':'image/svg+xml'}});
        else if(url.includes('/admin/maps'))data={maps:['wotr_map_1.2']};
        else if(url.includes('/admin/setups'))data={setups:[]};
        else if(url.includes('/overrides')){await new Promise(resolve=>setTimeout(resolve,300));data={overrides:{territories:{}}};}
        else if(url.includes('/catalog'))data={terrain_types:['plains','mountains'],archetypes:[],specials:[]};
        else if(url.includes('/landscape/manifest'))data={mountains:['mountain.png'],forest:['forest.png'],landmarks:['city.png']};
        return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
      };
      const territories=Object.fromEntries(['a','b','c'].map(id=>[id,{id,display_name:id,terrain_type:'plains',adjacent:[],produces:{power:0}}]));
      createRoot(document.getElementById('root')).render(React.createElement(MapOverridesPane,{territories,onClose:()=>{}}));
      </script></body></html>`);
    await page.call('Emulation.setDeviceMetricsOverride', { width: 1400, height: 1100, deviceScaleFactor: 1, mobile: false });
    await page.call('Page.navigate', { url: `${vite}/__landscape_border_test.html` });
    for (let attempt = 0; attempt < 60; attempt++) {
      if (await evaluate("!!document.querySelector('path.admin-graph__path[data-tid=a]')")) break;
      await delay(100);
    }
    await delay(500);
    await verifyMapNavigation(evaluate, '.admin-graph__viewport', '.admin-graph__world');
    await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Landscape').click()");
    await delay(150);
    await verifyMapNavigation(evaluate, '.admin-graph__viewport', '.admin-graph__world');
    await evaluate("[...document.querySelectorAll('label')].find(l=>l.textContent.includes('Border mode')).querySelector('input').click()");
    await delay(70);
    await evaluate("document.querySelector('.mapov-landscape__thumb').click()");
    await delay(70);
    const point = (x, y) => evaluate(`(()=>{const p=new DOMPoint(${x},${y}).matrixTransform(document.querySelector('path.admin-graph__path[data-tid=a]').getScreenCTM());return {x:p.x,y:p.y}})()`);
    const mouse = (type, position, buttons) => page.call('Input.dispatchMouseEvent', { type, ...position, button: 'left', buttons, clickCount: 1 });
    const click = async (position) => { await mouse('mousePressed', position, 1); await mouse('mouseReleased', position, 0); await delay(80); };
    const count = () => evaluate("document.querySelectorAll('.mapov-placement').length");
    await click(await point(135, 100));
    const filled = await count();
    assert.ok(filled >= 3, `Expected a filled shared border, got ${filled} pieces`);
    await click(await point(135, 100));
    assert.equal(await count(), filled, 'repeated clicks do not stack mountains');
    await click(await point(25, 100));
    assert.equal(await count(), filled, 'coastline does not fill the remaining outline');
    await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Paint').click()");
    await delay(70);
    await mouse('mousePressed', await point(135, 60), 1);
    await mouse('mouseMoved', await point(255, 150), 1);
    await delay(70);
    await mouse('mouseReleased', await point(255, 150), 0);
    await delay(70);
    await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Save').click()");
    await delay(100);
    const pieces = await evaluate("Object.values(window.saved.landscape.territories).flat()");
    assert.ok(pieces.every((p) => Math.abs(p.x - 140) < 0.1 && p.y >= 20 && p.y <= 180), JSON.stringify(pieces));
    // Free placement lifts terrain-category restrictions as well as boundaries.
    await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Navigate').click()");
    await evaluate("[...document.querySelectorAll('label')].find(l=>l.textContent.includes('Free placement')).querySelector('input').click()");
    await delay(80);
    assert.ok(await evaluate("!!document.querySelector('.mapov-landscape__thumb[title=\"forest · forest.png\"]')"));
    assert.ok(await evaluate("!!document.querySelector('.mapov-landscape__thumb[title=\"mountains · mountain.png\"]')"));
    assert.equal(await evaluate("document.querySelectorAll('.mapov-landscape__thumb[title^=landmarks]').length"), 0);
    await evaluate("document.querySelector('.mapov-landscape__thumb[title=\"forest · forest.png\"]').click()");
    await delay(80); await click(await point(80, 100));
    await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Save').click()");
    await delay(100);
    const freeArt = await evaluate("Object.values(window.saved.landscape.territories).flat().filter(p=>p.cat==='forest')");
    assert.equal(freeArt.length, 1, 'forest art can be selected and placed on a plains territory in free mode');
    assert.equal(freeArt[0].layer, 'interior');
    await evaluate("[...document.querySelectorAll('label')].find(l=>l.textContent.includes('Free placement')).querySelector('input').click()");
    await delay(80);
    assert.equal(await evaluate("document.querySelectorAll('.mapov-landscape__thumb[title^=forest]').length"), 0);
    await click(await point(200, 100));
    await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Save').click()");
    await delay(80);
    assert.equal(await evaluate("Object.values(window.saved.landscape.territories).flat().filter(p=>p.cat==='forest').length"), 1, 'turning off free placement clears a now-hidden brush');


    const toolbarButton = name => `[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===${JSON.stringify(name)})`;
    const choose = async name => { await evaluate(`${toolbarButton(name)}.click()`); await delay(80); };
    // Numeric scale changes and selection copies retain exact values, with one undo step.
    await evaluate("document.querySelector('.mapov-placement[aria-label*=forest]').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))");
    await delay(80);
    await evaluate("(()=>{const input=document.querySelector('input[aria-label^=\"Scale for forest\"]');input.focus();Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'1.375');input.dispatchEvent(new Event('input',{bubbles:true}));})()");
    await delay(60);
    await evaluate("document.querySelector('input[aria-label^=\"Scale for forest\"]').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))");
    await delay(80);
    await choose('Copy selection');
    await choose('Paste selection');
    assert.equal(await count(), filled + 2);
    const setScale = async (value) => {
      await evaluate(`(()=>{const input=document.querySelector('input[aria-label="Selection scale"]');input.focus();Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(value)});input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
      await delay(60);
      await evaluate("document.querySelector('input[aria-label=\"Selection scale\"]').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))");
      await delay(60);
    };
    assert.equal(await evaluate("document.querySelector('input[aria-label=\"Selection scale\"]').value"), '1.375');
    await setScale('1.625');
    await choose('Undo'); assert.equal(await count(), filled + 2);
    await choose('Undo'); assert.equal(await count(), filled + 1);
    await choose('Redo'); assert.equal(await count(), filled + 2);
    await choose('Redo');
    await evaluate("[...document.querySelectorAll('.mapov-placement[aria-label*=forest]')].at(-1).dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))");
    await delay(80);
    // Copy the selected pasted piece using keyboard shortcuts.
    await evaluate("document.activeElement?.blur();window.dispatchEvent(new KeyboardEvent('keydown',{key:'c',ctrlKey:true,bubbles:true}))");
    await delay(60);
    await evaluate("window.dispatchEvent(new KeyboardEvent('keydown',{key:'v',ctrlKey:true,bubbles:true}))");
    await delay(80);
    await choose('Save');
    const copiedForest = await evaluate("Object.values(window.saved.landscape.territories).flat().filter(p=>p.cat==='forest')");
    assert.deepEqual(copiedForest.map(p=>p.s), [1.375,1.625,1.625]);
    // Select multiple pieces and verify a bulk paste preserves the whole group.
    for (let index=0;index<2;index++) {
      await evaluate(`document.querySelectorAll('.mapov-placement[aria-label*=forest]')[${index}].dispatchEvent(new PointerEvent('pointerdown',{button:0,shiftKey:true,bubbles:true}))`);
      await delay(50);
    }
    await choose('Copy selection'); await choose('Paste selection');
    assert.equal(await evaluate("document.querySelectorAll('.mapov-placement--multi').length"), 3);
    await choose('Undo');
    assert.equal(await count(), filled + 3);

    const focus = () => evaluate("document.querySelector('.admin-graph__list-item[aria-selected=true]')?.textContent.trim()");
    await choose('Graph');
    await verifyMapNavigation(evaluate, '.admin-graph__viewport', '.admin-graph__world');
    assert.equal(await evaluate(`document.querySelector('[aria-label="Graph tool"] button').getAttribute('aria-pressed')`), 'true');
    await click(await point(80, 100)); assert.equal(await focus(), 'a');
    await click(await point(200, 100)); assert.equal(await focus(), 'b');
    assert.equal(await evaluate(`${toolbarButton('Undo')}.disabled`), true, 'selecting territories creates no graph edits');
    await click(await point(80, 100));
    await choose('Land');
    await click(await point(200, 100)); assert.equal(await focus(), 'a');
    assert.ok(await evaluate("document.querySelector('path[data-tid=b]').classList.contains('admin-graph__path--neighbor-land')"));
    await choose('Select'); await click(await point(320, 100)); assert.equal(await focus(), 'c');
    await choose('Land');
    const target = await point(80, 100);
    await page.call('Input.dispatchMouseEvent', { type: 'mousePressed', ...target, button: 'left', buttons: 1, clickCount: 1, modifiers: 1 });
    await page.call('Input.dispatchMouseEvent', { type: 'mouseReleased', ...target, button: 'left', buttons: 0, clickCount: 1, modifiers: 1 });
    await delay(80); assert.equal(await focus(), 'a');
    await choose('Save');
    const graph = await evaluate('window.saved.graph');
    assert.deepEqual(graph.adjacent.a, ['b']); assert.deepEqual(graph.adjacent.b, ['a']);
    assert.ok(!graph.adjacent.c?.length, 'Select and Alt-click do not toggle unwanted connections');
    await choose('Icon'); await choose('Graph');
    assert.equal(await evaluate("document.querySelector('.admin-graph__type--select').getAttribute('aria-pressed')"), 'true');


  } finally {
    page?.close();
    await manager.call('Target.closeTarget', { targetId });
    manager.close();
    await unlink(landscapeFixture).catch(() => {});
  }
});

test('Builder copies selected geometry and resizes with one-step undo, cancellation and zoom-aware grips', { skip: !browser, timeout: 25000 }, async () => {
  const metadata = await (await fetch(`${browser}/json/version`)).json();
  const manager = await connect(metadata.webSocketDebuggerUrl);
  const { targetId } = await manager.call('Target.createTarget', { url: 'about:blank' });
  const path = new URL('../frontend/__builder_transform_test.html', import.meta.url);
  let page;
  try {
    const tabs = await (await fetch(`${browser}/json`)).json();
    page = await connect(tabs.find(tab => tab.id === targetId).webSocketDebuggerUrl);
    const evaluate = async expression => {
      const result = await page.call('Runtime.evaluate', { expression, returnByValue: true });
      assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    const doc = { width: 1000, height: 800,
      joints: { a: { x: 100, y: 100 }, b: { x: 300, y: 100 }, c: { x: 300, y: 300 }, d: { x: 100, y: 300 } },
      borders: { ab: { a: 'a', b: 'b' }, bc: { a: 'b', b: 'c' }, cd: { a: 'c', b: 'd' }, da: { a: 'd', b: 'a' } },
      labels: { region: { x: 175, y: 175, display_name: 'Region' } } };
    const store = JSON.stringify({ currentId: 'test', drafts: [{ id: 'test', name: 'Transform regression', updated: 1, doc }] });
    await writeFile(path, `<html><body><div class="admin-page" id="root"></div><script type="module">
      import React from 'react';import {createRoot} from 'react-dom/client';import {MapBuilder} from '/src/pages/admin/MapBuilder.tsx';import '/src/index.css';import '/src/pages/Admin.css';
      window.fetch=async()=>new Response(JSON.stringify({maps:[]}),{headers:{'Content-Type':'application/json'}});
      localStorage.setItem('admin_map_builder_drafts_v1',${JSON.stringify(store)});
      createRoot(document.getElementById('root')).render(React.createElement(MapBuilder));
      </script><style>.mapbuilder{height:95vh!important}.mapbuilder__main{height:90vh!important}.mapbuilder__viewport{height:65vh!important;flex:none!important;min-height:400px!important}</style></body></html>`);
    await page.call('Emulation.setDeviceMetricsOverride', { width: 1400, height: 1100, deviceScaleFactor: 1, mobile: false });
    await page.call('Page.navigate', { url: `${vite}/__builder_transform_test.html` });
    for (let i = 0; i < 70; i++) { if (await evaluate("!!document.querySelector('.mapbuilder__joint')")) break; await delay(100); }
    await delay(1000);
    const button = name => `[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===${JSON.stringify(name)})`;
    const key = async (key, modifiers = {}) => { await evaluate(`window.dispatchEvent(new KeyboardEvent('keydown',{key:${JSON.stringify(key)},bubbles:true,cancelable:true,...${JSON.stringify(modifiers)}}))`); await delay(70); };
    const point = (x, y) => evaluate(`(()=>{const r=document.querySelector('.mapbuilder__svg').getBoundingClientRect();return {x:r.left+${x}*r.width/1000,y:r.top+${y}*r.height/800}})()`);
    const click = async (position, count = 1, modifiers = 0) => {
      await page.call('Input.dispatchMouseEvent', { type: 'mousePressed', ...position, button: 'left', buttons: 1, clickCount: count, modifiers });
      await page.call('Input.dispatchMouseEvent', { type: 'mouseReleased', ...position, button: 'left', buttons: 0, clickCount: count, modifiers });
      await delay(70);
    };
    const select = async () => { const p = await point(250, 250); await click(p); await click(p, 2); };
    const nodes = () => evaluate("Object.fromEntries([...document.querySelectorAll('[data-joint-id]')].map(n=>[n.dataset.jointId,{x:+n.getAttribute('cx'),y:+n.getAttribute('cy')}]))");
    const grip = id => evaluate(`(()=>{const r=document.querySelector('[data-resize-handle="${id}"]').getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})()`);
    const dragGrip = async (id, dx, dy, cancel) => {
      const from = await grip(id), zero = await point(0, 0), unit = await point(1, 1);
      const to = { x: from.x + dx * (unit.x - zero.x), y: from.y + dy * (unit.y - zero.y) };
      await page.call('Input.dispatchMouseEvent', { type: 'mousePressed', ...from, button: 'left', buttons: 1, clickCount: 1 });
      await page.call('Input.dispatchMouseEvent', { type: 'mouseMoved', ...to, button: 'left', buttons: 1 });
      await delay(70);
      if (cancel === 'escape') await key('Escape');
      if (cancel === 'pointer') await evaluate("document.querySelector('.mapbuilder__viewport').dispatchEvent(new PointerEvent('pointercancel',{bubbles:true,pointerId:1}))");
      await page.call('Input.dispatchMouseEvent', { type: 'mouseReleased', ...to, button: 'left', buttons: 0, clickCount: 1 });
      await delay(70);
    };
    const undo = () => key('z', { ctrlKey: true });
    const closeTo = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1, `${actual} should be close to ${expected}`);
    assert.equal(await evaluate(`${button('Copy selection')}.disabled`), true);
    assert.equal(await evaluate(`${button('Paste selection')}.disabled`), true);
    // A selected edge copies just itself and its two endpoints. Shift-clicking
    // additional edges includes their endpoints in the same bulk selection.
    await click(await point(200, 100));
    await key('c', { ctrlKey: true }); await key('v', { ctrlKey: true });
    assert.equal(Object.keys(await nodes()).length, 6);
    assert.equal(await evaluate("document.querySelectorAll('[data-border-id]').length"), 5);
    await undo(); assert.deepEqual(await nodes(), doc.joints);
    await click(await point(200, 100), 1, 8);
    await click(await point(200, 300), 1, 8);
    assert.equal(await evaluate("document.querySelectorAll('.mapbuilder__joint--selected').length"), 4);
    await select();
    assert.equal(await evaluate("document.querySelectorAll('[data-resize-handle]').length"), 8);
    assert.equal(await evaluate("document.querySelectorAll('.mapbuilder__border--selected').length"), 4);
    await key('c', { ctrlKey: true });
    await key('v', { metaKey: true });
    let pasted = await nodes();
    assert.equal(Object.keys(pasted).length, 8);
    const copiedIds = Object.keys(pasted).filter(id => !(id in doc.joints));
    closeTo(Math.min(...copiedIds.map(id => pasted[id].x)), 120);
    assert.equal(await evaluate("document.querySelectorAll('[data-border-id]').length"), 8);
    assert.equal(await evaluate("document.querySelectorAll('[data-label-id]').length"), 1);
    assert.equal(await evaluate("document.querySelectorAll('.mapbuilder__joint--selected').length"), 4);
    await undo(); assert.deepEqual(await nodes(), doc.joints);
    await key('z', { ctrlKey: true, shiftKey: true }); assert.equal(Object.keys(await nodes()).length, 8);
    await undo();
    await select();
    await evaluate(`${button('Copy selection')}.click()`); await delay(70);
    await evaluate(`${button('Paste selection')}.click()`); await delay(70);
    await key('v', { ctrlKey: true });
    assert.equal(Object.keys(await nodes()).length, 12);
    await undo(); assert.equal(Object.keys(await nodes()).length, 8);
    await undo(); assert.deepEqual(await nodes(), doc.joints);
    // Typing in a focused field retains ordinary copy/paste behavior.
    const ignored = await evaluate(`(()=>{const t=document.createElement('textarea');document.body.append(t);t.focus();const e=new KeyboardEvent('keydown',{key:'v',ctrlKey:true,bubbles:true,cancelable:true});t.dispatchEvent(e);t.remove();return !e.defaultPrevented})()`);
    assert.ok(ignored); assert.equal(Object.keys(await nodes()).length, 4);
    await select();
    await dragGrip('e', 100, 0);
    let resized = await nodes(); closeTo(resized.b.x, 400); closeTo(resized.c.y, 300); closeTo(resized.a.x, 100);
    await undo(); assert.deepEqual(await nodes(), doc.joints);
    await select();
    await dragGrip('se', 100, 100);
    resized = await nodes(); closeTo(resized.b.x, 400); closeTo(resized.c.y, 400); closeTo(resized.a.x, 100);
    const labelX = await evaluate("+document.querySelector('[data-label-id=region] circle').getAttribute('cx')");
    closeTo(labelX, 212.5);
    await undo(); assert.deepEqual(await nodes(), doc.joints);
    await select();
    await dragGrip('nw', -50, -50);
    resized = await nodes(); closeTo(resized.a.x, 50); closeTo(resized.a.y, 50); closeTo(resized.c.x, 300);
    await undo(); assert.deepEqual(await nodes(), doc.joints);
    await select();
    await dragGrip('se', 100, 100, 'escape'); assert.deepEqual(await nodes(), doc.joints);
    await dragGrip('e', 100, 0, 'pointer'); assert.deepEqual(await nodes(), doc.joints);
    await dragGrip('e', 0, 0);
    assert.equal(await evaluate(`${button('Undo')}.disabled`), true, 'cancelled and zero-distance resizes do not create history entries');
    // A keyboard user can focus a grip and resize without moving the group.
    await evaluate("document.querySelector('[data-resize-handle=e]').focus()");
    await page.call('Input.dispatchKeyEvent', { type: 'keyDown', key: 'ArrowRight', code: 'ArrowRight', modifiers: 8 });
    await page.call('Input.dispatchKeyEvent', { type: 'keyUp', key: 'ArrowRight', code: 'ArrowRight' });
    await delay(70);
    resized = await nodes(); closeTo(resized.b.x, 310); closeTo(resized.a.x, 100);
    await undo(); assert.deepEqual(await nodes(), doc.joints);
    await select();
    const gripWidth = await evaluate("document.querySelector('[data-resize-handle=e]').getBoundingClientRect().width");
    const beforeZoom = await evaluate("document.querySelector('.mapbuilder__world').style.transform");
    const center = await point(200, 200);
    await evaluate(`document.querySelector('.mapbuilder__viewport').dispatchEvent(new WheelEvent('wheel',{bubbles:true,cancelable:true,ctrlKey:true,deltaY:-40,clientX:${center.x},clientY:${center.y}}))`);
    await delay(200);
    assert.notEqual(await evaluate("document.querySelector('.mapbuilder__world').style.transform"), beforeZoom);
    closeTo(await evaluate("document.querySelector('[data-resize-handle=e]').getBoundingClientRect().width"), gripWidth);
    await dragGrip('e', 100, 0);
    resized = await nodes(); closeTo(resized.b.x, 400); closeTo(resized.a.x, 100);
    await evaluate('window.scrollTo(0,0)');
    const screenshot = await page.call('Page.captureScreenshot', { format: 'png' });
    await writeFile('/private/tmp/baggins-builder-selection.png', Buffer.from(screenshot.data, 'base64'));
  } finally {
    page?.close(); await manager.call('Target.closeTarget', { targetId }); manager.close();
    await unlink(path).catch(() => {});
  }
});

test('Builder assigns existing seeds, creates seeds inline and edits shared terrain without local names', { skip: !browser, timeout: 25000 }, async () => {
  const metadata = await (await fetch(`${browser}/json/version`)).json();
  const manager = await connect(metadata.webSocketDebuggerUrl);
  const { targetId } = await manager.call('Target.createTarget', { url: 'about:blank' });
  const path = new URL('../frontend/__builder_seed_test.html', import.meta.url);
  let page;
  try {
    const tabs = await (await fetch(`${browser}/json`)).json();
    page = await connect(tabs.find(tab => tab.id === targetId).webSocketDebuggerUrl);
    const evaluate = async expression => {
      const result = await page.call('Runtime.evaluate', { expression, returnByValue: true });
      assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails)); return result.result.value;
    };
    const doc = { width: 1000, height: 800,
      joints: { a: { x: 0, y: 0 }, b: { x: 500, y: 0 }, c: { x: 1000, y: 0 }, d: { x: 1000, y: 800 }, e: { x: 500, y: 800 }, f: { x: 0, y: 800 } },
      borders: { ab: { a: 'a', b: 'b' }, bc: { a: 'b', b: 'c' }, cd: { a: 'c', b: 'd' }, de: { a: 'd', b: 'e' }, ef: { a: 'e', b: 'f' }, fa: { a: 'f', b: 'a' }, be: { a: 'b', b: 'e' } }, labels: {} };
    const store = JSON.stringify({ currentId: 'test', drafts: [{ id: 'test', name: 'Seed assignments', updated: 1, doc }] });
    await writeFile(path, `<html><body><div class="admin-page" id="root"></div><script type="module">
      import React from 'react';import {createRoot} from 'react-dom/client';import {MapBuilder} from '/src/pages/admin/MapBuilder.tsx';import '/src/index.css';import '/src/pages/Admin.css';
      window.library={units:{},factions:{},territories:{shire:{id:'shire',display_name:'Shared Shire',terrain_type:'plains',produces:{power:2},ownable:true}},maps:{}};
      window.fetch=async(input,init={})=>{const url=String(input);let data={maps:[]};
        if(url.includes('/territory-seeds')){const record=JSON.parse(init.body);window.library.territories[record.id]=record;if(init.method==='POST')window.lastCreatedSeed=record;else window.lastUpdatedSeed=record;data={seed:record,warnings:{}};}
        else if(url.includes('/scenario-library'))data=window.library;
        else if(url.includes('/catalog'))data={terrain_types:['plains','forest','hills','mountains'],archetypes:['infantry']};
        return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});};
      localStorage.setItem('admin_map_builder_drafts_v1',${JSON.stringify(store)});
      createRoot(document.getElementById('root')).render(React.createElement(MapBuilder));
      </script><style>.mapbuilder{height:95vh!important}.mapbuilder__main{height:90vh!important}.mapbuilder__viewport{height:65vh!important;flex:none!important;min-height:400px!important}</style></body></html>`);
    await page.call('Emulation.setDeviceMetricsOverride', { width: 1400, height: 1100, deviceScaleFactor: 1, mobile: false });
    await page.call('Page.navigate', { url: `${vite}/__builder_seed_test.html` });
    for (let i = 0; i < 70; i++) { if (await evaluate("!!document.querySelector('.mapbuilder__joint')")) break; await delay(100); }
    await delay(700);
    const button = name => `[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===${JSON.stringify(name)})`;
    const clickButton = async name => { await evaluate(`${button(name)}.click()`); await delay(80); };
    const clickRegion = async (x, y) => {
      const p = await evaluate(`(()=>{const r=document.querySelector('.mapbuilder__svg').getBoundingClientRect();return {x:r.left+${x}*r.width/1000,y:r.top+${y}*r.height/800}})()`);
      await page.call('Input.dispatchMouseEvent', { type: 'mousePressed', ...p, button: 'left', buttons: 1, clickCount: 1 });
      await page.call('Input.dispatchMouseEvent', { type: 'mouseReleased', ...p, button: 'left', buttons: 0, clickCount: 1 });
      await delay(80);
    };
    const control = (label, tag) => `([...document.querySelector('[aria-label="Assign territory"]').querySelectorAll('.admin-form__row')].find(row=>row.querySelector('.admin-form__label')?.textContent.trim()===${JSON.stringify(label)}).querySelector(${JSON.stringify(tag)}))`;
    const change = async (expression, value, event = 'change') => {
      await evaluate(`(()=>{const el=${expression};Object.getOwnPropertyDescriptor(${event === 'input' ? 'HTMLInputElement' : 'HTMLSelectElement'}.prototype,'value').set.call(el,${JSON.stringify(value)});el.dispatchEvent(new Event(${JSON.stringify(event)},{bubbles:true}))})()`);
      await delay(80);
    };
    const picker = `document.querySelector('select[aria-label="Territory seed"]')`;
    await clickButton('Label'); await clickRegion(250, 400);
    assert.equal(await evaluate(`${picker}.querySelector('option[value="shire"]').textContent`), 'Shared Shire');
    assert.ok(await evaluate(`${button('Create new territory seed')} && !${button('Create new territory seed')}.disabled`));
    await change(picker, 'shire'); await clickButton('Assign territory');
    assert.equal(await evaluate("document.querySelector('[data-label-id=shire] text').textContent"), 'Shared Shire');
    await delay(450);
    let labels = await evaluate("JSON.parse(localStorage.getItem('admin_map_builder_drafts_v1')).drafts[0].doc.labels");
    assert.deepEqual(Object.keys(labels.shire).sort(), ['x', 'y']);
    await clickRegion(250, 400); await clickButton('Edit shared seed');
    await change(control('Display name', 'input'), 'Lush Shire', 'input');
    await change(control('Terrain type', 'select'), 'forest');
    await clickButton('Save seed and assign');
    assert.equal(await evaluate('window.lastUpdatedSeed.id'), 'shire');
    assert.equal(await evaluate('window.lastUpdatedSeed.terrain_type'), 'forest');
    assert.equal(await evaluate("document.querySelector('[data-label-id=shire] text').textContent"), 'Lush Shire');
    await clickRegion(750, 400);
    assert.equal(await evaluate(`${picker}.querySelector('option[value="shire"]').disabled`), true);
    await clickButton('Create new territory seed');
    await change(control('Display name', 'input'), 'Mountain Pass', 'input');
    assert.equal(await evaluate("document.querySelector('input[aria-label=\"New territory ID\"]').value"), 'mountain_pass');
    await change(control('Terrain type', 'select'), 'mountains');
    await change(control('Power production', 'input'), '6', 'input');
    const screenshot = await page.call('Page.captureScreenshot', { format: 'png' });
    await writeFile('/private/tmp/baggins-territory-seed-popup.png', Buffer.from(screenshot.data, 'base64'));
    await clickButton('Create and assign');
    const created = await evaluate('window.lastCreatedSeed');
    assert.equal(created.id, 'mountain_pass'); assert.equal(created.terrain_type, 'mountains'); assert.deepEqual(created.produces, { power: 6 });
    assert.equal(await evaluate("document.querySelector('[data-label-id=mountain_pass] text').textContent"), 'Mountain Pass');
    await delay(450);
    labels = await evaluate("JSON.parse(localStorage.getItem('admin_map_builder_drafts_v1')).drafts[0].doc.labels");
    assert.deepEqual(Object.keys(labels).sort(), ['mountain_pass', 'shire']);
    assert.ok(Object.values(labels).every(label => Object.keys(label).sort().join(',') === 'x,y'));
  } finally {
    page?.close(); await manager.call('Target.closeTarget', { targetId }); manager.close(); await unlink(path).catch(() => {});
  }
});

test('Manual marker overrides bypass edge buffers; calculated markers retain them', { skip: !browser, timeout: 20000 }, async () => {
  const metadata = await (await fetch(`${browser}/json/version`)).json();
  const manager = await connect(metadata.webSocketDebuggerUrl);
  const { targetId } = await manager.call('Target.createTarget', { url: 'about:blank' });
  const markerFixture = new URL('../frontend/__marker_override_test.html', import.meta.url);
  let page;
  try {
    const tabs = await (await fetch(`${browser}/json`)).json();
    page = await connect(tabs.find(tab => tab.id === targetId).webSocketDebuggerUrl);
    const evaluate = async expression => {
      const result = await page.call('Runtime.evaluate', { expression, returnByValue: true });
      assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    await writeFile(markerFixture, `<html><body><div id="root"></div><script type="module">
      import React from 'react';import {createRoot} from 'react-dom/client';
      import GameMap from '/src/components/GameMap.tsx';
      window.fetch=async input=>String(input).includes('.svg')
        ?new Response('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 200"><path id="land" d="M0 0H150V50H0Z"/><path id="sea_zone_1" d="M160 0H300V50H160Z"/></svg>')
        :new Response('{}',{headers:{'Content-Type':'application/json'}});
      const noop=()=>{};
      const props={gameState:{turn_number:1,current_faction:'',phase:'purchase',territories:{},faction_resources:{},pending_purchases:{},pending_moves:[],pending_mobilizations:[],pending_camp_placements:[],map_asset:'test_map'},
        selectedTerritory:null,selectedUnit:null,territoryData:{land:{name:'Land',terrain:'plains',stronghold:false,produces:2},sea_zone_1:{name:'Sea',terrain:'sea',stronghold:false,produces:0}},
        territoryUnits:{},rings:[],unitDefs:{},unitStats:{},factionData:{},onTerritorySelect:noop,onUnitSelect:noop,onUnitMove:noop,canAct:false,isMovementPhase:false,isCombatMove:false,isMobilizePhase:false,hasMobilizationSelected:false,pendingMoveConfirm:null,onSetPendingMove:noop,pendingMoves:[]};
      const root=createRoot(document.getElementById('root'));
      window.showMarkers=(overrides)=>root.render(React.createElement(GameMap,{...props,mapOverrides:overrides}));
      window.showMarkers({territories:{LAND:{focus:'override',positions:{marker:{x:10,y:5}}},sea_zone_1:{focus:'override',positions:{number:{x:200,y:5}}}}});
    </script></body></html>`);
    await page.call('Page.navigate', { url: `${vite}/__marker_override_test.html` });
    for (let i = 0; i < 80; i++) {
      if (await evaluate("document.querySelectorAll('.territory-markers').length>=2")) break;
      await delay(100);
    }
    const positions = () => evaluate(`(()=>{
      const land=document.querySelector('.territory-power-badge').closest('.territory-markers');
      const sea=document.querySelector('.sea-zone-number').closest('.territory-markers');
      return {land:{x:parseFloat(land.style.left),y:parseFloat(land.style.top)},sea:{x:parseFloat(sea.style.left),y:parseFloat(sea.style.top)}};
    })()`);
    let points = await positions();
    assert.ok(points.land.y < 80, JSON.stringify(points));
    assert.equal(points.land.y, points.sea.y, 'land and sea manual anchors at the same y match');
    assert.ok(Math.abs(points.land.x / points.land.y - 2) < 0.01, 'manual x/y preserve exact SVG coordinates');
    await evaluate("window.showMarkers({territories:{land:{focus:'calculated',positions:{marker:{x:10,y:5}}}}})");
    await delay(500);
    points = await positions();
    assert.ok(points.land.y >= 80, 'calculated anchors retain the edge buffer');
  } finally {
    if (page) page.close();
    await manager.call('Target.closeTarget', { targetId }).catch(() => {});
    manager.close();
    await unlink(markerFixture).catch(() => {});
  }
});
