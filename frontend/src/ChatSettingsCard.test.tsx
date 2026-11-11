import {act, cleanup, fireEvent, render, screen} from "@testing-library/react";
import {afterEach, describe, expect, it, vi} from "vitest";
import {ChatSettingsCard} from "./ChatSettingsCard";
vi.mock("./AccessibilityPanel", () => ({AccessibilityPanel: () =>
  <p>Chat-directed app access</p>}));
vi.mock("./WebSettings", () => ({WebSettings: () => <p>Keychain configuration</p>}));
vi.mock("./ExternalToolsSettings", () => ({ExternalToolsSettings: ({settings, save}: any) =>
  <button onClick={() => void save({web_scraping_enabled: !settings.web_scraping_enabled})}>
    Scraping {settings.web_scraping_enabled ? "ON" : "OFF"}</button>}));
afterEach(cleanup);
describe("chat settings", () => {
  it("keeps task, form and tool execution in chat while retaining settings", async () => {
    const api = vi.fn(async (path: string) => path === '/github/connection'
      ? {installed: true, connected: false, execution_mode: 'review'} : {});
    render(<ChatSettingsCard api={api}/>);
    expect(await screen.findByRole('button', {name: /サービス接続|Service connections/})).toBeInTheDocument();
    expect(screen.queryByRole('button', {name: /個人タスク・目標・プロフィール|Personal tasks, goals and profile/})).not.toBeInTheDocument();
    expect(screen.queryByRole('button', {name: /Webフォームの入力|Web form drafts/})).not.toBeInTheDocument();
    expect(screen.queryByRole('button', {name: /再利用できるツール|Reusable tools/})).not.toBeInTheDocument();
    expect(screen.getByText(/タスク・目標・フォーム入力・ツール作成はチャットで依頼|Ask in chat to manage tasks and goals, fill forms, or create tools/)).toBeInTheDocument();
  });
  it("opens service connections and returns with the common settings back control",async()=>{
    const api=vi.fn(async(path:string)=>path==='/connectors'?{connectors:[],categories:[]}:path==='/github/connection'?{installed:true,connected:false,execution_mode:'review'}:{});
    render(<ChatSettingsCard api={api}/>);
    fireEvent.click(await screen.findByRole('button',{name:/サービス接続|Service connections/}));
    expect(await screen.findByRole('region',{name:/サービス接続設定|Service connection settings/})).toBeInTheDocument();
    const back=screen.getByRole('button',{name:/設定に戻る|Back to settings/});expect(back).toHaveClass('settings-back');
    fireEvent.click(back);
    expect(screen.getByRole('button',{name:/サービス接続|Service connections/})).toBeInTheDocument();
  });
  it("connects memory controls to a narrow patch without changing model or permission settings", async () => {
    let settings = {conversation_memory_enabled: true, conversation_memory_cross_project_enabled: true,
      conversation_memory_min_relevance: .55, main_model: "fixture", permission_level: "confirm"};
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/settings") return {...settings};
      if (path === "/github/connection") return {installed: true, connected: false, execution_mode: "review"};
      expect(path).toBe("/settings/memory");
      expect(init?.method).toBe("PATCH");
      const changes = JSON.parse(String(init?.body));
      expect(changes).toEqual({conversation_memory_enabled: true, conversation_memory_cross_project_enabled: false,
        conversation_memory_min_relevance: .55});
      settings = {...settings, ...changes};
      return settings;
    });
    render(<ChatSettingsCard api={api}/>);
    fireEvent.click(await screen.findByRole("checkbox", {name: /プロジェクトをまたいだ|Across-project memory/}));
    fireEvent.click(screen.getByRole("button", {name: /記憶設定を保存|Save memory settings/}));
    await screen.findByText(/保存しました|Saved. Applies/);
    expect(settings.main_model).toBe("fixture");
    expect(settings.permission_level).toBe("confirm");
  });
  it("uses the existing narrow capability update and reflects later chat commands without losing scope", async () => {
    let saved = {web_scraping_enabled: false, computer_use_allowed_bundle_ids: ["com.example.fixture"]};
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/settings") return {...saved};
      if(path === "/github/connection")return {installed:true,connected:false,execution_mode:"review"};
      expect(path).toBe("/settings/capabilities");
      expect(init?.method).toBe("PATCH");
      const update = JSON.parse(String(init?.body));
      expect(update).toEqual({web_scraping_enabled: true});
      saved = {...saved, ...update};
      return saved;
    });
    render(<ChatSettingsCard api={api} />);
    fireEvent.click(await screen.findByRole("button", {name: "Scraping OFF"}));
    await screen.findByRole("button", {name: "Scraping ON"});
    expect(screen.getByText("Chat-directed app access")).toBeTruthy();
    saved = {...saved, web_scraping_enabled: false};
    await act(async () => window.dispatchEvent(new Event("olcr-command-complete")));
    await screen.findByRole("button", {name: "Scraping OFF"});
  });
});
