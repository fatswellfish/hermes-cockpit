// End-to-end proof against the BUILT bundle: does the sidebar label follow a
// locale switch the way the real host drives it?
//
// __zh_check.mjs ⑥ already does this against a fake ctx. This one is the same
// check but loads the installed artifact (plugin.js, i18n inlined) and mimics
// i18n/plugin-i18n.ts exactly:
//
//   t: (key, ...args) => translatePlugin(pluginId, getRuntimeI18nLocale(), key, args)
//
// — i.e. `t` reads the ACTIVE locale per call, not the one at registration
// time. If the sidbar label still reads English after the switch, the bug is
// in our registration, not in the host.
import { readFileSync, writeFileSync, unlinkSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const BUNDLE = path.join(HERE, 'plugin.js')

let failed = 0
const check = (name, fn) => {
  try {
    const note = fn()
    console.log(`PASS  ${name}${note ? `  — ${note}` : ''}`)
  } catch (e) {
    failed++
    console.log(`FAIL  ${name}  — ${e.message}`)
  }
}
const assert = (c, m) => { if (!c) throw new Error(m) }

const PID = process.pid
const files = {
  sdk: path.join(HERE, `__e2e_sdk_${PID}.mjs`),
  jsx: path.join(HERE, `__e2e_jsx_${PID}.mjs`),
  react: path.join(HERE, `__e2e_react_${PID}.mjs`),
  bundle: path.join(HERE, `__e2e_plugin_${PID}.mjs`)
}

writeFileSync(files.sdk, `
export const host = { notify: () => {}, navigate: () => {}, openSession: () => {} }
export const Tip = p => ({ type: 'Tip', props: p })
export const cn = (...a) => a.filter(Boolean).join(' ')
export const atom = init => { let v = init; const subs = []; return {
  get: () => v,
  set: n => { v = typeof n === 'function' ? n(v) : n; subs.forEach(f => f(v)) },
  subscribe: f => { subs.push(f); return () => {} }
} }
export const useValue = x => (x && typeof x.get === 'function' ? x.get() : x)
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const PALETTE_AREA = 'palette'
export const ROUTES_AREA = 'routes'
`, 'utf8')
writeFileSync(files.jsx, `export const jsx = (type, props) => ({ type, props: props || {} })
export const jsxs = jsx`, 'utf8')
writeFileSync(files.react, `export const useState = v => [typeof v === 'function' ? v() : v, () => {}]
export const useEffect = () => {}
export const useCallback = f => f
export const useRef = v => ({ current: v })
export const useMemo = f => f()`, 'utf8')

writeFileSync(files.bundle, readFileSync(BUNDLE, 'utf8')
  .replace(/from '@hermes\/plugin-sdk'/g, `from './${path.basename(files.sdk)}'`)
  .replace(/from 'react\/jsx-runtime'/g, `from './${path.basename(files.jsx)}'`)
  .replace(/from 'react'/g, `from './${path.basename(files.react)}'`), 'utf8')

const cleanup = () => { for (const f of Object.values(files)) { try { unlinkSync(f) } catch { /* ignore */ } } }

let mod
try {
  mod = await import(pathToFileURL(files.bundle).href)
} finally {
  cleanup()
}

// Mirror the host: a runtime locale signal + a listener list + a registry.
const makeHost = () => {
  let locale = 'en'
  const listeners = []
  const areas = new Map()

  const ctx = {
    i18n: {
      register: () => {},
      // Same shape as plugin-i18n.ts: locale is read per call.
      t: (key, ...args) => {
        const bundle = MESSAGES[locale] || MESSAGES.en
        let s = bundle[key]
        if (s === undefined) s = MESSAGES.en[key]
        if (s === undefined) return key
        const vars = args[0]
        if (vars && typeof vars === 'object') {
          s = s.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m))
        }
        return s
      },
      onLocaleChange: (cb) => { listeners.push(cb) },
      locale: () => locale
    },
    register: (c) => {
      const arr = areas.get(c.area) || []
      arr.push(c)
      areas.set(c.area, arr)
      return () => areas.set(c.area, (areas.get(c.area) || []).filter((x) => x !== c))
    },
    registerMany: (cs) => {
      const ds = cs.map((c) => ctx.register(c))
      return () => ds.forEach((d) => d())
    },
    onDispose: () => {}
  }

  return {
    ctx,
    areas,
    nav: () => (areas.get('sidebar.nav') || []).map((c) => c.data.label),
    palette: () => (areas.get('palette') || []).map((c) => c.data.label),
    switchTo: (next) => {
      locale = next
      // The host fires listeners after the signal flips (runtime.ts).
      listeners.slice().forEach((cb) => cb())
    }
  }
}

const { MESSAGES } = await import(pathToFileURL(path.join(HERE, 'src', 'i18n.js')).href)

const hostSim = makeHost()
mod.default.register(hostSim.ctx)

check('① 产物在 en 下注册：侧栏一个 Project Hub', () => {
  const nav = hostSim.nav()
  assert(nav.length === 1, `实际 ${nav.length} 项：${nav.join(' | ')}`)
  assert(nav[0] === 'Project Hub', `实际：${nav[0]}`)
  return nav[0]
})

check('② 切到 zh 后侧栏变为中文，且不重复', () => {
  hostSim.switchTo('zh')
  const nav = hostSim.nav()
  assert(nav.length === 1, `切语言后应仍只有 1 项，实际 ${nav.length}：${nav.join(' | ')}`)
  assert(nav[0] === MESSAGES.zh['plugin.name'], `期望「${MESSAGES.zh['plugin.name']}」，实际：${nav[0]}`)
  assert(/[\u4e00-\u9fa5]/.test(nav[0]), `不含中文：${nav[0]}`)
  return nav[0]
})

check('③ 命令面板同步变中文', () => {
  const p = hostSim.palette()
  assert(p.length === 1, `实际 ${p.length}：${p.join(' | ')}`)
  assert(p[0] === MESSAGES.zh['plugin.open'], `期望「${MESSAGES.zh['plugin.open']}」，实际：${p[0]}`)
  return p[0]
})

check('④ 切回 en 能还原（不会被钉死在中文）', () => {
  hostSim.switchTo('en')
  const nav = hostSim.nav()
  assert(nav.length === 1, `实际 ${nav.length}：${nav.join(' | ')}`)
  assert(nav[0] === 'Project Hub', `实际：${nav[0]}`)
  return nav[0]
})

check('⑤ 反复切换不累积（5 轮后仍各 1 项）', () => {
  for (let i = 0; i < 5; i++) {
    hostSim.switchTo(i % 2 === 0 ? 'zh' : 'en')
  }
  hostSim.switchTo('zh')
  const nav = hostSim.nav()
  const pal = hostSim.palette()
  assert(nav.length === 1, `侧栏累积到 ${nav.length} 项：${nav.join(' | ')}`)
  assert(pal.length === 1, `面板累积到 ${pal.length} 项：${pal.join(' | ')}`)
  return `侧栏 ${nav.length} 项 / 面板 ${pal.length} 项`
})

console.log(`\n${failed === 0 ? 'all passed' : `${failed} failed`}  (${5 - failed}/5)`)
process.exit(failed === 0 ? 0 : 1)
