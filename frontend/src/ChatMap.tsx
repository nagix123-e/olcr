import {useEffect,useRef,useState} from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import './chat-map.css';
import {api} from './api';
import {useLanguage} from './language';
import {UiIcon} from './UiIcon';
import {ChatMap as MapData,MapPoint,freePin,pinnedPoints} from './chatMapData';

function worldFitZoom(width:number,height:number){
 return Math.max(0,Math.floor(Math.min(
  Math.log2(Math.max(256,width)/256),
  Math.log2(Math.max(256,height)/256),
 )));
}

export function ChatMap({value,onChange,onClose,disabled=false}:{value:MapData;onChange?:(v:MapData)=>void;onClose?:()=>void;disabled?:boolean}){
 const {language}=useLanguage(),ja=language==='ja';
 const [tileStatus,setTileStatus]=useState<'loading'|'ready'|'error'>('loading');
 const tiles=useRef<L.TileLayer|null>(null);
 const [full,setFull]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
 const container=useRef<HTMLDivElement>(null),panel=useRef<HTMLElement>(null),toggle=useRef<HTMLButtonElement>(null),map=useRef<L.Map|null>(null),pins=useRef<L.LayerGroup|null>(null);
 const pointsKey=JSON.stringify(value.points);
 const coordinatesKey=JSON.stringify(value.points.map(({lat,lon})=>[lat,lon]));
 const markers=useRef<Map<string,L.Marker>>(new Map()),autoWorldFit=useRef(true),adjustingWorld=useRef(false),lastCoordinates=useRef<string|null>(null);
 const generation=useRef(0),requests=useRef(new Map<string,number>()),latest=useRef({value,onChange,disabled});latest.current={value,onChange,disabled};
 const removePin=(pin:string)=>{
  const current=latest.current;if(!current.onChange||current.disabled)return;
  requests.current.delete(pin);setBusy(requests.current.size>0);setError('');
  const next={...current.value,points:pinnedPoints(current.value.points).filter(point=>point.pin!==pin)};
  latest.current={...current,value:next};current.onChange(next);
 };
 const removeRef=useRef(removePin);removeRef.current=removePin;
 const select=async(lat:number,lon:number)=>{
   const current=latest.current,pin=freePin(current.value.points);if(!current.onChange||current.disabled||!pin)return;
   const epoch=++generation.current;requests.current.set(pin,epoch);setBusy(true);setError('');
   const name=`${lat.toFixed(4)}, ${lon.toFixed(4)}`;
   const next={...current.value,points:[...pinnedPoints(current.value.points),{lat,lon,name,pin}].sort((a,b)=>a.pin!.localeCompare(b.pin!))};
   latest.current={...current,value:next};current.onChange(next);
   try{const place=await api<{name:string}>(`/maps/reverse?lat=${lat}&lon=${lon}`);if(requests.current.get(pin)!==epoch)return;
    const value=latest.current.value;const points=pinnedPoints(value.points).map(point=>point.pin===pin&&point.name===name?{...point,name:place.name}:point);
    const updated={...value,points};latest.current={...latest.current,value:updated};latest.current.onChange?.(updated);
   }catch{if(requests.current.get(pin)===epoch)setError(ja?'地名を取得できません。ピンの地名を入力してください。':'Place lookup failed. Enter a place name for the pin.');}
   finally{if(requests.current.get(pin)===epoch){requests.current.delete(pin);setBusy(requests.current.size>0)}}
 };
 const selectRef=useRef(select);selectRef.current=select;
 useEffect(()=>{
  if(!container.current)return;
  const reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const instance=L.map(container.current,{
   worldCopyJump:true,minZoom:0,zoomAnimation:!reducedMotion,fadeAnimation:!reducedMotion,
   markerZoomAnimation:!reducedMotion,zoomSnap:0,zoomDelta:0.5,
   wheelDebounceTime:40,wheelPxPerZoomLevel:100,inertia:true,
  });map.current=instance;instance.setView([0,0],worldFitZoom(instance.getSize().x,instance.getSize().y),{animate:false});
  lastCoordinates.current=null;
  let tileErrors=0;
  const tileLayer=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,keepBuffer:3,updateWhenIdle:false,noWrap:true,attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'});
  tiles.current=tileLayer;
  tileLayer.on('loading',()=>{tileErrors=0;setTileStatus('loading')});
  tileLayer.on('tileerror',()=>{tileErrors++});
  tileLayer.on('load',()=>setTileStatus(tileErrors?'error':'ready'));
  tileLayer.addTo(instance);
  pins.current=L.layerGroup().addTo(instance);
  instance.on('click',(event:L.LeafletMouseEvent)=>void selectRef.current(event.latlng.lat,((event.latlng.lng+180)%360+360)%360-180));
  // Coalesce layout changes into one map update per frame. Keep the same map
  // and geographic center when switching between inline and fullscreen.
  let resizeFrame=0;
  instance.on('zoomstart dragstart',()=>{if(!adjustingWorld.current)autoWorldFit.current=false});
  const observer=new ResizeObserver(()=>{
   cancelAnimationFrame(resizeFrame);
   resizeFrame=requestAnimationFrame(()=>{
    instance.invalidateSize({pan:true,animate:false,debounceMoveend:true});
    // Keep the world map's left and right edges aligned with the canvas.
    if(!markers.current.size&&autoWorldFit.current){
     const size=instance.getSize(),zoom=worldFitZoom(size.x,size.y);
     if(Math.abs(instance.getZoom()-zoom)>0.001){
      adjustingWorld.current=true;instance.setZoom(zoom,{animate:false});adjustingWorld.current=false;
     }
    }
   });
  });observer.observe(container.current);
  return()=>{
   generation.current++;observer.disconnect();cancelAnimationFrame(resizeFrame);
   tiles.current=null;instance.removeLayer(tileLayer);tileLayer.off();if(pins.current)instance.removeLayer(pins.current);instance.stop();
   // Leaflet 1.9 leaves a WebKit zoom completion timer alive on remove().
   // Clear its flag before removing panes so a late callback becomes a no-op.
   (instance as L.Map & {_animatingZoom?:boolean})._animatingZoom=false;
   instance.remove();instance.off();markers.current.clear();requests.current.clear();pins.current=null;map.current=null;
  };
 },[]);
 useEffect(()=>{
  const layer=pins.current;if(!layer)return;
  const points=pinnedPoints(value.points);
  for(const [pin,marker] of markers.current)if(!points.some(point=>point.pin===pin)){layer.removeLayer(marker);markers.current.delete(pin)}
  points.forEach((point,index)=>{
   const letter=point.pin!,title=`${letter}: ${point.name}`;
   let marker=markers.current.get(letter);
   if(!marker){
    marker=L.marker([point.lat,point.lon],{icon:L.divIcon({
     className:`chat-map-pin chat-map-pin-${letter.toLowerCase()}`,
     html:`<svg viewBox="0 0 40 50" width="40" height="50" aria-hidden="true"><path d="M20 49C17 43 3 30 3 18a17 17 0 1 1 34 0c0 12-14 25-17 31Z"/><circle cx="20" cy="18" r="10"/><text x="20" y="18" dy=".35em">${letter}</text></svg>`,
     iconSize:[40,50],iconAnchor:[20,50],tooltipAnchor:[0,-38],
    }),keyboard:true,title,riseOnHover:true,bubblingMouseEvents:false}).bindTooltip('').addTo(layer);
    marker.on('dblclick',(event:L.LeafletMouseEvent)=>{if(event.originalEvent)L.DomEvent.stop(event.originalEvent);removeRef.current(letter)});
    markers.current.set(letter,marker);
   }
   marker.setLatLng([point.lat,point.lon]);
   const label=document.createElement('span');label.textContent=point.name;marker.setTooltipContent(label);
   const element=marker.getElement();if(element){element.title=title;element.setAttribute('aria-label',title)}
  });
 },[pointsKey]);
 useEffect(()=>{
  const instance=map.current;if(!instance)return;
  const initial=lastCoordinates.current===null;lastCoordinates.current=coordinatesKey;
  const points=JSON.parse(coordinatesKey) as [number,number][];
  if(!points.length){autoWorldFit.current=true;const size=instance.getSize();instance.setView([0,0],worldFitZoom(size.x,size.y),{animate:false});return}
  autoWorldFit.current=false;
  // Initial saved maps frame their pins. Picking a visible point or editing
  // its name must not steal the user's current zoom or interrupt a gesture.
  if(initial||points.some(point=>!instance.getBounds().contains(point))){
   instance.fitBounds(L.latLngBounds(points),{padding:[48,56],maxZoom:12,animate:false});
  }
 },[coordinatesKey]);
 useEffect(()=>{if(!full)return;const overflow=document.body.style.overflow;document.body.style.overflow='hidden';toggle.current?.focus();return()=>{document.body.style.overflow=overflow;toggle.current?.focus()}},[full]);
 const [manual,setManual]=useState({lat:'',lon:'',name:''});
 const addManual=()=>{const pin=freePin(value.points);if(!pin)return;const point:MapPoint={lat:Number(manual.lat),lon:Number(manual.lon),name:manual.name.trim(),pin};if(!manual.lat.trim()||!manual.lon.trim()||!Number.isFinite(point.lat)||Math.abs(point.lat)>90||!Number.isFinite(point.lon)||Math.abs(point.lon)>180||!point.name)return;onChange?.({...value,points:[...pinnedPoints(value.points),point].sort((a,b)=>a.pin!.localeCompare(b.pin!))});setError('')};
 return <section ref={panel} className={`chat-map${full?' is-fullscreen':''}`} role={full?'dialog':undefined} aria-modal={full||undefined} aria-label={ja?'地点マップ':'Location map'} onKeyDown={event=>{
  if(event.key==='Escape'&&full){event.stopPropagation();setFull(false)}
  if(event.key==='Tab'&&full){const nodes=Array.from(panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),[tabindex="0"],a[href]')||[]);const first=nodes[0],last=nodes.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus()}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus()}}
 }}>
 <header><strong>{ja?'地点マップ':'Location map'}</strong><div><button ref={toggle} type="button" aria-label={full?(ja?'通常表示に戻す':'Return to inline view'):(ja?'地図を全画面表示':'Expand map')} onClick={()=>setFull(v=>!v)}><UiIcon name={full?'collapse':'expand'}/></button>{onClose&&<button type="button" aria-label={ja?'地図を閉じる':'Close map'} onClick={onClose}><UiIcon name="close"/></button>}</div></header>
 <div ref={container} className="chat-map-canvas"/>
 {tileStatus==='loading'&&<p className="chat-map-tile-status" role="status">{ja?'地図を読み込み中…':'Loading map…'}</p>}
 {tileStatus==='error'&&<div className="chat-map-tile-status is-error" role="alert"><span>{ja?'地図画像を読み込めませんでした。通信を確認して再読み込みしてください。':'Map images could not load. Check your connection and retry.'}</span><button type="button" onClick={()=>tiles.current?.redraw()}>{ja?'地図を再読み込み':'Reload map'}</button></div>}
 <div className="chat-map-controls">{onChange&&<><p>{ja?'クリックでA→Bの順に配置。ピンをダブルクリックで解除できます。':'Click to place A, then B. Double-click a pin to remove it.'}</p></>}
 {value.points.map((point,index)=><div className="chat-map-location" key={index}><b>{point.pin||String.fromCharCode(65+index)}</b>{onChange?<input aria-label={ja?`地点${index+1}の地名`:`Place ${index+1} name`} maxLength={300} disabled={disabled} value={point.name} onChange={e=>{generation.current++;setBusy(false);onChange({...value,points:value.points.map((p,i)=>i===index?{...p,name:e.target.value}:p)})}}/>:<span>{point.name}</span>}<small>{point.lat.toFixed(4)}, {point.lon.toFixed(4)}</small>{onChange&&<button type="button" disabled={disabled} aria-label={ja?`地点${index+1}を削除`:`Remove place ${index+1}`} onClick={()=>removePin(point.pin||String.fromCharCode(65+index))}>×</button>}</div>)}
 {onChange&&<details><summary>{ja?'座標と地名を入力':'Enter coordinates and place name'}</summary><div className="chat-map-manual">{(['lat','lon','name'] as const).map(key=><input key={key} aria-label={key==='lat'?'Latitude':key==='lon'?'Longitude':ja?'地名':'Place name'} placeholder={key==='lat'?'緯度':key==='lon'?'経度':'地名'} value={manual[key]} disabled={disabled} maxLength={key==='name'?300:20} onChange={e=>setManual({...manual,[key]:e.target.value})}/>)}<button type="button" disabled={disabled||!manual.name.trim()||!freePin(value.points)} onClick={addManual}>{ja?'ピンを設定':'Set pin'}</button></div></details>}
 {busy&&<p role="status">{ja?'地名を確認中…':'Finding place…'}</p>}{error&&<p role="alert">{error}</p>}
 </div></section>
}
