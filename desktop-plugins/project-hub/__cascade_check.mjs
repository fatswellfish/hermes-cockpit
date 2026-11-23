/**
 * 级联检查：项目 → 会话 → todo，右栏在三种选中态下各显示什么。
 *
 * 防的缺陷：以前选中会话，右栏纹丝不动 —— 因为它只按 project.slug 过滤，
 * 级联的第三级根本不存在。这个套件对**构建产物** plugin.js 跑真实组件，
 * 断言每一种选中态到底渲染了哪些任务。
 *
 * 跑法：node desktop-plugins/project-hub/__cascade_check.mjs
 */
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PLUGIN = path.join(HERE, 'plugin.js')
const NL = String.fromCharCode(10)

// ── 造数据：会话与任务都带真实形状的 id ──────────────────────────────────
const SID_A = 'local-' + 'a'.repeat(64)
const SID_B = 'local-' + 'b'.repeat(64)

const DATA = {
  projects: [
    { slug: 'proj-a', name: 'Project A', path: 'C:/a', session_count: 2 },
    { slug: 'proj-b', name: 'Project B', path: 'C:/b', session_count: 1 }
  ],
  assigned_sessions: [
    { id: SID_A, title: 'Session A', project: 'proj-a', cwd: 'C:/a' },
    { id: SID_B, title: 'Session B', project: 'proj-b', cwd: 'C:/b' }
  ],
  unassigned_sessions: [],
  // T-1/T-2 由会话 A 引出；T-3 由会话 B 引出；
  // T-4 属 proj-a 但没写 session（要靠项目兜底带出）；
  // T-9 没有任何项目认领（未归属，任何选中态下都不许凭空消失）。
  tasks: [
    { id: 'T-1', title: 'raised by A', status: 'doing', progress: 50, project: 'proj-a', session: SID_A, source: 'agent', subtasks: [] },
    { id: 'T-2', title: 'also A', status: 'todo', progress: 0, project: 'proj-a', session: SID_A, source: 'agent', subtasks: [] },
    { id: 'T-3', title: 'raised by B', status: 'todo', progress: 0, project: 'proj-b', session: SID_B, source: 'work', subtasks: [] },
    { id: 'T-4', title: 'no session, project fallback', status: 'todo', progress: 0, project: 'proj-a', session: null, source: 'work', subtasks: [] }
  ],
  orphan_tasks: [
    { id: 'T-9', title: 'nobody claims me', status: 'todo', progress: 0, project: null, session: SID_B, source: 'home', subtasks: [], blocker: 'blocked by X' }
  ],
  stats: {}, stamp: ['x', 'y'], warnings: []
}

// ── 桩（与 __render_check.mjs 同款：自包含，不引用外部变量）──────────────
const HOST_SRC = '{ notify: function () {}, navigate: function () {}, openSession: function () {}, ' +
  'newChat: function () {}, request: function () {} }'
const JSX_SRC = 'function (type, props) { ' +
  'if (typeof type === "function") { try { return type(props || {}) } catch (e) { ' +
  'throw new Error("渲染 " + (type.name || "anon") + " 抛异常: " + e.message) } } ' +
  'return { __el: typeof type === "string" ? type : (type && type.name) || String(type), props: props || {} } }'

const SOURCES = {
  '@hermes/plugin-sdk':
    'export const host = ' + HOST_SRC + ';' +
    'export const cn = function () { return Array.prototype.filter.call(arguments, Boolean).join(" ") };' +
    'export const atom = function (init) { var v = init; var subs = []; ' +
    'return { get: function () { return v }, ' +
    'set: function (n) { v = typeof n === "function" ? n(v) : n; ' +
    'subs.forEach(function (f) { f(v) }) }, ' +
    'subscribe: function (f) { subs.push(f); return function () {} } } };' +
    'export const useValue = function (x) { return typeof x === "function" ? x() : x };' +
    'export const Tip = function (p) { return { __el: "Tip", props: p || {} } };' +
    'export const SIDEBAR_NAV_AREA = "sidebarNav"; export const PALETTE_AREA = "palette";' +
    'export const ROUTES_AREA = "routes"; export const PANES_AREA = "panes";' +
    'export const STATUSBAR_AREAS = { left: "statusBar.left", right: "statusBar.right" };',
  react:
    'export const useState = function (init) {' +
    '  var G = globalThis; G.__SLOT_IX__ = (G.__SLOT_IX__ || 0); var ix = G.__SLOT_IX__++;' +
    '  G.__SLOTS__ = G.__SLOTS__ || [];' +
    '  if (!(ix in G.__SLOTS__)) G.__SLOTS__[ix] = typeof init === "function" ? init() : init;' +
    '  return [G.__SLOTS__[ix], function (n) { G.__SLOTS__[ix] = n; }]; };' +
    'export const useEffect = function () {};' +
    'export const useLayoutEffect = function () {};' +
    'export const useCallback = function (fn) { return fn };' +
    'export const useRef = function (init) { return { current: init } };' +
    'export const useMemo = function (fn) { return fn() };' +
    'export const createContext = function (d) { return { _current: d } };' +
    'export const useContext = function (c) { return c._current };',
  'react/jsx-runtime':
    'export const jsx = ' + JSX_SRC + ';' +
    'export const jsxs = ' + JSX_SRC + ';' +
    'export const Fragment = "Fragment";'
}

