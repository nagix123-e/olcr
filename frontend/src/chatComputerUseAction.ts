import { persistPerceptionPolicy } from "./perception";
import { parseComputerUseIntent, parseComputerUseTarget } from "./computerUseIntent";

type NativeTarget = { applicationPid: number; applicationBundleId: string };
type Api = (path: string, init?: RequestInit) => Promise<any>;
type Bridge = { core: { invoke: (command: string, args?: Record<string, unknown>) => Promise<any> } };
type VisionGrounding = {
  candidate: { label: string; bbox: [number, number, number, number]; confidence: number };
  provenance: Record<string, any>;
  alignment: Record<string, any>;
};

export type SingleChatComputerUseAction = { kind: "open_disclosure" | "open_find"; question: string }
  | { kind: "goal"; question: string }
  | { kind: "press_button"; question: string; targetLabel?: string; expectedText: string }
  | { kind: "type_text"; question: string; targetLabel: string; inputText: string }
  | { kind: "press_tab"; question: string; targetLabel: string }
  | { kind: "press_key"; question: string; key: "Enter" | "Escape" | "Space" | "ArrowLeft" | "ArrowRight" | "ArrowUp" | "ArrowDown"; expectedText: string }
  | { kind: "scroll_wheel"; question: string; targetLabel: string; direction: "up" | "down" };
export type ChatComputerUseAction = SingleChatComputerUseAction
  | { kind: "sequence"; question: string; steps: SingleChatComputerUseAction[] };
export type ChatComputerUseStartAction = { target: string | null; action: ChatComputerUseAction };

/** Explicit app-scoped goals can bootstrap capture without a separate start command. */
export function parseAppScopedComputerUseGoal(message: string): { target: string; action: ChatComputerUseAction } | null {
  const text = message.trim();
  const prose = text.replace(/["'“‘「『][\s\S]*?["'”’」』]/g, " ");
  if (/https?:\/\/|\b(?:scrape|scraping|crawl|playwright|e2e)\b|スクレイピング|スクレイプ|ローカルE2E/i.test(prose)) return null;
  const match = text.match(/^([\p{L}\p{N} ._-]{1,60}?)(?:アプリ)?で\s*(.+)$/u)
    || text.match(/^(?:in|using)\s+([\p{L}\p{N} ._-]{1,60}?)\s*[,，:]\s*(.+)$/iu);
  if (!match) return null;
  if (/^(?:computer\s+use|コンピュータユーズ|コンピュータ使用|画面操作)$/i.test(match[1].trim())) return null;
  const appSearchGoal = /(?:["“‘「『].{1,120}["”’」』].{0,16}(?:検索|探して|\bsearch\b|\bfind\b)|(?:検索|探して|\bsearch\s+for\b|\bfind\b)\s*(?:欄|ボックス|field|box))/i.test(match[2]);
  const action = parseChatComputerUseAction(match[2])
    || (appSearchGoal ? { kind: "goal" as const, question: match[2] } : null);
  return action ? { target: match[1].trim(), action } : null;
}

function splitExplicitActionSequence(text: string, allowBareComma = false): string[] {
  const parts: string[] = [];
  const openings: Record<string, string> = { "\"": "\"", "'": "'", "“": "”", "‘": "’", "「": "」", "『": "』" };
  let closing: string | null = null;
  let start = 0;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (closing) {
      if (char === closing) closing = null;
      continue;
    }
    if (openings[char]) { closing = openings[char]; continue; }
    let delimiterLength = 0;
    if (char === ";") {
      const tail = text.slice(index + 1).match(/^\s*(?:then\s+)?/i)?.[0] || "";
      delimiterLength = 1 + tail.length;
    } else if (allowBareComma && (char === "," || char === "、")) {
      const tail = text.slice(index + 1).match(/^\s*(?:(?:then|and then)\s+)?/i)?.[0] || "";
      delimiterLength = 1 + tail.length;
    } else if (/\s/.test(char)) {
      const match = text.slice(index).match(/^\s+then\s+/i);
      if (match) delimiterLength = match[0].length;
    }
    if (!delimiterLength) {
      const japanese = text.slice(index).match(/^(?:、|。)(?:次に|続けて|その後)/);
      if (japanese) delimiterLength = japanese[0].length;
    }
    if (delimiterLength) {
      const part = text.slice(start, index).trim();
      if (part) parts.push(part);
      index += delimiterLength - 1;
      start = index + 1;
    }
  }
  const finalPart = text.slice(start).trim();
  if (finalPart) parts.push(finalPart);
  return parts;
}

function firstInstructionSentence(text: string): string {
  const openings: Record<string, string> = { "\"": "\"", "'": "'", "“": "”", "‘": "’", "「": "」", "『": "』" };
  let closing: string | null = null;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (closing) {
      if (char === closing) closing = null;
      continue;
    }
    if (openings[char]) {
      // Do not treat contractions or possessives as the beginning of a quote.
      if (char === "'" && /[\p{L}\p{N}]/u.test(text[index - 1] || "")
          && /[\p{L}\p{N}]/u.test(text[index + 1] || "")) continue;
      closing = openings[char];
      continue;
    }
    if (/[.!。！]/u.test(char) && (index + 1 === text.length || /\s/u.test(text[index + 1]))) {
      return text.slice(0, index + 1).trim();
    }
  }
  return text.trim();
}

/** Resolve a chat request that starts scoped native capture, then acts on that app. */
export function parseChatComputerUseStartAction(message: string): ChatComputerUseStartAction | null {
  const text = String(message || "").trim();
  if (parseComputerUseIntent(text) !== "start") return null;
  const clauses = splitExplicitActionSequence(text, true);
  if (clauses.length < 2) return null;
  for (let index = 0; index < clauses.length - 1; index += 1) {
    const startRequest = clauses[index].trim();
    if (parseComputerUseIntent(startRequest) !== "start") continue;
    const actionText = firstInstructionSentence(clauses.slice(index + 1).join("; then "));
    const action = parseChatComputerUseAction(actionText);
    if (action) return { target: parseComputerUseTarget(startRequest), action };
  }
  return null;
}

