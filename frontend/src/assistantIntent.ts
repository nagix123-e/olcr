/** Everyday requests stay independent of queued coding instructions. */
export function isAssistantActionRequest(text:string){
 const videoSearch=/youtube|動画(?:を|の候補|検索|探し)|\bvideos?\b/i.test(text)&&/検索|探して|探し|見つけ|\b(?:search|find)\b|look\s+for/i.test(text);
 if(videoSearch&&!/```|実装(?:して|する|したい)|設計(?:して|する)|機能.*(?:実装|追加|作って)|\b(?:implement|implementation|build|refactor)\b/i.test(text)&&!(!/youtube/i.test(text)&&/ローカル|このMac|ファイル/.test(text))&&text.length<=1600)return true;
 if(/実装|コード|設計|機能を追加|\b(?:implement|implementation|build|code)\b/i.test(text))return false;
 if(/^\/(?:timer|calendar|youtube)(?:\s|$)/i.test(text))return true;
 return /タイマー|カレンダー|予定|スケジュール|会議|ミーティング|youtube|youtu\.be|音楽|曲|\b(?:timer|calendar|appointment|event|meeting|schedule|music|song)\b/i.test(text)
  && /再生|かけ|流し|停止|止め|再開|設定|セット|追加|登録|削除|変更|一覧|教え|今日|明日|今週|来週|\d+\s*(?:秒|分|時間)|https?:\/\/|\b(?:play|pause|resume|stop|set|start|cancel|add|create|list|show|today|tomorrow|change|delete|connect)\b/i.test(text);
}
