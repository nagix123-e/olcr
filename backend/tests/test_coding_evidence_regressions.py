from olcr_api.coding_tasks import (
    _criterion_paths,
    derive_task_graph,
    evaluate_phase,
    marketing_design_structure_acceptance,
    normalize_automatic_replan_phases,
    repair_phase_complexity,
)


def test_stack_names_do_not_create_phantom_source_files():
    criterion = "src/App.tsxでAnime.js v4を使い、Node.js環境に対応する。"
    assert _criterion_paths(criterion, {"src/App.tsx"}) == ["src/App.tsx"]
    assert _criterion_paths("Create `Anime.js` in the workspace", set()) == ["Anime.js"]
    assert _criterion_paths("src/Anime.js exists", set()) == ["src/Anime.js"]
    assert _criterion_paths("Anime.js exists.", set()) == ["Anime.js"]


def test_path_match_does_not_treat_another_files_suffix_as_the_target():
    assert _criterion_paths("other-app.js exists.", {"app.js"}) == ["other-app.js"]


def test_rebuilt_task_graph_uses_canonical_mcp_ids():
    source = {"phases": [
        {"id": "ui", "required_mcp": ["Anime.js", "shadcn", "devdocs"]},
        {"id": "browser", "execution_mode": "VERIFICATION_ONLY", "verify": ["Browser opens"],
         "required_mcp": ["Playwright MCP"]},
    ]}
    repaired, _ = repair_phase_complexity(source)
    graph = derive_task_graph(repaired, ["animejs", "shadcn", "playwright"])
    assert [row["required_mcp"] for row in graph["tasks"]] == [["animejs", "shadcn"], ["playwright"]]
    assert repaired["phases"][1]["required_mcp"] == ["playwright"]


def test_phase_evidence_matches_mcp_display_names_but_not_handshake():
    phase = {"id": "ui", "done": ["app.js exists"], "required_mcp": ["Anime.js"]}
    report = {"phase_id": "ui", "status": "PASS",
              "typed_execution_summary": {"operations": [
                  {"tool": "workspace_write", "status": "success", "output": {"path": "app.js"}}
              ]}}
    handshake = {"mcp_name": "animejs", "tool_name": "tools/list", "status": "PASS"}
    assert evaluate_phase(phase, {**report, "mcp_evidence": [handshake]})["missing_mcp"] == ["animejs"]
    receipt = {**handshake, "tool_name": "search_animejs_docs"}
    evaluation = evaluate_phase(phase, {**report, "mcp_evidence": [receipt]})
    assert evaluation["phase_complete"]


def test_automatic_replan_cannot_drop_approved_phase_mcp_ownership():
    current = {"phases": [
        {"id": "p1", "goal": "Implement the app", "required_mcp": ["Anime.js"]},
        {"id": "p2", "goal": "Browser verification", "execution_mode": "VERIFICATION_ONLY",
         "required_mcp": ["Playwright MCP"]},
    ]}
    replacement = {"phases": [
        {"id": "p1", "goal": "Fix the app"},
        {"id": "p2", "goal": "Verify browser behavior", "execution_mode": "VERIFICATION_ONLY",
         "required_mcp": ["shadcn"]},
    ]}
    result, _ = normalize_automatic_replan_phases(
        replacement, current, required_mcp=["animejs", "playwright"], verification_requested=True)
    assert [phase["required_mcp"] for phase in result["phases"]] == [["animejs"], ["playwright"]]
    assert [row["required_mcp"] for row in result["tasks"]] == [["animejs"], ["playwright"]]


def test_saved_reference_receipts_reconcile_but_do_not_prove_browser_execution():
    phase = {"id": "ui", "done": ["app.js exists"], "required_mcp": ["Anime.js", "Playwright"]}
    report = {"phase_id": "ui", "plan_revision": 2, "status": "PASS",
              "typed_execution_summary": {"operations": [
                  {"tool": "workspace_write", "status": "success", "output": {"path": "app.js"}}
              ]}}
    receipts = [{"mcp_name": "animejs", "tool_name": "search_animejs_docs", "status": "PASS"},
                {"mcp_name": "playwright", "tool_name": "browser_navigate", "status": "PASS"}]
    result = evaluate_phase(phase, report, reference_mcp_evidence=receipts)
    assert result["used_mcp"] == ["animejs"]
    assert result["missing_mcp"] == ["playwright"]
    assert not result["phase_complete"]


def test_unexecuted_verification_routes_to_verifier_after_implementation():
    phase = {"id": "ui", "execution_mode": "IMPLEMENTATION_AND_VERIFICATION",
             "done": ["app.js exists"], "verify": ["Open the app in a browser"],
             "criterion_executors": {"VERIFY:1": "READ_ONLY_VERIFICATION"}}
    report = {"phase_id": "ui", "status": "PASS",
              "typed_execution_summary": {"operations": [
                  {"tool": "workspace_write", "status": "success", "output": {"path": "app.js"}}
              ]}}
    result = evaluate_phase(phase, report)
    assert result["required_not_run_ids"] == ["VERIFY:1"]
    assert result["next_required_executor"] == "READ_ONLY_VERIFICATION"
    assert result["phase_advance_eligible"]
    assert not result["phase_complete"]


def test_marketing_structure_observes_markup_without_english_label_requirements(tmp_path):
    app = tmp_path / "App.tsx"
    app.write_text("""export const App = () => <>
      <header>製品</header>
      <main><section>概要</section><section>使い方</section></main>
      <footer><a href="/docs">資料</a></footer>
    </>;""", encoding="utf-8")
    assert marketing_design_structure_acceptance(str(tmp_path))
    app.write_text("/* header hero feature workflow development tools cta footer links visual */")
    assert not marketing_design_structure_acceptance(str(tmp_path))
