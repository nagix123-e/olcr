import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CodingTask, CodingTaskProgress, shouldAcceptPlanRevision, taskIsActivelyRunning } from "./CodingTaskProgress";

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

afterEach(cleanup);

describe("CodingTaskProgress",()=>{
  it("shows a NORMAL-mode continuation checkpoint with its handoff and Continue action",()=>{
    const onResume=vi.fn();
    const value=task({status:"RESUMABLE",activity:"NONE",execution_mode:"NORMAL",recovery_reason:"RESOURCE_CHECKPOINT",batch_cursor:1,
      batch_handoff:{completed_phase_id:"p1",next_phase_id:"p2",next_constraints:["Implement API"],changed_files:["schema.ts"],verification:[{phase_id:"p1",status:"PASS"}]}});
    render(<CodingTaskProgress task={value} reports={[]} loading={false} error="" pausePending={false} onPause={vi.fn()} onResume={onResume} onArchive={vi.fn()}/>);
    expect(screen.getByLabelText("Coding Orchestrator: 続行待ち")).toBeTruthy();
    expect(screen.getByText("ここまで完了しました。続行すると次の工程を開始します。")).toBeTruthy();
    expect(screen.getByText("完了した工程: Define schema")).toBeTruthy();
    expect(screen.getByText("次の工程: Implement API")).toBeTruthy();
    expect(screen.getByText("変更ファイル数: 1")).toBeTruthy();
    expect(screen.getByText("検証状態: PASS")).toBeTruthy();
    fireEvent.click(screen.getByRole("button",{name:"続行"}));
    expect(onResume).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText("Authorization required")).toBeNull();
    expect(screen.queryByText("FAILED")).toBeNull();
  });
  it("renders persisted subtask progress and an identifiable current task",()=>{
    renderProgress(task());
    expect(screen.getByText("1 / 3 tasks completed")).toBeInTheDocument();
    expect(screen.getByLabelText("Current task")).toHaveTextContent("Implement API");
    expect(screen.getByText("RUNNING · UNVERIFIED")).toBeInTheDocument();
  });
  it.each(["WAITING","RUNNING","DONE","FAILED","PARTIAL"] as const)("renders %s as text, independent of color",status=>{
    const value=task({subtask_progress:[{task_id:"t1",phase_id:"p1",status,verification_status:"UNVERIFIED",attempt:0}],status:status==="DONE"?"COMPLETED":status==="FAILED"?"BLOCKED":status==="PARTIAL"?"RESUMABLE":"RUNNING"});
    renderProgress(value);
    expect(screen.getByText(`${status} · UNVERIFIED`)).toBeInTheDocument();
  });
  it("keeps verification separate from completion",()=>{
    renderProgress(task({subtask_progress:[{task_id:"t1",phase_id:"p1",status:"DONE",verification_status:"NOT_RUN",attempt:0}]}));
    expect(screen.getByText("DONE · NOT_RUN")).toBeInTheDocument();
  });
  it("shows authorization as a distinct non-failure state",()=>{
    renderProgress(task({status:"WAITING_FOR_USER",pending_authorization:{requested_scope:["new directory"]}}));
    expect(screen.getByLabelText("Authorization required")).toHaveTextContent("new directory");
  });
  it("falls back to phases without inventing legacy subtask history",()=>{
    renderProgress(task({subtask_progress:null,status:"RESUMABLE"}));
    expect(screen.getByText("1 / 3 phases completed")).toBeInTheDocument();
    expect(screen.getByText(/この既存 Plan には subtask 実行状態が保存されていない/)).toBeInTheDocument();
  });
  it("uses buttons for pause and resume actions",()=>{
    const {rerender}=renderProgress(task());
    expect(screen.getByRole("button",{name:"一時停止"})).toBeInTheDocument();
    rerender(<CodingTaskProgress task={task({status:"RESUMABLE"})} reports={[]} loading={false} error="" pausePending={false} onPause={vi.fn()} onResume={vi.fn()} onArchive={vi.fn()}/>);
    expect(screen.getByRole("button",{name:/再開/})).toBeInTheDocument();
  });
  it("renders the persisted active plan revision",()=>{
    renderProgress(task({plan_revision:1}));
    expect(screen.getByText("Plan revision 1")).toBeInTheDocument();
  });
});

describe("progress polling classification",()=>{
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
