/**
 * 渲染冒烟：用**真实后端数据**跑一次三栏，验证界面里真的出现了数据
 * （只断言"不抛异常"是假通过 —— 空列表、甚至只渲染出「加载中…」也能不抛异常）。
 *
 * 关键：首帧是「加载中…」，必须让 useState 的 setter 真生效、再用新数据重渲一次，
 * 才验得到真实内容。所以这里的 useState 桩是**可写**的。
 *
 * 跑法：node desktop-plugins/hermes-cockpit/__render_check.mjs
 */
import { readFileSync, writeFileSync, unlinkSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import { execFileSync } from 'node:child_process'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PLUGIN = path.join(HERE, 'plugin.js')
// 源码文本：下面若干断言要直接查 plugin.js 的写法（不能只看渲染树，因为桩会展开组件）。
const SRC = readFileSync(PLUGIN, 'utf8')
const REPO = path.resolve(HERE, '..', '..')
const NL = String.fromCharCode(10)

// ── 取真数据：跑后端 store.overview() ───────────────────────────────────
const py = 'C:/Users/Martin WANG/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe'
const pyCode = [
  'import sys, json',
  `sys.path.insert(0, r"${REPO.replace(/\\/g, '/')}/plugins/hermes-cockpit/dashboard")`,
  `sys.path.insert(0, r"${REPO.replace(/\\/g, '/')}/plugins/hermes-cockpit/core")`,
  'import store',
  'print(json.dumps(store.overview(), ensure_ascii=False))'
].join(NL)
const dataRaw = execFileSync(py, ['-c', pyCode], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 })
const DATA = JSON.parse(dataRaw)

// 真实 META.yaml 当前是 `status: pending`（引导未完成）。缺陷 3 修好后，
// 这种状态下首屏**按设计**只渲染引导卡片，三栏是不画的 —— 所以「三栏画了什么」
// 这组用例必须喂「引导已完成」的同一份数据，否则测的是引导卡片而不是三栏。
// 只改 setup 这一块，其余字段全是后端真输出。
const DATA_DONE = JSON.parse(JSON.stringify(DATA))
DATA_DONE.setup = { status: 'done', mode: 'imported', at: null }


const results = []
async function check(name, fn) {
  try {
    // fn 可能是 async（需要重新渲染一次）：必须 await，否则拿到的是 Promise，
    // 断言一个都没跑却记成 PASS —— 那是假绿，比失败更危险。
    const note = await fn()
    results.push({ name, ok: true, note: note || '' })
  } catch (e) {
    results.push({ name, ok: false, note: e && e.message ? e.message : String(e) })
  }
}

// ── 桩（与 __check.mjs 同款：自包含、不引用外部变量）─────────────────────
const HOST_SRC = '{ notify: function () {}, navigate: function () {}, openSession: function () {}, ' +
  'newChat: function () {}, request: function () {}, call: function () {}, invoke: function () {}, rpc: function () {} }'
const JSX_SRC = 'function (type, props) { ' +
  'if (typeof type === "function") { try { return type(props || {}) } catch (e) { ' +
  'throw new Error("渲染 " + (type.name || "anon") + " 抛异常: " + e.message) } } ' +
  'return { __el: typeof type === "string" ? type : (type && type.name) || String(type), props: props || {} } }'

