import {Feedback} from './InterfacePrimitives';
import {UiIcon} from './UiIcon';
import {useEffect,useState} from 'react';
import './skills-library.css';

type Skill={name:string;description?:string;version?:string;source?:string;scope?:string;state?:string;enabled?:boolean;trusted?:boolean;permission?:string;validation_state?:string;native?:boolean};
type CliSkill=Skill&{source_directory?:string};
type Inventory={installed:Skill[];cli:CliSkill[];locations?:{project?:string;global?:string|null}};

const statusLabel=(skill:Skill,ja:boolean)=>{
  if(skill.native)return ja?'利用可能':'Ready to use';
  if(skill.state==='ACTIVE'&&skill.enabled&&skill.trusted)return ja?'有効':'Enabled';
  if(skill.state==='ACTIVE'&&skill.enabled)return ja?'信頼確認が必要':'Trust review needed';
  if(skill.state==='ACTIVE'&&!skill.enabled)return ja?'無効':'Disabled';
  if(skill.state==='QUARANTINED')return ja?'隔離中':'Quarantined';
  if(skill.state==='AVAILABLE_FOR_QUARANTINE_INTAKE')return ja?'未登録・確認待ち':'Available for review';
  if(skill.state==='ACTIVE_SOURCE_MISSING')return ja?'ローカル内容が不足':'Local content missing';
  return skill.state|| (ja?'状態不明':'Unknown status');
};

export function SkillsLibrary({api,ja}:{api:<T=any>(path:string,init?:RequestInit)=>Promise<T>;ja:boolean}){
  const [inventory,setInventory]=useState<Inventory|null>(null),[error,setError]=useState(''),[loading,setLoading]=useState(true);
  const refresh=()=>{setLoading(true);setError('');void Promise.all([api<{installed:Skill[]}>('/skills'),api<{skills:CliSkill[]}>('/skills/cli')]).then(([registered,local])=>setInventory({installed:Array.isArray(registered.installed)?registered.installed.filter(skill=>!skill.native):[],cli:Array.isArray(local.skills)?local.skills:[]})).catch(e=>setError(e instanceof Error?e.message:String(e))).finally(()=>setLoading(false));};
  useEffect(()=>{refresh()},[]);
  const card=(skill:Skill,key:string,origin:string)=><article className="skill-library-card" key={key}><div className="skill-library-card-heading"><h3>{skill.name}</h3><span className="skill-library-status">{statusLabel(skill,ja)}</span></div><p className={skill.description?'':'skill-library-no-description'}>{skill.description|| (ja?'説明は登録されていません。':'No description is available.')}</p><dl><div><dt>{ja?'バージョン':'Version'}</dt><dd>{skill.version||'—'}</dd></div><div><dt>{ja?'場所':'Source'}</dt><dd>{origin}</dd></div>{skill.permission&&<div><dt>{ja?'権限':'Permission'}</dt><dd>{skill.permission==='local_installation'?(ja?'インストール済み':'Installed locally'):skill.permission}</dd></div>}</dl></article>;
  return <section className="skills-library" aria-label={ja?'スキル一覧':'Skills library'}>
    <div className="skills-library-heading"><div><h2>{ja?'スキル':'Skills'}</h2><p>{ja?'このPCで見つかったスキルと、取得できた説明です。':'Skills found on this PC and their available descriptions.'}</p></div><button type="button" onClick={refresh} disabled={loading}><span className="icon-label"><UiIcon name="refresh" size={16}/>{loading?(ja?'読み込み中…':'Loading…'):(ja?'更新':'Refresh')}</span></button></div>
    {loading&&!inventory&&<Feedback>{ja?'スキルを検索しています…':'Scanning for skills…'}</Feedback>}
    {error&&<Feedback kind="error" onRetry={refresh}>{ja?'スキル一覧を読み込めませんでした':'Could not load the skill list'}: {error}</Feedback>}
    {inventory&&<>
      <section className="skills-library-group"><h2>{ja?'OLCRに登録済み':'Registered with OLCR'} <span>{inventory.installed.length}</span></h2>{inventory.installed.length?<div className="skills-library-grid">{inventory.installed.map(skill=>card(skill,`olcr-${skill.name}`,`${skill.scope==='global'?(ja?'全体':'Global'):(ja?'プロジェクト':'Project')} · ${skill.source||'OLCR'}`))}</div>:<p className="skills-library-empty">{ja?'登録されたスキルはありません。':'No registered skills.'}</p>}</section>
      <section className="skills-library-group"><h2>{ja?'PC上のSkills CLI':'Skills CLI on this PC'} <span>{inventory.cli.length}</span></h2>{inventory.cli.length?<div className="skills-library-grid">{inventory.cli.map(skill=>card(skill,`cli-${skill.name}`,ja?'ローカルSkills CLI':'Local Skills CLI'))}</div>:<p className="skills-library-empty">{ja?'ローカルにSkills CLIスキルは見つかりませんでした。':'No local Skills CLI items were found.'}</p>}</section>
    </>}
  </section>;
}
