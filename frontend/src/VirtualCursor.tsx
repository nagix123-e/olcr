export type PointerGeometry = { displayId: number; originX: number; originY: number; widthPoints: number; heightPoints: number };
export type PointerFeedback = {
  phase: "PLANNED" | "AIMING" | "CLICKED" | "FAILED";
  alignment: {
    captureSessionId: string;
    targetApplication: { applicationPid: number; applicationBundleId: string };
    verifiedAt: number;
    verifiedSequence: number;
    mappedGlobalPoint: [number, number];
    pointerOriginGlobal: [number, number] | null;
    pointerCurrentGlobal: [number, number] | null;
    distancePoints: number | null;
    displayGeometry: PointerGeometry;
    targetBboxNormalized: [number, number, number, number];
  };
};

export function pointerMatchesFrame(pointer: PointerFeedback, frame: {
  captureSessionId: string;
  targetApplication?: { applicationPid: number; applicationBundleId: string } | null;
  displayGeometry?: PointerGeometry;
  sequence: number;
}, now = Date.now() / 1000): boolean {
  const a = pointer.alignment, g = a.displayGeometry, f = frame.displayGeometry;
  return ["PLANNED", "AIMING", "CLICKED", "FAILED"].includes(pointer.phase)
    && a.captureSessionId === frame.captureSessionId
    && a.targetApplication.applicationPid === frame.targetApplication?.applicationPid
    && a.targetApplication.applicationBundleId === frame.targetApplication?.applicationBundleId
    && !!f && ["displayId", "originX", "originY", "widthPoints", "heightPoints"].every(key => g[key as keyof PointerGeometry] === f[key as keyof PointerGeometry])
    && g.widthPoints > 0 && g.heightPoints > 0
    && Object.values(g).every(Number.isFinite)
    && Number.isFinite(a.verifiedAt) && now >= a.verifiedAt && now - a.verifiedAt <= (pointer.phase === "CLICKED" ? 30 : 5)
    && a.verifiedSequence <= frame.sequence
    && a.mappedGlobalPoint.length === 2 && a.mappedGlobalPoint.every(Number.isFinite)
    && a.mappedGlobalPoint[0] >= g.originX && a.mappedGlobalPoint[0] <= g.originX + g.widthPoints
    && a.mappedGlobalPoint[1] >= g.originY && a.mappedGlobalPoint[1] <= g.originY + g.heightPoints
    && (a.pointerOriginGlobal === null || (a.pointerOriginGlobal.length === 2 && a.pointerOriginGlobal.every(Number.isFinite)))
    && (a.pointerCurrentGlobal === null || (a.pointerCurrentGlobal.length === 2 && a.pointerCurrentGlobal.every(Number.isFinite)))
    && (a.distancePoints === null || (Number.isFinite(a.distancePoints) && a.distancePoints >= 0))
    && a.targetBboxNormalized.length === 4 && a.targetBboxNormalized.every(v => Number.isFinite(v) && v >= 0 && v <= 1)
    && a.targetBboxNormalized[0] < a.targetBboxNormalized[2] && a.targetBboxNormalized[1] < a.targetBboxNormalized[3];
}

export function VirtualCursor({ pointer, width, height }: { pointer: PointerFeedback; width: number; height: number }) {
  const a = pointer.alignment, g = a.displayGeometry;
  const map = ([x, y]: [number, number]) => [(x - g.originX) / g.widthPoints * width, (y - g.originY) / g.heightPoints * height];
  const [x, y] = map(a.mappedGlobalPoint);
  const [sx, sy] = map(a.pointerOriginGlobal ?? a.mappedGlobalPoint);
  const [cx, cy] = map(a.pointerCurrentGlobal ?? a.pointerOriginGlobal ?? a.mappedGlobalPoint);
  const [left, top, right, bottom] = a.targetBboxNormalized;
  const labels = { PLANNED: "操作位置を確認中", AIMING: "カーソル移動中", CLICKED: "前回のクリック位置", FAILED: "クリック停止" };
  return <div className="virtual-cursor" aria-label="Virtual cursor">
    <svg viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
      <rect x={left * width} y={top * height} width={(right - left) * width} height={(bottom - top) * height} fill="none" stroke="#b7ef45" strokeWidth="2" />
      <path d={`M ${sx} ${sy} L ${x} ${y}`} stroke="#b7ef45" strokeWidth="2" strokeDasharray="5 4" />
      <circle cx={x} cy={y} r="9" fill="none" stroke="#b7ef45" strokeWidth="2" />
      <path d={`M ${cx} ${cy} l 0 20 5 -5 5 9 5 -3 -5 -9 7 -1 Z`} fill="white" stroke="#172000" strokeWidth="2" />
    </svg>
    <span role="status">{labels[pointer.phase]}{a.distancePoints !== null && ` · ${pointer.phase === "CLICKED" ? "移動距離" : "対象まで"} ${Math.round(a.distancePoints)} pt`}</span>
  </div>;
}
