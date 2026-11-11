import {DialogFrame} from "./InterfacePrimitives";
import React, {useEffect, useState} from "react";
import {api} from "./api";
import {useLanguage} from "./language";
import {native} from "./native";

type WorkspaceStatus = "checking" | "available" | "exists" | "blocked" | "error";
type WorkspaceCheck = {name: string; status: WorkspaceStatus};
type CreateResult = "created" | "exists" | "error";

export function ProjectCreateDialog({
  create,
  close,
}: {
  create: (name: string, workspace: string, reuseExisting: boolean) => Promise<CreateResult>;
  close: () => void;
}) {
  const {language} = useLanguage();
  const [name, setName] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [drag, setDrag] = useState(false);
  const [workspaceCheck, setWorkspaceCheck] = useState<WorkspaceCheck | null>(null);
  const usingDefaultWorkspace = !workspace.trim() && Boolean(name.trim());
  const check = workspaceCheck?.name === name.trim() ? workspaceCheck.status : null;
  const waitingForCheck = usingDefaultWorkspace && (!check || check === "checking");
  const createDisabled = usingDefaultWorkspace && (waitingForCheck || check === "blocked" || check === "error");
  const ja = language === "ja";

  useEffect(() => {
    const projectName = name.trim();
    if (workspace.trim() || !projectName) {
      setWorkspaceCheck(null);
      return;
    }

    let active = true;
    setWorkspaceCheck({name: projectName, status: "checking"});
    const timer = window.setTimeout(() => {
      void api<{exists: boolean; is_directory: boolean}>(
        `/projects/default-workspace?name=${encodeURIComponent(projectName)}`,
      ).then(result => {
        if (!active) return;
        setWorkspaceCheck({
          name: projectName,
          status: !result.exists ? "available" : result.is_directory ? "exists" : "blocked",
        });
      }).catch(() => {
        if (active) setWorkspaceCheck({name: projectName, status: "error"});
      });
    }, 250);

    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [name, workspace]);

  useEffect(() => {
    const pending = native()?.event.listen<{paths: string[]}>("tauri://drag-drop", event => {
      if (event.payload.paths[0]) setWorkspace(event.payload.paths[0]);
    });
    return () => { void pending?.then(unlisten => unlisten()); };
  }, []);

  const drop = (event: React.DragEvent) => {
    event.preventDefault();
    setDrag(false);
    const file = event.dataTransfer.files[0] as (File & {path?: string}) | undefined;
    if (file) setWorkspace(file.path || file.webkitRelativePath || file.name);
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const projectName = name.trim();
    const workspacePath = workspace.trim();
    const currentCheck = workspaceCheck?.name === projectName ? workspaceCheck.status : null;
    if (!projectName || (usingDefaultWorkspace && (!currentCheck || currentCheck === "checking" || currentCheck === "blocked" || currentCheck === "error"))) return;
    const result = await create(projectName, workspacePath, !workspacePath && currentCheck === "exists");
    if (result === "exists") setWorkspaceCheck({name: projectName, status: "exists"});
  };

  return <DialogFrame title={ja?"プロジェクトを作成":"Create project"} onClose={close} dirty={Boolean(name||workspace)}><form onSubmit={event => void submit(event)}>
    <label>{ja ? "プロジェクト名" : "Project name"}
      <input value={name} required autoFocus onChange={event => setName(event.target.value)}/>
    </label>
    <label>{ja ? "ワークスペースフォルダ（任意）" : "Workspace folder (optional)"}
      <div className={drag ? "workspace-drop active" : "workspace-drop"}
        onDragOver={event => {event.preventDefault(); setDrag(true)}}
        onDragLeave={() => setDrag(false)}
        onDrop={drop}>
        <span>{ja ? "フォルダをドロップしてパスを指定" : "Drop a folder here to use its path"}</span>
        <input value={workspace} placeholder={ja ? "またはフォルダパスを入力" : "Or enter a folder path"}
          onChange={event => setWorkspace(event.target.value)}/>
      </div>
      {usingDefaultWorkspace && check === "exists" && <small className="workspace-warning" role="status">
        {ja
          ? `Downloads/${name.trim()} は既に存在します。「作成」を押すと、このフォルダをワークスペースとして再利用します。`
          : `Downloads/${name.trim()} already exists. Create will reuse this folder as the workspace.`}
      </small>}
      {usingDefaultWorkspace && check === "blocked" && <small className="workspace-warning blocked" role="alert">
        {ja ? "同名のフォルダ以外の項目があるため、この保存先は使えません。別の名前かフォルダを指定してください。" : "An item that is not a folder already uses this name. Choose another name or folder."}
      </small>}
      {usingDefaultWorkspace && check === "error" && <small className="workspace-warning blocked" role="alert">
        {ja ? "Downloadsの保存先を確認できません。もう一度名前を入力してください。" : "Could not check Downloads. Enter the project name again to retry."}
      </small>}
      {usingDefaultWorkspace && check === "checking" && <small role="status">{ja ? "保存先を確認中…" : "Checking the default location…"}</small>}
      <small>{ja
        ? "未指定の場合はDownloads内のプロジェクト名のフォルダをワークスペースにします。"
        : "Leave empty to use a Downloads folder named after the project."}</small>
    </label>
    <footer>
      <button type="button" onClick={()=>{if(!(name||workspace)||window.confirm(ja?"入力した内容を破棄して閉じますか？":"Discard the entered changes and close?"))close()}}>{ja ? "キャンセル" : "Cancel"}</button>
      <button className="primary" disabled={createDisabled}>{ja ? "作成" : "Create"}</button>
    </footer>
  </form></DialogFrame>;
}
