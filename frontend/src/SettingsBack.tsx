import {PageBack} from './InterfacePrimitives';
export function SettingsBack({onBack}:{onBack:()=>void}){return <PageBack onBack={onBack} destination="settings"/>}
