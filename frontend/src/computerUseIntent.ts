export type ComputerUseIntent = "start" | "stop" | "status";

const COMPUTER_USE_MARKER = /\bcomputer\s+use\b|コンピュータユーズ|コンピュータ使用|画面操作/i;
const EXPLICIT_NATIVE_E2E_START = /\b(?:e2e|end[- ]to[- ]end)\b|実\s*(?:gui\s*)?e(?:2e|to\s*e)/i;
const EXPLICIT_BROWSER_E2E = /https?:\/\/|\b(?:playwright|browser|website|site|web\s+app|web\s+game|local\s+browser|local\s+e2e)\b|(?:ブラウザ|ウェブサイト|webサイト|サイト|ブラウザゲーム|ウェブアプリ|webアプリ|ローカルE(?:2E|to\s*E))/i;

function isExplicitNativeE2EStart(text: string): boolean {
  return COMPUTER_USE_MARKER.test(text)
    && EXPLICIT_NATIVE_E2E_START.test(text)
    && !EXPLICIT_BROWSER_E2E.test(text)
    && !/\b(?:scrape|scraping|crawl)\b|スクレイピング|スクレイプ/i.test(text);
}

export function parseComputerUseTarget(message: string): string | null {
  const text = String(message || "").trim();
  const nativeE2EQuoted = text.match(/\bcomputer\s+use\b.{0,100}\b(?:for|on)\s+["“'「『]([^"”'」』]{1,100})["”'」』]/i);
  const nativeE2ETail = text.match(/\bcomputer\s+use\b.{0,80}\b(?:for|on|in)\s+([A-Za-z0-9][A-Za-z0-9 ._-]{0,60}?)(?:\s+(?:and|to)\b|[,!?。]|\.(?=\s|$)|$)/i);
  const nativeE2EJapanese = text.match(/[「『]([^」』]{1,100})[」』](?:アプリ)?(?:を)?(?:対象に)?(?:して)?(?:コンピュータユーズ|コンピュータ使用|画面操作).{0,48}実\s*(?:GUI\s*)?E(?:2E|to\s*E)/i);
  const nativeE2ETarget = (nativeE2EQuoted?.[1] || nativeE2EJapanese?.[1] || nativeE2ETail?.[1] || "").trim();
  if (nativeE2ETarget && !/^(?:computer\s+use|コンピュータユーズ|コンピュータ使用|画面操作)$/i.test(nativeE2ETarget)) {
    return nativeE2ETarget;
  }
  const patterns = [
    /^(?:start|begin|enable|launch)\s+(?:the\s+)?computer\s+use\s+(?:on|for|with|in)\s+["“']?(.+?)["”']?[.!。！]?$/i,
    /^computer\s+use\s+(?:on|for|with|in)\s+["“']?(.+?)["”']?\s+(?:start|begin|enable|launch)[.!。！]?$/i,
    /^(.+?)で(?:コンピュータユーズ|コンピュータ使用|画面操作)(?:を)?(?:開始(?:して)?|起動(?:して)?|始めて|有効にして)[。.!！]?$/i,
    /^(?:コンピュータユーズ|コンピュータ使用|画面操作)(?:を)?[「『"“'](.+?)[」』"”'](?:で)?(?:開始(?:して)?|起動(?:して)?|始めて|有効にして)[。.!！]?$/i,
  ];
  for (const pattern of patterns) {
    const match = text.match(pattern);
    const target = match?.[1]?.trim().replace(/[。.!！]+$/, "").trim();
    if (target && !/^(?:computer\s+use|コンピュータユーズ|コンピュータ使用|画面操作)$/i.test(target)) return target;
  }
  return null;
}

/** Resolve only an exact app bundle path explicitly written in the chat message. */
export function parseComputerUseApplicationPath(message: string): string | null {
  const text = String(message || "");
  const quoted = text.match(/["'`“「『]((?:\/|~\/)[^"'`”」』\r\n]{1,1000}\.app)["'`”」』]/);
  if (quoted?.[1]) return quoted[1].trim();
  const bare = text.match(/(?:^|\s)((?:\/|~\/)[^\s"'`<>]{1,1000}\.app)(?=$|[\s,，。.!！])/);
  return bare?.[1]?.trim() || null;
}

/** Keep native session controls disjoint from Playwright/local-project E2E requests. */
export function parseComputerUseIntent(message: string): ComputerUseIntent | null {
  const text = String(message || "").trim();
  if (!text || /https?:\/\//i.test(text)
      || /\b(?:scrape|scraping|crawl)\b|スクレイピング|スクレイプ/i.test(text)) return null;
  const nativeE2EStart = isExplicitNativeE2EStart(text);
  if (!nativeE2EStart
      && (/\b(?:e2e|end[- ]to[- ]end|playwright|local\s+browser)\b|実\s*(?:GUI\s*)?E(?:2E|to\s*E)|ローカルE(?:2E|to\s*E)/i.test(text)
        || EXPLICIT_BROWSER_E2E.test(text))) return null;
  if (nativeE2EStart) return "start";
  if (/^\s*(?:(?:status|check|show)\s+(?:the\s+)?computer\s+use|computer\s+use\s+(?:status|check|show)|(?:コンピュータユーズ|コンピュータ使用|画面操作)(?:の)?(?:状態|状況)(?:を?(?:確認|表示|教えて)(?:して)?)?)\s*[。.!！]*$/i.test(text)) return "status";
  if (/^\s*(?:(?:stop|end|disable)\s+(?:the\s+)?computer\s+use|(?:コンピュータユーズ|コンピュータ使用|画面操作)(?:を)?(?:停止(?:して)?|終了(?:して)?|止めて|やめて))\s*[。.!！]*$/i.test(text)) return "stop";
  if (/(?:\b(?:start|begin|enable|launch)\s+(?:the\s+)?computer\s+use\b|\bcomputer\s+use\b.{0,24}\b(?:start|begin|enable|launch)\b|(?:コンピュータユーズ|コンピュータ使用|画面操作).{0,24}(?:開始|起動|始めて|有効にして)|(?:開始|起動|始めて|有効にして).{0,24}(?:コンピュータユーズ|コンピュータ使用|画面操作))/i.test(text)) return "start";
  return null;
}
