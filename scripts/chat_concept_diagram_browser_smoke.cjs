// Real browser UI smoke with isolated, deterministic API responses.
// Start Vite with VITE_OLCR_API=http://127.0.0.1:5195/__diagram_api first.
const {chromium}=require('../mcp-runtime/node/node_modules/playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
(async()=>{
  const browser=await chromium.launch({channel:'chrome',headless:true});
  const page=await browser.newPage({viewport:{width:1150,height:860}});
  page.setDefaultTimeout(10000);
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  const messages=[];let failNext=false,lastPayload,conversations=[];
  await page.route('**/__diagram_api/**',async route=>{
    const request=route.request(),pathname=new URL(request.url()).pathname.replace('/__diagram_api','');
    const respond=body=>route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
    if(pathname==='/health')return respond({status:'ok'});
    if(pathname==='/commands')return respond({commands:[]});
    if(pathname==='/projects')return respond({projects:[]});
    if(pathname==='/settings')return respond({computer_use_enabled:false,conversation_memory_enabled:false});
    if(pathname==='/brain/models')return respond({models:[],providers:[]});
    if(pathname==='/brain/configuration')return respond({mode:'unified',selections:{brain:'fixture',coder:'fixture',short_coding:'fixture',general_inference:'fixture'},efforts:{brain:null,coder:null,short_coding:null,general_inference:null}});
    if(pathname.endsWith('/coding-tasks'))return respond({tasks:[]});
    if(pathname==='/conversations')return respond({conversations});
    if(pathname==='/conversations/c1')return respond({id:'c1',title:'Concept fixture',project_id:null,messages});
    if(pathname==='/chat'||pathname==='/chat/stream'){
      lastPayload=request.postDataJSON();
      if(failNext){failNext=false;return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'Fixture send failure'})});}
      const blocks=[...(lastPayload.attachment?[{type:'attachment',name:lastPayload.attachment.name}]:[]),...(lastPayload.concept_diagram?[lastPayload.concept_diagram]:[])];
      messages.push({role:'user',content:lastPayload.message,blocks},{role:'assistant',content:'概念図を受け取りました。'});conversations=[{id:'c1',title:'Concept fixture'}];
      if(pathname==='/chat/stream')return route.fulfill({contentType:'text/event-stream',body:[{type:'meta',conversation_id:'c1'},{type:'chunk',text:'概念図を受け取りました。'},{type:'done',task:{id:'fixture'}}].map(value=>'data: '+JSON.stringify(value)+'\n\n').join('')});
      return respond({conversation_id:'c1',response:'概念図を受け取りました。'});
    }
    return respond({});
  });
  try{
    await page.goto(process.env.OLCR_DIAGRAM_UI_URL||'http://127.0.0.1:5195/');
    const plus=page.getByRole('button',{name:'操作を追加'});await plus.waitFor({state:'visible'});await page.waitForFunction(()=>!document.querySelector('.attach-button').disabled);
    await plus.click();await page.getByRole('menuitem',{name:'概念図',exact:true}).click();
    const editor=page.getByRole('region',{name:'概念図エディター'});await editor.waitFor();
    await page.getByRole('button',{name:'＋ ボックス',exact:true}).click();
    await page.getByRole('textbox',{name:'ボックスの文字'}).nth(0).fill('入力');
    await page.getByRole('button',{name:'＋ ボックス',exact:true}).click();
    await page.getByRole('textbox',{name:'ボックスの文字'}).nth(1).fill('出力');
    await page.getByRole('button',{name:'全体表示'}).click();
    await page.waitForFunction(()=>document.querySelector('.react-flow__node')?.getBoundingClientRect().width<200);
    // Move a real React Flow node and join real handles using pointer input.
    const node=page.locator('.react-flow__node').nth(1),box=await node.boundingBox();
    await page.mouse.move(box.x+box.width/2,box.y+8);await page.mouse.down();await page.mouse.move(box.x+box.width/2+60,box.y+8+45,{steps:10});await page.mouse.up();
    await page.getByRole('button',{name:'全体表示'}).click();
    await page.waitForTimeout(180);
    const source=await page.locator('.react-flow__node').nth(0).locator('.react-flow__handle-right').boundingBox();
    const target=await page.locator('.react-flow__node').nth(1).locator('.react-flow__handle-left').boundingBox();
    await page.mouse.move(source.x+source.width/2,source.y+source.height/2);await page.mouse.down();await page.mouse.move(target.x+target.width/2,target.y+target.height/2,{steps:20});await page.waitForTimeout(100);await page.mouse.up();
    await page.waitForFunction(()=>document.querySelectorAll('.react-flow__edge').length===1);
    // Edge/node deletion and Undo/Redo restore the structured connections.
    const edgeBox=await page.locator('.react-flow__edge-path').boundingBox();await page.mouse.click(edgeBox.x+edgeBox.width/2,edgeBox.y+edgeBox.height/2);
    await page.getByRole('button',{name:'選択を削除'}).click();await page.waitForFunction(()=>document.querySelectorAll('.react-flow__edge').length===0);
    await page.getByRole('button',{name:'元に戻す'}).click();await page.waitForFunction(()=>document.querySelectorAll('.react-flow__edge').length===1);
    await page.getByRole('button',{name:'やり直す'}).click();await page.waitForFunction(()=>document.querySelectorAll('.react-flow__edge').length===0);
    await page.getByRole('button',{name:'元に戻す'}).click();await page.waitForFunction(()=>document.querySelectorAll('.react-flow__edge').length===1);
    const deleteNodeBox=await page.locator('.react-flow__node').nth(1).boundingBox();await page.mouse.click(deleteNodeBox.x+deleteNodeBox.width/2,deleteNodeBox.y+5);
    await page.getByRole('button',{name:'選択を削除'}).click();await page.waitForFunction(()=>document.querySelectorAll('.react-flow__node').length===1 && document.querySelectorAll('.react-flow__edge').length===0);
    await page.getByRole('button',{name:'元に戻す'}).click();await page.waitForFunction(()=>document.querySelectorAll('.react-flow__node').length===2 && document.querySelectorAll('.react-flow__edge').length===1);
    if(process.env.OLCR_DIAGRAM_SCREENSHOT)await page.screenshot({path:path.resolve(process.env.OLCR_DIAGRAM_SCREENSHOT),fullPage:true});
    await page.getByRole('button',{name:'概念図を閉じる'}).click();assert.equal(await editor.count(),0);
    await page.getByRole('button',{name:/概念図のドラフト/}).click();assert.equal(await page.getByRole('textbox',{name:'ボックスの文字'}).nth(0).inputValue(),'入力');
    await page.getByRole('textbox',{name:'メッセージ',exact:true}).fill('この図を説明して');
    failNext=true;await page.getByRole('button',{name:'送信',exact:true}).click();await page.getByText(/Fixture send failure/).waitFor();
    await page.waitForFunction(()=>document.querySelectorAll('.concept-diagram-editor textarea:not(:disabled)').length===2);assert.equal(await page.getByRole('textbox',{name:'メッセージ',exact:true}).inputValue(),'この図を説明して');assert.equal(await page.locator('.concept-diagram-editor textarea').count(),2);
    await plus.click();const chooser=page.waitForEvent('filechooser');await page.getByRole('menuitem',{name:'ファイルを添付'}).click();await(await chooser).setFiles({name:'note.txt',mimeType:'text/plain',buffer:Buffer.from('supporting note')});
    await page.getByRole('button',{name:'送信',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.concept-diagram-editor'));
    assert.equal(lastPayload.concept_diagram.edges.length,1);assert.ok(lastPayload.concept_diagram.nodes[1].position.x>240);assert.ok(lastPayload.concept_diagram.nodes[1].position.y>0);assert.equal(lastPayload.attachment.name,'note.txt');
    await page.locator('.concept-diagram-preview').first().waitFor();
    // Reload and select persisted conversation: no draft should replace the sent diagram.
    await page.reload();await page.getByRole('button',{name:'Concept fixture',exact:true}).click();await page.locator('.concept-diagram-preview').first().waitFor();
    assert.equal(await page.locator('.concept-diagram-preview .react-flow__node').count(),2);
    // Diagram-only submission uses stream and retains the payload.
    await plus.click();await page.getByRole('menuitem',{name:'概念図',exact:true}).click();await page.getByRole('button',{name:'＋ ボックス',exact:true}).click();await page.getByRole('textbox',{name:'ボックスの文字'}).fill('図だけの送信');await page.getByRole('button',{name:'送信',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.concept-diagram-editor'));assert.equal(lastPayload.concept_diagram.nodes[0].label,'図だけの送信');
    // A narrow viewport keeps the composer and menu usable.
    await page.setViewportSize({width:620,height:860});await plus.click();assert.equal(await page.getByRole('menu').evaluate(node=>getComputedStyle(node).overflowY),'auto');
    await page.keyboard.press('ArrowDown');await page.keyboard.press('Escape');assert.equal(await page.getByRole('menu').count(),0);
    assert.deepEqual(errors,[]);
    console.log('PASS: boxes, drag/connect, delete/undo/redo, close/reopen, failed-send preservation, file+diagram send, reload, diagram-only stream, narrow viewport, keyboard menu');
  }catch(error){console.error("Browser errors:",errors);console.error((await page.locator("body").innerText()).slice(-1800));await page.screenshot({path:"/private/tmp/olcr-diagram-failure.png"});throw error;}finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
