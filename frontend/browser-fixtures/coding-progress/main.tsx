import React, { useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { CodingTaskProgress, type CodingPhaseReport, type CodingTask } from "../../src/CodingTaskProgress";
import "../../src/styles.css";
import "./fixture.css";
import { fixtures } from "./fixtures";

function App() {
  const requested = new URLSearchParams(window.location.search).get("fixture") || "running";
  const fixtureName = fixtures[requested] ? requested : "running";
  const initial = useMemo(() => fixtures[fixtureName], [fixtureName]);
  const [task, setTask] = useState<CodingTask>(() => structuredClone(initial.task));
  const [reports] = useState<CodingPhaseReport[]>(() => structuredClone(initial.reports));
  const [notice, setNotice] = useState("");

  const pause = () => { setTask((current) => ({ ...current, status: "RESUMABLE", activity: "NONE", pause_requested: true })); setNotice("一時停止を要求しました"); };
  const resume = () => { setTask((current) => ({ ...current, status: "RUNNING", activity: "RUNNING", pause_requested: false })); setNotice("再開しました"); };
  const archive = () => { setTask((current) => ({ ...current, archived: true })); setNotice("アーカイブしました"); };

  return <main className="fixture-page">
    <header className="fixture-header">
      <div><small>TEST-ONLY BROWSER FIXTURE</small><h1>CodingTaskProgress</h1><p>バックエンド接続なし・静的DTOのみ</p></div>
      <label>Fixture<select aria-label="Fixture state" value={fixtureName} onChange={(event) => { window.location.search = `?fixture=${event.target.value}`; }}>
        {Object.keys(fixtures).map((name) => <option key={name} value={name}>{name}</option>)}
      </select></label>
    </header>
    {notice && <p className="fixture-notice" role="status">{notice}</p>}
    <CodingTaskProgress task={task} reports={reports} loading={false} error="" pausePending={false} onPause={pause} onResume={resume} onArchive={archive} />
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);

