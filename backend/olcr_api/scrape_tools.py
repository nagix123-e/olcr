"""Model-independent public page extraction; executed inside the managed browser."""
import re
from urllib.parse import urlsplit

TOOLS = ('list', 'page', 'text', 'emails', 'social', 'images', 'sitemap', 'shopify')

def tool_from_instruction(text: str) -> str:
    for tool, pattern in [('shopify', r'shopify'), ('sitemap', r'sitemap|サイトマップ'),
                          ('emails', r'e-?mail|メール'), ('social', r'social|SNS|ソーシャル'),
                          ('images', r'images?|画像'), ('text', r'本文|全文|page text|article text'),
                          ('page', r'metadata|JSON-LD|メタデータ|ページ情報')]:
        if re.search(pattern, text, re.I): return tool
    return 'list'

def tool_url(tool: str, url: str) -> str:
    parsed = urlsplit(url)
    if tool == 'shopify': return parsed._replace(path='/products.json', query='limit=250', fragment='').geturl()
    if tool == 'sitemap' and not parsed.path.endswith('.xml'): return parsed._replace(path='/sitemap.xml', query='', fragment='').geturl()
    return url

EXTRACT_SCRIPT = r'''() => {
 const cfg = __OLCR_CONFIG__;
 const text = e => String(e?.innerText ?? e?.textContent ?? '').replace(/\s+/g,' ').trim().slice(0,16000);
 const fullText = e => String(e?.innerText ?? e?.textContent ?? '').replace(/\s+/g,' ').trim();
 const visible = e => e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0;
 const url = value => {try {const u=new URL(value,location.href); if(!['http:','https:'].includes(u.protocol)||u.username||u.password) return ''; return u.origin+u.pathname;} catch {return '';}};
 const base = {page_url: url(location.href)};
 const cssPath = e => {const parts=[]; while(e && e!==document.documentElement){let s=e.tagName.toLowerCase(); if(e.id){parts.unshift('#'+CSS.escape(e.id));break;} const p=e.parentElement;if(p){const same=[...p.children].filter(n=>n.tagName===e.tagName);if(same.length>1)s+=':nth-of-type('+(same.indexOf(e)+1)+')';} parts.unshift(s);e=p;} return parts.join(' > ');};
 let rows=[], selector=cfg.record_selector;
 const meta = key => document.querySelector('meta[name="'+key+'"],meta[property="'+key+'"]')?.content || '';
 if(cfg.tool==='page'||cfg.tool==='text') {
  const content=fullText(document.querySelector('article,main,[role=main]')||document.body);
  const structured=[...document.querySelectorAll('script[type="application/ld+json"]')].slice(0,20).flatMap(e=>{try{return [JSON.parse(e.textContent.slice(0,100000))];}catch{return [];}});
  rows=[{...base,title:document.title,description:meta('description')||meta('og:description'),author:meta('author'),date:meta('article:published_time'),word_count:content.split(/\s+/).filter(Boolean).length,...(cfg.tool==='text'?{text:content.slice(0,50000),text_truncated:content.length>50000}:{image:url(meta('og:image')),json_ld:structured,microdata:[...document.querySelectorAll('[itemprop]')].slice(0,100).map(e=>({name:e.getAttribute('itemprop'),value:e.getAttribute('content')||url(e.getAttribute('href')||e.getAttribute('src'))||text(e)})),open_graph:Object.fromEntries([...document.querySelectorAll('meta[property^="og:"]')].slice(0,50).map(e=>[e.getAttribute('property'),e.content]))})}];
 } else if(cfg.tool==='emails') {
  const values=[...fullText(document.body).matchAll(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi)].map(m=>m[0]);
  for(const e of document.querySelectorAll('a[href^="mailto:"]')) {try{values.push(decodeURIComponent(e.getAttribute('href').slice(7).split('?')[0]));}catch{}}
  rows=[...new Set(values.map(v=>v.toLowerCase()))].map(email=>({...base,email}));
 } else if(cfg.tool==='social') {
  rows=[...document.querySelectorAll('a[href]')].flatMap(e=>{const href=url(e.href);if(!href)return []; const host=new URL(href).hostname.replace(/^www\./,'');return /(^|\.)(facebook\.com|instagram\.com|linkedin\.com|twitter\.com|x\.com|youtube\.com|tiktok\.com|pinterest\.com|github\.com|threads\.net|bsky\.app)$/.test(host)?[{...base,network:host,url:href,text:text(e)}]:[];});
 } else if(cfg.tool==='images') {
  for(const e of document.querySelectorAll('img')) rows.push({...base,image_url:url(e.getAttribute('data-src')||e.getAttribute('data-lazy-src')||e.currentSrc||e.src),alt:e.alt||'',width:e.naturalWidth,height:e.naturalHeight});
  for(const e of document.querySelectorAll('video[poster]')) rows.push({...base,image_url:url(e.poster),alt:e.getAttribute('aria-label')||'',width:e.videoWidth,height:e.videoHeight});
  for(const e of [...document.querySelectorAll('body *')].slice(0,10000)){const bg=getComputedStyle(e).backgroundImage;for(const m of bg.matchAll(/url\(["']?([^"')]+)["']?\)/g))rows.push({...base,image_url:url(m[1]),alt:e.getAttribute('aria-label')||'',width:0,height:0});}
  rows=rows.filter(r=>r.image_url);
 } else if(cfg.tool==='sitemap') {
  let xml=document; if(!document.querySelector('urlset,sitemapindex')){try{xml=new DOMParser().parseFromString(document.body.innerText,'application/xml');}catch{}}
  rows=[...xml.getElementsByTagName('loc')].map(e=>({...base,url:url(e.textContent),kind:e.parentElement?.localName||''})).filter(r=>r.url);
 } else if(cfg.tool==='shopify') {
  let data;try{data=JSON.parse(document.body.innerText);}catch{return {error:'SHOPIFY_PUBLIC_PRODUCTS_JSON_REQUIRED'};}
  rows=(data.products||[]).flatMap(p=>(p.variants||[{}]).map(v=>({...base,product_id:p.id,title:p.title,handle:p.handle,vendor:p.vendor,product_type:p.product_type,tags:p.tags,variant_id:v.id,variant_title:v.title,sku:v.sku,price:v.price,available:v.available,image_url:url(p.images?.[0]?.src)})));
 } else {
  let records=[];
  let rects=[];
  if(cfg.point){let e=document.elementFromPoint(cfg.point.x,cfg.point.y);while(e?.parentElement){const siblings=[...e.parentElement.children].filter(n=>n.tagName===e.tagName&&visible(n)&&[...e.classList].every(c=>n.classList.contains(c)));if(siblings.length>=3){records=siblings;selector=cssPath(e.parentElement)+' > '+e.tagName.toLowerCase()+[...e.classList].map(c=>'.'+CSS.escape(c)).join('');break;}e=e.parentElement;}}
  else if(selector && selector!=='auto') records=[...document.querySelectorAll(selector)];
  else {let best=0;for(const parent of [...document.querySelectorAll('table,tbody,ul,ol,main,section,div')].slice(0,5000)){if(parent.closest('nav,header,footer,aside'))continue;const groups=new Map();for(const e of parent.children){if(!visible(e)||['SCRIPT','STYLE','NAV'].includes(e.tagName))continue;const key=e.tagName+'.'+[...e.classList].sort().join('.');groups.set(key,[...(groups.get(key)||[]),e]);}for(const group of groups.values()){if(group.length<3)continue;const score=group.length*(group[0].querySelector('a,img,h1,h2,h3,h4,td')?3:1);if(score>best){best=score;records=group;selector=cssPath(parent)+' > '+group[0].tagName.toLowerCase()+[...group[0].classList].map(c=>'.'+CSS.escape(c)).join('');}}}}
  rects=records.slice(0,100).map(e=>{const b=e.getBoundingClientRect();return {x:100*b.x/innerWidth,y:100*b.y/innerHeight,width:100*b.width/innerWidth,height:100*b.height/innerHeight};});
  cfg.field_match_counts=Object.fromEntries(Object.keys(cfg.fields||{}).map(key=>[key,0]));
  rows=records.map(e=>{
   const requested=Object.keys(cfg.fields||{});
   const r=requested.length?{...base}:{...base,text:text(e)};
   const recordText=fullText(e);
   const links=[...(e.matches('a[href]')?[e]:[]),...e.querySelectorAll('a[href]')].filter(visible);
   const heading=links.map(text).find(value=>value&&value.length<=120&&!/^(地図|map|details?|詳細|無料でナビ)$/i.test(value))
     || [...e.querySelectorAll('h1,h2,h3,h4')].map(text).find(value=>value&&value.length<=120);
   const address=recordText.match(/(?:北海道|東京都|京都府|大阪府|.{2,3}県)[^\s<>]{1,48}/)?.[0]
     .replace(/\[地図\].*$/, '').trim()||null;
   const category=recordText.match(/[<＜]([^<>＜＞]{2,64})[>＞]/);
   let matched=0;
   for(const key of requested){
    const normalized=key.toLocaleLowerCase();
    let value=null;
    if(/name|title|施設名|商品名|名称/.test(normalized)) value=heading||null;
    else if(/address|所在地|住所/.test(normalized)) value=address;
    else if(/category|genre|カテゴリ|カテゴリー|ジャンル/.test(normalized)) value=category?.[1]||null;
    r[key]=value;
    if(value) matched++;
   }
   if(requested.length) for(const key of requested) cfg.field_match_counts[key]=(cfg.field_match_counts[key]||0)+(r[key]?1:0);
   if(!requested.length){
    const leaves=[...e.querySelectorAll('p,span,div,li,time,small')].filter(n=>!n.children.length&&text(n));
    leaves.slice(0,50).forEach((n,i)=>r['text_'+(i+1)]=text(n));
    const cells=[...e.querySelectorAll('td,th')];cells.forEach((c,i)=>r['column_'+(i+1)]=text(c));
    links.slice(0,10).forEach((a,i)=>{r['link_'+(i+1)]=url(a.href);r['link_text_'+(i+1)]=text(a);});
    [...e.querySelectorAll('img')].slice(0,10).forEach((im,i)=>{r['image_'+(i+1)]=url(im.getAttribute('data-src')||im.getAttribute('data-lazy-src')||im.currentSrc||im.src);r['alt_'+(i+1)]=im.alt||'';});
    [...e.querySelectorAll('h1,h2,h3,h4,[aria-label]')].slice(0,10).forEach((n,i)=>r['label_'+(i+1)]=n.getAttribute('aria-label')||text(n));
    for(const n of [e,...e.querySelectorAll('[style]')].slice(0,20)){const m=getComputedStyle(n).backgroundImage.match(/url\(["']?([^"')]+)["']?\)/);if(m)r['background_image']=url(m[1]);}
   }
   return r;
  });
  cfg.rects=rects;
 }
 const internal_links=[...new Set([...document.querySelectorAll('a[href]')].map(e=>url(e.href)).filter(u=>u&&new URL(u).origin===location.origin&&!/\.(?:pdf|png|jpg|gif|zip|mp4)$/i.test(new URL(u).pathname)&&!/\/(?:logout|delete|checkout|cart|login)(?:\/|$)/i.test(new URL(u).pathname)))].slice(0,100);
 const nav=[...document.querySelectorAll('a[rel="next"],a,button,[role="button"]')].filter(e=>visible(e)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true').flatMap(e=>{
 if(e.matches('button,[role=button]') && (e.form || e.type==='submit')) return [];
 const label=[text(e),e.getAttribute('aria-label'),e.getAttribute('title')].filter(Boolean).join(' ').replace(/\s+/g,' ').trim();
 if(e.matches('a[rel="next"]')||/(?:\bnext(?:\s+page)?\b|次へ|次のページ|次ページ|次の\s*\d+\s*件|次の結果|next\s*[›»→>])/i.test(label)) return [{type:e.matches('a[href]')?'next':'load_more',selector:cssPath(e)}];
 if(/^(load more|show more|もっと見る|さらに表示|さらに読み込む)$/i.test(label)&&e.matches('button,[role="button"]'))return [{type:'load_more',selector:cssPath(e)}];return [];
 }).slice(0,10);
 if(!nav.length){
  const here=new URL(location.href),pageKeys=['page','p','pg','page_no','pageno'];
  const numericLinks=[...document.querySelectorAll('a[href]')].filter(e=>visible(e)&&/^\d+$/.test(text(e)));
 const numbered=numericLinks.flatMap(e=>{try{const target=new URL(e.href,location.href);if(target.origin!==here.origin||target.pathname!==here.pathname)return [];for(const key of pageKeys){const entry=[...target.searchParams.entries()].find(([name])=>name.toLowerCase()===key);if(!entry)continue;const current=Number([...here.searchParams.entries()].find(([name])=>name.toLowerCase()===key)?.[1]||1),next=Number(entry[1]);if(Number.isInteger(next)&&next>current)return [{type:'url_parameter',page_parameter:entry[0],page:next}];}return [];}catch{return [];}}).sort((a,b)=>a.page-b.page);
  if(numbered.length)nav.push({type:'url_parameter',page_parameter:numbered[0].page_parameter});
 }
 if(!nav.length&&rows.length>=3){
  const scrollRoots=[document.scrollingElement,...document.querySelectorAll('main,[role="main"],ul,ol,section,div')].filter(Boolean);
  const scrollable=scrollRoots.some(e=>{const style=getComputedStyle(e);return (e===document.scrollingElement||/(auto|scroll)/.test(style.overflowY))&&e.scrollHeight>e.clientHeight+160;});
  if(scrollable)nav.push({type:'infinite_scroll'});
 }
 rows=[...new Map(rows.map(r=>[JSON.stringify(r),r])).values()];
 return {rows:rows.slice(0,cfg.limit),record_count:rows.length,truncated:rows.length>cfg.limit,internal_links, navigation_candidates:nav,record_selector:selector,record_rects:cfg.rects||[],field_match_counts:cfg.field_match_counts||{}};
}'''
