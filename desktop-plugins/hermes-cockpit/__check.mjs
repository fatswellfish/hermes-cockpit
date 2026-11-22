/**
 * __check.mjs — hermes-cockpit 桌面插件的自测（真跑，不靠"应该能行"）。
 *
 * 做法：把 `@hermes/plugin-sdk`、`react`、`react/jsx-runtime` 桩掉，真 import 一次
 * plugin.js，真调 `register(ctx)`，真跑每个注册项的 render，再喂真 HTTP 响应验写语义。
 *
 * ★ 三个实测踩过的坑（换实现之前先看这里）：
 *   1. loader 跑在**独立 worker 线程**，拿不到主线程的 globalThis；
 *   2. `register(url, { data })` 的 data 是 structuredClone 过去的，**函数传不过去**，
 *      而且实测普通对象到 worker 里会退化（键名丢失）；
 *   3. 生成的桩模块源码是**独立模块作用域**，引用 loader 里的变量会 ReferenceError。
 *   ⇒ 结论：桩的源码必须**自包含**——每个导出直接内联函数体，不引用任何外部变量。
 *      下面的 BUILD_STUB_SOURCE 就是干这个的。
 *
 * 覆盖（对应第五轮任务 4）：
 *   ① export default 有 id / name / register
 *   ② register(ctx) 调用 ctx.register ≥ 4 次，id 不重复
 *   ③ 注册的 area 覆盖 panes + 侧栏 + 路由 + ⌘K，元数据齐全
 *   ④ 无 JSX 语法残留（正则扫 `<[A-Z]`）
 *   ⑤ 每个注册项的 render 首帧不抛异常
 *   ⑥ 写操作真语义：409 stale → 提示 + 自动 reload；403 → 原因原样透出；200 → 零误报
 *   ⑦ 边界：无硬编码颜色、不直连宿主库、不绕 API 前缀
 *
 * 跑法：node desktop-plugins/hermes-cockpit/__check.mjs
 * 退出码：全通过 0，有失败 1。
 */
