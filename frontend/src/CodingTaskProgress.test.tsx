import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CodingTask, CodingTaskProgress, shouldAcceptPlanRevision, taskIsActivelyRunning } from "./CodingTaskProgress";
const request=vi.hoisted(()=>vi.fn(async()=>({files:[],reports:[]})));
vi.mock('./api',()=>({api:request}));
const locale = vi.hoisted(() => ({ language: "en" }));
vi.mock("./language", () => ({ useLanguage: () => ({ language: locale.language, setLanguage: vi.fn() }) }));

const graph=[
  {task_id:"t1",phase_id:"p1",goal:"Define schema",depends_on:[],verification:["schema test"]},
  {task_id:"t2",phase_id:"p2",goal:"Implement API",depends_on:["t1"],verification:["API test"]},
  {task_id:"t3",phase_id:"p3",goal:"Build frontend",depends_on:["t2"],verification:["typecheck"]},
];
const phases=[
  {id:"p1",goal:"Define schema",status:"pass",dependencies:[]},
  {id:"p2",goal:"Implement API",status:"pending",dependencies:["p1"]},
  {id:"p3",goal:"Build frontend",status:"pending",dependencies:["p2"]},
];
function task(overrides:Partial<CodingTask>={}):CodingTask{return {id:"task",status:"RUNNING",activity:"QWEN_IMPLEMENTATION",current_phase_id:"p2",retry_count:0,max_retries:2,plan:{phases,tasks:graph},subtask_progress:[
  {task_id:"t1",phase_id:"p1",status:"DONE",verification_status:"PASS",attempt:0},
  {task_id:"t2",phase_id:"p2",status:"RUNNING",verification_status:"UNVERIFIED",attempt:0},
  {task_id:"t3",phase_id:"p3",status:"WAITING",verification_status:"UNVERIFIED",attempt:0},
],...overrides}}
function renderProgress(value: CodingTask){return render(<CodingTaskProgress task={value} reports={[]} loading={false} error="" pausePending={false} onPause={vi.fn()} onResume={vi.fn()} onArchive={vi.fn()}/>)}

afterEach(() => { cleanup(); locale.language = "en"; });

