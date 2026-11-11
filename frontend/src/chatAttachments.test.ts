import {expect, it} from 'vitest';
import {attachmentFromFile, attachmentFromFolderFiles, attachmentFromDirectoryEntry} from './chatAttachments';
it('recognizes audio without a MIME type and preserves its binary payload',async()=>{
  const audio=new File([new Uint8Array([0,255,10])],'Meeting audio.M4A');
  const result=await attachmentFromFile(audio);
  expect(result).toMatchObject({name:'Meeting audio.M4A',mimeType:'audio/mp4',content:'',bytesBase64:btoa(String.fromCharCode(0,255,10))});
  expect(result.dataUrl).toBeUndefined();
});
function file(path: string, value: string) {
  const result = new File([value], path.split('/').at(-1)!);
  Object.defineProperty(result, 'webkitRelativePath', {value: path});
  return result;
}
it('keeps nested relative paths, binary bytes and empty files as working files', async () => {
  const result = await attachmentFromFolderFiles([file('Sample/src/a.txt', 'hello'), file('Sample/empty.txt', '')]);
  expect(result.kind).toBe('folder');
  expect(result.name).toBe('Sample');
  expect(result.content).toBe('');
  expect(result.files).toEqual([{path:'src/a.txt', bytesBase64:btoa('hello')}, {path:'empty.txt', bytesBase64:''}]);
});
it('ignores generated directories and rejects an empty folder', async () => {
  const result = await attachmentFromFolderFiles([file('Sample/a.txt', 'a'), file('Sample/node_modules/x.js', 'ignored')]);
  expect(result.files).toHaveLength(1);
  await expect(attachmentFromFolderFiles([])).rejects.toThrow('1–2000');
});
it('reads every directory-reader batch instead of stopping at the first batch', async () => {
  const leaf = (path: string) => ({name:path.split('/').at(-1)!, fullPath:path, isDirectory:false, file:(resolve: (f:File)=>void)=>resolve(file(path.slice(1), 'text'))});
  const batches = [[leaf('/Folder/a.txt')], [leaf('/Folder/b.txt')], []];
  const entry = {name:'Folder', fullPath:'/Folder', isDirectory:true, createReader:()=>({readEntries:(resolve:(batch:unknown[])=>void)=>resolve(batches.shift()!)})};
  const result = await attachmentFromDirectoryEntry(entry as unknown as FileSystemDirectoryEntry);
  expect(result.files?.map(value=>value.path)).toEqual(['a.txt','b.txt']);
});