const loaderSrc = [
  'const SOURCES = ' + JSON.stringify(SOURCES),
  'const PREFIX = "cascade-stub:"',
  'export async function resolve(spec, ctx, next) {',
  '  if (Object.prototype.hasOwnProperty.call(SOURCES, spec)) {',
  '    return { url: PREFIX + encodeURIComponent(spec), shortCircuit: true }',
  '  }',
  '  return await next(spec, ctx)',
  '}',
  'export async function load(url, ctx, next) {',
  '  if (url.indexOf(PREFIX) !== 0) return await next(url, ctx)',
  '  const key = decodeURIComponent(url.slice(PREFIX.length))',
  '  return { format: "module", shortCircuit: true, source: SOURCES[key] }',
  '}'
].join(NL)

const loaderFile = path.join(HERE, '__cascade_loader.mjs')
writeFileSync(loaderFile, loaderSrc, 'utf8')
const { register } = await import('node:module')
register(pathToFileURL(loaderFile))

// 产物只 default-export 插件，RightCol 没导出 —— 追加一行导出再交给 loader。
// 用临时文件而不是 data: URL，因为 data: URL 解析不了裸模块名。
const src = readFileSync(PLUGIN, 'utf8')
const patchedFile = path.join(HERE, '__cascade_patched.mjs')
writeFileSync(patchedFile, src + NL + 'export { RightCol };' + NL, 'utf8')

const mod = await import(pathToFileURL(patchedFile).href)
const RightCol = mod.RightCol
if (typeof RightCol !== 'function') {
  console.error('FAIL  RightCol 没导出，级联检查无法进行')
  process.exit(1)
}

// ── 渲染 + 取文本 ────────────────────────────────────────────────────────
function textOf(node, depth = 0) {
  if (node == null || depth > 60) return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(n => textOf(n, depth + 1)).join(' ')
  const out = []
  if (node.props && node.props.children != null) out.push(textOf(node.props.children, depth + 1))
  if (node.children) out.push(textOf(node.children, depth + 1))
  return out.join(' ')
}

function render(selected) {
  globalThis.__SLOTS__ = []
  globalThis.__SLOT_IX__ = 0
  return RightCol({
    data: DATA, selected, readonly: true, reload: () => {}, warnings: [],
    t: k => k
  })
}

let pass = 0
const failures = []
function check(name, fn) {
  try {
    const note = fn()
    console.log(`PASS  ${name}${note ? '  — ' + note : ''}`)
    pass += 1
  } catch (e) {
    failures.push(name)
    console.log(`FAIL  ${name}  — ${e.message}`)
  }
}

// ── 断言 ─────────────────────────────────────────────────────────────────
check('选中项目：只显示该项目任务', () => {
  const s = textOf(render({ kind: 'project', slug: 'proj-a', path: 'C:/a' }))
  if (!s.includes('T-1')) throw new Error('漏了 proj-a 的 T-1')
  if (!s.includes('T-2')) throw new Error('漏了 proj-a 的 T-2')
  if (s.includes('T-3')) throw new Error('串进了 proj-b 的 T-3')
  return 'T-1/T-2 在，T-3 不在'
})

check('选中会话 A：只显示该会话引出的任务（第三级）', () => {
  const s = textOf(render({ kind: 'session', id: SID_A, project: 'proj-a', path: 'C:/a' }))
  if (!s.includes('T-1')) throw new Error('会话 A 没带出 T-1 —— 级联第三级没生效')
  if (s.includes('T-3')) throw new Error('串进了会话 B 的 T-3')
  return 'T-1 在，T-3 不在'
})

check('选中会话 A：无 session 的任务按项目兜底', () => {
  const s = textOf(render({ kind: 'session', id: SID_A, project: 'proj-a', path: 'C:/a' }))
  if (!s.includes('T-4')) throw new Error('T-4 属 proj-a 但没写 session，应兜底带出')
  return 'T-4 已兜底'
})

check('选中会话 B：看不到会话 A 的任务', () => {
  const s = textOf(render({ kind: 'session', id: SID_B, project: 'proj-b', path: 'C:/b' }))
  if (!s.includes('T-3')) throw new Error('会话 B 没带出 T-3')
  if (s.includes('T-1')) throw new Error('串进了会话 A 的 T-1')
  return 'T-3 在，T-1 不在'
})

check('选中项目时未归属任务仍然显示', () => {
  const s = textOf(render({ kind: 'project', slug: 'proj-a', path: 'C:/a' }))
  if (!s.includes('T-9')) throw new Error('未归属任务 T-9 在选项目时消失了')
  return 'T-9 仍在'
})

check('未选任何东西：显示未归属任务，不显示项目任务', () => {
  const s = textOf(render(null))
  if (!s.includes('T-9')) throw new Error('未归属任务 T-9 没显示')
  if (s.includes('T-1')) throw new Error('不该显示项目任务 T-1')
  return 'T-9 在，T-1 不在'
})

console.log(`\n${failures.length ? 'FAILED' : 'all passed'}  (${pass}/${pass + failures.length})`)
process.exit(failures.length ? 1 : 0)