/** Resolve explicit native actions; each sequence step must fit a verified action contract. */
export function parseChatComputerUseAction(message: string): ChatComputerUseAction | null {
  const text = String(message || "").trim();
  // Quoted text is often literal data to type into an application. Do not let
  // a value such as "OLCR-E2E-INPUT" route a native action into the generic
  // E2E planner; detect test-workflow intent only in the surrounding prose.
  const unquotedText = text.replace(/["'“‘「『][\s\S]*?["'”’」』]/g, " ");
  if (!text || /https?:\/\//i.test(text)
      || /\b(?:e2e|end[- ]to[- ]end|playwright|local\s+browser)\b|実\s*(?:GUI\s*)?E(?:2E|to\s*E)|ローカルE(?:2E|to\s*E)/i.test(unquotedText)
      || /\b(?:scrape|scraping|crawl)\b|スクレイピング|スクレイプ/i.test(text)) return null;

  const parts = splitExplicitActionSequence(text);
  if (parts.length > 1) {
    if (parts.length > 4) return null;
    const steps = parts.map(parseSingleChatComputerUseAction);
    if (steps.some((step): step is null => step === null)) return null;
    return { kind: "sequence", question: text, steps: steps as SingleChatComputerUseAction[] };
  }
  return parseSingleChatComputerUseAction(text);
}

function parseSingleChatComputerUseAction(text: string): SingleChatComputerUseAction | null {
  if (!text) return null;

  const englishScroll = text.match(/^\s*(?:computer\s+use\s*:\s*)?scroll\s+(up|down)\s+(?:in|within)\s+(?:the\s+)?["'“‘](.{1,120}?)["'”’]\s+(?:area|region|pane)\s*[.!。！]*$/i);
  const japaneseScroll = text.match(/^\s*(?:コンピュータユーズで\s*)?[「『]([^\r\n「」『』]{1,120}?)[」』](?:エリア|領域|欄|パネル)?を(上|下)にスクロール(?:して|する)?\s*[。.!！]*$/i);
  const scrollDirection = (englishScroll?.[1]?.toLowerCase() || japaneseScroll?.[2]) === "up" || japaneseScroll?.[2] === "上" ? "up" : "down";
  const scrollTarget = (englishScroll?.[2] || japaneseScroll?.[1] || "").trim();
  if (scrollTarget && scrollTarget.length <= 120 && !/[\r\n]/.test(scrollTarget)) {
    return { kind: "scroll_wheel", question: text, targetLabel: scrollTarget, direction: scrollDirection };
  }

  const englishTab = text.match(/^\s*(?:computer\s+use\s*:\s*)?(?:press\s+tab\s+to\s+move\s+focus\s+to|tab\s+to\s+focus)\s+["'“‘](.{1,120}?)["'”’]\s+(?:and\s+)?(?:verify|confirm|check)\s+(?:that\s+)?(?:it\s+(?:is|became)\s+focused|it\s+(?:received|has|receives)\s+focus)\s*[.!。！]*$/i);
  const japaneseTab = text.match(/^\s*(?:コンピュータユーズで\s*)?Tab(?:キー)?を押して\s*[「『]([^\r\n「」『』]{1,120}?)[」』](?:にフォーカスが移ったこと|がフォーカスされたこと)を(?:確認|確かめ)(?:して|し|する)?\s*[。.!！]*$/i);
  const tabTarget = (englishTab?.[1] || japaneseTab?.[1] || "").trim();
  if (tabTarget && tabTarget.length <= 120 && !/[\r\n]/.test(tabTarget)) {
    return { kind: "press_tab", question: text, targetLabel: tabTarget };
  }

  const englishKey = text.match(/^\s*(?:computer\s+use\s*:\s*)?press\s+(Enter|Escape|Space|ArrowLeft|ArrowRight|ArrowUp|ArrowDown)\s+(?:key\s+)?and\s+(?:verify|confirm|check)\s+(?:that\s+)?(?:the\s+)?(?:visible\s+)?(?:text\s+)?["'“‘](.{1,240}?)["'”’](?:\s+appears?)?\s*[.!。！]*$/i);
  const japaneseKey = text.match(/^\s*(?:コンピュータユーズで\s*)?(Enter|Escape|Space|ArrowLeft|ArrowRight|ArrowUp|ArrowDown|左矢印|右矢印|上矢印|下矢印|スペース|エスケープ)(?:キー)?を押して\s*[「『](.{1,240}?)[」』](?:が表示されたこと|が現れたこと|を表示したこと)?を?(?:確認|確かめ)(?:して|し|する)?\s*[。.!！]*$/i);
  const keyRaw = englishKey?.[1] || japaneseKey?.[1] || "";
  const keyAliases: Record<string, "Enter" | "Escape" | "Space" | "ArrowLeft" | "ArrowRight" | "ArrowUp" | "ArrowDown"> = {
    enter: "Enter", escape: "Escape", エスケープ: "Escape", space: "Space", スペース: "Space",
    arrowleft: "ArrowLeft", 左矢印: "ArrowLeft", arrowright: "ArrowRight", 右矢印: "ArrowRight",
    arrowup: "ArrowUp", 上矢印: "ArrowUp", arrowdown: "ArrowDown", 下矢印: "ArrowDown",
  };
  const key = keyAliases[keyRaw.toLocaleLowerCase()];
  const expectedKeyText = (englishKey?.[2] || japaneseKey?.[2] || "").trim();
  if (key && expectedKeyText && expectedKeyText.length <= 240 && !/[\r\n]/.test(expectedKeyText)) {
    return { kind: "press_key", question: text, key, expectedText: expectedKeyText };
  }

  const english = text.match(/^\s*(?:computer\s+use\s*:\s*)?(?:open|expand)\s+(?:the\s+)?(?:disclosure|section|group)\s+(?:["'“”](.{1,120}?)["'“”]|(.{1,120}?))\s*[.!。！]*$/i);
  const englishNamedSection = text.match(/^\s*(?:computer\s+use\s*:\s*)?(?:open|expand)\s+(?:the\s+)?(.{1,120}?)\s+(?:disclosure|section|group)\s*[.!。！]*$/i);
  const japaneseQuoted = text.match(/^\s*(?:コンピュータユーズで\s*)?[「『](.{1,120}?)[」』](?:の)?(?:セクション|項目|開示部)?を?(?:開いて|展開して|開く|展開)\s*[。.!！]*$/i);
  const japaneseSection = text.match(/^\s*(?:コンピュータユーズで\s*)?([^\r\n「『]{1,120}?)(?:の)?(?:セクション|項目|開示部)を?(?:開いて|展開して|開く|展開)\s*[。.!！]*$/i);
  const label = (english?.[1] || english?.[2] || englishNamedSection?.[1] || japaneseQuoted?.[1] || japaneseSection?.[1] || "").trim();
  if (label && label.length <= 120 && !/[\r\n]/.test(label)) {
    return { kind: "open_disclosure", question: text };
  }
  const englishTextEntry = text.match(/^\s*(?:computer\s+use\s*:\s*)?(?:type|enter|fill)\s+["'“‘]([\s\S]{1,2048}?)["'”’]\s+into\s+(?:the\s+)?["'“‘]([\s\S]{1,120}?)["'”’](?:\s+(?:text\s+)?field)?\s*[.!。！]*$/i);
  const japaneseTextEntry = text.match(/^\s*(?:コンピュータユーズで\s*)?[「『]([\s\S]{1,2048}?)[」』]を\s*(?:[「『]([^\r\n「」『』]{1,120}?)[」』]|([^\r\n「」『』]{1,120}?))\s*(?:入力欄|テキスト欄|欄|フィールド)に入力して\s*[。.!！]*$/i);
  const englishFillField = text.match(/^\s*(?:computer\s+use\s*:\s*)?fill\s+(?:in\s+)?(?:the\s+)?["'“‘]([^\r\n]{1,120}?)["'”’]\s+(?:text\s+)?field\s+with\s+["'“‘]([\s\S]{1,2048}?)["'”’]\s*[.!。！]*$/i);
  const japaneseTargetFirstTextEntry = text.match(/^\s*(?:コンピュータユーズで\s*)?[「『]([^\r\n「」『』]{1,120}?)[」』](?:入力欄|テキスト欄|欄|フィールド)に\s*[「『]([\s\S]{1,2048}?)[」』](?:と)?入力して\s*[。.!！]*$/i);
  const inputText = (englishTextEntry?.[1] || japaneseTextEntry?.[1]
    || englishFillField?.[2] || japaneseTargetFirstTextEntry?.[2] || "");
  const textTargetLabel = (englishTextEntry?.[2] || japaneseTextEntry?.[2] || japaneseTextEntry?.[3]
    || englishFillField?.[1] || japaneseTargetFirstTextEntry?.[1] || "").trim();
  if (inputText && textTargetLabel && inputText.length <= 2_048
      && textTargetLabel.length <= 120 && !inputText.includes("\0")) {
    return { kind: "type_text", question: text, targetLabel: textTargetLabel, inputText };
  }
  const englishButton = text.match(/^\s*(?:computer\s+use\s*:\s*)?(?:click|press)\s+(?:(?:the\s+)?button\s+(?:labeled|labelled|named)\s+)?["'“‘](.{1,120}?)["'”’]\s+(?:button\s+)?(?:and\s+)?(?:verify|confirm)\s+(?:(?:that\s+)?(?:(?:the\s+)?(?:visible\s+)?(?:status|text)\s+(?:changes?\s+to|becomes)|(?:the\s+)?(?:screen|window)\s+(?:shows|displays|contains))\s+)?["'“‘](.{1,240}?)["'”’]\s*[.!。！]*$/i);
  const japaneseButtonOutcome = text.match(/^\s*[「『](.{1,120}?)[」』](?:ボタン)?を(?:クリックして|クリックし|押して|押し)(?:、|,)\s*(?:画面に)?[「『]?(.{1,240}?)[」』]?\s*(?:と|が)(?:表示される(?:まで)?|表示された(?:ことを|のを))(?:\s*(?:確認(?:して|し|する)?|確かめ(?:て|る)?|進めて(?:ください)?))?\s*[。.!！]*$/i);
  const japaneseButton = text.match(/^\s*(?:コンピュータユーズで\s*)?[「『](.{1,120}?)[」』](?:ボタン)?を(?:クリックして|クリックし|押して|押し)(?:から)?\s*[「『](.{1,240}?)[」』](?:が表示される(?:こと)?を|の表示を|を)?(?:確認(?:して|し|する)?|確かめ(?:て|る)?)\s*[。.!！]*$/i);
  const targetLabel = (englishButton?.[1] || japaneseButtonOutcome?.[1] || japaneseButton?.[1] || "").trim();
  const expectedText = (englishButton?.[2] || japaneseButtonOutcome?.[2] || japaneseButton?.[2] || "").trim();
  if (targetLabel && expectedText && targetLabel.length <= 120 && expectedText.length <= 240
      && !/[\r\n]/.test(targetLabel + expectedText)) {
    return { kind: "press_button", question: text, targetLabel, expectedText };
  }
  // Let the local goal planner choose a currently visible AX button and
  // derive an observable postcondition from the user's requested outcome.
  const englishOutcome = text.match(/^\s*(?:computer\s+use\s*:\s*)?(?:start|begin|launch|open|play|continue|submit)\s+.{1,160}?\s+(?:and\s+)?(?:verify|confirm|check)\s+(?:that\s+)?["'“‘](.{1,240}?)["'”’]\s*[.!。！]*$/i);
  const japaneseOutcome = text.match(/^\s*(?:コンピュータユーズで\s*)?.{1,120}?(?:を|に)(?:開始|起動|開いて|始めて|プレイして|続けて|送信して).{0,100}?[「『](.{1,240}?)[」』](?:が表示される(?:こと)?を|の表示を|を)?(?:確認(?:して|し|する)?|確かめ(?:て|る)?)\s*[。.!！]*$/i);
  const outcomeText = (englishOutcome?.[1] || japaneseOutcome?.[1] || "").trim();
  if (outcomeText && outcomeText.length <= 240 && !/[\r\n]/.test(outcomeText)) {
    return { kind: "press_button", question: text, expectedText: outcomeText };
  }
  const openFind = /^\s*(?:computer\s+use\s*:\s*)?(?:open|show)\s+(?:the\s+)?(?:find|search)(?:\s+(?:search\s+)?(?:field|bar|box))?\s*[.!。！]*$/i.test(text)
    || /^\s*(?:コンピュータユーズで\s*)?(?:検索(?:欄|バー|ボックス)|(?:Find|Search)(?:欄|バー|ボックス))(?:を)?(?:開いて|表示して|出して|開く|表示)\s*[。.!！]*$/i.test(text);
  if (openFind) return { kind: "open_find", question: text };
  if (/入力[\s\S]{0,100}(?:検索|確認)|(?:検索|探して)[\s\S]{0,100}(?:結果|確認)|\b(?:search|find)\b[\s\S]{0,100}\b(?:results?|verify|confirm)\b/i.test(text)) return { kind: "goal", question: text };
  const englishGoal = /^\s*(?:computer\s+use\s*:\s*)?(?:open|start|begin|launch|play|continue|select|choose|turn\s+on|navigate\s+to)\s+(?!computer\s+use\b).{1,200}?[.!。！]?\s*$/i.test(text);
  const japaneseGoal = /^\s*(?:コンピュータユーズで\s*)?.{1,140}?(?:を|に)(?:開いて|開始して|始めて|起動して|プレイして|続けて|選択して|オンにして|有効にして)[。.!！]*\s*$/i.test(text)
    && !/(?:コンピュータユーズ|コンピュータ使用|画面操作)(?:を)?(?:開始|起動|始めて|有効にして)/i.test(text);
  return englishGoal || japaneseGoal ? { kind: "goal", question: text } : null;
}

function assertObservationIdentity(observation: any, target: NativeTarget) {
  if (observation?.source !== "AX" || observation?.permissionGranted !== true
      || observation?.applicationPid !== target.applicationPid
      || observation?.applicationBundleId !== target.applicationBundleId
      || typeof observation?.observationId !== "string" || !observation.observationId
      || typeof observation?.sessionId !== "string" || !observation.sessionId
      || !Array.isArray(observation?.elements)) {
    throw new Error("COMPUTER_USE_AX_TARGET_OR_PERMISSION_MISMATCH");
  }
}

/** Run a bounded sequence by re-observing and verifying every step in the existing runtime. */
export async function executeChatComputerUseAction({
  action, enabled, projectId, conversationId, selectedTarget, bridge, api, onStepProgress,
  groundVisualTarget, recordUserMessage = true, mixedGoals = false, signal,
}: {
  action: ChatComputerUseAction;
  enabled: boolean;
  projectId: string | null;
  conversationId: string;
  selectedTarget: NativeTarget | null;
  bridge: Bridge;
  api: Api;
  onStepProgress?: (step: number, total: number) => void;
  groundVisualTarget?: (label: string) => Promise<VisionGrounding>;
  /** False when the initiating chat message already contains this action. */
  recordUserMessage?: boolean;
  mixedGoals?: boolean;
  signal?: AbortSignal;
}): Promise<{ message: string; conversationId: string }> {
  signal?.throwIfAborted();
  if (action.kind === "goal" && mixedGoals) {
    return executeAutonomousGoal({ action, enabled, projectId, conversationId, selectedTarget, bridge, api,
      groundVisualTarget, recordUserMessage, onStepProgress, signal });
  }
  if (action.kind === "sequence") {
    if (action.steps.length < 2 || action.steps.length > 4) throw new Error("COMPUTER_USE_SEQUENCE_LENGTH_INVALID");
    const results: string[] = [];
    let activeConversationId = conversationId;
    for (let index = 0; index < action.steps.length; index += 1) {
      onStepProgress?.(index + 1, action.steps.length);
      try {
        const result = await executeSingleChatComputerUseAction({
        action: action.steps[index], enabled, projectId, conversationId: activeConversationId,
          selectedTarget, bridge, api, recordUserMessage, groundVisualTarget, signal,
        });
        results.push(result.message);
        activeConversationId = result.conversationId;
      } catch (error: any) {
        const reason = error?.message || String(error);
        throw new Error(`COMPUTER_USE_SEQUENCE_STEP_FAILED:${index + 1}/${action.steps.length}:verified_steps=${results.length}:${reason}`);
      }
    }
    return { message: results.join("\n"), conversationId: activeConversationId };
  }
  if (action.kind === "goal") {
    let activeConversationId = conversationId;
    const verifiedEffects: string[] = [];
    const verifiedMessages: string[] = [];
    for (let goalStepIndex = 0; goalStepIndex <= 4; goalStepIndex += 1) {
      onStepProgress?.(Math.min(goalStepIndex + 1, 4), 4);
      const result = await executeSingleChatComputerUseAction({
        action, enabled, projectId, conversationId: activeConversationId, selectedTarget, bridge, api,
        recordUserMessage: recordUserMessage && goalStepIndex === 0, groundVisualTarget,
        goalStepIndex, verifiedEffects, signal,
      });
      activeConversationId = result.conversationId;
      if (result.goalComplete || result.clarification) {
        return { message: [...verifiedMessages, result.message].filter(Boolean).join("\n"),
          conversationId: activeConversationId };
      }
      verifiedEffects.push(result.message);
      verifiedMessages.push(result.message);
    }
    return { message: verifiedMessages.join("\n"), conversationId: activeConversationId };
  }
  return executeSingleChatComputerUseAction({ action, enabled, projectId, conversationId, selectedTarget, bridge, api, recordUserMessage, groundVisualTarget, signal });
}

/** Run one supported native action through the existing plan, native executor and verifier. */
async function executeSingleChatComputerUseAction({
  action, enabled, projectId, conversationId, selectedTarget, bridge, api, recordUserMessage,
  goalStepIndex = 0, verifiedEffects = [], groundVisualTarget, signal, deadline,
}: {
  action: SingleChatComputerUseAction;
  enabled: boolean;
  projectId: string | null;
  conversationId: string;
  selectedTarget: NativeTarget | null;
  bridge: Bridge;
  api: Api;
  recordUserMessage: boolean;
  groundVisualTarget?: (label: string) => Promise<VisionGrounding>;
  signal?: AbortSignal;
  deadline?: number;
  goalStepIndex?: number;
  verifiedEffects?: string[];
}): Promise<{ message: string; conversationId: string; goalComplete?: boolean; clarification?: boolean }> {
  signal?.throwIfAborted();
  if (!enabled) throw new Error("COMPUTER_USE_DISABLED");
  const capture = await bridge.core.invoke("screen_capture_status");
  if (capture?.running !== true || !capture?.captureSessionId) throw new Error("COMPUTER_USE_SESSION_NOT_RUNNING");
  const target = capture.targetApplication || selectedTarget;
  if (!target || !Number.isInteger(target.applicationPid) || target.applicationPid <= 0
      || typeof target.applicationBundleId !== "string" || !target.applicationBundleId.trim()
      || (selectedTarget && (selectedTarget.applicationPid !== target.applicationPid
        || selectedTarget.applicationBundleId !== target.applicationBundleId))) {
    throw new Error("COMPUTER_USE_TARGET_IDENTITY_MISMATCH");
  }

  let before = await bridge.core.invoke("observe_accessibility_application", {
    applicationPid: target.applicationPid,
    includeMenuBar: action.kind === "open_find",
  });
  assertObservationIdentity(before, target);
  await api("/desktop/world-state", { method: "PUT", body: JSON.stringify({ observation: before }) });
  const pointerPermission = await bridge.core.invoke("pointer_event_permission_status");
  const planPath = action.kind === "open_find" ? "/computer/actions/shortcut/open-find/plan"
    : action.kind === "open_disclosure" ? "/computer/actions/open-disclosure/plan"
      : action.kind === "type_text" ? "/computer/actions/text/plan"
        : action.kind === "press_key" ? "/computer/actions/keyboard/key/plan"
        : action.kind === "press_tab" ? "/computer/actions/keyboard/tab/plan"
          : action.kind === "scroll_wheel" ? "/computer/actions/scroll/plan" : "/computer/actions/button/plan";
  const verifyPath = action.kind === "open_find" ? "/computer/actions/shortcut/open-find/verify"
    : action.kind === "open_disclosure" ? "/computer/actions/open-disclosure/verify"
      : action.kind === "type_text" ? "/computer/actions/text/verify"
        : action.kind === "press_key" ? "/computer/actions/keyboard/key/verify"
        : action.kind === "press_tab" ? "/computer/actions/keyboard/tab/verify"
          : action.kind === "scroll_wheel" ? "/computer/actions/scroll/verify" : "/computer/actions/button/verify";
  const expectedMechanism = action.kind === "open_find" ? "keyboard_shortcut"
    : action.kind === "press_tab" ? "keyboard_shortcut"
      : action.kind === "press_key" ? "deterministic_input"
      : action.kind === "scroll_wheel" ? "deterministic_input"
      : action.kind === "open_disclosure" ? null : "accessibility";
  let plannedButtonTarget = action.kind === "press_button" ? action.targetLabel : undefined;
  let plannedExpectedText = action.kind === "press_button" ? action.expectedText : undefined;
  const postPlan = (visionPointer?: VisionGrounding) => api(planPath, {
    method: "POST",
    body: JSON.stringify({
      question: action.question,
      project_id: projectId,
      conversation_id: conversationId,
      ...((action.kind === "press_button" || plannedButtonTarget) ? {
        ...(plannedButtonTarget ? { target_label: plannedButtonTarget } : {}),
        ...(plannedExpectedText ? { expected_text: plannedExpectedText } : {}),
      } : {}),
      ...(action.kind === "type_text" ? {
        target_label: action.targetLabel,
        input_text: action.inputText,
      } : {}),
      ...(action.kind === "press_tab" ? { target_label: action.targetLabel } : {}),
      ...(action.kind === "press_key" ? { key: action.key, expected_text: action.expectedText } : {}),
      ...(action.kind === "scroll_wheel" ? { target_label: action.targetLabel,
        direction: action.direction } : {}),
      ...(!recordUserMessage ? { record_user_message: false } : {}),
      ...(action.kind === "goal" ? { goal_step_index: goalStepIndex, verified_effects: verifiedEffects } : {}),
      observation: { ax: before, pointer_event_permission_granted: pointerPermission === true,
        ...(visionPointer ? { vision_pointer: visionPointer } : {}) },
    }),
  });
  let plan = await postPlan();
  let visionGrounding: VisionGrounding | undefined;
  if (plan?.status === "VISION_GROUNDING_REQUIRED") {
    const label = String(plan?.selected_element?.label || "").trim();
    plannedButtonTarget = label || plannedButtonTarget;
    plannedExpectedText = String(plan?.expected_text_contains || plannedExpectedText || "").trim();
    if (!label || !plannedExpectedText || !groundVisualTarget) {
      throw new Error("COMPUTER_USE_VISION_GROUNDING_UNAVAILABLE");
    }
    visionGrounding = await groundVisualTarget(label);
    if (visionGrounding.candidate.label.trim().toLocaleLowerCase()
          !== label.toLocaleLowerCase()
        || visionGrounding.candidate.confidence < 0.65
        || visionGrounding.alignment?.status !== "READY"
        || visionGrounding.alignment?.targetWindowContainsPoint !== true) {
      throw new Error("COMPUTER_USE_VISION_TARGET_MISMATCH");
    }
    // Grounding and two-frame tracking can take longer than the native
    // executor's 2.5 s AX snapshot lifetime. Refresh the exact target after
    // vision work, then bind the final route to that fresh observation.
    before = await bridge.core.invoke("observe_accessibility_application", {
      applicationPid: target.applicationPid,
      includeMenuBar: action.kind === "open_find",
    });
    assertObservationIdentity(before, target);
    plan = await postPlan(visionGrounding);
  }
  const outcomeDrivenAction = action.kind === "goal"
    || (action.kind === "press_button" && !action.targetLabel);
  if (outcomeDrivenAction && plan?.status === "COMPLETE" && plan?.verified === true
      && typeof plan?.completion_text === "string" && plan.completion_text.trim()) {
    return { message: String(plan.message || `Verified completion evidence: ${plan.completion_text}`),
      conversationId: String(plan.conversation_id || conversationId), goalComplete: true };
  }
  if (outcomeDrivenAction && plan?.status === "CLARIFY") {
    return { message: String(plan.clarification || "Please clarify the outcome I should verify."),
      conversationId: String(plan.conversation_id || conversationId), clarification: true };
  }
  const route = plan?.action_route;
  if (plan?.status !== "PASS" || route?.status !== "PASS" || route?.verification_required !== true
      || route.observation_id !== before.observationId || route.session_id !== before.sessionId
      || (expectedMechanism ? (action.kind === "press_button" || action.kind === "goal"
          ? !["accessibility", "vision_pointer"].includes(route.mechanism)
          : route.mechanism !== expectedMechanism)
        : !["accessibility", "deterministic_input"].includes(route.mechanism))
      || !plan.selected_element?.id || !plan.selected_element?.label
      || (action.kind === "press_button" && action.targetLabel
        && plan.selected_element.label.localeCompare(action.targetLabel, undefined, { sensitivity: "accent" }) !== 0)
      || (action.kind === "type_text"
        && plan.selected_element.label.localeCompare(action.targetLabel, undefined, { sensitivity: "accent" }) !== 0)
      || (action.kind === "press_tab"
        && plan.selected_element.label.localeCompare(action.targetLabel, undefined, { sensitivity: "accent" }) !== 0)
      || (action.kind === "scroll_wheel"
        && plan.selected_element.label.localeCompare(action.targetLabel, undefined, { sensitivity: "accent" }) !== 0)) {
    throw new Error(`COMPUTER_USE_ACTION_PLAN_REJECTED:${plan?.reason || route?.reason || "ROUTE_MISMATCH"}`);
  }

  const element = plan.selected_element;
  await bridge.core.invoke("signal_perception", { signal: "action_trigger" })
    .then(persistPerceptionPolicy).catch(() => false);
  const command = action.kind === "open_find" ? "activate_accessibility_find_shortcut"
    : action.kind === "press_tab" ? "press_accessibility_tab"
    : action.kind === "press_key" ? "press_accessibility_key"
    : action.kind === "scroll_wheel" ? "scroll_accessibility_area"
    : route.mechanism === "vision_pointer" ? "click_vision_grounded_button"
    : action.kind === "type_text" ? "enter_accessibility_text"
    : (action.kind === "press_button" || action.kind === "goal") ? "press_accessibility_button"
    : route.mechanism === "accessibility" ? "press_accessibility_disclosure" : "click_accessibility_disclosure";
  const invokeArgs: Record<string, unknown> = {
    observationId: before.observationId,
    sessionId: before.sessionId,
    applicationPid: target.applicationPid,
    elementId: element.id,
    label: element.label,
  };
  if (action.kind === "type_text") Object.assign(invokeArgs, { inputText: action.inputText });
  if (action.kind === "press_key") Object.assign(invokeArgs, { key: action.key });
  if (action.kind === "press_tab") {
    const focused = plan.current_focus_element;
    if (!focused?.id) throw new Error("COMPUTER_USE_CURRENT_FOCUS_PLAN_MISSING");
    Object.assign(invokeArgs, {
      currentFocusElementId: focused.id,
      currentFocusLabel: focused.label || "",
      targetElementId: element.id,
      targetLabel: element.label,
    });
  }
  if (action.kind === "scroll_wheel") {
    Object.assign(invokeArgs, {
      areaElementId: element.id,
      areaLabel: element.label,
      scrollbarElementId: String(element.scrollbar_id || ""),
      beforeScrollValue: Number(plan.scroll_value),
      direction: action.direction,
    });
  }
  if (route.mechanism === "vision_pointer") {
    if (!visionGrounding) throw new Error("COMPUTER_USE_VISION_GROUNDING_REQUIRED");
    Object.assign(invokeArgs, {
      captureSessionId: visionGrounding.provenance.capture_session_id,
      seedSequence: visionGrounding.provenance.frame_sequence,
      seedBboxNormalized: visionGrounding.candidate.bbox,
      candidateConfidence: visionGrounding.candidate.confidence,
      targetApplication: visionGrounding.provenance.target_application,
      candidateLabel: visionGrounding.candidate.label,
    });
  }
  signal?.throwIfAborted();
  if (deadline && Date.now() >= deadline) throw new Error("COMPUTER_USE_TIME_BUDGET_EXCEEDED");
  const currentCapture = await bridge.core.invoke("screen_capture_status");
  if (!currentCapture?.running || currentCapture.captureSessionId !== capture.captureSessionId
      || currentCapture.targetApplication?.applicationPid !== target.applicationPid
      || currentCapture.targetApplication?.applicationBundleId !== target.applicationBundleId) {
    throw new Error("COMPUTER_USE_SESSION_CHANGED_BEFORE_ACTION");
  }
  signal?.throwIfAborted();
  if (deadline && Date.now() >= deadline) throw new Error("COMPUTER_USE_TIME_BUDGET_EXCEEDED");
  const dispatch = await bridge.core.invoke(command, invokeArgs);
  const result = route.mechanism === "vision_pointer" ? dispatch?.action : dispatch;
  const after = result?.observation;
  if (result?.executionAttempted !== true) throw new Error("COMPUTER_USE_ACTION_NOT_EXECUTED");
  assertObservationIdentity(after, target);

  const verification = await api(verifyPath, {
    method: "POST",
    body: JSON.stringify({
      plan_id: plan.plan_id,
      observation: { ax: after },
      execution_receipt: {
        source: "native_accessibility", action_kind: action.kind === "goal" ? "press_button" : action.kind,
        status: result.status, execution_attempted: result.executionAttempted === true,
        ...(action.kind === "type_text" ? { action: "AXSetValue", element_id: element.id,
          value_matched: result.valueMatched === true, verified: result.verified === true } : {}),
        ...(action.kind === "press_key" ? { action: "CGEventKeyboard", key: action.key,
          element_id: element.id } : {}),
        ...(action.kind === "scroll_wheel" ? { action: "CGEventScrollWheel",
          direction: result.direction, element_id: element.id,
          scrollbar_element_id: String(element.scrollbar_id || "") } : {}),
        ...(route.mechanism === "vision_pointer" ? {
          action: result.action,
          alignment: dispatch.alignment,
        } : {}),
      },
    }),
  });
  await api("/desktop/world-state", { method: "PUT", body: JSON.stringify({ observation: after }) });
  if (!verification?.verified || verification?.status !== "PASS") {
    throw new Error(`COMPUTER_USE_EFFECT_NOT_VERIFIED:${verification?.reason || verification?.verification_state || result?.status || "UNKNOWN"}`);
  }

  await bridge.core.invoke("signal_perception", { signal: "expected_effect" })
    .then(persistPerceptionPolicy).catch(() => false);
  return { message: String(verification.message || (action.kind === "type_text"
    ? `Entered text in ${element.label}; the field value was verified.`
    : action.kind === "press_key" ? ("Pressed " + action.key + "; the requested text appeared.")
    : action.kind === "press_tab" ? `Moved keyboard focus to ${element.label}; the new focus was verified.`
    : action.kind === "scroll_wheel" ? `Scrolled ${action.direction} in ${element.label}; the scroll position changed.`
    : action.kind === "press_button" || action.kind === "goal"
    ? `Pressed ${element.label}; the requested text appeared.`
    : `Opened ${element.label}; the new accessibility state confirmed it.`)),
    conversationId: String(verification.conversation_id || conversationId) };
}

/** A mixed goal chooses one existing executor at a time; no unverified action is retried. */
async function executeAutonomousGoal(options: {
  action: { kind: "goal"; question: string }; enabled: boolean; projectId: string | null;
  conversationId: string; selectedTarget: NativeTarget | null; bridge: Bridge; api: Api;
  groundVisualTarget?: (label: string) => Promise<VisionGrounding>;
  recordUserMessage: boolean; onStepProgress?: (step: number, total: number) => void; signal?: AbortSignal;
}): Promise<{ message: string; conversationId: string }> {
  if (!options.enabled) throw new Error("COMPUTER_USE_DISABLED");
  const { bridge, api } = options;
  let signal = options.signal;
  const effects: string[] = [];
  const deadline = Date.now() + 300_000;
  let waits = 0;
  let recorded = false;
  let observationRecoveries = 0;
  let previousSignature = "";
  const initialCapture = await bridge.core.invoke("screen_capture_status");
  const initialTarget = initialCapture?.targetApplication || options.selectedTarget;
  if (!initialCapture?.running || !initialCapture.captureSessionId || !initialTarget) {
    throw new Error("COMPUTER_USE_SESSION_NOT_RUNNING");
  }
  const initialAx = await bridge.core.invoke("observe_accessibility_application", {
    applicationPid: initialTarget.applicationPid, includeMenuBar: true,
  });
  assertObservationIdentity(initialAx, initialTarget);
  const created = await api("/computer/goals", { method: "POST", signal,
    body: JSON.stringify({ question: options.action.question, project_id: options.projectId,
      conversation_id: options.conversationId, capture_session_id: initialCapture.captureSessionId,
      observation: { ax: initialAx } }) });
  const taskId = String(created?.task_id || "");
  if (!taskId) throw new Error("COMPUTER_USE_GOAL_CREATE_FAILED");
  const budgetController = new AbortController();
  const parentSignal = signal;
  const onParentAbort = () => budgetController.abort(parentSignal?.reason);
  parentSignal?.addEventListener("abort", onParentAbort, { once: true });
  if (parentSignal?.aborted) onParentAbort();
  const budgetTimer = setTimeout(() => budgetController.abort(
    new Error("COMPUTER_USE_TIME_BUDGET_EXCEEDED")), Math.max(0, deadline - Date.now()));
  signal = budgetController.signal;
  try {
  while (Date.now() < deadline) {
    signal?.throwIfAborted();
    const capture = await bridge.core.invoke("screen_capture_status");
    if (!capture?.running || capture.captureSessionId !== initialCapture.captureSessionId
        || capture.targetApplication?.applicationPid !== initialTarget.applicationPid
        || capture.targetApplication?.applicationBundleId !== initialTarget.applicationBundleId) {
      throw new Error("COMPUTER_USE_TARGET_IDENTITY_MISMATCH");
    }
    const ax = await bridge.core.invoke("observe_accessibility_application", {
      applicationPid: initialTarget.applicationPid, includeMenuBar: true,
    });
    assertObservationIdentity(ax, initialTarget);
    options.onStepProgress?.(Math.min(effects.length + 1, 4), 4);
    let next;
    try {
    next = await api("/computer/actions/goal/next", { method: "POST", signal,
      body: JSON.stringify({ question: options.action.question, project_id: options.projectId,
        conversation_id: options.conversationId, observation: { ax },
        goal_task_id: taskId, goal_step_index: effects.length, verified_effects: effects,
        record_user_message: options.recordUserMessage && !recorded }) });
    if (next?.status === "REOBSERVE" && next.proposal_id) {
      signal?.throwIfAborted();
      if (Date.now() >= deadline) throw new Error("COMPUTER_USE_TIME_BUDGET_EXCEEDED");
      const freshCapture = await bridge.core.invoke("screen_capture_status");
      if (!freshCapture?.running || freshCapture.captureSessionId !== initialCapture.captureSessionId
          || freshCapture.targetApplication?.applicationPid !== initialTarget.applicationPid
          || freshCapture.targetApplication?.applicationBundleId !== initialTarget.applicationBundleId) {
        throw new Error("COMPUTER_USE_TARGET_IDENTITY_MISMATCH");
      }
      const freshAx = await bridge.core.invoke("observe_accessibility_application", {
        applicationPid: initialTarget.applicationPid, includeMenuBar: true,
      });
      assertObservationIdentity(freshAx, initialTarget);
      next = await api("/computer/actions/goal/validate", { method: "POST", signal,
        body: JSON.stringify({ proposal_id: next.proposal_id, question: options.action.question,
          project_id: options.projectId, conversation_id: options.conversationId,
          goal_task_id: taskId, observation: { ax: freshAx } }) });
    }
    recorded = true;
    } catch (error: any) {
      // Only a rejected observation is retried: no native action has run yet.
      if (/NATIVE_GOAL_OBSERVATION_STALE/.test(String(error?.message)) && observationRecoveries++ < 2) continue;
      if (signal.aborted) signal.throwIfAborted();
      throw error;
    }
    signal?.throwIfAborted();
    if (Date.now() >= deadline) throw new Error("COMPUTER_USE_TIME_BUDGET_EXCEEDED");
    if (next?.status === "COMPLETE" && next.verified === true && next.completion_text) {
      return { message: [...effects, next.message].join("\n"), conversationId: options.conversationId };
    }
    if (next?.status === "CLARIFY") {
      return { message: [...effects, next.clarification].join("\n"), conversationId: options.conversationId };
    }
    if (next?.status === "WAIT") {
      if (++waits > 40) throw new Error("COMPUTER_USE_WAIT_BUDGET_EXCEEDED");
      await new Promise<void>((resolve, reject) => {
        const onAbort = () => { clearTimeout(timer); reject(signal?.reason || new Error("COMPUTER_USE_CANCELLED")); };
        const timer = setTimeout(() => { signal?.removeEventListener("abort", onAbort); resolve(); }, 500);
        signal?.addEventListener("abort", onAbort, { once: true });
        if (signal?.aborted) onAbort();
      });
      continue;
    }
    const chosen = next?.action as SingleChatComputerUseAction;
    if (next?.status !== "NEXT_ACTION" || !chosen || chosen.kind === "goal"
        || !["press_button", "type_text", "press_key", "press_tab", "scroll_wheel", "open_disclosure", "open_find"].includes(chosen.kind)) {
      throw new Error("COMPUTER_USE_GOAL_PLAN_INVALID");
    }
    if (effects.length >= 4) throw new Error("COMPUTER_USE_ACTION_BUDGET_EXCEEDED");
    const signature = JSON.stringify([chosen, ax.text, ax.elements?.map((e: any) => [e.role, e.label, e.focused, e.expanded, e.enabled])]);
    if (signature === previousSignature) throw new Error("COMPUTER_USE_NO_PROGRESS");
    previousSignature = signature;
    const result = await executeSingleChatComputerUseAction({ ...options,
      action: chosen, selectedTarget: initialTarget, recordUserMessage: false, deadline, signal });
    effects.push(result.message);
  }
  throw new Error("COMPUTER_USE_TIME_BUDGET_EXCEEDED");
  } catch (error) {
    // Keep the interruption in the existing task store without logging input values.
    await api(`/computer/goals/${taskId}`, { method: "PATCH",
      body: JSON.stringify({ conversation_id: options.conversationId,
        state: parentSignal?.aborted ? "cancelled" : "failed" }) }).catch(() => undefined);
    throw error;
  } finally {
    clearTimeout(budgetTimer);
    parentSignal?.removeEventListener("abort", onParentAbort);
  }
}
