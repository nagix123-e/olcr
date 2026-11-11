import { describe, expect, it } from 'vitest';
import { isChatCapabilityRequest } from './chatRouteIntent';

describe('isChatCapabilityRequest', () => {
  it('sends scraping and local E2E lifecycle requests through the structured chat route', () => {
    expect(isChatCapabilityRequest('Scrape this URL: https://example.test/catalog')).toBe(true);
    expect(isChatCapabilityRequest('このページをスクレイピングして https://example.test/catalog')).toBe(true);
    expect(isChatCapabilityRequest('Start local E2E')).toBe(true);
    expect(isChatCapabilityRequest('computer use ONの状態で実E2Eを開始して')).toBe(true);
    expect(isChatCapabilityRequest('Computer Useで自分のサイトのE2Eを開始して')).toBe(true);
    expect(isChatCapabilityRequest('ローカルE2Eを停止して')).toBe(true);
  });

  it('routes visible browser actions from chat and leaves ordinary questions streamable', () => {
    expect(isChatCapabilityRequest('「Player name」に「Alice」を入力してから「Start game」ボタンをクリックして')).toBe(true);
    expect(isChatCapabilityRequest('click "Start game" then click "Continue"')).toBe(true);
    expect(isChatCapabilityRequest('What is E2E testing?')).toBe(false);
    expect(isChatCapabilityRequest('Tell me what web scraping means')).toBe(false);
  });

  it('routes short lifecycle controls through chat only when a matching task is present', () => {
    expect(isChatCapabilityRequest('show results', { hasScrapeTask: true })).toBe(true);
    expect(isChatCapabilityRequest('show results')).toBe(false);
    expect(isChatCapabilityRequest('ローカルE2Eの状態を確認して', { hasLocalE2ETask: true })).toBe(true);
  });

  it('routes a free-form answer into chat only while scrape clarification is pending', () => {
    expect(isChatCapabilityRequest('商品名と価格だけを集めて', { hasPendingScrapeClarification: true })).toBe(true);
    expect(isChatCapabilityRequest('商品名と価格だけを集めて', { hasScrapeTask: true })).toBe(false);
  });
});
