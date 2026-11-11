import {act,cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import L from 'leaflet';
import {api} from './api';
import {ChatMap} from './ChatMap';
import {emptyMap} from './chatMapData';

vi.mock('./api',()=>({api:vi.fn().mockResolvedValue({name:'Selected place'})}));
vi.mock('./language',()=>({useLanguage:()=>({language:'ja'})}));

let observers:ResizeObserverCallback[]=[];
const maps:L.Map[]=[];let viewportWidth=640,viewportHeight=300;
beforeEach(()=>{
 vi.useFakeTimers();observers.length=0;maps.length=0;viewportWidth=640;viewportHeight=300;
 vi.stubGlobal('matchMedia',vi.fn(()=>({matches:false})));
 vi.stubGlobal('ResizeObserver',class {
  constructor(callback:ResizeObserverCallback){observers.push(callback)}
  observe(){} disconnect(){}
 });
 vi.spyOn(HTMLElement.prototype,'clientWidth','get').mockImplementation(function(this:HTMLElement){return this.classList?.contains('chat-map-canvas')?viewportWidth:0});
 vi.spyOn(HTMLElement.prototype,'clientHeight','get').mockImplementation(function(this:HTMLElement){return this.classList?.contains('chat-map-canvas')?viewportHeight:0});
 const original=L.map;
 vi.spyOn(L,'map').mockImplementation((...args)=>{const map=original(...args);maps.push(map);return map});
});
afterEach(()=>{cleanup();vi.clearAllTimers();vi.useRealTimers();vi.restoreAllMocks();vi.unstubAllGlobals()});

it('opens an empty map with the entire world visible',()=>{
 render(<ChatMap value={emptyMap()}/>);
 const bounds=maps[0].getBounds();
 expect(bounds.contains([0,-179])).toBe(true);
 expect(bounds.contains([0,179])).toBe(true);
 expect(bounds.contains([-60,0])).toBe(true);
 expect(bounds.contains([80,0])).toBe(true);
});

it('fits one world closer when the unpinned map gets taller',()=>{
 render(<ChatMap value={emptyMap()}/>);const map=maps[0];
 const tile=Object.values((map as unknown as {_layers:Record<string,L.Layer>})._layers).find(layer=>layer instanceof L.TileLayer) as L.TileLayer;
 expect(tile.options.noWrap).toBe(true);
 viewportWidth=900;viewportHeight=520;
 act(()=>{observers[0]([],{} as ResizeObserver);vi.advanceTimersByTime(20)});
 expect(map.getZoom()).toBe(1);
 expect(map.getBounds().contains([-85,0])).toBe(true);
 expect(map.getBounds().contains([85,0])).toBe(true);
});

it('preserves the chosen view and marker when only its place name changes',()=>{
 const value={...emptyMap(),points:[{lat:35.68,lon:139.76,name:'東京'}]};
 const view=render(<ChatMap value={value} onChange={vi.fn()}/>),map=maps[0];
 map.setView([35.7,139.8],9,{animate:false});
 const marker=view.container.querySelector('.chat-map-pin'),center=map.getCenter();
 view.rerender(<ChatMap value={{...value,points:[{...value.points[0],name:'東京都千代田区'}]}} onChange={vi.fn()}/>);
 expect(map.getZoom()).toBe(9);expect(map.getCenter()).toEqual(center);
 expect(view.container.querySelector('.chat-map-pin')).toBe(marker);
 expect(marker).toHaveAttribute('aria-label','A: 東京都千代田区');
});

it('places a pin at the click without jumping from the current zoom',async()=>{
 let value=emptyMap();
 const onChange=vi.fn(next=>{value=next});
 const view=render(<ChatMap value={value} onChange={onChange}/>),map=maps[0];
 map.setView([35.7,139.7],8,{animate:false});
 await act(async()=>{map.fire('click',{latlng:L.latLng(35.68,139.76)});await Promise.resolve()});
 view.rerender(<ChatMap value={value} onChange={onChange}/>);
 expect(value.points[0]).toMatchObject({lat:35.68,lon:expect.closeTo(139.76)});
 expect(map.getZoom()).toBe(8);expect(map.getCenter()).toEqual(L.latLng(35.7,139.7));
 const marker=Object.values((map as unknown as {_layers:Record<string,L.Layer>})._layers).find(layer=>layer instanceof L.Marker) as L.Marker;
 expect(marker.getLatLng().equals([35.68,139.76])).toBe(true);
 expect(marker.options.icon!.options.iconAnchor).toEqual([20,50]);
 expect(view.container.querySelector('.chat-map-pin svg')).toHaveAttribute('viewBox','0 0 40 50');
});

it('keeps the same map and view through fullscreen, resizing once per frame',()=>{
 render(<ChatMap value={emptyMap()}/>);const map=maps[0];
 map.setView([30,100],4,{animate:false});const update=vi.spyOn(map,'invalidateSize');
 fireEvent.click(screen.getByRole('button',{name:'地図を全画面表示'}));
 expect(screen.getByRole('dialog')).toBeVisible();
 act(()=>{observers[0]([],{} as ResizeObserver);observers[0]([],{} as ResizeObserver);vi.advanceTimersByTime(20)});
 expect(update).toHaveBeenCalledTimes(1);expect(maps).toHaveLength(1);
 expect(map.getZoom()).toBe(4);
 // Leaflet rounds the map origin to a screen pixel during invalidateSize.
 expect(map.project(map.getCenter()).distanceTo(map.project([30,100]))).toBeLessThan(1);
 fireEvent.keyDown(screen.getByRole('dialog'),{key:'Escape'});
 expect(screen.queryByRole('dialog')).not.toBeInTheDocument();expect(maps).toHaveLength(1);
});

it('can close during a pending WebKit zoom completion without touching removed panes',()=>{
 const view=render(<ChatMap value={emptyMap()}/>);
 const map=maps[0] as L.Map & {_animatingZoom:boolean;_onZoomTransitionEnd:()=>void};
 map._animatingZoom=true;
 setTimeout(()=>map._onZoomTransitionEnd(),250);
 view.unmount();
 expect(()=>vi.advanceTimersByTime(300)).not.toThrow();
});

it('fills A then B, keeps B when A is removed, and refills the empty A slot',async()=>{
 let value=emptyMap();const onChange=vi.fn(next=>{value=next});
 const view=render(<ChatMap value={value} onChange={onChange}/>),map=maps[0];
 const click=async(lat:number,lon:number)=>{await act(async()=>{map.fire('click',{latlng:L.latLng(lat,lon)});await Promise.resolve()});view.rerender(<ChatMap value={value} onChange={onChange}/>)};
 expect(screen.queryByRole('button',{name:'A を指定'})).toBeNull();
 await click(35,139);await click(36,140);
 expect(value.points.map(p=>p.pin)).toEqual(['A','B']);
 await click(37,141);expect(value.points).toHaveLength(2);expect(value.points[1].lat).toBe(36);
 const allMarkers=()=>Object.values((map as unknown as {_layers:Record<string,L.Layer>})._layers).filter(layer=>layer instanceof L.Marker) as L.Marker[];
 const a=allMarkers().find(marker=>marker.getLatLng().lat===35)!;
 act(()=>{a.fire('dblclick')});view.rerender(<ChatMap value={value} onChange={onChange}/>);
 expect(value.points.map(p=>p.pin)).toEqual(['B']);
 expect(view.container.querySelector('.chat-map-pin-b')).toBeInTheDocument();
 await click(38,142);
 expect(value.points.map(p=>p.pin)).toEqual(['A','B']);expect(value.points[0].lat).toBe(38);expect(value.points[1].lat).toBe(36);
});

it('keeps both pins when place lookups finish out of order',async()=>{
 const resolve:Array<(value:any)=>void>=[];
 vi.mocked(api).mockImplementationOnce(()=>new Promise(done=>resolve.push(done))).mockImplementationOnce(()=>new Promise(done=>resolve.push(done)));
 let value=emptyMap();const onChange=vi.fn(next=>{value=next});
 const view=render(<ChatMap value={value} onChange={onChange}/>),map=maps[0];
 act(()=>{map.fire('click',{latlng:L.latLng(35,139)})});view.rerender(<ChatMap value={value} onChange={onChange}/>);
 act(()=>{map.fire('click',{latlng:L.latLng(36,140)})});view.rerender(<ChatMap value={value} onChange={onChange}/>);
 await act(async()=>{resolve[1]({name:'Second place'});await Promise.resolve()});view.rerender(<ChatMap value={value} onChange={onChange}/>);
 await act(async()=>{resolve[0]({name:'First place'});await Promise.resolve()});
 expect(value.points.map(point=>[point.pin,point.name])).toEqual([['A','First place'],['B','Second place']]);
});
