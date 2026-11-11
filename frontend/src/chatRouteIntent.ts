const SCRAPE_ROUTE = /(?:\b(?:scrape|scraping|crawl|web\s*scrap(?:e|ing))\b|スクレイピング|スクレイプ)/i;
const E2E_LIFECYCLE_ROUTE = /(?:\b(?:start|run|launch|serve|open|attach|connect|stop|close|end|status|check|show)\b.{0,60}\b(?:local\s+)?(?:e2e|end[- ]to[- ]end|playwright)\b|\b(?:local\s+)?(?:e2e|end[- ]to[- ]end|playwright)\b.{0,60}\b(?:start|run|launch|serve|open|attach|connect|stop|close|end|status|check|show)\b|(?:実\s*(?:GUI\s*)?E(?:2E|to\s*E)|ローカルE(?:2E|to\s*E)).{0,60}(?:開始|起動|立ち上げ|動かして|接続して|停止|終了|状態|進捗|確認)|(?:開始|起動|立ち上げ|動かして|接続して|停止|終了|状態|進捗|確認).{0,60}(?:実\s*(?:GUI\s*)?E(?:2E|to\s*E)|ローカルE(?:2E|to\s*E))|(?:サイト|ウェブサイト|webサイト|ブラウザゲーム|ウェブアプリ|webアプリ).{0,30}E(?:2E|to\s*E).{0,40}(?:開始|起動|立ち上げ|動かして|接続して|停止|終了|状態|進捗|確認)|(?:開始|起動|立ち上げ|動かして|接続して|停止|終了|状態|進捗|確認).{0,40}E(?:2E|to\s*E).{0,30}(?:サイト|ウェブサイト|webサイト|ブラウザゲーム|ウェブアプリ|webアプリ))/i;
const BROWSER_ACTION_ROUTE = /(?:^\s*(?:click|press|activate|type|fill|enter)\b|(?:ボタン|リンク).{0,16}(?:クリック|押して|選択)|(?:クリック|押して|選択).{0,16}(?:ボタン|リンク)|(?:入力欄|フィールド|欄).{0,24}(?:入力|記入|タイプ)|(?:入力|記入|タイプ).{0,24}(?:入力欄|フィールド|欄))/i;
const SCRAPE_CONTROL_ROUTE = /^(?:pause|stop|resume|continue|status(?:\s+check)?|export|download|show\s+(?:the\s+)?(?:scrape\s+)?results?|(?:一時停止|停止|再開|進捗|状態|結果|結果を表示|結果をダウンロード|結果をエクスポート))(?:\s*[。.!！]*)$/i;
const E2E_CONTROL_ROUTE = /^(?:status|check|show|stop|close|end)\s+(?:(?:the\s+)?(?:local\s+)?e2e|computer\s+use|browser|local\s+test)|^(?:ローカル)?E(?:2E|to\s*E)(?:の)?(?:状態|進捗|停止|終了|閉じる|状況)/i;

type ChatTaskContext = { hasScrapeTask?: boolean; hasPendingScrapeClarification?: boolean; hasLocalE2ETask?: boolean };

/** Structured browser capabilities must use /api/chat so their task router runs. */
export function isChatCapabilityRequest(message: string, context: ChatTaskContext = {}): boolean {
  const text = String(message || '').trim();
  if (/^(?:what\s+is|what\s+does|tell\s+me\s+what|explain)\b/i.test(text)
      && !/^(?:scrape|crawl|start|run|launch|attach)\b/i.test(text)) return false;
  return !!text && (SCRAPE_ROUTE.test(text) || E2E_LIFECYCLE_ROUTE.test(text)
    || BROWSER_ACTION_ROUTE.test(text)
    || (context.hasScrapeTask === true && SCRAPE_CONTROL_ROUTE.test(text))
    || context.hasPendingScrapeClarification === true
    || (context.hasLocalE2ETask === true && E2E_CONTROL_ROUTE.test(text)));
}