describe("CodingTaskProgress",()=>{
  it('shows the observed failure during a retry and does not cap the displayed attempt',()=>{
    const value=task({retry_count:4});
    value.subtask_progress![1].failure_summary='history contains latest draws';
    renderProgress(value);
    expect(screen.getByText('1 / 3 tasks completed')).toBeTruthy();
    expect(screen.getByText('Repairing the latest failure: history contains latest draws')).toBeTruthy();
    expect(screen.getByText('Correction attempts 4')).toBeTruthy();
  });
  it('does not show failure evidence from an older plan revision',()=>{
    render(<CodingTaskProgress task={task({plan_revision:1})} reports={[{id:'old',phase_id:'p2',attempt:0,validation_status:'PASS',structured_report:{plan_revision:0,status:'FAIL',errors:['stale failure']}}]} loading={false} error="" pausePending={false} onPause={vi.fn()} onResume={vi.fn()} onArchive={vi.fn()}/>);
    expect(screen.queryByText('stale failure')).toBeNull();
  });
  it('retains passed evidence for a completed phase preserved by replanning',()=>{
    render(<CodingTaskProgress task={task({plan_revision:1})} reports={[{id:'preserved',phase_id:'p1',attempt:0,validation_status:'PASS',structured_report:{plan_revision:0,status:'PASS',manager_decision:{decision:'PASS'}}}]} loading={false} error="" pausePending={false} onPause={vi.fn()} onResume={vi.fn()} onArchive={vi.fn()}/>);
    expect(screen.getByLabelText('Phase progress')).toHaveTextContent('Passed');
  });
  it('shows a passed phase even when an optional build was not run',()=>{
    render(<CodingTaskProgress task={task()} reports={[{id:'passed',phase_id:'p2',attempt:0,validation_status:'PASS',structured_report:{status:'PASS',build_executed:'NOT_RUN',build_pass:'NOT_RUN',manager_decision:{decision:'PASS'}}}]} loading={false} error="" pausePending={false} onPause={vi.fn()} onResume={vi.fn()} onArchive={vi.fn()}/>);
    expect(screen.getByLabelText('Phase progress')).toHaveTextContent('Passed');
    expect(screen.getByLabelText('Phase progress')).not.toHaveTextContent('Not run');
  });
  it('offers blocker revalidation without claiming that the task can never resume',()=>{
    const onResume=vi.fn();
    render(<CodingTaskProgress task={task({status:'BLOCKED',recovery_reason:'GLOBAL_RECOVERY_LIMIT'})} reports={[]} loading={false} error="" pausePending={false} onPause={vi.fn()} onResume={onResume} onArchive={vi.fn()}/>);
    fireEvent.click(screen.getByRole('button',{name:'Recheck the blocker'}));
    expect(onResume).toHaveBeenCalledOnce();
    expect(screen.getByLabelText('Recovery limit details')).toHaveTextContent('remaining recovery budget');
  });
  it('shows requirement clarification as chat input waiting, without an authorization card',()=>{
    renderProgress(task({status:'WAITING_FOR_USER',activity:'NONE',pending_authorization:{type:'REQUIREMENTS_CLARIFICATION'}}));
    expect(screen.getByLabelText('Coding Orchestrator: Waiting')).toHaveTextContent('Waiting for requirements');
    expect(screen.queryByLabelText('Authorization required')).toBeNull();
  });
  it('explains ineffective recovery and offers retry without calling it an approval',()=>{
    locale.language='ja';const onResume=vi.fn();
    render(<CodingTaskProgress task={task({status:'WAITING_FOR_USER',activity:'NONE',recovery_reason:'REPLAN_INEFFECTIVE',pending_authorization:{type:'REPLAN_INEFFECTIVE',reason:'Phase replan did not change the failing phase structure.',unmet_criteria:['node:test tests pass']}})} reports={[]} loading={false} error="" pausePending={false} onPause={vi.fn()} onResume={onResume} onArchive={vi.fn()}/>);
    expect(screen.getByLabelText('再試行方法')).toHaveTextContent('node:test tests pass');
    expect(screen.getByLabelText('再試行方法')).toHaveTextContent('保存済みの失敗情報を使い');
    expect(screen.queryByLabelText('承認が必要')).toBeNull();
    expect(screen.queryByText('Phase replan did not change the failing phase structure.')).toBeNull();
    fireEvent.click(screen.getByRole('button',{name:'失敗情報を使ってこの工程を再実行'}));
    expect(onResume).toHaveBeenCalledOnce();
  });
  it("offers explicit bounded recovery only for narrative-only retry exhaustion",()=>{
    locale.language = "ja";
    const onResume=vi.fn();
    const {rerender}=render(<CodingTaskProgress task={task({status:"BLOCKED",recovery_reason:"NARRATIVE_ONLY_RETRY_EXHAUSTED"})} reports={[]} loading={false} error="" pausePending={false} onPause={vi.fn()} onResume={onResume} onArchive={vi.fn()}/>);
    fireEvent.click(screen.getByRole("button",{name:"同じコーディングタスクを段階的な復旧で再開"}));
    expect(onResume).toHaveBeenCalledTimes(1);
    rerender(<CodingTaskProgress task={task({status:"BLOCKED",recovery_reason:"GLOBAL_RECOVERY_LIMIT"})} reports={[]} loading={false} error="" pausePending={false} onPause={vi.fn()} onResume={onResume} onArchive={vi.fn()}/>);
    expect(screen.queryByRole("button",{name:"同じコーディングタスクを段階的な復旧で再開"})).toBeNull();
  });
  it("shows a NORMAL-mode continuation checkpoint with its handoff and Continue action",()=>{
    locale.language = "ja";
    const onResume=vi.fn();
    const value=task({status:"RESUMABLE",activity:"NONE",execution_mode:"NORMAL",recovery_reason:"RESOURCE_CHECKPOINT",batch_cursor:1,
      batch_handoff:{completed_phase_id:"p1",next_phase_id:"p2",next_constraints:["Implement API"],changed_files:["schema.ts"],verification:[{phase_id:"p1",status:"PASS"}]}});
    render(<CodingTaskProgress task={value} reports={[]} loading={false} error="" pausePending={false} onPause={vi.fn()} onResume={onResume} onArchive={vi.fn()}/>);
    expect(screen.getByLabelText("コーディング オーケストレーター: 続行待ち")).toBeTruthy();
    expect(screen.getByText("ここまで完了しました。続行すると次の工程を開始します。")).toBeTruthy();
    expect(screen.getByText("完了した工程: Define schema")).toBeTruthy();
    expect(screen.getByText("次の工程: Implement API")).toBeTruthy();
    expect(screen.getByText("変更ファイル数: 1")).toBeTruthy();
    expect(screen.getByText("検証状態: 合格")).toBeTruthy();
    fireEvent.click(screen.getByRole("button",{name:"続行"}));
    expect(onResume).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText("Authorization required")).toBeNull();
    expect(screen.queryByText("FAILED")).toBeNull();
  });
  it("renders persisted subtask progress and an identifiable current task",()=>{
    renderProgress(task());
    expect(screen.getByText("1 / 3 tasks completed")).toBeInTheDocument();
    expect(screen.getByLabelText("Current task")).toHaveTextContent("Implement API");
    expect(screen.getByLabelText("Phase progress")).toHaveTextContent("Unverified");
  });
  it("hides task graph and automatic diagrams while retaining plan details",()=>{
    renderProgress(task());
    expect(screen.queryByText(/Task Graph/)).toBeNull();
    expect(screen.queryByText(/View implementation plan as a diagram/)).toBeNull();
    expect(screen.getByText('Plan and execution details')).toBeInTheDocument();
  });
  it("shows the plan diagram when explicitly requested",()=>{
    renderProgress(task({original_goal:'Show a concept diagram of the implementation plan'}));
    expect(screen.getByText('View implementation plan as a diagram')).toBeInTheDocument();
  });
  it("shows authorization as a distinct non-failure state",()=>{
    renderProgress(task({status:"WAITING_FOR_USER",pending_authorization:{requested_scope:["new directory"]}}));
    expect(screen.getByLabelText("Authorization required")).toHaveTextContent("new directory");
  });
  it("falls back to phases without inventing legacy subtask history",()=>{
    renderProgress(task({subtask_progress:null,status:"RESUMABLE"}));
    expect(screen.getByText("1 / 3 phases completed")).toBeInTheDocument();
    expect(screen.getByLabelText("Phase progress")).toBeInTheDocument();
  });
  it("uses buttons for pause and resume actions",()=>{
    const {rerender}=renderProgress(task());
    expect(screen.getByRole("button",{name:"Pause"})).toBeInTheDocument();
    rerender(<CodingTaskProgress task={task({status:"RESUMABLE"})} reports={[]} loading={false} error="" pausePending={false} onPause={vi.fn()} onResume={vi.fn()} onArchive={vi.fn()}/>);
    expect(screen.getByRole("button",{name:/Resume/})).toBeInTheDocument();
  });
  it("renders the persisted active plan revision",()=>{
    renderProgress(task({plan_revision:1}));
    expect(screen.getByText("Plan revision 1")).toBeInTheDocument();
  });
});

