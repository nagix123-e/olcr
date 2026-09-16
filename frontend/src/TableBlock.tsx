import React from "react";

export type TableColumn = {
  key: string;
  label: string;
  align?: "left" | "center" | "right";
};

export type TableBlock = {
  type: "table";
  caption?: string;
  columns: TableColumn[];
  rows: Record<string, string | number | null>[];
  source_label?: string;
};

/** Render trusted display data as an accessible, scrollable semantic table. */
export function TableBlockView({ block }: { block: TableBlock }) {
  const caption = block.caption?.trim();
  return (
    <div className="table-scroll" role="region" aria-label={caption || "Data table"} tabIndex={0}>
      <table className="message-table">
        {caption && <caption>{caption}</caption>}
        <thead>
          <tr>
            {block.columns.map(column => (
              <th key={column.key} scope="col" style={{ textAlign: column.align || "left" }}>{column.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {block.rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {block.columns.map(column => {
                const value = row[column.key];
                return <td key={column.key} style={{ textAlign: column.align || "left" }}>{value == null ? "—" : String(value)}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {block.source_label && <div className="table-source">出典：{block.source_label}</div>}
    </div>
  );
}

