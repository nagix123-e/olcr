import { describe, expect, it } from "vitest";
import { isLatestScrapeTaskBlock } from "./scrapeTaskVisibility";

describe("isLatestScrapeTaskBlock", () => {
  it("renders only the newest embedded scraper for a reused browser session", () => {
    const messages = [
      { blocks: [{ type: "scrape_task", session_id: "same" }] },
      { blocks: [{ type: "scrape_task", session_id: "other" }] },
      { blocks: [{ type: "scrape_task", session_id: "same" }] },
    ];
    expect(isLatestScrapeTaskBlock(messages, 0, 0, "same")).toBe(false);
    expect(isLatestScrapeTaskBlock(messages, 1, 0, "other")).toBe(true);
    expect(isLatestScrapeTaskBlock(messages, 2, 0, "same")).toBe(true);
  });

  it("keeps the newest of multiple scraper blocks in one message", () => {
    const messages = [{ blocks: [
      { type: "scrape_task", session_id: "same" },
      { type: "scrape_task", session_id: "same" },
    ] }];
    expect(isLatestScrapeTaskBlock(messages, 0, 0, "same")).toBe(false);
    expect(isLatestScrapeTaskBlock(messages, 0, 1, "same")).toBe(true);
  });
});