describe("progress polling classification",()=>{
  it('dismisses the console separately from archiving or pausing a task',()=>{
    const onDismiss=vi.fn(),onArchive=vi.fn(),onPause=vi.fn();
    render(<CodingTaskProgress task={task()} reports={[]} loading={false} error="" pausePending={false}
      onPause={onPause} onResume={vi.fn()} onArchive={onArchive} onDismiss={onDismiss}/>);
    fireEvent.click(screen.getByRole('button',{name:/Dismiss Coding Orchestrator card|コーディング オーケストレーターを閉じる/}));
    expect(onDismiss).toHaveBeenCalledOnce();
    expect(onArchive).not.toHaveBeenCalled();expect(onPause).not.toHaveBeenCalled();
  });
  it("only treats active execution states as pollable",()=>{
    expect(taskIsActivelyRunning(task({status:"RUNNING"}))).toBe(true);
    expect(taskIsActivelyRunning(task({status:"FINAL_REPORTING"}))).toBe(true);
    expect(taskIsActivelyRunning(task({status:"COMPLETED"}))).toBe(false);
    expect(taskIsActivelyRunning(task({status:"RESUMABLE"}))).toBe(false);
  });
  it("rejects an out-of-order older plan revision",()=>{
    const current=task({plan_revision:1,plan:{phases:phases.slice(0,2),tasks:graph.slice(0,2)}});
    expect(shouldAcceptPlanRevision(current,task({plan_revision:0}))).toBe(false);
    expect(shouldAcceptPlanRevision(current,task({plan_revision:1}))).toBe(true);
  });
});


