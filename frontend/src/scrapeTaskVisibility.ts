export type ScrapeTaskMessage = {
  blocks?: Array<{ type?: string; session_id?: string }>;
};

export function isLatestScrapeTaskBlock(
  messages: ScrapeTaskMessage[],
  messageIndex: number,
  blockIndex: number,
  sessionId: string,
): boolean {
  for (let message = messageIndex; message < messages.length; message += 1) {
    const firstBlock = message === messageIndex ? blockIndex + 1 : 0;
    for (let block = firstBlock; block < (messages[message].blocks?.length || 0); block += 1) {
      const candidate = messages[message].blocks?.[block];
      if (candidate?.type === "scrape_task" && candidate.session_id === sessionId) return false;
    }
  }
  return true;
}
