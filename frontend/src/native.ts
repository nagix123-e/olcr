type Native = {core:{invoke:<T>(command:string,args?:Record<string,unknown>)=>Promise<T>};event:{listen:<T>(name:string,cb:(event:{payload:T})=>void)=>Promise<()=>void>}};
export function native(){return (window as Window & {__TAURI__?:Native}).__TAURI__}
export async function openExternal(url:string){
 const bridge=native();if(!bridge) { window.open(url,'_blank','noopener,noreferrer'); return; }
 return bridge.core.invoke('open_external_url',{url});
}
export async function pickPath(directory=false){
 const bridge=native();if(!bridge)throw Error('Native file chooser requires OLCR desktop');
 return bridge.core.invoke<string|null>('plugin:dialog|open',{options:{directory,multiple:false}});
}
