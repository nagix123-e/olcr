import './coding-review.css';

export function DiffView({diff,label='Diff'}:{diff:string;label?:string}) {
  const lines=diff.replace(/\n$/,'').split('\n'),visible=lines.slice(0,5000);
  let left=0,right=0;
  return <div className="review-diff" role="region" aria-label={label} tabIndex={0}><pre>{visible.map((line,index)=>{
    const header=/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
    if(header){left=Number(header[1]);right=Number(header[2]);}
    const meta=Boolean(header)||/^(---|\+\+\+|diff |index |new file|deleted file|\\)/.test(line);
    const added=!meta&&line.startsWith('+'),removed=!meta&&line.startsWith('-');
    const before=meta?'':added?'':String(left++),after=meta?'':removed?'':String(right++);
    return <span className={`review-diff-line ${added?'added':removed?'removed':meta?'meta':''}`} key={index}><span className="review-line-number" aria-hidden="true">{before}</span><span className="review-line-number" aria-hidden="true">{after}</span><code>{line||' '}</code></span>;
  })}</pre>{lines.length>5000&&<p>… {lines.length-5000} more lines</p>}</div>;
}
