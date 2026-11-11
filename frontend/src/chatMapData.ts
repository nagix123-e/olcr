export type MapPoint={lat:number;lon:number;name:string;pin?:'A'|'B'};
export type ChatMap={type:'location_map';schema_version:1;points:MapPoint[]};
export const emptyMap=():ChatMap=>({type:'location_map',schema_version:1,points:[]});
export function mapError(value:ChatMap,allowEmpty=false){return value?.type!=='location_map'||value.schema_version!==1||!Array.isArray(value.points)||value.points.length>(2)||(!allowEmpty&&!value.points.length)||value.points.some(p=>!p||!Number.isFinite(p.lat)||Math.abs(p.lat)>90||!Number.isFinite(p.lon)||Math.abs(p.lon)>180||!p.name?.trim()||p.name.length>300||(p.pin!=null&&!['A','B'].includes(p.pin)))||new Set(pinnedPoints(value.points).map(p=>p.pin)).size!==value.points.length?'MAP_LOCATIONS_INVALID':'';}

export function pinnedPoints(points:MapPoint[]):MapPoint[]{return points.map((point,index)=>({...point,pin:point.pin==='A'||point.pin==='B'?point.pin:(index===0?'A':'B')}));}
export function freePin(points:MapPoint[]):'A'|'B'|undefined{const used=pinnedPoints(points);return (['A','B'] as const).find(pin=>!used.some(point=>point.pin===pin));}
