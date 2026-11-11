import {useId} from 'react';
import {SettingHelp} from './SettingHelp';
export type CredentialField = {id:string;label:string;input_type:'text'|'email'|'password'|'url';placeholder?:string;required?:boolean;help?:string};
/** Shared, private credential inputs for model, web-search and tool providers. */
export function CredentialFields({fields,values,onChange,disabled=false}:{fields:CredentialField[];values:Record<string,string>;onChange:(id:string,value:string)=>void;disabled?:boolean}) {
 const id=useId();
 return <div className="credential-fields">{fields.map(field=><div className="credential-field" key={field.id}><span className="setting-label-with-help"><label htmlFor={`${id}-${field.id}`}>{field.label}</label>{field.help&&<SettingHelp label={field.label}>{field.help}</SettingHelp>}</span><input id={`${id}-${field.id}`} type={field.input_type} autoComplete={field.input_type==='password'?'new-password':'off'} spellCheck={false} disabled={disabled} required={field.required!==false} placeholder={field.placeholder} value={values[field.id]||''} aria-label={field.label} onChange={e=>onChange(field.id,e.target.value)}/></div>)}</div>;
}
