import {native} from "./native";

type OcrElement={text:string;bboxNormalized:number[];confidence:number};
type OcrObservation={source:string;observationId:string;coordinateSpace:string;width:number;height:number;elements:OcrElement[];imageSha256?:string};
type OcrRoi={dataUrl:string;width:number;height:number;sourceBboxNormalized:number[];sourceBboxPixels:number[]};
const APPLE_VISION_OCR_TIMEOUT_MS=750;
const GUI_GROUNDING_OCR_TIMEOUT_MS=1500;
let nativeOcrInFlight=false;
export type AttachedImagePerception={ocr_observation?:OcrObservation;ocr_duration_ms?:number;ocr_attempt?:{source:"APPLE_VISION_OCR";status:"error";duration_ms:number;image_sha256:string};ocr_roi?:{data_url:string;source:string;observation_id:string;image_sha256:string;source_bbox_normalized:number[];source_bbox_pixels:number[];width:number;height:number;target_text:string;target_confidence:number}};

export function isMedicationImageIntent(text:string):boolean {
  return /(?:処方箋|薬袋|服薬|服用予定|薬を登録|お薬の説明)/.test(text)&&/(?:登録|管理|予定|服薬|スケジュール)/.test(text);
}

/** Only route explicit text extraction requests through fast OCR. */
export function isTextOnlyReadIntent(text:string):boolean {
  const value=text.trim();
  if(!value||/(?:click|tap|press|select|where|位置|座標|クリック|押して|タップ|選択|操作)/i.test(value))return false;
  return /(?:\b(?:read|extract|transcribe|recognize|ocr)\b.{0,80}\b(?:text|文字|label|labels|word|words)\b|\bwhat\s+(?:does|is)\b.{0,60}\b(?:say|written|text)\b|文字を読|文字起こし|テキストを抽出|何と書いて|書かれている文字)/i.test(value);
}

export function isGuiGroundingIntent(text:string):boolean {
  return /\b(?:screen|screenshots?|screen shots?|windows?|dialogs?|desktops?|apps?|interfaces?|controls?|buttons?|checkbox(?:es)?|radio buttons?|click|tap|fields?|text fields?|input fields?|search bars?|menus?|toolbars?|panels?|cursor|pointer|ui)\b/i.test(text)
    ||/(?:画面|スクリーンショット|ダイアログ|デスクトップ|アプリ|インターフェース|ボタン|チェックボックス|クリック|入力欄)/i.test(text);
}

function normalizeMatchText(value:string):string{return value.normalize("NFKC").toLocaleLowerCase().replace(/[\s\p{P}\p{S}]+/gu,"")}
export function chooseOcrGroundedTextTarget(query:string,elements:OcrElement[]):OcrElement|null{
  const normalizedQuery=normalizeMatchText(query);
  const ranked=elements.filter(item=>typeof item.text==="string"&&item.text.trim().length>=2&&Array.isArray(item.bboxNormalized)&&item.bboxNormalized.length===4&&item.confidence>=0.55)
    .map(item=>{const label=normalizeMatchText(item.text);const exact=label.length>=2&&normalizedQuery.includes(label);const queryWords=new Set(query.toLocaleLowerCase().match(/[\p{L}\p{N}]+/gu)||[]);const labelWords=(item.text.toLocaleLowerCase().match(/[\p{L}\p{N}]+/gu)||[]);const overlap=labelWords.filter(word=>word.length>=2&&queryWords.has(word)).length;return{item,score:exact?100+Math.min(label.length,50):overlap?overlap:0}})
    .filter(item=>item.score>0).sort((a,b)=>b.score-a.score);
  if(!ranked.length||ranked.length>1&&ranked[0].score===ranked[1].score)return null;
  return ranked[0].item;
}