describe('Compact coding plan and workspace setup',()=>{
  it('keeps technical details and file reviews closed initially',()=>{
    const {container}=renderProgress(task());
    expect(container.querySelector('.coding-technical-details')).not.toHaveAttribute('open');
    expect(container.querySelector('.coding-review-disclosure')).not.toHaveAttribute('open');
  });
  it('shows actionable non-Git setup instead of an empty review panel',async()=>{
    locale.language='ja';request.mockClear();
    renderProgress(task({status:'WAITING_FOR_USER',pending_authorization:{type:'WORKSPACE_SETUP',reason:'WORKTREE_REQUIRES_GIT_REPOSITORY',message:'このフォルダはGit管理されていません'}}));
    expect(screen.getByLabelText('作業場所の選択')).toHaveTextContent('Git管理');
    expect(screen.queryByText('変更と実行結果')).toBeNull();
    fireEvent.click(screen.getByRole('button',{name:'このTaskは現在のWorkspaceで進める'}));
    await waitFor(()=>expect(request).toHaveBeenCalledWith('/coding-tasks/task/workspace/setup',expect.objectContaining({body:JSON.stringify({action:'direct'})})));
    expect(screen.getByRole('button',{name:/空の初期commit/})).toBeInTheDocument();
  });
});

it('shows the actual API model during planning even when the implementation model is local',()=>{
 renderProgress(task({status:'PLANNING',activity:'QWEN_PLANNING',plan:null,model_invocation:{state:'request_started',role:'planner_replanner',model:'planner-api',runtime:'google',created_at:100}}));
 expect(screen.getByText(/Model: planner-api · API/)).toBeInTheDocument();
 expect(document.querySelector('.coding-status-description')).toHaveTextContent('Planning');
});

it('shows live generation counts and the current model without claiming a phase completed',()=>{
 renderProgress(task({model_invocation:{state:'request_started',role:'long_implementation',model:'local-coder',runtime:'mlx',created_at:Date.now()/1000},
   model_progress:{state:'worker_progress',event:'bonsai_generation_progress',generated_tokens:8320,max_tokens:16384,created_at:Date.now()/1000}}));
 expect(screen.getByLabelText('Model progress')).toHaveTextContent('8,320');
 expect(screen.getByLabelText('Model progress')).toHaveTextContent('Generating');
 expect(screen.getByText('1 / 3 tasks completed')).toBeInTheDocument();
});

it('shows a localized same-model no-thinking retry in coding task progress',()=>{
 locale.language='ja';
 const now=Date.now()/1000;
 renderProgress(task({model_invocation:{state:'request_started',role:'long_implementation',model:'ornith-1.5:9b',runtime:'ollama',created_at:now},
   model_progress:{state:'worker_progress',event:'structured_no_thinking_retry',model:'ornith-1.5:9b',failure:'reasoning_budget',created_at:now}}));
 expect(screen.getByLabelText('モデルの進行状況')).toHaveTextContent('ornith-1.5:9b');
 expect(screen.getByLabelText('モデルの進行状況')).toHaveTextContent('同じモデル');
 expect(screen.getByLabelText('モデルの進行状況')).toHaveTextContent('思考なし');
});

it('shows an interruptible slot wait instead of implying inference is running',()=>{
 renderProgress(task({model_progress:{state:'slot_waiting',created_at:Date.now()/1000}}));
 expect(screen.getByLabelText('Model progress')).toHaveTextContent('Waiting for an inference slot');
 expect(screen.getByRole('button',{name:'Pause'})).toBeEnabled();
});

it('shows fallback and pause requests while preserving the current phase',()=>{
 const {rerender}=renderProgress(task({model_progress:{state:'fallback',model:'api-coder',reason:'output_limit',created_at:Date.now()/1000}}));
 expect(screen.getByLabelText('Model progress')).toHaveTextContent('api-coder');
 expect(screen.getByLabelText('Model progress')).toHaveTextContent('Output limit');
 rerender(<CodingTaskProgress task={task({pause_requested:true})} reports={[]} loading={false} error="" pausePending={true} onPause={vi.fn()} onResume={vi.fn()} onArchive={vi.fn()}/>);
 expect(screen.getByRole('status')).toHaveTextContent('Stopping');
 expect(screen.getByText('1 / 3 tasks completed')).toBeInTheDocument();
});
