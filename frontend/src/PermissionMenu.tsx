export function PermissionIcon(){return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 3 4.5 6v5.5c0 4.4 3 7.7 7.5 9.5 4.5-1.8 7.5-5.1 7.5-9.5V6L12 3Z"/><path d="m8.5 12 2.3 2.3 4.7-4.8"/></svg>}

type PermissionLevel = 'confirm' | 'automatic' | 'full';

interface PermissionMenuProps {
  isOpen: boolean;
  level: PermissionLevel;
  setLevel: (level: PermissionLevel) => void;
  onClose: () => void;
  ja: boolean;
  saving?: boolean;
  error?: string;
}

export function PermissionMenu({ isOpen, level, setLevel, onClose, ja, saving=false, error }: PermissionMenuProps) {
  if (!isOpen) return null;

  const options: { id: PermissionLevel; label: string; desc: string; risk?: string }[] = [
    {
      id: 'confirm',
      label: ja ? '承認を確認' : 'Confirm Approval',
      desc: ja ? '外部ファイルの編集やインターネットの利用は常に確認する' : 'Always confirm edits to external files or internet usage',
    },
    {
      id: 'automatic',
      label: ja ? 'おまかせ承認' : 'Automatic Approval',
      desc: ja ? '安全でない可能性があると検出された操作についてのみ確認する' : 'Only confirm operations detected as potentially unsafe',
    },
    {
      id: 'full',
      label: ja ? 'フルアクセス' : 'Full Access',
      desc: ja ? 'コンピューターへのフルアクセス' : 'Full access to computer',
      risk: ja ? '(高リスク)' : '(High risk)',
    },

  ];

  return (
    <div className="permission-menu" role="dialog" aria-label={ja ? "権限設定" : "Permission settings"} onKeyDown={event=>{if(event.key==='Escape'){event.stopPropagation();onClose()}}}>
      <div className="permission-menu-header">
        <h3>{ja ? 'OLCRの権限範囲' : 'OLCR permissions'}</h3>
        <button type="button" className="permission-menu-close" aria-label={ja ? "権限設定を閉じる" : "Close permission settings"} onClick={onClose}>×</button>
      </div>
      {error&&<p className="permission-save-error" role="alert">{ja?"権限を保存できませんでした。":"Could not save permissions."} {error}</p>}
      {saving&&<p role="status">{ja?"保存中…":"Saving…"}</p>}
      <div className="permission-options">
        {options.map((opt) => (
          <button
            key={opt.id} aria-pressed={level === opt.id}
            type="button" disabled={saving}
            className={`permission-option ${level === opt.id ? 'selected' : ''}`}
            onClick={() => {
              setLevel(opt.id);
            }}
          >
            <div className="permission-option-info">
              <span className="permission-option-label">
                {opt.label} {opt.risk && <span className="permission-option-risk">{opt.risk}</span>}
              </span>
              <span className="permission-option-desc">{opt.desc}</span>
            </div>
            {level === opt.id && <span className="permission-option-check">✓</span>}
          </button>
        ))}
      </div>
    </div>
  );
}