import { readFileSync, writeFileSync, unlinkSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
import assert from 'node:assert/strict'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PLUGIN = path.join(HERE, 'plugin.js')
const SRC = readFileSync(PLUGIN, 'utf8')

const results = []
async function check(name, fn) {
  try {
    const note = await fn()
    results.push({ name, ok: true, note: note || '' })
  } catch (e) {
    results.push({ name, ok: false, note: e && e.message ? e.message : String(e) })
  }
}

// ── 桩：每个模块的源码自包含（不引用外部变量）──────────────────────────────

/** 函数型桩的源码。参数是导出名，返回可直接写进模块的表达式源码。 */
function fnSource(name) {
  switch (name) {
    case 'host':
      return '{ notify: function () {}, navigate: function () {}, openSession: function () {}, ' +
             'newChat: function () {}, request: function () {}, call: function () {}, ' +
             'invoke: function () {}, rpc: function () {} }'
    case 'cn':
      return 'function () { return Array.prototype.filter.call(arguments, Boolean).join(" ") }'
    case 'useValue':
      return 'function (x) { return typeof x === "function" ? x() : x }'
    case 'jsx':
    case 'jsxs':
      return 'function (type, props) { return { __el: type, props: props || {} } }'
    case 'useEffect':
    case 'useLayoutEffect':
      return 'function (fn) {}'
    case 'useState':
      return 'function (init) { return [typeof init === "function" ? init() : init, function () {}] }'
    case 'useCallback':
      return 'function (fn) { return fn }'
    case 'useRef':
      return 'function (init) { return { current: init } }'
    case 'useMemo':
      return 'function (fn) { return fn() }'
    case 'createContext':
      return 'function (d) { return { _current: d } }'
    case 'useContext':
      return 'function (c) { return c._current }'
    default:
      return null
  }
}

/** 常量型桩（字符串原样导出）。 */
const CONST_STUBS = {
  '@hermes/plugin-sdk': {
    Tip: 'Tip',
    SIDEBAR_NAV_AREA: 'sidebarNav',
    PALETTE_AREA: 'palette',
    ROUTES_AREA: 'routes',
    PANES_AREA: 'panes'
  },
  'react/jsx-runtime': { Fragment: 'Fragment' }
}

/** 每个模块要导出的名字（函数型 + 常量型）。 */
const MODULE_EXPORTS = {
  '@hermes/plugin-sdk': ['host', 'cn', 'useValue', 'Tip', 'SIDEBAR_NAV_AREA', 'PALETTE_AREA', 'ROUTES_AREA', 'PANES_AREA'],
  react: ['useState', 'useEffect', 'useLayoutEffect', 'useCallback', 'useRef', 'useMemo', 'createContext', 'useContext'],
  'react/jsx-runtime': ['jsx', 'jsxs', 'Fragment']
}

/** 生成一个自包含的桩模块源码。 */
function moduleSource(spec) {
  const lines = []
  for (const name of Object.keys(spec)) {
    lines.push(`export const ${name} = ${spec[name]}`)
  }
  return lines.join('; ')
}

// STATUSBAR_AREAS 是对象，单独处理
const SDK_SPEC = {}
for (const name of MODULE_EXPORTS['@hermes/plugin-sdk']) {
  SDK_SPEC[name] = fnSource(name) || JSON.stringify(CONST_STUBS['@hermes/plugin-sdk'][name])
}
SDK_SPEC['STATUSBAR_AREAS'] = JSON.stringify({ left: 'statusBar.left', right: 'statusBar.right' })

const REACT_SPEC = {}
for (const name of MODULE_EXPORTS.react) REACT_SPEC[name] = fnSource(name)

const RT_SPEC = {}
for (const name of MODULE_EXPORTS['react/jsx-runtime']) RT_SPEC[name] = fnSource(name) || JSON.stringify('Fragment')

const STUB_SOURCES = {
  '@hermes/plugin-sdk': moduleSource(SDK_SPEC),
  react: moduleSource(REACT_SPEC),
  'react/jsx-runtime': moduleSource(RT_SPEC)
}

const NL = String.fromCharCode(10)
const loaderSrc = [
  'const SOURCES = ' + JSON.stringify(STUB_SOURCES),
  'const PREFIX = "cockpit-stub:"',
  'function keyOf(spec) {',
  '  return Object.prototype.hasOwnProperty.call(SOURCES, spec) ? spec : null',
  '}',
  'export async function resolve(spec, ctx, next) {',
  '  const k = keyOf(spec)',
  '  if (k) return { url: PREFIX + encodeURIComponent(k), shortCircuit: true }',
  '  return await next(spec, ctx)',
  '}',
  'export async function load(url, ctx, next) {',
  '  if (url.indexOf(PREFIX) !== 0) return await next(url, ctx)',
  '  const key = decodeURIComponent(url.slice(PREFIX.length))',
  '  return { format: "module", shortCircuit: true, source: SOURCES[key] }',
  '}'
].join(NL)

// ── 静态检查（不需要执行插件）────────────────────────────────────────────

await check('④ 无 JSX 语法残留（无 `<大写` 标签）', () => {
  const hits = []
  SRC.split('\n').forEach((line, i) => {
    const bare = line
      .replace(/'(?:[^'\\]|\\.)*'/g, "''")
      .replace(/"(?:[^"\\]|\\.)*"/g, '""')
      .replace(/`(?:[^`\\]|\\.)*`/g, '``')
      .replace(/\/\*.*?\*\//g, '')
      .replace(/\/\/.*$/, '')
    if (/<[A-Z]/.test(bare)) hits.push(`${i + 1}: ${line.trim()}`)
  })
  assert.equal(hits.length, 0, `JSX 残留：\n${hits.join('\n')}`)
  return '0 处'
})

await check('⑦a 无硬编码颜色（#d9534f 为现役插件同款警示色，允许）', () => {
  const hex = SRC.match(/#[0-9a-fA-F]{3,8}\b/g) || []
  const bad = hex.filter(h => h.toLowerCase() !== '#d9534f')
  assert.equal(bad.length, 0, `硬编码颜色：${bad.join(', ')}`)
  return `${hex.length} 处，均为 #d9534f`
})

