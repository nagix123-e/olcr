import React from 'react';
import {createRoot} from 'react-dom/client';
import {CodingTaskReview} from '../../src/CodingTaskReview';
import {LanguageProvider} from '../../src/language';
import '../../src/styles.css';
import '../../src/features.css';

const review={available:true,revision:'fixture-review-1',workspace:{mode:'worktree',root:'/isolated/task',source_root:'/example/project',branch:'olcr/task-navbar',snapshot_complete:true},
 files:[{path:'src/components/Navbar.tsx',status:'MODIFIED',revertible:true,reviewable:true,phase_ids:['p1'],diff:'--- a/src/components/Navbar.tsx\n+++ b/src/components/Navbar.tsx\n@@ -1,3 +1,5 @@\n export function Navbar() {\n-  return <nav>Home</nav>;\n+  return <nav aria-label="Main">\n+    <a href="/">Home</a>\n+  </nav>;\n }\n'},
 {path:'src/components/Navbar.test.tsx',status:'ADDED',revertible:true,reviewable:true,phase_ids:['p2'],diff:'--- a/src/components/Navbar.test.tsx\n+++ b/src/components/Navbar.test.tsx\n@@ -0,0 +1,3 @@\n+it("exposes the main navigation", () => {\n+  expect(screen.getByRole("navigation")).toBeVisible();\n+});\n'}],
 steering:[{id:'instruction',message:'既存の配色を維持してください。',status:'APPLIED',received_revision:0,applied_revision:1}],
 reports:[{id:'result',phase_id:'p1',structured_report:{status:'PASS',test_executed:['npm test -- Navbar'],test_pass:['Navbar tests passed'],build_pass:'PASS'}}]};

window.fetch=async(input,init)=>{
 const path=String(input);let value:unknown=review;
 if(path.includes('/git/operations'))value={available:true,operations:[]};
 else if(path.includes('/git/status'))value={workspace:'/isolated/task',branch:'olcr/task-navbar',head:'a'.repeat(40),files:[{path:'src/components/Navbar.tsx',status:' M'}],remotes:[{name:'origin',url:'https://github.com/example/project.git',github:true}],setup_command:'gh auth login',setup_url:'https://cli.github.com/manual/gh_auth_login'};
 else if(path.endsWith('/steer')&&init?.method==='POST'){const body=JSON.parse(String(init.body));review.steering.push({id:'new',message:body.message,status:'QUEUED',received_revision:1,applied_revision:null as unknown as number});value={steering:review.steering}}
 else if(init?.method==='POST')value={status:'FIXTURE_ONLY'};
 return new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
};
createRoot(document.getElementById('root')!).render(<LanguageProvider><main style={{maxWidth:1040,margin:'24px auto',padding:'0 24px'}}><small>TEST-ONLY BROWSER FIXTURE · 実ファイル・GitHubへの接続なし</small><h1>Taskの変更をレビュー</h1><CodingTaskReview task={{id:'fixture',status:'RESUMABLE',activity:'NONE',plan_revision:1,retry_count:0,max_retries:2}} conversationId="fixture-chat" ja/></main></LanguageProvider>);
