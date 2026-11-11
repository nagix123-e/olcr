import { useState } from "react";
import {useLanguage} from "./language";

export type ScrapeExportBlockData = {
  type: "scrape_export";
  run_id: string;
  output_format: "csv" | "json" | "jsonl";
  record_count: number;
};

export function ScrapeExportBlock({ block, download }: {
  block: ScrapeExportBlockData;
  download: (path: string, filename: string) => Promise<void>;
}) {
  const {language}=useLanguage();const ja=language==="ja";
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [format, setFormat] = useState<ScrapeExportBlockData["output_format"]>(block.output_format || "csv");
  const [saved, setSaved] = useState(false);
  const exportResults = async () => {
    if (!block.run_id || !["csv", "json", "jsonl"].includes(format)) return;
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      await download(`/web/scrape/runs/${encodeURIComponent(block.run_id)}/export?format=${format}`,
        `olcr-scrape-results.${format}`);
      setSaved(true);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      if (message !== "EXPORT_CANCELLED") setError(message);
    } finally {
      setBusy(false);
    }
  };

  return <section className="scrape-export-block" aria-label={ja?"スクレイピング結果のエクスポート":"Scrape results export"}>
    <span>{block.record_count} {ja?"件":"records"}</span>
    <label>{ja?"出力形式":"Final output"}
      <select aria-label={ja?"出力形式":"Final output format"} value={format} disabled={busy} onChange={event => setFormat(event.target.value as ScrapeExportBlockData["output_format"])}>
        <option value="csv">CSV</option><option value="json">JSON</option><option value="jsonl">JSONL</option>
      </select>
    </label>
    <button disabled={busy || block.record_count < 1} onClick={() => void exportResults()}>
      {ja?(busy?"ファイルを保存中…":`このコンピューターに${format.toUpperCase()}を保存`):(busy ? "Saving file…" : `Save ${format.toUpperCase()} to this computer`)}
    </button>
    {saved && <p role="status">{ja?`${format.toUpperCase()}ファイルをこのコンピューターに保存しました。`:`Saved ${format.toUpperCase()} file to this computer.`}</p>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
