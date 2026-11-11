/** Exact management requests only; explanatory prose must remain ordinary chat. */
export function chatWorkspaceCommand(text: string): string | null {
  const value = text.trim().replace(/[。.!！]+$/, "").trim();
  if (/^(?:設定(?:を)?(?:開いて|表示して)|権限設定(?:を)?(?:開いて|表示して)|open settings|show settings)$/i.test(value)) return "/settings";
  if (/^(?:スクレイピング(?:の)?管理(?:を)?(?:開いて|表示して)|スクレイプ管理|open scrape (?:manager|studio)|manage scraping)$/i.test(value)) return "/scrape manage";
  if (/^(?:ルーチン(?:の)?(?:一覧|管理)(?:を)?(?:表示して|開いて)?|検証済みルーチン(?:を)?(?:表示して|開いて)|show routines|manage routines)$/i.test(value)) return "/routines";
  const toggle = value.match(/^(スクレイピング|ローカルE2E|コーディング)(?:を)?(オン|オフ)(?:にして)?$/i);
  if (toggle) return `/${({"スクレイピング":"scrape", "ローカルe2e":"e2e", "コーディング":"coding"} as Record<string,string>)[toggle[1].toLowerCase()]} ${toggle[2] === "オン" ? "on" : "off"}`;
  const scope = value.match(/^画面操作(?:の)?対象(?:に)?\s*([A-Za-z0-9][A-Za-z0-9._-]{0,254})(?:を)?(追加して|許可して|削除して|外して)$/);
  if (scope) return `/computer use ${["追加して", "許可して"].includes(scope[2]) ? "allow" : "remove"} ${scope[1]}`;
  return null;
}