async function prepareOcrEvidence(text:string,dataUrl:string,gui:boolean,knownImageSha256?:string):Promise<AttachedImagePerception|null>{
  const bridge=native();if(!bridge)return null;
  let imageSha256=knownImageSha256;
  try{
    if(!imageSha256){
      const bytes=await (await fetch(dataUrl)).arrayBuffer();
      const digest=await crypto.subtle.digest("SHA-256",bytes);
      imageSha256=Array.from(new Uint8Array(digest),byte=>byte.toString(16).padStart(2,"0")).join("");
    }
  }catch{return null;}
  if(!imageSha256)return null;
  // A timed-out Tauri command cannot be cancelled from the frontend. Keep at
  // most one native OCR worker outstanding; later requests still fall through
  // to UI-Venus instead of piling up unbounded native work.
  if(nativeOcrInFlight)return null;
  const ocrStarted=performance.now();
  nativeOcrInFlight=true;
  let timeoutId:ReturnType<typeof setTimeout>|undefined;
  let ocrResult:{kind:"returned";observation:OcrObservation}|{kind:"error"|"timeout"};
  try{
    const nativeCall=bridge.core.invoke<OcrObservation>("recognize_image_text",{dataUrl});
    // Release the single-flight guard only when the native worker actually
    // settles, even if the caller already moved on after the bounded timeout.
    void nativeCall.then(()=>{nativeOcrInFlight=false;},()=>{nativeOcrInFlight=false;});
    ocrResult=await Promise.race([
      nativeCall.then(observation=>({kind:"returned" as const,observation}),()=>({kind:"error" as const})),
      new Promise<{kind:"timeout"}> (resolve=>{timeoutId=setTimeout(()=>resolve({kind:"timeout"}),gui?GUI_GROUNDING_OCR_TIMEOUT_MS:APPLE_VISION_OCR_TIMEOUT_MS);}),
    ]);
  }catch{
    nativeOcrInFlight=false;
    return{ocr_attempt:{source:"APPLE_VISION_OCR",status:"error",
      duration_ms:Math.max(0,performance.now()-ocrStarted),image_sha256:imageSha256}};
  }finally{
    if(timeoutId!==undefined)clearTimeout(timeoutId);
  }
  const ocrDurationMs=Math.max(0,performance.now()-ocrStarted);
  if(ocrResult.kind!=="returned"){
    return{ocr_attempt:{source:"APPLE_VISION_OCR",status:"error",duration_ms:ocrDurationMs,image_sha256:imageSha256}};
  }
  const observation=ocrResult.observation;
  if(!observation||observation.source!=="APPLE_VISION_OCR"||!observation.observationId
      ||observation.coordinateSpace!=="normalized_image_top_left"||!Array.isArray(observation.elements)){
    return{ocr_attempt:{source:"APPLE_VISION_OCR",status:"error",duration_ms:ocrDurationMs,image_sha256:imageSha256}};
  }
  const ocrObservation={...observation,imageSha256};
  const evidence:AttachedImagePerception={ocr_observation:ocrObservation,ocr_duration_ms:ocrDurationMs};
  if(!gui)return evidence;
  const target=chooseOcrGroundedTextTarget(text,observation.elements||[]);
  if(!target)return evidence;
  try{
    const roi=await bridge.core.invoke<OcrRoi>("crop_ocr_grounded_roi",{dataUrl,textBboxNormalized:target.bboxNormalized});
    evidence.ocr_roi={data_url:roi.dataUrl,source:"APPLE_VISION_OCR",observation_id:observation.observationId,image_sha256:imageSha256,source_bbox_normalized:roi.sourceBboxNormalized,source_bbox_pixels:roi.sourceBboxPixels,width:roi.width,height:roi.height,target_text:target.text,target_confidence:target.confidence};
  }catch{/* OCR succeeded; ROI extraction is an optional optimization. */}
  return evidence;
}

export async function prepareAttachedImagePerception(text:string,dataUrl:string):Promise<AttachedImagePerception|null>{
  const gui=isGuiGroundingIntent(text);
  if(!isTextOnlyReadIntent(text)&&!gui&&!isMedicationImageIntent(text))return null;
  return prepareOcrEvidence(text,dataUrl,gui);
}

/** Run the existing OCR-seeded crop path for an explicit selected-screen target request. */
export function prepareGuiGroundingOcr(text:string,dataUrl:string,imageSha256?:string):Promise<AttachedImagePerception|null>{
  return prepareOcrEvidence(text,dataUrl,true,imageSha256);
}
