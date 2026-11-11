// Isolated API/model fixtures. OLCR_MAP_LIVE_TILES=1 verifies real OSM images.
const {chromium}=require(process.env.OLCR_PLAYWRIGHT_MODULE || '../mcp-runtime/node/node_modules/playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const page=await browser.newPage({viewport:{width:1150,height:860}});
 page.setDefaultTimeout(10000);
 const liveTiles=process.env.OLCR_MAP_LIVE_TILES==='1';
 const errors=[];page.on('pageerror',e=>{errors.push(e.message);console.error(e.stack)});
 let messages=[],lastPayload,failNext=false,reverseCount=0;
 let failTiles=process.env.OLCR_MAP_FAIL_TILES==='1';
 if(!liveTiles)await page.route('https://tile.openstreetmap.org/**',route=>failTiles?route.fulfill({status:503,body:'Tile fixture unavailable'}):route.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j2ioAAAAASUVORK5CYII=','base64')}));
 await page.route('**/__map_api/**',async route=>{
  const request=route.request(),path=new URL(request.url()).pathname.replace('/__map_api','');
  const respond=body=>route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
  if(path==='/health')return respond({status:'ok'});
  if(path==='/commands')return respond({commands:[]});
  if(path==='/projects')return respond({projects:[]});
  if(path==='/settings')return respond({computer_use_enabled:false,conversation_memory_enabled:false});
  if(path==='/brain/models')return respond({models:[],providers:[]});
  if(path==='/permissions')return respond({level:'automatic'});
  if(path==='/brain/configuration')return respond({mode:'unified',selections:{brain:'fixture',coder:'fixture',short_coding:'fixture',general_inference:'fixture'},efforts:{brain:null,coder:null,short_coding:null,general_inference:null}});
  if(path.endsWith('/coding-tasks'))return respond({tasks:[]});
  if(path==='/conversations')return respond(request.method()==='POST'?{id:'c1',title:'Map fixture'}:{conversations:[{id:'c1',title:'Map fixture'}]});
  if(path==='/conversations/c1')return respond({id:'c1',project_id:null,title:'Map fixture',messages});
  if(path==='/maps/reverse'){reverseCount++;return respond({name:reverseCount===1?'日本, 東京都, 千代田区':'日本, 大阪府, 大阪市'});}
  if(path==='/chat'||path==='/chat/stream'){
   lastPayload=request.postDataJSON();
   if(failNext){failNext=false;return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'fixture failure'})});}
   messages.push({role:'user',content:lastPayload.message,blocks:[lastPayload.location_map]},{role:'assistant',content:'両地点を比較しました。'});
   if(path==='/chat/stream')return route.fulfill({contentType:'text/event-stream',body:[{type:'meta',conversation_id:'c1'},{type:'chunk',text:'両地点を比較しました。'},{type:'done',task:{id:'fixture'}}].map(v=>'data: '+JSON.stringify(v)+'\n\n').join('')});
   return respond({conversation_id:'c1',response:'両地点を比較しました。'});
  }
  return respond({});
 });
 try{
  await page.goto('http://127.0.0.1:5197/');
  const plus=page.getByRole('button',{name:'操作を追加'});await plus.waitFor();await page.waitForFunction(()=>!document.querySelector('.attach-button').disabled);
  await plus.click();await page.getByRole('menuitem',{name:'地図・地点を指定'}).click();
  const map=page.locator('.chat-map-canvas');await map.waitFor();
  if(failTiles){
   await page.getByRole('alert').filter({hasText:'地図画像を読み込めませんでした'}).waitFor();
   failTiles=false;await page.getByRole('button',{name:'地図を再読み込み'}).click();
   await page.waitForFunction(()=>!document.querySelector('.chat-map-tile-status'));
  }

  await map.click({position:{x:190,y:140}});await page.getByRole('textbox',{name:'地点1の地名'}).waitFor();
  await map.click({position:{x:330,y:150}});await page.getByRole('textbox',{name:'地点2の地名'}).waitFor();
  assert.equal(await page.locator('.chat-map-pin').count(),2);
  await page.getByRole('textbox',{name:'地点1の地名'}).fill('日本, 東京都, 千代田区（確認済み）');
  if(liveTiles){
   // Correct fixture labels to real coordinates before validating map imagery.
   await page.getByText('座標と地名を入力',{exact:true}).click();
   for(const point of [{slot:'A を指定',lat:'35.6812',lon:'139.7671',name:'日本, 東京都, 千代田区'},{slot:'B を指定',lat:'34.6937',lon:'135.5023',name:'日本, 大阪府, 大阪市'}]){
    await page.getByRole('button',{name:point.slot,exact:true}).click();
    await page.getByRole('textbox',{name:'Latitude',exact:true}).fill(point.lat);
    await page.getByRole('textbox',{name:'Longitude',exact:true}).fill(point.lon);
    await page.getByRole('textbox',{name:'地名',exact:true}).fill(point.name);
    await page.getByRole('button',{name:'ピンを設定',exact:true}).click();
   }
   await page.getByRole('textbox',{name:'地点1の地名'}).fill('日本, 東京都, 千代田区（確認済み）');
   await page.getByText('座標と地名を入力',{exact:true}).click();
   await page.waitForFunction(()=>Array.from(document.querySelectorAll('.leaflet-tile')).filter(img=>img.complete&&img.naturalWidth===256).length>=4,{},{timeout:30000});
  }
  const canvas=await map.elementHandle();
  await page.getByRole('button',{name:'地図を全画面表示'}).click();await page.getByRole('dialog',{name:'地点マップ'}).waitFor();
  assert.equal(await canvas.evaluate(el=>el===document.querySelector('.chat-map-canvas')),true);
  if(liveTiles){
   await page.waitForFunction(()=>Array.from(document.querySelectorAll('.leaflet-tile')).filter(img=>img.complete&&img.naturalWidth===256).length>=4,{},{timeout:30000});
   await page.waitForLoadState('networkidle');
   if(process.env.OLCR_MAP_SCREENSHOT)await page.screenshot({path:process.env.OLCR_MAP_SCREENSHOT.replace('.png','-fullscreen.png'),fullPage:true});
  }
  await page.keyboard.press('Escape');assert.equal(await page.getByRole('dialog').count(),0);
  await page.getByRole('button',{name:'地図を閉じる'}).click();await page.getByRole('button',{name:/地点マップ · 2\/2/}).click();
  assert.equal(await page.locator('.chat-map-pin').count(),2);
  await page.getByRole('textbox',{name:'メッセージ',exact:true}).fill('ピンの落ちている二点における人口密度を比較して');
  failNext=true;await page.getByRole('button',{name:'送信',exact:true}).click();
  await page.getByRole('button',{name:'送信',exact:true}).waitFor();
  assert.equal(await page.locator('.chat-map-location').count(),2);
  await page.getByRole('button',{name:'送信',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.messages').textContent.includes('両地点を比較しました。'));
  assert.equal(lastPayload.location_map.points.length,2);assert.match(lastPayload.location_map.points[0].name,/確認済み/);
  await page.waitForFunction(()=>document.querySelectorAll('.messages .chat-map').length===1);
  await page.locator('.messages .chat-map').waitFor();
  assert.equal(await page.locator('.messages .chat-map-pin').count(),2);
  assert.equal(await page.locator('form .chat-map').count(),0);
  await page.locator('.messages').evaluate(el=>el.scrollTo(0,el.scrollHeight));
  if(liveTiles)await page.waitForFunction(()=>Array.from(document.querySelectorAll('.messages .leaflet-tile')).filter(img=>img.complete&&img.naturalWidth===256).length>=4,{},{timeout:30000});
  if(process.env.OLCR_MAP_SCREENSHOT)await page.screenshot({path:process.env.OLCR_MAP_SCREENSHOT,fullPage:true});
  assert.deepEqual(errors,[]);console.log((liveTiles?'REAL OSM TILES + ':'')+'PASS: pin selection, labels, fullscreen DOM continuity, collapse/reopen, retry, request payload, history map');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
