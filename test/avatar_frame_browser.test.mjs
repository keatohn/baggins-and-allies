// AVATAR_FRAME_BROWSER_URL=http://127.0.0.1:9233 node --test test/avatar_frame_browser.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { writeFile, unlink } from 'node:fs/promises';
const browser = process.env.AVATAR_FRAME_BROWSER_URL;
const vite = process.env.AVATAR_FRAME_VITE_URL ?? 'http://127.0.0.1:5188';
const fixture = new URL('../frontend/__avatar_frame_test.html', import.meta.url);
const delay = (ms) => new Promise(resolve => setTimeout(resolve, ms));
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

test('portrait frames load in each layout, preserve clicks and recover from failed artwork', {skip: !browser, timeout: 20000}, async () => {
  const metadata = await (await fetch(`${browser}/json/version`)).json();
  const manager = await connect(metadata.webSocketDebuggerUrl);
  const {targetId} = await manager.call('Target.createTarget', {url:'about:blank'});
  let page;
  try {
    const tabs = await (await fetch(`${browser}/json`)).json();
    page = await connect(tabs.find(tab => tab.id === targetId).webSocketDebuggerUrl);
    const evaluate = async expression => {
      const result = await page.call('Runtime.evaluate', {expression, returnByValue:true});
      assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    const until = async expression => {
      for (let i=0;i<80;i++) { if(await evaluate(expression)) return; await delay(100); }
      assert.fail(`Timed out: ${expression}`);
    };
    await writeFile(fixture, `<html><body><div id="root"></div><script type="module">
      import React from 'react'; import {createRoot} from 'react-dom/client';
      import AvatarFrame from '/src/components/account/AvatarFrame.tsx';
      import {tierRingProps} from '/src/components/account/tierRing.ts';
      import '/src/index.css'; import '/src/components/AccountMenu.css'; import '/src/components/Header.css';
      const h = React.createElement;
      function App() {
        const [src,setSrc] = React.useState('/frames/gold.png'); window.setFrameSource=setSrc;
        const tier={visual:{frame_src:src,ring_color:'#c9a227',glow:'soft'}};
        const ring=tierRingProps(tier);
        const hosts=['account-menu__btn account-menu__btn--avatar','profile-avatar-preview','friends-list__avatar','friends-list__avatar','seat-card__avatar','header-turn-ticker-portrait'];
        return h('main',null,h('h2',null,'Profile frame layouts'),h('section',null,...hosts.map((name,i)=>h('div',{key:i},h(i===2||i===3?'span':i===1||i===4?'div':'button',{className:name+ring.className,style:{...ring.style,...(i===5?{width:64,height:64}:{})},onClick:()=>window.clicked=i,'data-host':i},h('img',{className:i===0?'account-menu__avatar-img':i===1?'profile-avatar-preview__img':'',src:'/assets/units/rohan_peasant.png',alt:'Portrait'}),h(AvatarFrame,{tier})),h('p',null,['Menu','Profile','Friends','Leaderboard','Player card','Turn portrait'][i])))),h('h2',null,'All five uploaded frames'),h('section',null,...['steel','gold','mithril','secret_fire','silmaril'].map(name=>h('div',{key:name},h('div',{className:'profile-avatar-preview'+ring.className,style:ring.style},h('img',{className:'profile-avatar-preview__img',src:'/assets/units/rohan_peasant.png',alt:'Portrait'}),h(AvatarFrame,{tier:{visual:{frame_src:'/frames/'+name+'.png'}}})),h('p',null,name)))));
      }
      createRoot(document.getElementById('root')).render(h(App));
    </script><style>body{padding:32px}section{display:flex;gap:40px;align-items:center}section>div{text-align:center}h2{margin:20px 0}</style></body></html>`);
    await page.call('Emulation.setDeviceMetricsOverride',{width:1100,height:600,deviceScaleFactor:1,mobile:false});
    await page.call('Page.navigate',{url:`${vite}/__avatar_frame_test.html`});
    await until("document.querySelectorAll('.tier-avatar__frame--loaded').length===11");
    await delay(350); // Let existing portrait border transitions settle.
    const sizes = await evaluate(`Array.from(document.querySelectorAll('[data-host]')).map(el=>{
      const frame=el.querySelector('.tier-avatar__frame'), rect=el.getBoundingClientRect(), fr=frame.getBoundingClientRect(), css=getComputedStyle(el);
      return {width:rect.width,height:rect.height,fw:fr.width,fh:fr.height,overflow:css.overflow,border:css.borderColor,pointer:getComputedStyle(frame).pointerEvents,hidden:frame.getAttribute('aria-hidden'),alt:frame.alt};
    })`);
    for(const size of sizes) {
      assert.ok(Math.abs(size.width-size.fw)<1, JSON.stringify(size));
      assert.ok(Math.abs(size.height-size.fh)<1, JSON.stringify(size));
      assert.equal(size.overflow,'visible'); assert.equal(size.border,'rgba(0, 0, 0, 0)');
      assert.equal(size.pointer,'none'); assert.equal(size.hidden,'true'); assert.equal(size.alt,'');
    }
    const point=await evaluate("(()=>{const r=document.querySelector('[data-host=\"0\"]').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()");
    await page.call('Input.dispatchMouseEvent',{type:'mousePressed',...point,button:'left',buttons:1,clickCount:1});
    await page.call('Input.dispatchMouseEvent',{type:'mouseReleased',...point,button:'left',buttons:0,clickCount:1});
    assert.equal(await evaluate('window.clicked'),0);
    await delay(350);
    assert.equal(await evaluate("getComputedStyle(document.querySelector('[data-host]')).borderColor"),'rgba(0, 0, 0, 0)');
    const screenshot=await page.call('Page.captureScreenshot',{format:'png'});
    await writeFile('/private/tmp/baggins-avatar-frames.png',Buffer.from(screenshot.data,'base64'));
    await evaluate("window.setFrameSource('/frames/missing-test-frame.png')");
    await until("document.querySelectorAll('[data-host] .tier-avatar__frame').length===0");
    await delay(350);
    assert.equal(await evaluate("getComputedStyle(document.querySelector('[data-host]')).borderColor"),'rgb(201, 162, 39)');
    assert.equal(await evaluate("getComputedStyle(document.querySelector('[data-host]')).overflow"),'hidden');
    await evaluate("window.setFrameSource('/frames/mithril.png')");
    await until("document.querySelectorAll('[data-host] .tier-avatar__frame--loaded').length===6");
    const recovered=await evaluate("Array.from(document.querySelectorAll('[data-host]')).map(el=>({width:el.getBoundingClientRect().width,height:el.getBoundingClientRect().height}))");
    assert.deepEqual(recovered,sizes.map(({width,height})=>({width,height})));
    await evaluate('window.setFrameSource(null)');
    await until("document.querySelectorAll('[data-host].tier-avatar').length===0");
    assert.equal(await evaluate("document.querySelectorAll('[data-host].tier-ring').length"),6);
  } finally {
    await unlink(fixture).catch(()=>{});
    page?.close(); await manager.call('Target.closeTarget',{targetId}); manager.close();
  }
});
