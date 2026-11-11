import { describe, expect, it } from "vitest";
import { RequestFence } from "./requestFence";

describe("RequestFence", () => {
  it("rejects an old conversation poll after navigation to a new conversation", () => {
    const fence = new RequestFence();
    const oldConversationPoll = fence.begin();

    const newConversationSelection = fence.begin();

    expect(fence.isCurrent(oldConversationPoll)).toBe(false);
    expect(fence.isCurrent(newConversationSelection)).toBe(true);
  });

  it("keeps only the newest overlapping history refresh", () => {
    const fence = new RequestFence();
    const slowRefresh = fence.begin();
    const fastRefresh = fence.begin();

    expect(fence.isCurrent(slowRefresh)).toBe(false);
    expect(fence.isCurrent(fastRefresh)).toBe(true);
  });

  it("invalidates in-flight loads when the polling effect is disposed", () => {
    const fence = new RequestFence();
    const inFlight = fence.begin();
    fence.invalidate();

    expect(fence.isCurrent(inFlight)).toBe(false);
  });

  it("lets a poll retain its owner snapshot while unrelated poll requests advance", () => {
    const selection = new RequestFence();
    const polls = new RequestFence();
    const owner = selection.snapshot();
    const firstPoll = polls.begin();
    const secondPoll = polls.begin();

    expect(selection.isCurrent(owner)).toBe(true);
    expect(polls.isCurrent(firstPoll)).toBe(false);
    expect(polls.isCurrent(secondPoll)).toBe(true);

    selection.begin();
    expect(selection.isCurrent(owner)).toBe(false);
  });
});
