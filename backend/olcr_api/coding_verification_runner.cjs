// OLCR-owned acceptance runner. Check success requires an observed assertion.
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {pathToFileURL, fileURLToPath} = require('node:url');
const {createRequire} = require('node:module');
const {spawnSync} = require('node:child_process');
const strict = require('node:assert/strict');
const [contractPath, workspaceArg, resources, browserExecutable] = process.argv.slice(2);
const playwrightExpect = require(path.join(resources,'node_modules/playwright/test')).expect.configure({timeout:4000});
const workspace = fs.realpathSync(workspaceArg);
const contract = JSON.parse(fs.readFileSync(contractPath, 'utf8'));
const projectBuild=Object.freeze(contract._olcr_project_build || {});
const inRoot = relative => {
  const target = fs.realpathSync(path.resolve(workspace, relative));
  if (target !== workspace && !target.startsWith(workspace + path.sep)) throw Error('outside workspace');
  return target;
};
const readFile = relative => fs.readFileSync(inRoot(relative), 'utf8');
const compiledModules=new Map();
const loadModule = async relative => {
  const source=inRoot(relative);
  if(contract._olcr_vite_preview && /\.[jt]sx?$/.test(source)) {
    if(!compiledModules.has(source))compiledModules.set(source,(async()=>{
      const localRequire=createRequire(path.join(workspace,'package.json'));
      const vite=await import(pathToFileURL(localRequire.resolve('vite')).href);
      const directory=path.join(workspace,'.olcr-node-modules',String(compiledModules.size));
      const output={output:{format:'cjs',entryFileNames:'module.cjs'}};
      const options=Number(vite.version.split('.')[0])>=8 ? {rolldownOptions:output} : {rollupOptions:output};
      await vite.build({root:workspace,configFile:false,logLevel:'silent',
        build:{ssr:source,outDir:directory,emptyOutDir:true,minify:false,...options}});
      return localRequire(path.join(directory,'module.cjs'));
    })());
    return compiledModules.get(source);
  }
  const namespace=await import(pathToFileURL(source).href);
  // Node exposes object-valued CommonJS exports under default. Offer the
  // same real exports by name without changing or emulating the module.
  return namespace.default && typeof namespace.default==='object'
    ? {...namespace.default,...namespace} : namespace;
};
function observedBrowserAPI(target, calls, proxies=new WeakMap()) {
  const kind=target?.constructor?.name?.replace(/^_+/,'');
  if(!['Page','Locator','FrameLocator'].includes(kind))return target;
  if(proxies.has(target))return proxies.get(target);
  const dialogHandlers=new Map();
  const proxy=new Proxy(target,{get(object,key){
    const value=Reflect.get(object,key);
    if(typeof value!=='function' || key==='constructor')return value;
    return (...args)=>{
      if(kind==='Page' && ['on','once'].includes(key) && args[0]==='dialog') {
        if(dialogHandlers.size)throw new TypeError('Verification contract: multiple pending dialog handlers respond to the same dialog. Use one handler with a response queue for sequential dialogs.');
        const callback=args[1];
        const handler=dialog=>{
          if(key==='once')dialogHandlers.delete(callback);
          try { Promise.resolve(callback(dialog)).catch(error=>(calls.__eventErrors??=[]).push(error)); }
          catch(error) { (calls.__eventErrors??=[]).push(error); }
        };
        dialogHandlers.set(callback,handler);
        args=[args[0],handler];
      }
      if(kind==='Page' && ['off','removeListener'].includes(key) && args[0]==='dialog') {
        const handler=dialogHandlers.get(args[1]);
        if(handler){dialogHandlers.delete(args[1]);args=[args[0],handler];}
      }
      if(kind==='Page' && key==='removeAllListeners' && (args[0]===undefined||args[0]==='dialog')) {
        dialogHandlers.clear();
      }
      if(kind==='Page' && ['setContent','addScriptTag','addStyleTag','addInitScript','route','routeWebSocket','unroute','unrouteAll'].includes(key))
        throw Error('Readonly verification cannot replace the application or its resources: Page.'+String(key));
      const started=calls.__clock=(calls.__clock||0)+1;
      // Observe real state transitions around user actions. A completed click
      // is not evidence that the app handled it; missing event wiring produces
      // no page exception and used to look like a defective downstream check.
      const actionPage=['click','fill','selectOption','reload'].includes(String(key))
        ? (kind==='Page'?object:kind==='Locator'?object.page():null) : null;
      const fillTarget=key==='fill' ? (kind==='Page'?object.locator(args[0]):kind==='Locator'?object:null) : null;
      const invoke=()=>{
        if(kind==='Locator' && key==='click') {
          // Native <option> nodes are not visible click targets in Chromium.
          // Recover the intended user selection through the owning <select>,
          // which emits the real input/change events instead of mutating DOM.
          return object.evaluate(element=>element.tagName==='OPTION'
            ? {value:element.value} : null).then(option=>{
              if(!option)return Reflect.apply(value,object,args);
              const select=object.locator('xpath=ancestor::select[1]');
              return select.count().then(count=>{
                if(count!==1)throw new Error('Cannot select a native option without exactly one owning <select>.');
                const options={};
                if(Number.isFinite(args[0]?.timeout))options.timeout=args[0].timeout;
                return select.selectOption(option.value,options);
              });
            });
        }
        return Reflect.apply(value,object,args);
      };
      let beforeAction;
      let beforeFill;
      const result=actionPage
        ? observedPageState(actionPage).catch(()=>null).then(async before=>{
            beforeAction=before;
            if(fillTarget)beforeFill=await observedFillControl(fillTarget).catch(()=>null);
            return invoke();
          }) : invoke();
      if(result && typeof result.then==='function') {
        const call={api:kind+'.'+String(key),observed:false,started,
          selector:kind==='Locator' ? object._selector : typeof args[0]==='string' ? args[0] : undefined};
        if(['click','fill','selectOption','reload'].includes(String(key))) {
          call.action=key==='selectOption' ? 'select' : String(key);
          call.selector=kind==='Locator' ? object._selector : typeof args[0]==='string' ? args[0] : undefined;
        }
        const supplied=key==='fill' ? (kind==='Page' ? args[1] : kind==='Locator' ? args[0] : undefined) : undefined;
        if(key==='fill' && typeof supplied==='string')call.input_summary={length:supplied.length,
          line_count:supplied.split(/\r\n|\r|\n/).length,contains_newline:/[\r\n]/.test(supplied)};
        if(typeof supplied==='string' && /[\r\n]/.test(supplied) && supplied.length<=500) {
          call.requested_input=supplied;call.selector=kind==='Page' ? args[0] : object._selector;
        }
        calls.push(call);
        // Observe rejections even when malformed check code discards the
        // result; awaiting this thenable still propagates the real rejection.
        const tracked=result.then(async value=>{
          if(actionPage) {
            const after=await observedPageState(actionPage).catch(()=>null);
            if(after || key==='fill') {
              const compact=state=>({text:(state?.text||'').slice(0,300),buttons:(state?.buttons||[]).slice(0,4),
                fields:(state?.fields||[]).filter(field=>!/password|secret|token|api[-_ ]?key|credential|authorization|auth/i
                  .test([field.id,field.name,field.type].filter(Boolean).join(' '))).slice(0,24)});
              if(beforeAction && after) {
                call.observed_state_changed=JSON.stringify(compact(beforeAction))!==JSON.stringify(compact(after));
                call.state_before=compact(beforeAction);
              }
              if(after)call.state_after=compact(after);
              if(key==='fill' && typeof supplied==='string' && supplied.length<=500) {
                const afterFill=fillTarget?await observedFillControl(fillTarget).catch(()=>null):null;
                const selector=call.selector||'';
                const idMatch=selector.match(/#([A-Za-z_][\w:-]*)/);
                const nameMatch=selector.match(/\[name\s*=\s*(["'])([^"']+)\1\s*\]/i);
                const role=selector.match(/^internal:role=(textbox|spinbutton)(?:\b|$)/i)?.[1]?.toLowerCase();
                const findField=state=>{
                  const fields=state?.fields||[];
                  if(idMatch)return fields.find(field=>field.id===idMatch[1]);
                  if(nameMatch)return fields.find(field=>field.name===nameMatch[2]);
                  if(role) {
                    const matching=fields.filter(field=>field.tag==='textarea'
                      || (field.tag==='input' && (role==='spinbutton' ? field.type==='number'
                        : ['text','search','email','url','tel'].includes(field.type))));
                    return matching.length===1 ? matching[0] : undefined;
                  }
                  if(selector.startsWith('internal:') && fields.length===1)return fields[0];
                  return undefined;
                };
                const fieldBefore=findField(beforeAction)||beforeFill;
                const fieldAfter=afterFill||findField(after);
                const field=fieldAfter||fieldBefore;
                const sensitive=field?.sensitive||/password|secret|token|api[-_ ]?key|credential|authorization|auth/i
                  .test([selector,field?.id,field?.name,field?.type].filter(Boolean).join(' '));
                if(field && !sensitive && field.type!=='password' && field.tag!=='select') {
                  call.field_id=field.id||'';call.field_name=field.name||'';call.field_type=field.type||'';
                  if(fieldBefore && typeof fieldBefore.value==='string')call.previous_value=fieldBefore.value;
                  if(typeof fieldAfter?.value==='string')call.observed_value=fieldAfter.value;
                  call.requested_value=supplied;
                }
              }
            }
          }
          call.finished=calls.__clock=(calls.__clock||0)+1;
          if(['textContent','innerText','inputValue'].includes(key) && typeof value==='string' && value.length<=500) {
            call.read_type=String(key);call.text=value.trim();
          }
          if(key==='allTextContents' && Array.isArray(value) && value.every(item=>typeof item==='string') && value.join('\n').length<=500){
            call.read_type='allTextContents';call.text=value.join('\n').trim();
          }
          return value;
        },error=>{call.rejected=true;call.finished=calls.__clock=(calls.__clock||0)+1;throw error});
        tracked.catch(()=>{});
        return {then(resolve,reject){call.observed=true;return tracked.then(resolve,reject)},
          catch(reject){call.observed=true;return tracked.catch(reject)},
          finally(callback){call.observed=true;return tracked.finally(callback)}};
      }
      return observedBrowserAPI(result,calls,proxies);
    };
  }});
  proxies.set(target,proxy);
  return proxy;
}
async function selectionExclusionWitness(page,calls,spec,ids) {
  if(!page || !spec || !ids.some(id=>spec.ids.includes(id)) || calls.some(call=>!call.observed))return null;
  try {
    const fields=await page.locator('textarea,input:not([type=password]):not([type=hidden]):not([type=button]):not([type=submit]),[contenteditable=true]').evaluateAll(
      elements=>elements.map(el=>({id:el.id,tag:el.tagName.toLowerCase()})));
    const buttons=await page.locator('button,input[type=button],input[type=submit]').evaluateAll(
      elements=>elements.map(el=>({id:el.id,text:(el.textContent||el.value||'').trim()})));
    const regions=await page.locator('output,[role=status],[aria-live=polite],[aria-live=assertive]').evaluateAll(
      elements=>elements.filter(el=>!el.querySelector('ul,ol')).map(el=>({id:el.id,tag:el.tagName.toLowerCase()})));
    if(fields.length!==1 || fields[0].tag!=='textarea' || !fields[0].id || buttons.length!==1 || !buttons[0].id
       || !spec.button_labels.includes(buttons[0].text) || regions.length!==1 || !regions[0].id)return null;
    const targets=new Map();
    async function target(selector) {
      if(!selector)return {};
      if(!targets.has(selector))targets.set(selector,page.locator(selector).evaluate(el=>({
        id:el.id,tag:el.tagName.toLowerCase(),parent_id:el.parentElement?.id||''})).catch(()=>({})));
      return targets.get(selector);
    }
    const clicks=[],reads=[],fills=[];
    for(const call of calls) {
      if(!call.observed || !call.finished || call.rejected)continue;
      const element=await target(call.selector);
      if(call.api.endsWith('.click') && element.id===buttons[0].id)clicks.push(call);
      if(call.requested_input!==undefined && element.id===fields[0].id)fills.push(call);
      if(call.text!==undefined && (element.id===regions[0].id || element.tag==='span' && element.parent_id===regions[0].id))reads.push(call);
    }
    for(let i=1;i<reads.length;i++) {
      const previous=reads[i-1],current=reads[i];
      const before=clicks.filter(call=>call.finished<previous.started).at(-1);
      const after=clicks.filter(call=>call.finished<current.started).at(-1);
      if(!before || !after || clicks.indexOf(after)!==clicks.indexOf(before)+1 || previous.finished>=after.started)continue;
      const firstInput=fills.filter(call=>call.finished<before.started).at(-1);
      const lastInput=fills.filter(call=>call.finished<after.started).at(-1);
      if(!firstInput || !lastInput || firstInput.requested_input!==lastInput.requested_input)continue;
      const choices=[...new Set(lastInput.requested_input.split(/\r?\n/).map(value=>value.trim()).filter(Boolean))];
      const observed=reads.filter(call=>choices.includes(call.text) &&
        fills.filter(fill=>fill.finished<call.started).at(-1)?.requested_input===lastInput.requested_input).map(call=>call.text);
      if(choices.length<2 || new Set(observed).size<2 || !choices.includes(current.text) || previous.text!==current.text)continue;
      return {ids:ids.filter(id=>spec.ids.includes(id)),input_id:fields[0].id,button_id:buttons[0].id,
        output_id:regions[0].id,button_label:buttons[0].text,choices:choices.slice(0,12),
        previous:previous.text,selected:current.text,previous_click:before.finished,current_click:after.finished,
        observed_results:observed.slice(-16)};
    }
  } catch { /* Unsupported/dynamic controls still use ordinary check review. */ }
  return null;
}
function testFiles(dir = workspace) {
  return fs.readdirSync(dir, {withFileTypes:true}).flatMap(e => {
    if (e.isSymbolicLink() || ['node_modules','.git','dist'].includes(e.name)) return [];
    const p = path.join(dir,e.name);
    const relative=path.relative(workspace,p).split(path.sep).join('/');
    return e.isDirectory() ? testFiles(p) : (/(?:\.test|\.spec)\.[cm]?[jt]sx?$/.test(e.name)
      || /(?:^|\/)(?:tests?|__tests__)\/.*\.[cm]?js$/i.test(relative)) ? [p] : [];
  });
}
let cachedTests, cachedTestError, lastTestRun;
function testSourceLocation(file, line, column) {
  try {
    // ESM stacks use file: URLs, including percent-encoded spaces. Decode
    // before the usual realpath/root check; external frames remain excluded.
    const absolute=inRoot(file.startsWith('file:') ? fileURLToPath(file) : file), relative=path.relative(workspace,absolute);
    const lines=readFile(relative).split('\n'), number=Number(line);
    if(!Number.isInteger(number)||number<1||number>lines.length)return null;
    return {path:relative,line:number,column:Number(column),source_line:lines[number-1],
      context:lines.slice(Math.max(0,number-4),number+3).map((text,index)=>({line:Math.max(1,number-3)+index,text}))};
  } catch {return null}
}
function projectTestRunner(files) {
  let script;
  try {script=JSON.parse(readFile('package.json')).scripts?.test}catch{}
  if(typeof script==='string'&&script.trim()) {
    // An explicit test script outranks package presence and source heuristics.
    // Identify only its runner; never execute model/project shell text here.
    if(!/[;&|<>`$\r\n"']/.test(script)) {
      const words=script.trim().split(/\s+/),executable=path.basename(words[0]);
      if(executable==='node'&&words.includes('--test'))return 'node';
      if(executable==='vitest')return 'vitest';
    }
    return null;
  }
  const runners=new Set();
  const record=name=>{if(name==='node:test')runners.add('node');if(name==='vitest')runners.add('vitest')};
  for(const file of files) {
    try {
      const ast=sourceParser.parse(readFile(file),{sourceType:'unambiguous',plugins:['typescript','jsx']});
      sourceTraverse(ast,{
        ImportDeclaration(p){record(p.node.source.value)},
        CallExpression(p){
          const {callee,arguments:args}=p.node;
          if((callee.type==='Identifier'&&callee.name==='require'||callee.type==='Import')
              && args.length===1&&args[0].type==='StringLiteral')record(args[0].value);
        }
      });
    } catch { /* Leave invalid/unsupported sources to the real test runner. */ }
  }
  return runners.size===1 ? [...runners][0] : null;
}
function runTestsSync() {
  if(cachedTestError)throw cachedTestError;
  if(cachedTests)return cachedTests;
  const files = testFiles();
  if (!files.length) throw Error('No node:test files exist');
  const vitest=path.join(workspace,'node_modules/vitest/vitest.mjs');
  if(contract._olcr_vite_preview && fs.existsSync(vitest) && projectTestRunner(files)!=='node') {
    const report=path.join(workspace,'.olcr-vitest.json');
    const result=spawnSync(process.execPath,[vitest,'run','--reporter=json','--outputFile='+report],
      {cwd:workspace,encoding:'utf8',timeout:60000});
    let data={};try{data=JSON.parse(fs.readFileSync(report,'utf8'))}catch{}
    const count=Number(data.numTotalTests||0),failed=Number(data.numFailedTests||0),passed=Number(data.numPassedTests||0);
    const skipped=Number(data.numPendingTests||0),todo=Number(data.numTodoTests||0);
    const failures=(data.testResults||[]).flatMap(suite=>(suite.assertionResults||[])
      .filter(item=>item.status==='failed').map(item=>({name:item.fullName,
        detail:(item.failureMessages||[]).join('\n').slice(-4000)})));
    lastTestRun={command:[process.execPath,vitest,'run'],tests:count,registered_tests:count,
      passed,failed,skipped,todo,executed_tests:passed+failed,returncode:result.status,failures};
    if(result.error || result.status!==0 || !count || passed<=0 || failed || data.success!==true) {
      cachedTestError=Error('vitest failed (no executed passing tests or failing assertions): '+((result.stdout||'')+(result.stderr||'')).slice(-8000));
      throw cachedTestError;
    }
    cachedTests={tests:count,testCount:count,passed,skipped,todo,returncode:0,exitCode:0};return cachedTests;
  }
  const result = spawnSync(process.execPath, ['--test', '--test-reporter=tap', ...files], {cwd:workspace, encoding:'utf8', timeout:60000});
  const output = (result.stdout || '') + (result.stderr || '');
  const count = Number(output.match(/^# tests (\d+)$/m)?.[1] || 0);
  // Node 20 counts an empty test file itself as one passing subtest. Require
  // a registered test/suite name beyond those file-container entries.
  const names = [...output.matchAll(/^\s*# Subtest: (.+)$/gm)].map(m=>m[1]);
  const isFileContainer=name=>files.some(file=>name===file || name===path.relative(workspace,file));
  const fileContainers=names.filter(isFileContainer).length;
  const passingFileContainers=[...output.matchAll(/^ok \d+ - (.+)$/gm)]
    .filter(match=>isFileContainer(match[1])).length;
  // Suites appear in TAP as named subtests but are not counted by `# tests`.
  // Use Node's summary count (minus file containers) so nested suites do not
  // inflate registration and confuse setup-failure ownership.
  const registered=Math.max(0,count-fileContainers);
  const failures=[];
  let current,stackStarted=false;
  for(const line of output.split('\n')) {
    const failed=line.match(/^\s*not ok \d+ - (.+)/);
    if(failed) {current={name:failed[1],detail:'',source_locations:[]};failures.push(current);stackStarted=false;}
    else if(line.trim()==='...' || /^\s*(?:# Subtest:|ok \d+ -|1\.\.)/.test(line))current=undefined;
    else if(current) {
      const frame=line.match(/(?:location:\s*'|\()([^\n]+?):(\d+):(\d+)(?:'|\))/);
      if(frame && current.source_locations.length<3) {
        const location=testSourceLocation(frame[1],frame[2],frame[3]);
        if(location&&!current.source_locations.some(item=>item.path===location.path&&item.line===location.line))current.source_locations.push(location);
      }
      if(/^\s*stack:/.test(line))stackStarted=true;
      if(!stackStarted && current.detail.length<1200)current.detail+=(line+'\n').slice(0,1200-current.detail.length);
    }
  }
  // An exception while loading a test/module appears before the file-container
  // subtest. Its generic "test failed" detail omits the actual source error.
  const lines=output.split('\n');
  for(let index=0; index<lines.length; index++) {
    const container=lines[index].match(/^# Subtest: (.+)$/)?.[1];
    if(!container || !files.some(file=>container===file || container===path.relative(workspace,file)))continue;
    const failure=failures.find(item=>item.name===container && item.detail.includes('ERR_TEST_FAILURE'));
    if(!failure)continue;
    let start=index;
    while(start>0 && lines[start-1].startsWith('# ') && !lines[start-1].startsWith('# Subtest:'))start--;
    const preamble=lines.slice(start,index);
    const error=preamble.find(line=>/^# [A-Za-z]*Error(?: \[[^\]]+\])?:/.test(line));
    if(!error)continue;
    const locations=[];
    for(const line of preamble) {
      const frame=line.match(/(?:^# |\(|^#\s+at )((?:file:\/\/\/|\/).*?):(\d+)(?::(\d+))?\)?$/);
      if(frame) {
        const location=testSourceLocation(frame[1],frame[2],frame[3]||1);
        if(location&&!locations.some(item=>item.path===location.path&&item.line===location.line))locations.push(location);
      }
    }
    failure.detail=error.slice(2)+'\n'+preamble.join('\n').slice(0,1000);
    if(locations.length) {
      failure.name='Node.js module loading';
      failure.source_locations=locations;
    }
  }
  const actionableFailures=failures.filter(item=>
    !/(?:^|\n)\s*failureType:\s*['\"]?subtestsFailed['\"]?\s*(?:\n|$)/m.test(item.detail));
  const passed=Math.max(0,Number(output.match(/^# pass (\d+)$/m)?.[1]||0)-passingFileContainers);
  const failed=Number(output.match(/^# fail (\d+)$/m)?.[1]||0);
  const skipped=Number(output.match(/^# skipped (\d+)$/m)?.[1]||0),todo=Number(output.match(/^# todo (\d+)$/m)?.[1]||0);
  lastTestRun={command:[process.execPath,'--test'],tests:count,registered_tests:registered,
    passed,failed,skipped,todo,executed_tests:passed+failed,
    cancelled:Number(output.match(/^# cancelled (\d+)$/m)?.[1]||0),returncode:result.status,failures:actionableFailures};
  if (result.error || result.status !== 0 || count === 0 || registered === 0 || passed === 0) {
    cachedTestError=Error('node:test failed (no executed passing tests or failing assertions): '+output.slice(-8000));
    throw cachedTestError;
  }
  // The native coding model also uses the familiar testCount/exitCode names.
  // They are aliases of the same observed successful subprocess result.
  cachedTests={tests:count, testCount:count, passed, skipped, todo, returncode:result.status, exitCode:result.status, output};
  return cachedTests;
}
async function runTests(){return runTestsSync()}
const projectRequire=createRequire(path.join(workspace,'package.json'));
function readonlyRequire(specifier,assert) {
  const builtin=specifier.replace(/^node:/,'');
  if(['assert','assert/strict'].includes(builtin))return assert;
  if(builtin==='path')return path;
  if(builtin==='url')return require('node:url');
  if(builtin==='fs')return Object.fromEntries(['readFileSync','existsSync','readdirSync','statSync','lstatSync','realpathSync']
    .map(method=>[method,(file,...args)=>{
      try{return fs[method](inRoot(file),...args)}catch(e){
        const target=path.resolve(workspace,file);
        if(method==='existsSync'&&e.code==='ENOENT'&&(target===workspace||target.startsWith(workspace+path.sep)))return false;
        throw e;
      }
    }]));
  if(builtin==='child_process')return {execFileSync:(file,args)=>{
    if(![process.execPath,'node'].includes(file) || !Array.isArray(args) || !args.includes('--test'))throw Error('Readonly verification supports node --test only');
    return runTestsSync().output;
  }};
  if(specifier.startsWith('.'))return projectRequire(inRoot(specifier));
  throw Error('Readonly verification module not supported: '+specifier);
}
let browser, server;
const sourceParser=require(path.join(resources,'node_modules/@babel/parser'));
const sourceTraverse=require(path.join(resources,'node_modules/@babel/traverse')).default;
const readonlyCalls=new Set(['getComputedStyle','matchMedia','parseFloat','parseInt','isFinite','isNaN',
  'Number','String','Boolean','BigInt','encodeURIComponent','decodeURIComponent','encodeURI','decodeURI']);
const readonlyMethods=new Set(['querySelector','querySelectorAll','getElementById','getElementsByTagName',
  'getElementsByClassName','closest','matches','contains','getAttribute','getAttributeNames','hasAttribute',
  'getBoundingClientRect','getClientRects','getPropertyValue','getPropertyPriority','escape','isSameNode','isEqualNode',
  'compareDocumentPosition','getItem','key','item','map','filter','reduce','reduceRight','every','some',
  'find','findIndex','findLast','findLastIndex','flat','flatMap','concat','slice','join','at','includes',
  'indexOf','lastIndexOf','entries','keys','values','get','has','forEach','toReversed','toSorted','toSpliced',
  'toLowerCase','toUpperCase','toLocaleLowerCase','toLocaleUpperCase','trim','trimStart','trimEnd','split',
  'substring','substr','charAt','charCodeAt','codePointAt','startsWith','endsWith','repeat','padStart','padEnd',
  'normalize','localeCompare','match','matchAll','search','replace','replaceAll','toString','toFixed','toPrecision','toExponential',
  'test','exec','from','isArray','fromEntries','hasOwn','getOwnPropertyNames','getOwnPropertySymbols',
  'getOwnPropertyDescriptor','getOwnPropertyDescriptors','getPrototypeOf','is','parse','stringify',
  'abs','ceil','floor','round','trunc','min','max','pow','sqrt','sign','random','hypot','log','log2','log10',
  'sin','cos','tan','acos','asin','atan','atan2']);
const localMutationMethods=new Set(['push','pop','shift','unshift','splice','sort','reverse','set','add','delete','clear']);
function validateObservation(source) {
  const ast=sourceParser.parse('const __olcr_observation=('+source+');');
  const reject=detail=>{throw Error('Readonly verification observation modified the DOM or application state: '+detail)};
  const memberName=node=>node.computed ? node.property.type==='StringLiteral' ? node.property.value : null : node.property?.name;
  const localContainer=(node,scope,seen=new Set())=>{
    if(!node)return false;
    if(['ObjectExpression','ArrayExpression'].includes(node.type))return true;
    if(node.type==='NewExpression')return node.callee.type==='Identifier'
      && ['Array','Map','Set'].includes(node.callee.name) && !scope.getBinding(node.callee.name);
    if(node.type!=='Identifier')return false;
    const binding=scope.getBinding(node.name);
    if(!binding || !binding.constant || seen.has(binding) || !binding.path.isVariableDeclarator())return false;
    seen.add(binding);return localContainer(binding.path.node.init,binding.path.scope,seen);
  };
  const localFunction=(node,scope,seen=new Set())=>{
    if(['FunctionExpression','ArrowFunctionExpression'].includes(node.type))return true;
    if(node.type!=='Identifier')return false;
    const binding=scope.getBinding(node.name);
    if(!binding || !binding.constant || seen.has(binding))return false;
    if(binding.path.isFunctionDeclaration())return true;
    if(!binding.path.isVariableDeclarator() || !binding.path.node.init)return false;
    seen.add(binding);return localFunction(binding.path.node.init,binding.path.scope,seen);
  };
  const writable=(node,scope)=>{
    if(node.type==='Identifier')return Boolean(scope.getBinding(node.name));
    if(node.type==='MemberExpression')return localContainer(node.object,scope);
    if(node.type==='ArrayPattern')return node.elements.every(item=>!item||writable(item,scope));
    if(node.type==='ObjectPattern')return node.properties.every(item=>writable(item.value||item.argument,scope));
    if(node.type==='AssignmentPattern')return writable(node.left,scope);
    if(node.type==='RestElement')return writable(node.argument,scope);
    return false;
  };
  sourceTraverse(ast,{
    Function(p){if(p.node.async)reject('asynchronous callback')},
    AssignmentExpression(p){if(!writable(p.node.left,p.scope))reject('write outside callback locals')},
    UpdateExpression(p){if(!writable(p.node.argument,p.scope))reject('update outside callback locals')},
    UnaryExpression(p){if(p.node.operator==='delete'&&!writable(p.node.argument,p.scope))reject('delete outside callback locals')},
    'ForInStatement|ForOfStatement'(p){if(p.node.left.type!=='VariableDeclaration'&&!writable(p.node.left,p.scope))reject('loop write outside callback locals')},
    TaggedTemplateExpression(){reject('unrecognized template call')},
    NewExpression(p){
      const callee=p.node.callee;
      if(callee.type!=='Identifier' || p.scope.getBinding(callee.name)
          || !['Array','Map','Set','Date','RegExp','URL','URLSearchParams','Error','TypeError','SyntaxError','RangeError'].includes(callee.name))
        reject('unrecognized constructor');
    },
    'CallExpression|OptionalCallExpression'(p){
      const callee=p.node.callee;
      if(localFunction(callee,p.scope))return;
      if(callee.type==='Identifier') {
        if(!p.scope.getBinding(callee.name)&&readonlyCalls.has(callee.name))return;
      } else if(['MemberExpression','OptionalMemberExpression'].includes(callee.type)) {
        const name=memberName(callee);
        if(readonlyMethods.has(name))return;
        if(localMutationMethods.has(name)&&localContainer(callee.object,p.scope))return;
      }
      reject('unrecognized or mutating call: '+(callee.name||memberName(callee)||callee.type));
    }
  });
}
function observationCallback(callback) {
  // Keep browser observations from "fixing" the DOM/CSS under test. User
  // interactions use Playwright fill/click; evaluate callbacks observe it.
  // Compile the wrapper here so no eval permission is needed in the page.
  const source=typeof callback==='function' ? callback.toString() : String(callback);
  // MutationObserver does not see input.value, CSSOM, storage, or queued work.
  // Reject those effects before evaluating; local result arrays/objects remain writable.
  validateObservation(source);
  const expression=typeof callback==='function' ? '('+source+')(...args)' : '('+source+')';
  return new Function(`return function(...args){
    const observer=new MutationObserver(()=>{}),restorations=[];
    let blocked=false;
    const deny=()=>{blocked=true;throw Error('Readonly verification observation modified the DOM or application state');};
    const protect=(prototype,key,setter=false)=>{
      if(!prototype)return;
      const descriptor=Object.getOwnPropertyDescriptor(prototype,key);
      if(!descriptor?.configurable || setter&&!descriptor.set || !setter&&typeof descriptor.value!=='function')return;
      Object.defineProperty(prototype,key,setter ? {...descriptor,set:deny} : {...descriptor,value:deny});
      restorations.push(()=>Object.defineProperty(prototype,key,descriptor));
    };
    observer.observe(document,{subtree:true,childList:true,attributes:true,characterData:true});
    try {
      for(const key of ['insertRule','deleteRule','replace','replaceSync'])protect(globalThis.CSSStyleSheet?.prototype,key);
      for(const key of ['setProperty','removeProperty'])protect(globalThis.CSSStyleDeclaration?.prototype,key);
      for(const key of ['setItem','removeItem','clear'])protect(globalThis.Storage?.prototype,key);
      for(const key of ['value','checked','indeterminate'])protect(globalThis.HTMLInputElement?.prototype,key,true);
      protect(globalThis.HTMLTextAreaElement?.prototype,'value',true);
      for(const key of ['value','selectedIndex'])protect(globalThis.HTMLSelectElement?.prototype,key,true);
      protect(globalThis.HTMLOptionElement?.prototype,'selected',true);
      protect(globalThis.EventTarget?.prototype,'dispatchEvent');
      const value=${expression};
      if(value && typeof value.then==='function'){
        Promise.resolve(value).catch(()=>{});
        throw Error('Readonly verification observations must be synchronous; await browser APIs outside the callback');
      }
      if(blocked || observer.takeRecords().length)throw Error('Readonly verification observation modified the DOM or application state');
      return value;
    } finally {
      for(const restore of restorations.reverse())restore();
      observer.disconnect();
    }
  }`)();
}
async function observedPageState(page) {
  return {text:(await page.locator('body').innerText({timeout:1000})).slice(0,2000),
            fields:await page.locator('textarea,input:not([type=password]),select,[contenteditable=true]').evaluateAll(elements=>
              elements.filter(el=>!(el.tagName==='INPUT' && el.type==='password')).slice(0,24).map(el=>({
                id:el.id,name:el.name||'',tag:el.tagName.toLowerCase(),type:el.type||'',type_attribute:el.getAttribute('type'),
                value:(typeof el.value==='string' ? el.value : el.textContent||'').slice(0,500)}))),
            messages:await page.locator('p,label,[role=alert],[role=status],[aria-live]').evaluateAll(elements=>
              elements.filter(el=>el.innerText?.trim() && el.getClientRects().length &&
                !Array.from((function*(node){for(;node;node=node.parentElement)yield node})(el)).some(node=>{
                  const style=getComputedStyle(node);return node.hidden || style.visibility==='hidden' || style.display==='none' || Number(style.opacity)===0;
                })).slice(0,10).map(el=>({tag:el.tagName.toLowerCase(),id:el.id,
                  selector:el.id ? '#'+CSS.escape(el.id) : el.tagName.toLowerCase()+Array.from(el.classList).slice(0,3).map(name=>'.'+CSS.escape(name)).join(''),
                  text:el.innerText.trim().slice(0,500),visible:true}))),
            buttons:await page.locator('button,input[type=button],input[type=submit]').evaluateAll(elements=>
              elements.slice(0,10).map(el=>({id:el.id,text:(el.textContent||el.value||'').trim().slice(0,200),
                disabled:el.matches(':disabled') || el.getAttribute('aria-disabled')==='true'})))};
}
async function observedFillControl(locator) {
  return locator.evaluate(el=>{
    const id=el.id||'',name=el.name||el.getAttribute('name')||'',type=el.type||'';
    const sensitive=type==='password'||/password|secret|token|api[-_ ]?key|credential|authorization|auth/i.test(id+' '+name);
    return {id,name,tag:el.tagName.toLowerCase(),type,
      ...(sensitive?{sensitive:true}:{value:(typeof el.value==='string'?el.value:el.textContent||'').slice(0,500)})};
  });
}
process.on('SIGTERM',async()=>{try{if(browser)await browser.close()}finally{process.exit(143)}});
(async () => {
  const passed_ids = [], failures = [], browser_observations = [], resource_warnings = [];
  let test_runs = [], browser_checks = 0;
  if(contract.requires_tests) {
    try {await runTests()}
    catch(e) {failures.push({ids:['REQUIRED_TESTS'],error:String(e.stack||e).slice(-8000)})}
  }
  let baseURL;
  if (contract.checks.some(c=>c.kind==='BROWSER')) {
    server = http.createServer((req,res) => {
      try {
        const relative = decodeURIComponent(new URL(req.url, 'http://localhost').pathname).replace(/^\/+/, '') || contract.entrypoint;
        if(relative==='favicon.ico'&&!fs.existsSync(path.join(workspace,relative))) {res.statusCode=204;res.end();return}
        const file = inRoot(contract._olcr_vite_preview ? path.join(contract._olcr_vite_preview,relative) : relative);
        res.setHeader('Content-Type', ({'.html':'text/html; charset=utf-8','.htm':'text/html; charset=utf-8',
          '.js':'text/javascript; charset=utf-8','.mjs':'text/javascript; charset=utf-8',
          '.css':'text/css; charset=utf-8','.json':'application/json; charset=utf-8',
          '.svg':'image/svg+xml','.wasm':'application/wasm'})[path.extname(file).toLowerCase()] || 'application/octet-stream');
        res.end(fs.readFileSync(file));
      } catch {res.statusCode=404;res.end();}
    });
    await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve)});
    baseURL = 'http://127.0.0.1:'+server.address().port+'/';
    const manifest = JSON.parse(fs.readFileSync(path.join(resources,'runtime-manifest.json'),'utf8'));
    // Current installations keep Chromium in its own optional component.
    // Python resolves that component; old immutable bundles retain a relative
    // browser path in their Node manifest.
    const executablePath = browserExecutable || (manifest.browser?.path && path.join(resources,manifest.browser.path));
    if(!executablePath || !fs.existsSync(executablePath))throw Error('VERIFICATION_BROWSER_RUNTIME_UNAVAILABLE');
    const {chromium} = require(path.join(resources,'node_modules/playwright'));
    browser = await chromium.launch({headless:true,executablePath});
  }
  for (const check of contract.checks) {
    let assertions=0, page;
    const errors=[],browserErrors=[],externalRequests=[],layoutObservations=[];
    const assert = new Proxy(strict,{apply(target,receiver,args){assertions++;return Reflect.apply(target,receiver,args)},get(target,key){const value=target[key];return typeof value==='function' ? (...args)=>{assertions++;return value(...args)} : value}});
    const trackMatchers = matchers => new Proxy(matchers,{get(target,key){
      const value=Reflect.get(target,key);
      if(typeof value==='function')return (...args)=>{assertions++;return Reflect.apply(value,target,args)};
      return value && typeof value==='object' ? trackMatchers(value) : value;
    }});
    const expect = actual => trackMatchers(playwrightExpect(actual));
    const browserCalls=[];
    let exclusionWitness=null;
    try {
      if(check.kind==='BROWSER') {
        const mobile=check.ids.some(id=>(contract.mobile_criteria||[]).includes(id));
        page=await browser.newPage(mobile ? {viewport:{width:375,height:812}} : {});
        page.setDefaultTimeout(4000);
        const sourceLocation=(reference,line,column)=>{
          try {
            const url=new URL(reference);
            if(url.origin!==new URL(baseURL).origin)return null;
            const relative=decodeURIComponent(url.pathname).replace(/^\/+/, '')||contract.entrypoint;
            const lines=fs.readFileSync(inRoot(relative),'utf8').split(/\r?\n/);
            return {path:relative,line,column,source_line:(lines[line-1]||'').slice(0,800),
              context:lines.slice(Math.max(0,line-3),line+2).map((source,index)=>({line:Math.max(1,line-2)+index,source:source.slice(0,500)}))};
          } catch {return null}
        };
        const recordSourceError=(message,stack,locations)=>{
          message=message.replace(/^Uncaught\s+/,'').replace(/^[A-Za-z]*Error:\s*/,'');
          const previous=browserErrors.find(item=>item.message===message);
          if(previous) {
            for(const location of locations)if(previous.source_locations.length<3&&!previous.source_locations.some(item=>item.path===location.path&&item.line===location.line))previous.source_locations.push(location);
            previous.source_paths=[...new Set(previous.source_locations.map(item=>item.path))];
          } else browserErrors.push({message,source_paths:[...new Set(locations.map(item=>item.path))],source_locations:locations.slice(0,3),stack:stack.slice(0,2000)});
          if(!errors.includes(message))errors.push(message);
        };
        page.on('pageerror',e=>{
          const stack=String(e.stack||e),source_paths=[],source_locations=[];
          for(const reference of stack.match(/https?:\/\/[^\s)]+:\d+:\d+/g)||[]) {
            if(!reference.startsWith(baseURL))continue;
            try {
              const frame=reference.match(/^(.*):(\d+):(\d+)$/);
              const url=new URL(frame[1]);
              const relative=decodeURIComponent(url.pathname).replace(/^\/+/, '')||contract.entrypoint;
              const file=inRoot(relative);
              if(fs.statSync(file).isFile()) {
                source_paths.push(relative);
                const line=Number(frame[2]),column=Number(frame[3]);
                const lines=fs.readFileSync(file,'utf8').split(/\r?\n/);
                source_locations.push({path:relative,line,column,source_line:(lines[line-1]||'').slice(0,800),
                  context:lines.slice(Math.max(0,line-3),line+2).map((source,index)=>({
                    line:Math.max(1,line-2)+index,source:source.slice(0,500)}))});
              }
            } catch { /* A virtual/evaluated frame is not an application file. */ }
          }
          recordSourceError(e.message,stack,source_locations);
        });
        // Chromium omits a JS stack URL for parse errors. Its debugger
        // protocol still identifies the loaded script and exact source line.
        // Only same-origin application files grant source-repair authority;
        // anonymous verifier evaluate() scripts remain check-owned.
        const debuggerSession=await page.context().newCDPSession(page),scriptURLs=new Map();
        for(const event of ['Debugger.scriptParsed','Debugger.scriptFailedToParse'])debuggerSession.on(event,script=>scriptURLs.set(script.scriptId,{url:script.url,hasSourceURL:script.hasSourceURL}));
        debuggerSession.on('Runtime.exceptionThrown',({exceptionDetails:details})=>{
          const script=scriptURLs.get(details.scriptId);
          if(!script||script.hasSourceURL)return;
          const url=details.url||script.url;
          const location=url&&sourceLocation(url,Number(details.lineNumber)+1,Number(details.columnNumber)+1);
          if(!location)return;
          const description=String(details.exception?.description||details.text||'Browser exception');
          recordSourceError(description.split('\n')[0],description,[location]);
        });
        await debuggerSession.send('Debugger.enable');
        await debuggerSession.send('Runtime.enable');
        page.on('console',message=>{
          if(message.type()!=='error')return;
          // Offline verification deliberately aborts remote resources. A
          // blocked optional font/style is not a JavaScript source exception.
          try {
            const url=new URL(message.location().url);
            if(url.origin!==new URL(baseURL).origin && externalRequests.includes(url.origin)
                && /^Failed to load resource:/.test(message.text())) {
              if(resource_warnings.length<20)resource_warnings.push({ids:check.ids,origin:url.origin,reason:'blocked_external_resource'});
              return;
            }
          } catch { /* Unattributed console errors remain failures. */ }
          errors.push(message.text());
        });
        // Native-model checks use this singular spelling. It still observes
        // the real DOM through Playwright's canonical implementation.
        const locatorPrototype=Object.getPrototypeOf(page.locator('body'));
        if(!locatorPrototype.allTextContent)locatorPrototype.allTextContent=function(){return this.allTextContents()};
        // A model sometimes uses this structural predicate for an empty
        // list. Count actual DOM matches; attachment does not imply visibility.
        if(!locatorPrototype.isAttached)locatorPrototype.isAttached=async function(){return (await this.count())>0};
        const evaluate=page.evaluate.bind(page);
        page.__olcrStorageFaultEvaluate=evaluate;
        page.evaluate=(callback,arg)=>evaluate(observationCallback(callback),arg);
        for(const name of ['$eval','$$eval']) {
          const evaluateSelector=page[name].bind(page);
          page[name]=(selector,callback,arg)=>evaluateSelector(selector,observationCallback(callback),arg);
        }
        if(!locatorPrototype.__olcrObservationGuard) {
          for(const name of ['evaluate','evaluateAll']) {
            const evaluateLocator=locatorPrototype[name];
            locatorPrototype[name]=function(callback,arg,options){return evaluateLocator.call(this,observationCallback(callback),arg,options)};
          }
          Object.defineProperty(locatorPrototype,'__olcrObservationGuard',{value:true});
        }
        await page.route('**/*',route=>{
          const url=new URL(route.request().url());
          if(url.origin===new URL(baseURL).origin)return route.continue();
          if(['http:','https:','ws:','wss:'].includes(url.protocol)) {
            externalRequests.push(url.origin); // Never record query strings or credentials.
            if(contract.forbid_external_requests)errors.push('External network request is forbidden: '+url.origin);
          }
          return route.abort();
        });
        // WebSocket handshakes do not go through HTTP request routing.
        await page.routeWebSocket('**/*',socket=>{
          const url=new URL(socket.url());
          if(url.host===new URL(baseURL).host){socket.connectToServer();return}
          externalRequests.push(url.origin);
          if(contract.forbid_external_requests)errors.push('External network request is forbidden: '+url.origin);
          return socket.close({code:1008,reason:'External connections are blocked during verification'});
        });
        await page.goto(new URL(contract.entrypoint,baseURL).href);
        if(browserErrors.length)throw Error('Browser page errors: '+errors.join('; '));
      }
      // Only the disposable check page can receive this host-owned fault.
      // Restore the original descriptor rather than mutating application data.
      const setStorageFailure=async enabled=>{
        if(!page || typeof enabled!=='boolean')throw new Error('storage_failure requires a browser and boolean');
        await page.context().addInitScript(enabled=>{
          const key=Symbol.for('olcr.storageWriteDescriptor');
          if(!Storage.prototype[key])Object.defineProperty(Storage.prototype,key,{value:Object.getOwnPropertyDescriptor(Storage.prototype,'setItem')});
          Object.defineProperty(Storage.prototype,'setItem',enabled
            ? {configurable:true,writable:true,value:function(){throw new DOMException('OLCR simulated storage failure','QuotaExceededError')}}
            : Storage.prototype[key]);
        },enabled);
        await page.__olcrStorageFaultEvaluate(enabled=>{
          const key=Symbol.for('olcr.storageWriteDescriptor');
          if(!Storage.prototype[key])Object.defineProperty(Storage.prototype,key,{value:Object.getOwnPropertyDescriptor(Storage.prototype,'setItem')});
          Object.defineProperty(Storage.prototype,'setItem',enabled
            ? {configurable:true,writable:true,value:function(){throw new DOMException('OLCR simulated storage failure','QuotaExceededError')}}
            : Storage.prototype[key]);
        },enabled);
      };
      const assertResponsiveLayout=async(viewport,stylesheet=null)=>{
        if(!page || ![375,1280].includes(viewport?.width))throw Error('Responsive layout requires a real browser and a supported viewport');
        await page.setViewportSize(viewport);
        const selector='button,input:not([type=hidden]),select,textarea,a[href],[role=button]';
        const facts=await page.evaluate(({selector,stylesheet})=>{
          const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return !el.closest('[hidden],[aria-hidden="true"]')&&s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0};
          const dialog=[...document.querySelectorAll('dialog,[role=dialog],[aria-modal=true]')].filter(visible).at(-1);
          const controls=[...document.querySelectorAll(selector)].map((el,index)=>({el,index}))
            .filter(({el})=>visible(el)&&!el.matches(':disabled')&&!el.closest('[aria-disabled="true"]')&&(!dialog||dialog.contains(el))).slice(0,4)
            .map(({el,index})=>({index,tag:el.tagName.toLowerCase(),id:el.id||'',text:(el.textContent||'').trim().slice(0,80)}));
          const loaded=[...document.styleSheets].filter(sheet=>{try{return sheet.href&&decodeURIComponent(new URL(sheet.href).pathname).replace(/^\/+/, '')===stylesheet?.replace(/^\.\//,'')&&sheet.cssRules.length>0}catch{return false}}).length>0;
          return {viewport:{width:innerWidth,height:innerHeight},document_width:document.documentElement.scrollWidth,
            content_visible:controls.length>0||[...document.querySelectorAll('h1,h2,main,canvas,svg')].some(visible),
            stylesheet,stylesheet_loaded:loaded,controls};
        },{selector,stylesheet});
        layoutObservations.push(facts);
        assert.equal(facts.viewport.width,viewport.width,'Viewport width was not applied');
        assert.ok(facts.document_width<=viewport.width+1,'The rendered document overflows the viewport horizontally');
        assert.ok(facts.content_visible,'No rendered application content was observed');
        if(stylesheet)assert.ok(facts.stylesheet_loaded,'The requested stylesheet was not loaded and parsed by the browser: '+stylesheet);
        for(const control of facts.controls) {
          const target=page.locator(selector).nth(control.index);
          await target.scrollIntoViewIfNeeded();
          const box=await target.boundingBox();control.box=box;
          assert.ok(box&&box.x>=-1&&box.x+box.width<=viewport.width+1,'A visible control extends outside the viewport: '+(control.id||control.tag));
          try {await target.click({trial:true,timeout:1000});control.actionable=true}
          catch(error){control.actionable=false;throw Error('A visible application control is obstructed or unusable: '+(control.id||control.tag)+'\n'+error.message)}
        }
      };
      const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
      await new AsyncFunction('assert','__olcr_context','{\nconst {workspace,loadModule,readFile,runTests,page,baseURL,require,expect,projectBuild,setStorageFailure,assertResponsiveLayout}=__olcr_context;\n'+check.code+'\n}')(assert,
        {workspace,loadModule,readFile,runTests,setStorageFailure,assertResponsiveLayout,page:observedBrowserAPI(page,browserCalls),baseURL,expect,projectBuild,require:specifier=>readonlyRequire(specifier,assert),externalRequestCount:()=>externalRequests.length});
      if(browserCalls.__eventErrors?.length)throw new TypeError('Verification contract dialog handler failed: '+String(browserCalls.__eventErrors[0]));
      if(browserCalls.some(call=>!call.observed))throw Error('Unawaited browser API result; await each observation/action before asserting');
      exclusionWitness=await selectionExclusionWitness(page,browserCalls,contract.selection_exclusion,check.ids);
      if(exclusionWitness)throw Error('Observed consecutive equal selections with another eligible choice');
      if(!assertions) throw Error('Check executed no observed assertions. Use the supplied assert/expect helpers; importing or redeclaring assert can bypass observation.');
      if(errors.length) throw Error('Browser page errors: '+errors.join('; '));
      if(page && contract.collect_observations)browser_observations.push({ids:check.ids,passed:true,
        layout_observations:layoutObservations,
        observed_page:await observedPageState(page),
        reads:browserCalls.filter(call=>call.observed && call.finished && call.text!==undefined)
          .slice(-16).map(call=>({selector:call.selector,text:call.text}))});
      passed_ids.push(...check.ids);
      if(check.kind==='BROWSER')browser_checks++;
    } catch(thrown) {
      const e=thrown instanceof Error ? thrown : new Error(String(thrown));
      if(!exclusionWitness && e.code==='ERR_ASSERTION')
        exclusionWitness=await selectionExclusionWitness(page,browserCalls,contract.selection_exclusion,check.ids);
      const stack=String(e.stack||e);
      const projectFrame=stack.split('\n').some(line=>line.includes(workspace+'/') && /:\d+(?::\d+)?(?:\)|\s|$)/.test(line));
      const failure={ids:check.ids,kind:check.kind,check_source:check.code.slice(0,1600),error:stack.slice(-8000),
        contract_error:(!projectFrame && ['ReferenceError','SyntaxError','TypeError'].includes(e.name))
          || /no observed assertions|Readonly verification|outside workspace/.test(String(e.message||e))};
      if(externalRequests.length)failure.external_requests=[...new Set(externalRequests)].slice(0,10);
      const unawaited=browserCalls.filter(call=>!call.observed).map(call=>call.api);
      if(unawaited.length) {
        failure.contract_error=true;
        failure.unawaited_api_calls=[...new Set(unawaited)].slice(0,16);
        failure.error='Verification code did not await browser API results: '+failure.unawaited_api_calls.join(', ')+'\n'+failure.error;
      }
      if(exclusionWitness) {
        failure.observed_no_repeat=exclusionWitness;
        failure.error='OLCR observed consecutive equal selections after separate clicks: '+JSON.stringify(exclusionWitness)+'\n'+failure.error;
      }
      if(browserErrors.length) {
        failure.browser_errors=browserErrors.slice(0,5);
        failure.project_source_error=browserErrors.some(item=>item.source_paths.length>0);
        failure.error=('Browser source errors: '+browserErrors.map(item=>item.message).join('; ')).slice(0,2000)+'\n'+failure.error.slice(-5000);
      }
      if(e.code==='ERR_ASSERTION') {
        const inspect=require('node:util').inspect;
        const options={depth:3,maxArrayLength:15,maxStringLength:500};
        failure.assertion={actual:inspect(e.actual,options),expected:inspect(e.expected,options),operator:e.operator};
        if(typeof e.actual==='string' && typeof e.expected==='string' && e.actual.length<=500 && e.expected.length<=500) {
          failure.assertion.raw_actual=e.actual;
          failure.assertion.raw_expected=e.expected;
        }
        const location=stack.match(/<anonymous>:(\d+):(\d+)/);
        if(location) {
          // AsyncFunction adds two header lines; our context adds two more.
          const line=Number(location[1])-5,column=Number(location[2])-1;
          const sourceLine=check.code.split('\n')[line];
          if(sourceLine!==undefined)failure.assertion_source={line:line+1,column:column+1,
            snippet:sourceLine.slice(Math.max(0,column-100),column+300)};
        }
      }
      if(page) {
        failure.layout_observations=layoutObservations;
        failure.input_actions=browserCalls.filter(call=>call.observed && call.requested_input!==undefined)
          .map(call=>({value:call.requested_input,selector:call.selector})).slice(0,5);
        failure.browser_actions=browserCalls.filter(call=>call.observed && call.action)
          .map(call=>({action:call.action,selector:call.selector,
            outcome:call.rejected?'rejected':call.finished?'completed':'pending',
            ...(call.observed_state_changed!==undefined?{observed_state_changed:call.observed_state_changed,
              state_before:call.state_before,state_after:call.state_after}:{}),
            ...(['field_id','field_name','field_type','requested_value','previous_value','observed_value']
              .reduce((result,name)=>{
                if(call[name]!==undefined)result[name]=call[name];
                return result;
              },{})),
            ...(call.input_summary?{input_summary:call.input_summary}:{})})).slice(-12);
        failure.reads=browserCalls.filter(call=>call.observed && call.finished && call.text!==undefined)
          .map(call=>({type:call.read_type,selector:call.selector,text:call.text})).slice(-8);
        try {
          failure.observed_page=await observedPageState(page);
        } catch { /* Keep the original failure if the page itself has crashed. */ }
      }
      failures.push(failure);
    }
    finally {if(page)await page.close()}
  }
  if(lastTestRun)test_runs.push(lastTestRun);
  process.stdout.write('OLCR_VERIFICATION_RESULT='+JSON.stringify({passed_ids,failures,test_runs,browser_checks,browser_observations,resource_warnings})+'\n');
  process.exitCode=failures.length ? 1 : 0;
})().catch(e=>{
  const error=String(e.stack||e);
  process.stdout.write('OLCR_VERIFICATION_RESULT='+JSON.stringify({passed_ids:[],test_runs:[],browser_checks:0,
    executor_error:true,failures:[{ids:[],error,executor_error:true,contract_error:true}]})+'\n');
  process.stderr.write(error+'\n');process.exitCode=1;
})
  .finally(async()=>{if(browser)await browser.close();if(server)await new Promise(resolve=>server.close(resolve))});
