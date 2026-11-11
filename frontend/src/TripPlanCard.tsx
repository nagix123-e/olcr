import './trip-plan.css';
export type TripPlanBlock={type:'trip_plan';status:'collecting'|'ready'|'paused';updated_at:number;brief:{origin:string;destination:string;start_date:string;end_date:string;travelers:number|null;budget:string;interests:string;preferences:string;pace:string}};
export function TripPlanCard({block,ja}:{block:TripPlanBlock;ja:boolean}){
 const b=block.brief;
 const fields:[string,string][]=[
  [ja?'出発地':'From',b.origin],[ja?'目的地':'Destination',b.destination],
  [ja?'日程':'Dates',[b.start_date,b.end_date].filter(Boolean).join(' – ')],
  [ja?'人数':'Travelers',b.travelers?String(b.travelers):''],[ja?'予算':'Budget',b.budget],
  [ja?'興味':'Interests',b.interests],[ja?'希望':'Preferences',b.preferences],[ja?'ペース':'Pace',b.pace]];
 return <aside className="trip-plan-card" aria-label={ja?'保存済みの旅行条件':'Saved travel brief'}>
  <details open={block.status==='collecting'}><summary>{ja?'旅行条件':'Travel brief'} · {block.status==='ready'?(ja?'旅程案作成済み':'Draft ready'):block.status==='paused'?(ja?'編集終了':'Paused'):(ja?'条件を入力中':'Collecting details')}</summary>
   <dl>{fields.filter(([,value])=>value).map(([label,value])=><div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
   <p>{ja?'変更は「旅程を修正して」、新しい旅行は「/trip new」と送ってください。':'Send “revise my itinerary” to change it, or “/trip new” to start another trip.'}</p>
  </details>
 </aside>;
}