await check('⑦b 不直连宿主库、不改 cwd、不用宿主 RPC', () => {
  const forbidden = ['sqlite', 'kanban.db', 'projects.db', 'state.db', 'chdir', 'process.cwd']
  const hits = forbidden.filter(t => SRC.includes(t))
  assert.equal(hits.length, 0, `出现禁用标记：${hits.join(', ')}`)
  assert.ok(!/host\.(request|call|invoke|rpc)\s*\(/.test(SRC), '出现了 host.request/call/invoke/rpc')
  return '干净'
})

await check('⑦c 数据只走 /api/plugins/hermes-cockpit', () => {
  assert.ok(SRC.includes('const API = `/api/plugins/${ID}`'), 'API 前缀不是 /api/plugins/${ID}')
  const bare = SRC.match(/fetch\(\s*['"`]\/api\//g) || []
  assert.equal(bare.length, 0, `存在绕过 API 常量的裸路径 fetch：${bare.join(', ')}`)
  const n = (SRC.match(/fetch\(/g) || []).length
  assert.ok(n > 0, '一处 fetch 都没有')
  return `${n} 处 fetch，前缀统一`
})

// ── 真 import 插件 ────────────────────────────────────────────────────────

const loaderFile = path.join(HERE, '__check_loader.mjs')
writeFileSync(loaderFile, loaderSrc, 'utf8')
const { register } = await import('node:module')
register(pathToFileURL(loaderFile))

let mod = null
try {
  mod = await import(pathToFileURL(PLUGIN).href)
  results.push({
    name: '0 插件可被 import（SDK / react 全部桩掉）',
    ok: true,
    note: 'plugin.js 语法与模块形状成立'
  })
} catch (e) {
  results.push({
    name: '0 插件可被 import（SDK / react 全部桩掉）',
    ok: false,
    note: e && e.message ? e.message : String(e)
  })
}

const def = mod && mod.default
const called = { register: [], notify: [], navigate: [], openSession: [] }
const ctx = { register: e => called.register.push(e) }

if (def) {
  await check('① export default 有 id / name / register', () => {
    assert.equal(def.id, 'hermes-cockpit')
    assert.equal(typeof def.name, 'string')
    assert.ok(def.name.length > 0)
    assert.equal(typeof def.register, 'function')
    return `id=${def.id} name=${def.name}`
  })

  await check('①b register(ctx) 不抛异常', () => {
    def.register(ctx)
    return `${called.register.length} 次注册`
  })

  await check('② ctx.register 调用 ≥ 4 次，id 不重复', () => {
    assert.ok(called.register.length >= 4, `只注册了 ${called.register.length} 个`)
    for (const e of called.register) {
      assert.ok(e.id, `注册项缺 id：${JSON.stringify(e)}`)
      assert.ok(e.area, `注册项 ${e.id} 缺 area`)
    }
    const ids = called.register.map(e => e.id)
    assert.equal(new Set(ids).size, ids.length, `注册 id 重复：${ids.join(', ')}`)
    return `${called.register.length} 个：${ids.join(', ')}`
  })

  await check('③ area 覆盖 panes + 侧栏 + 路由 + ⌘K', () => {
    const areas = new Set(called.register.map(e => e.area))
    const need = { panes: 'panes', sidebarNav: '侧栏', routes: '路由', palette: '⌘K' }
    const missing = Object.keys(need).filter(a => !areas.has(a)).map(a => need[a])
    assert.equal(missing.length, 0, `缺 area：${missing.join(', ')}`)
    return Array.from(areas).join(' + ')
  })

  await check('③b 元数据：panes 带 placement/title/width，route/侧栏带 path，⌘K 命令可执行', () => {
    const byId = Object.fromEntries(called.register.map(e => [e.id, e]))
    assert.equal(byId['cockpit-pane'].data.placement, 'right')
    assert.ok(byId['cockpit-pane'].data.title)
    assert.ok(byId['cockpit-pane'].data.width)
    assert.equal(byId['cockpit-page'].data.path, '/cockpit')
    assert.equal(byId['cockpit-nav'].data.path, '/cockpit')
    assert.ok(byId['cockpit-nav'].data.label)
    assert.ok(byId['cockpit-nav'].data.codicon, '侧栏入口缺 codicon')
    assert.equal(typeof byId['cockpit-cmd'].data.run, 'function')
    assert.equal(byId['cockpit-cmd'].data.label, '打开项目中枢')
    // host.navigate 跑在 loader worker 里，主线程观察不到调用；
    // 这里至少真执行一次确认不抛，再用源码断言它确实指向 /cockpit
    byId['cockpit-cmd'].data.run()
    assert.ok(/run:\s*\(\)\s*=>\s*host\.navigate\('\/cockpit'\)/.test(SRC),
              '⌘K 命令没有导航到 /cockpit')
    return 'panes / routes / sidebarNav / palette 元数据齐全，⌘K run() 可执行且指向 /cockpit'
  })

  await check('⑤ 每个注册项的 render 都能真跑（首帧不抛异常）', () => {
    const rendered = []
    for (const e of called.register) {
      if (typeof e.render !== 'function') continue
      const out = e.render()
      assert.ok(out !== undefined, `${e.id} 的 render 返回 undefined`)
      rendered.push(e.id)
    }
    assert.ok(rendered.length >= 2, `只有 ${rendered.length} 个可渲染项`)
    return rendered.join(', ')
  })
}

// ── ⑥ 写操作真语义 ────────────────────────────────────────────────────────
//
// 注意：桩模块的函数体跑在 loader 的 **worker 线程**里，主线程改不动它的 host.notify。
// 所以这里不断言「notify 被调用」，而是断言能跨线程观察到的两件事：
//   · 返回值语义（ok / code / data）
//   · **reload 回调有没有被调用**（callback 是主线程传进去的，能观察）
// 提示文案本身另行用源码断言保证它真的写了那句中文。

if (def) {
  const realFetch = globalThis.fetch
  let reloaded = 0
  const respond = (status, json) => async () => ({ ok: status < 400, status, json: async () => json })

  await check('⑥a 409 stale → 自动 reload（跨线程可观察）+ 源码含刷新文案', async () => {
    const probe = mod.__writeProbe
    assert.ok(probe && typeof probe.postJSON === 'function',
              'plugin.js 未导出 __writeProbe.postJSON（自测钩子缺失）')
    globalThis.fetch = respond(409, { detail: '文件已被他人修改，请刷新后重试', code: 'stale' })
    reloaded = 0
    const r = await probe.postJSON('/tasks/toggle', { id: 'T-1' }, () => { reloaded += 1 })
    assert.equal(r.ok, false)
    assert.equal(r.code, 409)
    assert.equal(r.reason, 'stale')
    assert.equal(reloaded, 1, '409 时没有自动 reload —— 用户操作会被静默丢掉')
    assert.ok(SRC.includes("'文件已被改动，正在刷新…'"),
              '源码里没有找到 409 的刷新提示文案')
    // 409 分支必须发 warning 通知，不能静默
    assert.ok(/code === 409[\s\S]{0,200}host\.notify\(\{\s*kind:\s*'warning'/.test(SRC.replace(/\n\s*/g, ' ')),
              '409 分支没有 host.notify warning')
    return `reload×${reloaded}，reason=stale，文案与 warning 通知均在源码中`
  })

  await check('⑥b 403 越界 → 原因原样透出，且不当成 stale 去 reload', async () => {
    globalThis.fetch = respond(403, {
      detail: '该会话在 fresh 作用域之外（早于 setup.at），不可归类',
      code: 'out_of_scope'
    })
    reloaded = 0
    const r = await mod.__writeProbe.postJSON('/sessions/assign',
      { session_id: 's', project: 'p' }, () => { reloaded += 99 })
    assert.equal(r.ok, false)
    assert.equal(r.code, 403)
    assert.equal(r.reason, 'out_of_scope')
    assert.equal(reloaded, 0, '403 不是并发冲突，不该当成 stale 去 reload')
    assert.ok(r.message.includes('fresh 作用域之外'), `403 原因没有透出：${r.message}`)
    return `reason=out_of_scope，message 原样透出，未触发 reload`
  })

  await check('⑥c 200 成功 → data 透传、不触发 reload', async () => {
    globalThis.fetch = respond(200, { ok: true, session_id: 'local-x' })
    reloaded = 0
    const r = await mod.__writeProbe.postJSON('/sessions/new', { cwd: 'C:/x' }, () => { reloaded += 1 })
    assert.equal(r.ok, true)
    assert.equal(r.data.session_id, 'local-x')
    assert.equal(reloaded, 0, '成功路径不该触发 stale reload')
    return 'ok=true，data 透传，未误触发 reload'
  })

  globalThis.fetch = realFetch
}

// ── 汇总 ──────────────────────────────────────────────────────────────────

try { unlinkSync(loaderFile) } catch (e) { /* 已删 */ }

const pass = results.filter(r => r.ok).length
for (const r of results) console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.note ? '  — ' + r.note : ''}`)
console.log(`\n${pass}/${results.length} passed`)
process.exit(pass === results.length ? 0 : 1)
