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

test('Landscape border mode fills and paints only the clicked shared border', { skip: !browser, timeout: 20000 }, async () => {
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
        else if(url.includes('/overrides'))data={overrides:{territories:{}}};
        else if(url.includes('/catalog'))data={terrain_types:['plains','mountains'],archetypes:[],specials:[]};
        else if(url.includes('/landscape/manifest'))data={mountains:['mountain.png']};
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
    await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Landscape').click()");
    await delay(150);
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
  } finally {
    page?.close();
    await manager.call('Target.closeTarget', { targetId });
    manager.close();
    await unlink(landscapeFixture).catch(() => {});
  }
});
