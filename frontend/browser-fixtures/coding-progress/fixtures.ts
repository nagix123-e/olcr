import type { CodingPhaseReport, CodingTask } from "../../src/CodingTaskProgress";

type Fixture = { task: CodingTask; reports: CodingPhaseReport[] };

const phases = [
  { id: "p1", goal: "既存のTetris実装を調査し、変更範囲を確定する", status: "pass" },
  { id: "p2", goal: "長いファイル名を含む画面を実装して検証する", dependencies: ["p1"] },
  { id: "p3", goal: "キーボード操作とゲームオーバー処理を確認する", dependencies: ["p2"] },
];

const graph = [
  { task_id: "t1", phase_id: "p1", goal: "リポジトリの既存コードとテストを確認する", verification: ["調査結果を記録"] },
  { task_id: "t2", phase_id: "p2", goal: "src/components/tetris/very-long-component-name-for-responsive-check.tsx を更新する", depends_on: ["t1"], verification: ["npm test", "npm run build"] },
  { task_id: "t3", phase_id: "p3", goal: "左右移動、回転、ソフトドロップ、ライン消去を実機相当で確認する", depends_on: ["t2"], verification: ["キーボード操作"] },
];

const reports: CodingPhaseReport[] = [
  { id: "r1", phase_id: "p1", attempt: 0, validation_status: "PASS", structured_report: { status: "PASS", build_executed: "YES", build_pass: "PASS", manager_decision: { decision: "PASS" } } },
];

const baseTask = (overrides: Partial<CodingTask>): CodingTask => ({
  id: "fixture-coding-task",
  status: "RUNNING",
  activity: "RUNNING",
  current_phase_id: "p2",
  retry_count: 1,
  max_retries: 2,
  queue_position: null,
  archived: false,
  pause_requested: false,
  plan: { phases, tasks: graph },
  subtask_progress: [
    { task_id: "t1", phase_id: "p1", status: "DONE", verification_status: "PASS", attempt: 0, finished_at: 1710000000 },
    { task_id: "t2", phase_id: "p2", status: "RUNNING", verification_status: "NOT_RUN", attempt: 1, started_at: 1710000100 },
    { task_id: "t3", phase_id: "p3", status: "WAITING", verification_status: "UNVERIFIED", attempt: 0 },
  ],
  ...overrides,
});

export const fixtures: Record<string, Fixture> = {
  running: { task: baseTask({}), reports },
  partial: {
    task: baseTask({
      status: "RESUMABLE",
      activity: "NONE",
      current_phase_id: "p2",
      pause_requested: true,
      subtask_progress: [
        { task_id: "t1", phase_id: "p1", status: "DONE", verification_status: "PASS", attempt: 0 },
        { task_id: "t2", phase_id: "p2", status: "PARTIAL", verification_status: "NOT_RUN", attempt: 2, failure_summary: "検証が完了する前に一時停止しました。" },
        { task_id: "t3", phase_id: "p3", status: "WAITING", verification_status: "UNVERIFIED", attempt: 0 },
      ],
    }),
    reports,
  },
  failed: {
    task: baseTask({
      status: "BLOCKED",
      activity: "NONE",
      current_phase_id: "p2",
      subtask_progress: [
        { task_id: "t1", phase_id: "p1", status: "DONE", verification_status: "PASS", attempt: 0 },
        { task_id: "t2", phase_id: "p2", status: "FAILED", verification_status: "FAILED", attempt: 2, failure_summary: "テストが失敗しました。修正が必要です。" },
        { task_id: "t3", phase_id: "p3", status: "WAITING", verification_status: "UNVERIFIED", attempt: 0 },
      ],
    }),
    reports: [...reports, { id: "r2", phase_id: "p2", attempt: 2, validation_status: "PASS", structured_report: { status: "FAIL", errors: ["テストが失敗しました。修正が必要です。"] } }],
  },
  completed: {
    task: baseTask({
      status: "COMPLETED",
      activity: "NONE",
      current_phase_id: null,
      retry_count: 0,
      subtask_progress: [
        { task_id: "t1", phase_id: "p1", status: "DONE", verification_status: "PASS", attempt: 0 },
        { task_id: "t2", phase_id: "p2", status: "DONE", verification_status: "PASS", attempt: 0 },
        { task_id: "t3", phase_id: "p3", status: "DONE", verification_status: "PASS", attempt: 0 },
      ],
      plan: { phases: phases.map((phase) => ({ ...phase, status: "pass" })), tasks: graph },
      final_report: { text: "Implemented\nTetrisの操作と表示を実装しました。\n\nChanged files\n- src/components/tetris/game.ts\n- src/components/tetris/styles.css\n\nVerification\n- npm test: PASS\n- npm run build: PASS\n\nUnverified\n- Runtime behavior: NOT_RUN\n\nUnresolved\n- なし\n\nNext\n- 追加の手動確認は不要です。" },
    }),
    reports: phases.map((phase, index) => ({ id: `r${index + 1}`, phase_id: phase.id, attempt: 0, validation_status: "PASS", structured_report: { status: "PASS", build_executed: "YES", build_pass: "PASS", manager_decision: { decision: "PASS" } } })),
  },
  long: {
    task: baseTask({
      id: "fixture-long-content-task",
      plan: { phases: phases.map((phase) => ({ ...phase, goal: `${phase.goal} — これは狭い画面で安全に折り返されることを確認するための長いフェーズ説明です` })), tasks: graph.map((item) => ({ ...item, goal: `${item.goal} — long-content fixture の折り返し確認用テキスト` })) },
      subtask_progress: [
        { task_id: "t1", phase_id: "p1", status: "DONE", verification_status: "PASS", attempt: 0 },
        { task_id: "t2", phase_id: "p2", status: "RUNNING", verification_status: "NOT_RUN", attempt: 1, started_at: 1710000100, failure_summary: "非常に長い検証メッセージを表示してもカードとコントロールが画面外へ押し出されないことを確認します。" },
        { task_id: "t3", phase_id: "p3", status: "WAITING", verification_status: "UNVERIFIED", attempt: 0 },
      ],
    }),
    reports,
  },
};

