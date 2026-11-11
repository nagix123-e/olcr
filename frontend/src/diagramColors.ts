export const DIAGRAM_COLORS = {
  black: {ja:'黒', en:'Black', stroke:'#242424', text:'#242424'},
  red: {ja:'淡い赤', en:'Soft red', stroke:'#D5949B', text:'#A4515D'},
  blue: {ja:'淡い青', en:'Soft blue', stroke:'#94B2DB', text:'#416C9E'},
  yellow: {ja:'淡い黄', en:'Soft yellow', stroke:'#D8C073', text:'#7F6A2B'},
} as const;
export type DiagramColor = keyof typeof DIAGRAM_COLORS;
export const diagramColor=(color:DiagramColor|null|undefined,role:'stroke'|'text')=>DIAGRAM_COLORS[color||'black'][role];
export const validDiagramColor=(color:unknown)=>color===undefined||color===null||typeof color==='string'&&Object.hasOwn(DIAGRAM_COLORS,color);
