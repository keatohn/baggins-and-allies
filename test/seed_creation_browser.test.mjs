// Start local Vite and Chrome with remote debugging; no live data is used.
// MAP_BUILDER_BROWSER_URL=http://127.0.0.1:9233 node --test test/seed_creation_browser.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {writeFile, unlink} from 'node:fs/promises';
const browser = process.env.MAP_BUILDER_BROWSER_URL;
const vite = process.env.MAP_BUILDER_VITE_URL ?? 'http://127.0.0.1:5188';
const fixture = new URL('../frontend/__seed_creation_test.html', import.meta.url);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
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

test('all seed types share empty/copy creation and preserve independent records', {skip: !browser, timeout:20000}, async () => {
 const meta=await(await fetch(`${browser}/json/version`)).json();
 const manager=await connect(meta.webSocketDebuggerUrl);
 const {targetId}=await manager.call('Target.createTarget',{url:'about:blank'});
 let page;
 try {
  const tabs=await(await fetch(`${browser}/json`)).json();
  page=await connect(tabs.find(t=>t.id===targetId).webSocketDebuggerUrl);
  const evaluate=async expression=>{const r=await page.call('Runtime.evaluate',{expression,returnByValue:true});assert.ok(!r.exceptionDetails,JSON.stringify(r.exceptionDetails));return r.result.value;};
  const click=async text=>{await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===${JSON.stringify(text)}).click()`);await delay(60);};
  const input=async (selector,value)=>{await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});Object.getOwnPropertyDescriptor(${selector.startsWith('select')?'HTMLSelectElement':'HTMLInputElement'}.prototype,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()`);await delay(60);};
  await writeFile(fixture,`<html><body><div id="root"></div><script type="module">
 import React from 'react';import {createRoot} from 'react-dom/client';import {ScenarioLibraryPanel} from '/src/pages/admin/ScenarioLibraryPanel.tsx';import {api} from '/src/services/api.ts';
 api.adminGetScenarioLibrary=async()=>({units:{original:{id:'original',display_name:'Original',cost:{power:3}}},factions:{original:{id:'original',display_name:'Original',color:'#123456'}},territories:{original:{id:'original',display_name:'Original',produces:{power:2}}},maps:{}});
 api.getCatalog=async()=>({archetypes:[],terrain_types:[]});api.adminPreviewScenarioLibrary=async(value)=>{window.captured=value;return {changed_setups:[],changes:{}}};
 createRoot(document.getElementById('root')).render(React.createElement(ScenarioLibraryPanel,{onClose:()=>{}}));</script></body></html>`);
  await page.call('Page.navigate',{url:`${vite}/__seed_creation_test.html`});
  for(let i=0;i<70;i++){if(await evaluate("!!document.querySelector('.admin-entity__item')"))break;await delay(100);}
  for(const tab of ['Units','Factions','Territories']){
   await click(tab);await click('Add');
   await evaluate("document.querySelectorAll('[name=admin-add-seed-mode]')[1].click()");await delay(60);
   assert.equal(await evaluate("document.querySelectorAll('[name=admin-add-seed-mode]')[1].disabled"),false);
   await input('select#admin-copy-seed','original');
   await input('input#admin-new-seed-id','original');
   await evaluate("document.querySelector('#admin-new-seed-id').form.requestSubmit()");await delay(60);
   assert.equal(await evaluate("document.querySelector('[role=alert]').textContent"),'That id already exists.');
   await input('input#admin-new-seed-id','cloned');
   await evaluate("document.querySelector('#admin-new-seed-id').form.requestSubmit()");await delay(60);
   assert.equal(await evaluate("document.querySelector('.admin-entity__item--active').textContent"),'cloned');
   await click('Add');await input('input#admin-new-seed-id','empty');
   await evaluate("document.querySelector('#admin-new-seed-id').form.requestSubmit()");await delay(60);
   await click('Review');
   const records=await evaluate(`window.captured.${tab.toLowerCase()}`);
   assert.deepEqual(records.cloned,{...records.original,id:'cloned'});
   assert.deepEqual(records.empty,tab==='Units'?{id:'empty',cost:{power:0}}:{id:'empty'});
   await click('Add');await click('Cancel');
   assert.equal(await evaluate("!!document.querySelector('#admin-add-seed-title')"),false);
  }
 } finally { await manager.call('Target.closeTarget',{targetId});page?.close();manager.close();await unlink(fixture).catch(()=>{}); }
});
