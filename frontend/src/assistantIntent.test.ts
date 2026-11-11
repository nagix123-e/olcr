import {describe,expect,it} from 'vitest';
import {isAssistantActionRequest} from './assistantIntent';

describe('YouTube video-search routing',()=>{
 it.each(['YouTubeでPythonの使い方動画を検索して','Pythonの解説動画を探して','Find videos on YouTube about Python'])('routes video search outside coding followups: %s',text=>{
  expect(isAssistantActionRequest(text)).toBe(true);
 });
 it.each(['YouTube動画の検索機能を実装して','implement a YouTube video search widget','ローカル動画を検索して'])('does not turn coding or local-file work into YouTube search: %s',text=>{
  expect(isAssistantActionRequest(text)).toBe(false);
 });
});
