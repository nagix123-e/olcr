export function pcmWav(chunks: Float32Array[], sourceRate: number): string {
  const length=chunks.reduce((sum,chunk)=>sum+chunk.length,0),source=new Float32Array(length);
  let offset=0;for(const chunk of chunks){source.set(chunk,offset);offset+=chunk.length}
  const count=Math.floor(length*16000/sourceRate),buffer=new ArrayBuffer(44+count*2),view=new DataView(buffer);
  const write=(at:number,value:string)=>{for(let i=0;i<value.length;i++)view.setUint8(at+i,value.charCodeAt(i))};
  write(0,'RIFF');view.setUint32(4,36+count*2,true);write(8,'WAVE');write(12,'fmt ');view.setUint32(16,16,true);
  view.setUint16(20,1,true);view.setUint16(22,1,true);view.setUint32(24,16000,true);view.setUint32(28,32000,true);
  view.setUint16(32,2,true);view.setUint16(34,16,true);write(36,'data');view.setUint32(40,count*2,true);
  for(let i=0;i<count;i++){const position=i*sourceRate/16000,left=Math.floor(position),fraction=position-left;
    const value=Math.max(-1,Math.min(1,(source[left]||0)*(1-fraction)+(source[left+1]||0)*fraction));view.setInt16(44+i*2,value<0?value*32768:value*32767,true)}
  const bytes=new Uint8Array(buffer);let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));return btoa(binary);
}

export async function microphone(onSamples:(chunk:Float32Array,rate:number)=>void,primedContext?:AudioContext) {
  const context=primedContext||new AudioContext();
  let stream:MediaStream|undefined;
  try {
    const resumed=context.resume();void resumed.catch(()=>{});
    stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true},video:false});
    await resumed;
    const source=context.createMediaStreamSource(stream),processor=context.createScriptProcessor(4096,1,1),silence=context.createGain();silence.gain.value=0;
    processor.onaudioprocess=event=>onSamples(event.inputBuffer.getChannelData(0).slice(),context.sampleRate);
    source.connect(processor);processor.connect(silence);silence.connect(context.destination);
    return ()=>{processor.onaudioprocess=null;source.disconnect();processor.disconnect();silence.disconnect();stream?.getTracks().forEach(track=>track.stop());void context.close()};
  } catch(error) {stream?.getTracks().forEach(track=>track.stop());void context.close();throw error}
}