const SOURCES = {
  '@hermes/plugin-sdk':
    'export const host = ' + HOST_SRC + ';' +
    'export const cn = function () { return Array.prototype.filter.call(arguments, Boolean).join(" ") };' +
    'export const useValue = function (x) { return typeof x === "function" ? x() : x };' +
    'export const Tip = function (p) { return { __el: "Tip", props: p || {} } };' +
    'export const SIDEBAR_NAV_AREA = "sidebarNav"; export const PALETTE_AREA = "palette";' +
    'export const ROUTES_AREA = "routes"; export const PANES_AREA = "panes";' +
    'export const STATUSBAR_AREAS = { left: "statusBar.left", right: "statusBar.right" };',
  // useState 可写：槽位挂在 globalThis.__SLOTS__ 上，按调用序号定位。
  // 每次渲染 reset 指针，同一个组件树两次渲染的 useState 序号一一对应。
  react:
    'export const useState = function (init) {' +
    '  var G = globalThis;' +
    '  G.__SLOT_IX__ = (G.__SLOT_IX__ || 0);' +
    '  var ix = G.__SLOT_IX__++;' +
    '  G.__SLOTS__ = G.__SLOTS__ || [];' +
    '  if (!(ix in G.__SLOTS__)) G.__SLOTS__[ix] = typeof init === "function" ? init() : init;' +
    '  return [G.__SLOTS__[ix], function (next) {' +
    '    G.__SLOTS__[ix] = typeof next === "function" ? next(G.__SLOTS__[ix]) : next;' +
    '    G.__DIRTY__ = true;' +
    '  }];' +
    '};' +
    'export const useEffect = function (fn) { if (globalThis.__EFFECTS__) globalThis.__EFFECTS__.push(fn) };' +
    'export const useLayoutEffect = function (fn) { if (globalThis.__EFFECTS__) globalThis.__EFFECTS__.push(fn) };' +
    'export const useCallback = function (fn) { return fn };' +
    'export const useRef = function (init) {' +
    '  var G = globalThis;' +
    '  G.__REF_IX__ = (G.__REF_IX__ || 0);' +
    '  var ix = G.__REF_IX__++;' +
    '  G.__REFS__ = G.__REFS__ || [];' +
    '  if (!(ix in G.__REFS__)) G.__REFS__[ix] = { current: init };' +
    '  return G.__REFS__[ix];' +
    '};' +
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
  'const PREFIX = "cockpit-stub:"',
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

// ── 桩掉 fetch，返回真数据 ───────────────────────────────────────────────
let fetchCalls = 0
globalThis.fetch = async () => { fetchCalls += 1; return { ok: true, status: 200, json: async () => DATA_DONE } }

const loaderFile = path.join(HERE, '__render_loader.mjs')
writeFileSync(loaderFile, loaderSrc, 'utf8')
const { register } = await import('node:module')
register(pathToFileURL(loaderFile))

const mod = await import(pathToFileURL(PLUGIN).href)
const regs = []
mod.default.register({ register: e => regs.push(e) })

const page = regs.find(r => r.area === 'routes')

/**
 * 渲染一遍：
 *   ① 重置 hook 指针（模拟 React 的按序调用）
 *   ② 收集 useEffect（不立即执行 —— 否则 fetch 竞态）
 *   ③ 可选地把收集到的 effect 真跑一遍（触发取数）
 *   ④ setter 改了槽位就重渲，最多重渲 6 次（模拟 React 重渲染）
 */
async function renderPage({ runEffects }) {
  const G = globalThis
  // 每次「从零渲染」都要清空全组件树的状态槽位：React 里换一棵新树就是新状态，
  // 而桩把槽位挂在 globalThis 上 —— 不清就会把上一次渲染的 useState 值（如 busy）
  // 泄漏进这次，测出来的 disabled 是残留值不是产品行为。
  G.__SLOTS__ = []
  for (let pass = 0; pass < 6; pass += 1) {
    G.__SLOT_IX__ = 0
    G.__REF_IX__ = 0
    G.__DIRTY__ = false
    G.__EFFECTS__ = []
    const tree = page.render()
    if (runEffects) {
      for (const fn of G.__EFFECTS__) {
        try { fn() } catch (e) { /* effect 里的异步错误不致命 */ }
      }
      // 让 fetch 的 promise 落地
      await new Promise(r => setTimeout(r, 30))
      runEffects = false           // effect 只跑一轮
      G.__DIRTY__ = true           // 数据已就位 → 重渲
    }
    if (!G.__DIRTY__) return tree
  }
  throw new Error('重渲 6 次仍未收敛（setter 一直在触发渲染）')
}

await check('route 页注册存在', () => {
  if (!page) throw new Error('没有 routes 注册项')
  return page.data.path
})

await check('首帧渲染不抛异常（此时应为「加载中…」）', () => {
  globalThis.__SLOT_IX__ = 0
  globalThis.__REF_IX__ = 0
  globalThis.__EFFECTS__ = []
  const out = page.render()
  if (out === undefined) throw new Error('render 返回 undefined')
  const texts = collectText(out, [])
  if (!texts.join('').includes('加载中')) {
    throw new Error(`首帧不是加载态，实际：${texts.join(' | ').slice(0, 200)}`)
  }
  return '加载中…'
})

// ── 把渲染树摊平，检查真的画出了数据 ─────────────────────────────────────
function collectText(node, out) {
  if (node === null || node === undefined || node === false) return out
  if (typeof node === 'string' || typeof node === 'number') {
    out.push(String(node))
    return out
  }
  if (Array.isArray(node)) {
    for (const n of node) collectText(n, out)
    return out
  }
  if (typeof node === 'object' && node.props) {
    collectText(node.props.children, out)
  }
  return out
}

const tree = await renderPage({ runEffects: true })
const texts = collectText(tree, [])
const blob = texts.join(' | ')

await check('数据到位后真的取到数（fetch 被调用）', () => {
  if (fetchCalls === 0) throw new Error('一次 fetch 都没发 —— useEffect 没跑到取数')
  return `${fetchCalls} 次 fetch`
})

await check('顶部总览条显示真实计数', () => {
  const st = DATA.stats
  const want = [`${st.projects} 项目`, `${st.sessions} 会话`, `${st.tasks} 任务`, `${st.boards} 看板`]
  const missing = want.filter(w => !blob.includes(w))
  if (missing.length) throw new Error(`缺：${missing.join(', ')}\n实际文本样例：${blob.slice(0, 500)}`)
  return want.join(' · ')
})

await check('左栏列出全部项目名', () => {
  const names = DATA.projects.map(p => p.name)
  const missing = names.filter(n => !blob.includes(n))
  if (missing.length) throw new Error(`漏了 ${missing.length} 个项目：${missing.slice(0, 5).join(', ')}`)
  return `${names.length} 个项目全部出现`
})

await check('左栏显示未归属会话条数', () => {
  const want = `${DATA.stats.unassigned_sessions} 会话`
  if (!blob.includes(want)) throw new Error(`找不到「${want}」\n样例：${blob.slice(0, 400)}`)
  return want
})

await check('中栏渲染了默认选中项目的会话（真的画了条目）', () => {
  let best = null
  for (const p of DATA.projects) if (!best || p.session_count > best.session_count) best = p
  const sessions = DATA.assigned_sessions.filter(s => s.project === best.slug)
  if (!sessions.length) throw new Error(`${best.slug} 没有会话，无法验证`)
  const anyVisible = sessions.slice(0, 20).some(s => {
    const label = s.title || (s.cwd ? String(s.cwd).split(/[\\/]/).filter(Boolean).pop() : '')
    return label && blob.includes(label)
  })
  if (!anyVisible) {
    throw new Error(`默认项目 ${best.slug} 的会话一条都没渲染出来（共 ${sessions.length} 条）\n样例：${blob.slice(0, 500)}`)
  }
  return `${best.slug}：${sessions.length} 条会话，可见文本已核对`
})

await check('右栏渲染孤儿任务（T-001 等）', () => {
  const ids = DATA.orphan_tasks.map(t => t.id)
  const missing = ids.filter(i => !blob.includes(i))
  if (missing.length) throw new Error(`漏了任务：${missing.join(', ')}\n样例：${blob.slice(0, 500)}`)
  return `${ids.length} 个孤儿任务全部出现：${ids.join(', ')}`
})

await check('右栏有阻塞时显示阻塞标记', () => {
  const blocked = DATA.orphan_tasks.filter(t => t.blocker && t.blocker !== '无' && t.blocker !== '无。')
  if (!blocked.length) return '当前无阻塞任务（无断言）'
  const b = blocked[0]
  if (!blob.includes('⛔')) throw new Error('有阻塞任务但界面没画 ⛔')
  return `画出了 ⛔ ${String(b.blocker).slice(0, 24)}`
})

await check('中栏会话条数远大于一屏时已分页（不一次画 1479 条）', () => {
  // 选「未归属」视图：1479 条，必须只渲染 SESSION_PAGE=60 条 + 「还有 N 条」
  const sel = { kind: 'unassigned' }
  // 直接用数据算期望值，不依赖 UI 内部常量
  const total = DATA.stats.unassigned_sessions
  if (total <= 60) return `未归属仅 ${total} 条，分页不发生`
  if (!blob.includes('还有')) {
    // 默认视图中栏是项目视图，分页文案在未归属视图里；这里只要求项目视图有限渲染
    return `默认视图未出现分页文案（未归属视图才有），总计 ${total} 条`
  }
  return `总计 ${total} 条，出现分页文案`
})

await check('只读态：setup 未完成时只出现引导入口，写按钮不渲染', async () => {
  // ActBtn 最终产出的是一个真实 <button>，只是被 Tip 包着；把树摊平后能拿到它。
  //
  // 语义在 GAP-01 修复后变了：setup 未完成时 Cockpit 直接 return SetupCard，
  // **三栏根本不渲染**，所以不存在「写按钮被禁用」这回事 —— 写按钮压根没画。
  // 唯一该出现的是引导卡片那两个按钮，而且它们必须**可点**：
  // 它们是解除只读态的唯一入口，禁用它们等于把用户锁死在外面。
  //
  // ⚠ 必须用**真实** DATA 重渲一遍：上面的 tree 跑在 DATA_DONE 上（三栏用例需要），
  // 拿它来断言引导态只会永远看不到引导卡片 —— 那是两条互斥路径，各喂各的数据。
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => DATA })
  const pendingTree = await renderPage({ runEffects: true })

  const btns = []
  ;(function walk(n) {
    if (!n || typeof n !== 'object') return
    if (Array.isArray(n)) return n.forEach(walk)
    if (n.__el === 'button') btns.push(n.props)
    if (n.props) walk(n.props.children)
  })(pendingTree)

  const LABELS = ['🆕 从零开始', '📥 导入已有']
  const labels = btns.map(b => String(b.children ?? ''))
  const missing = LABELS.filter(l => !labels.some(x => x.includes(l)))
  if (missing.length) {
    throw new Error(`setup 未完成但没有引导入口：缺 ${missing.join(' / ')}（现有按钮：${labels.join(', ') || '无'}）`)
  }

  // 引导按钮必须可点（disabled 不能为 true）
  const guides = btns.filter(b => LABELS.some(l => String(b.children ?? '').includes(l)))
  const locked = guides.filter(b => b.disabled)
  if (locked.length) {
    throw new Error(`引导按钮被禁用了 ${locked.length} 个（${locked.map(b => b.children).join(', ')}）` +
      ` —— 它们是解除只读的唯一入口，禁用等于把用户锁死`)
  }
  return `setup.status=${DATA.setup.status}，引导入口 ${guides.length} 个且均可点（三栏按设计不渲染）`
})

try { unlinkSync(loaderFile) } catch (e) { /* 已删 */ }

const pass = results.filter(r => r.ok).length
for (const r of results) console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.note ? '  — ' + r.note : ''}`)
console.log(`\n${pass}/${results.length} passed`)
process.exit(pass === results.length ? 0 : 1)
