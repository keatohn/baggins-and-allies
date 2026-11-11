// Start local Vite and Chrome with remote debugging; no live data is used.
// MAP_BUILDER_BROWSER_URL=http://127.0.0.1:9233 node --test test/scenario_selection_browser.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {writeFile, unlink} from 'node:fs/promises';
const browser = process.env.MAP_BUILDER_BROWSER_URL;
const vite = process.env.MAP_BUILDER_VITE_URL ?? 'http://127.0.0.1:5188';
const fixture = new URL('../frontend/__setup_selection_test.html', import.meta.url);
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

test('Setup sidebars support bulk seed selection, cancellation and retained overrides on every subtab', {skip: !browser, timeout:20000}, async () => {
 const meta=await(await fetch(`${browser}/json/version`)).json();
 const manager=await connect(meta.webSocketDebuggerUrl);
 const {targetId}=await manager.call('Target.createTarget',{url:'about:blank'});
 let page;
 try {
  const tabs=await(await fetch(`${browser}/json`)).json();
  page=await connect(tabs.find(t=>t.id===targetId).webSocketDebuggerUrl);
  const evaluate=async expression=>{const r=await page.call('Runtime.evaluate',{expression,returnByValue:true});assert.ok(!r.exceptionDetails,JSON.stringify(r.exceptionDetails));return r.result.value;};
  const click=async text=>{await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===${JSON.stringify(text)}).click()`);await delay(60);};
  await writeFile(fixture,`<html><meta name="viewport" content="width=device-width, initial-scale=1"><body><div class="admin-page" id="root"></div><script type="module">
 import React from 'react';import {createRoot} from 'react-dom/client';import {ScenarioOverrides} from '/src/pages/admin/ScenarioOverrides.tsx';import '/src/index.css';import '/src/pages/Admin.css';
 const library={units:{archer:{id:'archer',display_name:'Archer',attack:2},guard:{id:'guard',display_name:'Guard',attack:4,cost:{power:3},specials:[]}},factions:{evil:{id:'evil',display_name:'Enemy'},good:{id:'good',display_name:'Allies',color:'#31572c',alliance:'good'}},territories:{shire:{id:'shire',display_name:'The Shire'}},maps:{world:{bree:{id:'bree',display_name:'Bree',produces:{power:1}},shire:{id:'shire',display_name:'The Shire',produces:{power:2},ownable:true,terrain_type:'plains',adjacent:[]}}}};
 function App(){const [section,setSection]=React.useState('units');const [source,setSource]=React.useState({map:'world',units:{archer:{id:'archer',display_name:'Archer',attack:2},guard:{seed:'guard',overrides:{attack:5}}},factions:{good:{seed:'good',overrides:{color:'#123456'}}},territory_ids:['shire'],territory_overrides:{shire:{display_name:'Shire'}}});window.source=source;return React.createElement(React.Fragment,null,React.createElement('div',{className:'admin-page__tabs'},...['units','territories','factions'].map(key=>React.createElement('button',{className:'admin-page__tab',onClick:()=>setSection(key)},key))),React.createElement('div',{className:'admin-page__panel'},React.createElement(ScenarioOverrides,{key:section,section,source,library,onChange:(_,next)=>setSource(next)})));}
 createRoot(document.getElementById('root')).render(React.createElement(App));</script></body></html>`);

  await page.call('Emulation.setDeviceMetricsOverride',{width:1280,height:1000,deviceScaleFactor:1,mobile:false});
  await page.call('Page.navigate',{url:`${vite}/__setup_selection_test.html`});
  for(let i=0;i<70;i++){if(await evaluate("!!document.querySelector('.admin-entity__item')"))break;await delay(100);}
  const ids=()=>evaluate("[...document.querySelectorAll('.admin-entity__item')].map(b=>b.textContent)");
  for(const [section,singular,added,initial] of [['units','Unit','archer','guard'],['territories','Territory','bree','shire'],['factions','Faction','evil','good']]){
   await click(section);
   const before=await evaluate('JSON.stringify(window.source)');
   assert.equal(await evaluate("document.querySelectorAll('.scenario-overrides select').length"),0);
   await click(`Update ${singular} List`);
   await click('Select all shown');
   await click('Cancel');
   assert.equal(await evaluate('JSON.stringify(window.source)'),before);
   await click(`Update ${singular} List`);
   await click('Select all shown');
   await click('Update List');
   assert.deepEqual((await ids()).sort(),[added,initial].sort());
   await click(added);
   assert.equal(await evaluate("document.querySelector('.admin-entity__main h3').textContent"),added);
   await click(initial);
   if(section==='units') assert.equal(await evaluate('window.source.units.guard.overrides.attack'),5);
   if(section==='factions') assert.equal(await evaluate('window.source.factions.good.overrides.color'),'#123456');
   if(section==='territories') assert.equal(await evaluate('window.source.territory_overrides.shire.display_name'),'Shire');
   await click(`Update ${singular} List`);
   await click('Clear shown');
   await click('Update List');
   assert.deepEqual(await ids(),[]);
   await click(`Update ${singular} List`);
   await click('Select all shown');
   await click('Update List');
   assert.equal((await ids()).length,2);
  }
  await click('units'); await click('guard');
  await evaluate(`document.querySelector('[aria-label="Override attack"]').click()`);await delay(60);
  assert.equal(await evaluate('window.source.units.guard.overrides.attack'),4);
  for(const width of [1280,390]){
   await page.call('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:width<700});
   assert.equal(await evaluate('document.documentElement.scrollWidth>innerWidth'),false);
   await click('Update Unit List');
   assert.equal(await evaluate('document.documentElement.scrollWidth>innerWidth'),false);
   const styles=await evaluate(`(()=>{const d=document.querySelector('[role=dialog]'),close=[...d.querySelectorAll('button')].find(b=>b.textContent==='Close'),title=d.querySelector('h2');return {font:getComputedStyle(close).fontFamily,case:getComputedStyle(close).textTransform,right:close.getBoundingClientRect().left>title.getBoundingClientRect().right};})()`);
   assert.ok(styles.font.includes('IM Fell English'));assert.equal(styles.case,'none');assert.ok(styles.right);
   await click('Close');
   const shot=await page.call('Page.captureScreenshot',{format:'png'});
   await writeFile(`/private/tmp/setup-selection-${width}.png`,Buffer.from(shot.data,'base64'));
  }
 } finally {await unlink(fixture).catch(()=>{});page?.close();await manager.call('Target.closeTarget',{targetId});manager.close();}
});
