// Produce a source edit, never run project code. Parsers come from OLCR's tools.
const fs = require('node:fs');
const path = require('node:path');
const modules = path.join(process.argv[2], 'node_modules');
const parser = require(path.join(modules, '@babel/parser'));
const traverse = require(path.join(modules, '@babel/traverse')).default;
const {source, missing, errorLines, esm} = JSON.parse(fs.readFileSync(0, 'utf8'));
const ast = parser.parse(source, {sourceType: 'unambiguous'});
const native = new Set(['test','it','describe','before','after','beforeEach','afterEach']);
const used = new Set(), edits = [];
let confirmed = false, assertName = '_nodeAssert';
while (source.includes(assertName)) assertName += '_';
const slice = n => source.slice(n.start,n.end);
function plainExpected(n) {
  if (['StringLiteral','NumericLiteral','BooleanLiteral','NullLiteral'].includes(n.type)) return true;
  if (n.type === 'ArrayExpression') return n.elements.every(e => e && plainExpected(e));
  return n.type === 'ObjectExpression' && n.properties.every(p =>
    p.type === 'ObjectProperty' && !p.computed && !p.shorthand && plainExpected(p.value));
}
traverse(ast, {CallExpression(p) {
  const fn = p.node.callee;
  if (fn.type !== 'Identifier' || p.scope.hasBinding(fn.name)) return;
  if (missing !== 'expect' && native.has(fn.name)) used.add(fn.name);
  if (fn.name === missing && errorLines.includes(fn.loc.start.line)) confirmed = true;
  if (missing !== 'expect' || fn.name !== 'expect' || p.node.arguments.length !== 1) return;
  // Only convert standalone synchronous, recognized matchers. Leave custom,
  // asynchronous, asymmetric and locally bound expectations to normal review.
  let member = p.parentPath, negative = false;
  if (member.isMemberExpression({computed:false}) && member.node.property.name === 'not') {
    negative = true; member = member.parentPath;
  }
  if (!member.isMemberExpression({computed:false})) return;
  const call = member.parentPath;
  if (!call.isCallExpression() || call.node.callee !== member.node || !call.parentPath.isExpressionStatement()) return;
  const matcher = member.node.property.name, args = call.node.arguments;
  const actual = p.node.arguments[0];
  if (actual.type === 'SpreadElement' || args.some(a => a.type === 'SpreadElement')) return;
  let method, expected;
  if (matcher === 'toBeNull' && !args.length) {method='strictEqual'; expected='null';}
  else if (matcher === 'toBe' && args.length === 1) {method='strictEqual'; expected=slice(args[0]);}
  else if (matcher === 'toStrictEqual' && args.length === 1) {method='deepStrictEqual'; expected=slice(args[0]);}
  else if (matcher === 'toEqual' && !negative && args.length === 1 && plainExpected(args[0])) {
    // Use a strict comparison for literal data, never invert/relax it.
    method='deepStrictEqual'; expected=slice(args[0]);
  } else return;
  if (negative) method=method==='strictEqual'?'notStrictEqual':'notDeepStrictEqual';
  edits.push({start:call.node.start,end:call.node.end,text:`${assertName}.${method}((${slice(actual)}), (${expected}))`});
}});
if (!confirmed || (!used.size && !edits.length)) process.exit(0);
const useESM = esm || ast.program.sourceType === 'module';
const imports = [];
if (used.size) {
  const names = [...used].sort().join(', ');
  imports.push(useESM ? `import { ${names} } from 'node:test';` : `const { ${names} } = require('node:test');`);
}
if (edits.length) imports.push(useESM ? `import ${assertName} from 'node:assert/strict';` : `const ${assertName} = require('node:assert/strict');`);
const anchor = ast.program.directives.at(-1) || ast.program.interpreter;
const pos = anchor?.end || 0, nl = source.includes('\r\n') ? '\r\n' : '\n';
edits.push({start:pos,end:pos,text:(anchor?nl:'')+imports.join(nl)+nl});
let updated=source;
// Sort including replacements at offset zero before insertions at that offset.
for (const e of edits.sort((a,b)=>b.start-a.start || b.end-a.end)) updated=updated.slice(0,e.start)+e.text+updated.slice(e.end);
process.stdout.write(JSON.stringify(updated));
