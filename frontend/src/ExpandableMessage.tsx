import { useId, useState, type ReactNode } from "react";

// Transcript polling can replace message subtrees. Keep the disclosure choice
// outside that subtree so a refresh/remount does not immediately collapse it.
const expandedByKey = new Map<string, boolean>();
const MAX_REMEMBERED_DISCLOSURES = 200;

function rememberExpanded(key: string, expanded: boolean) {
  expandedByKey.delete(key);
  if (expanded) expandedByKey.set(key, true);
  while (expandedByKey.size > MAX_REMEMBERED_DISCLOSURES) {
    const oldest = expandedByKey.keys().next().value;
    if (oldest === undefined) break;
    expandedByKey.delete(oldest);
  }
}

export function ExpandableMessage({
  preview,
  children,
  stateKey,
}: {
  preview: string;
  children: ReactNode;
  stateKey?: string;
}) {
  const [expanded, setExpanded] = useState(() => stateKey ? expandedByKey.get(stateKey) === true : false);
  const contentId = useId();

  const toggle = () => {
    const next = !expanded;
    if (stateKey) rememberExpanded(stateKey, next);
    setExpanded(next);
  };

  return (
    <section className={expanded ? "long-prompt expanded" : "long-prompt"}>
      <button
        type="button"
        className="long-prompt-toggle"
        aria-expanded={expanded}
        aria-controls={contentId}
        onClick={toggle}
      >
        <span className="long-prompt-preview">{preview}</span>
        <span className="long-prompt-action">
          {expanded ? "全文を閉じる" : "全文を表示"}
          <span aria-hidden="true">{expanded ? "⌃" : "⌄"}</span>
        </span>
      </button>
      <div id={contentId} className="long-prompt-content" hidden={!expanded}>
        {children}
      </div>
    </section>
  );
}
