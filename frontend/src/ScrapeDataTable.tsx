import { useEffect, useState } from "react";

type Props = { api: (path: string, init?: RequestInit) => Promise<any>; runId: string; status: string; recordCount: number; onUseUrls?: (urls: string[]) => void; download?: (path: string, filename: string) => Promise<void> };
type Page = { columns: string[]; rows: { id: number; values: Record<string, unknown> }[]; total: number };
const display = (v: unknown) => v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
export function ScrapeDataTable({ api, runId, status, recordCount, download, onUseUrls }: Props) {
  const [page, setPage] = useState<Page>({ columns: [], rows: [], total: 0 });
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [column, setColumn] = useState("");
  const [target, setTarget] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const editable = ["COMPLETED", "STOPPED", "FAILED", "INTERRUPTED"].includes(status);
  useEffect(() => {
    let live = true;
    void api(`/web/scrape/runs/${encodeURIComponent(runId)}/table?offset=${offset}&limit=50&search=${encodeURIComponent(search)}`)
      .then(result => { if (live) { setPage(result); setError(""); } })
      .catch(reason => { if (live) setError(String(reason.message || reason)); });
    return () => { live = false; };
  }, [api, runId, recordCount, status, offset, search, revision]);
  useEffect(() => { if (column && !page.columns.includes(column)) setColumn(""); }, [page.columns, column]);
  const edit = async (operation: string, extra: Record<string, unknown> = {}) => {
    setBusy(true); setError("");
    try { await api(`/web/scrape/runs/${encodeURIComponent(runId)}/table`, { method: "POST", body: JSON.stringify({ operation, column, target, ...extra }) }); setOffset(0); setRevision(r => r + 1); }
    catch (reason: any) { setError(reason.message || String(reason)); }
    finally { setBusy(false); }
  };
  return <section className="scrape-data-table" aria-label="Collected data table">
    <h3>Collected data · {page.total} rows</h3>
    <label>Search data<input value={search} onChange={e => { setSearch(e.target.value); setOffset(0); }} maxLength={200} /></label>
    <div className="scrape-table-tools">
      <label>Column<select value={column} onChange={e => setColumn(e.target.value)}><option value="">Choose column</option>{page.columns.map(c => <option key={c}>{c}</option>)}</select></label>
      <label>New name / merge column<input value={target} onChange={e => setTarget(e.target.value)} maxLength={120} /></label>
      <button disabled={!editable || busy || !column || !target} onClick={() => void edit("rename")}>Rename column</button>
      <button disabled={!editable || busy || !column} onClick={() => void edit("drop")}>Remove column</button>
      {[[-1, "Move column left"], [1, "Move column right"]].map(([direction,label]) => <button key={label} disabled={!editable || busy || !column || page.columns.indexOf(column) + Number(direction) < 0 || page.columns.indexOf(column) + Number(direction) >= page.columns.length} onClick={() => {
        const columns = [...page.columns], index = columns.indexOf(column), next = index + Number(direction);
        [columns[index],columns[next]] = [columns[next],columns[index]];
        void edit("reorder", {value: JSON.stringify(columns)});
      }}>{label}</button>)}
      <button disabled={!editable || busy || !column} onClick={() => void edit("number")}>Convert to number</button>
      <button disabled={!editable || busy || !column || !target} onClick={() => void edit("merge", { value: " " })}>Merge columns</button>
      <button disabled={!editable || busy} onClick={() => void edit("deduplicate")}>Remove duplicate rows</button>
      <button disabled={!editable || busy} onClick={() => void edit("drop_empty")}>Remove empty rows</button>
      {download && ["csv", "json", "jsonl", "xlsx"].map(format => <button key={format} disabled={busy || !page.total} onClick={() => {
        void download(`/web/scrape/runs/${encodeURIComponent(runId)}/export?format=${format}`, `olcr-results.${format}`).catch(reason => setError(reason.message));
      }}>Export {format.toUpperCase()}</button>)}
    </div>
    {onUseUrls && page.columns.includes("url") && <button disabled={!page.rows.length} onClick={() => onUseUrls(page.rows.map(r => String(r.values.url || "")).filter(url => /^https?:\/\//.test(url)))}>Use visible URLs for page extraction</button>}
    {download && page.columns.includes("image_url") && <button disabled={busy || !page.total} onClick={() => {
      setBusy(true); void download(`/web/scrape/runs/${encodeURIComponent(runId)}/images.zip`, "olcr-images.zip").catch(reason => setError(reason.message)).finally(() => setBusy(false));
    }}>Download images ZIP · up to 20 public images / 20 MB</button>}
    {!editable && <p>Stop collection to edit saved data.</p>}
    {error && <p role="alert">{error}</p>}
    <div className="scrape-table-scroll"><table><thead><tr>{page.columns.map(c => <th key={c}>{c}</th>)}</tr></thead>
      <tbody>{page.rows.map(row => <tr key={row.id}>{page.columns.map(c => <td key={c}>
        {editable && Object.hasOwn(row.values, c) ? <input aria-label={`Row ${row.id} ${c}`} key={`${revision}-${display(row.values[c])}`} defaultValue={display(row.values[c])} disabled={busy} onBlur={e => { if (e.target.value !== display(row.values[c])) void edit("cell", { row_id: row.id, column: c, value: e.target.value }); }} /> : display(row.values[c])}
      </td>)}</tr>)}</tbody></table></div>
    <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous rows</button>
    <span> {page.total ? offset + 1 : 0}–{Math.min(offset + 50, page.total)} / {page.total} </span>
    <button disabled={offset + 50 >= page.total} onClick={() => setOffset(offset + 50)}>Next rows</button>
  </section>;
}
