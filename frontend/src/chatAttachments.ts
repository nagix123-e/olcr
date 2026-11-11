import type {Attachment} from './ChatComposer';
function bytesToBase64(bytes:Uint8Array){let binary='';for(let i=0;i<bytes.length;i+=0x8000)binary+=String.fromCharCode(...bytes.subarray(i,Math.min(i+0x8000,bytes.length)));return btoa(binary)}
export async function attachmentFromFile(file:File):Promise<Attachment>{
    if(file.size===0) throw Error('The selected file is empty');
    if(file.size>50_000_000) throw Error('Attached file is too large (max 50 MB)');
    // Some macOS file providers omit File.type. Preserve original bytes while
    // identifying supported image and audio attachments.
    const extension=file.name.toLowerCase().split('.').pop()||'';
    const inferredImage=extension==='png'?'image/png':extension==='jpg'||extension==='jpeg'?'image/jpeg':extension==='webp'?'image/webp':'';
    const inferredAudio:Record<string,string>={wav:'audio/wav',wave:'audio/wav',mp3:'audio/mpeg',m4a:'audio/mp4',aac:'audio/aac',aif:'audio/aiff',aiff:'audio/aiff',flac:'audio/flac',ogg:'audio/ogg',opus:'audio/ogg',caf:'audio/x-caf'};
    const mimeType=file.type.startsWith('image/')?file.type:inferredImage||inferredAudio[extension]||file.type||'application/octet-stream';
    const bytesBase64=bytesToBase64(new Uint8Array(await file.arrayBuffer()));
    if(mimeType.startsWith('image/')){
      const dataUrl=await new Promise<string>((resolve,reject)=>{
        const reader=new FileReader();
        reader.onload=()=>typeof reader.result==='string'&&reader.result.startsWith('data:image/')?resolve(reader.result):reject(Error('The image could not be read'));
        reader.onerror=()=>reject(reader.error||Error('The image could not be read'));
        reader.readAsDataURL(file);
      });
      return {name:file.name,content:'',mimeType,dataUrl,bytesBase64};
    }
    const isText=mimeType.startsWith('text/')||/^(txt|md|csv|tsv|json|xml|yaml|yml|toml|py|js|ts|tsx|jsx|rs|html|css|svg)$/.test(extension);
    const content=isText?(await file.text()).slice(0,200000):'';
    return {name:file.name,content,mimeType,bytesBase64};
  }

export async function attachmentFromFolderFiles(files: File[], name?: string): Promise<Attachment> {
  files = files.filter(file => !(file.webkitRelativePath || file.name).split('/').some(part => ['.git', 'node_modules', '.venv', '__pycache__', '.DS_Store'].includes(part)));
  if (!files.length || files.length > 2000) throw Error('A working folder must contain 1–2000 files');
  if (files.reduce((sum, file) => sum + file.size, 0) > 50_000_000) throw Error('Working folder is too large (max 50 MB)');
  const folderName = name || files[0].webkitRelativePath.split('/')[0] || 'Folder';
  const entries = [];
  for (const file of files) {
    const relative = file.webkitRelativePath || file.name;
    const path = relative.startsWith(folderName + '/') ? relative.slice(folderName.length + 1) : relative;
    entries.push({path, bytesBase64: bytesToBase64(new Uint8Array(await file.arrayBuffer()))});
  }
  return {kind: 'folder', name: folderName, content: '', mimeType: 'application/x-olcr-working-folder', files: entries};
}

export async function attachmentFromDirectoryEntry(entry: FileSystemDirectoryEntry): Promise<Attachment> {
  const files: File[] = [];
  let total = 0;
  async function visit(item: FileSystemEntry): Promise<void> {
    if (['.git', 'node_modules', '.venv', '__pycache__', '.DS_Store'].includes(item.name)) return;
    if (item.isDirectory) {
      const reader = (item as FileSystemDirectoryEntry).createReader();
      for (;;) {
        const batch = await new Promise<FileSystemEntry[]>((resolve, reject) => reader.readEntries(resolve, reject));
        if (!batch.length) break;
        for (const child of batch) await visit(child);
      }
    } else {
      const file = await new Promise<File>((resolve, reject) => (item as FileSystemFileEntry).file(resolve, reject));
      total += file.size;
      if (files.length >= 2000 || total > 50_000_000) throw Error('Working folder exceeds 2000 files or 50 MB');
      Object.defineProperty(file, 'webkitRelativePath', {value: item.fullPath.replace(/^\//, '')});
      files.push(file);
    }
  }
  await visit(entry);
  return attachmentFromFolderFiles(files, entry.name);
}
