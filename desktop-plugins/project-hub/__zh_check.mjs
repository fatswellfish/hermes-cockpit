// Prove the zh bundle renders Chinese at the component level.
//
// __render_check.mjs asserts against the English fallback (that is what a
// component renders with no translator prop). This one passes a real zh
// translator, which is what the host provides when display.language=zh.
import { readFileSync, writeFileSync, unlinkSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const BUNDLE = path.join(HERE, 'plugin.js')
const { MESSAGES, makeT } = await import(pathToFileURL(path.join(HERE, 'src', 'i18n.js')).href)

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

// Stub the host SDK + react the same way the other suites do, then import the
// built bundle so we exercise exactly what the app loads.
const stub = `
export const host = {
  notify: () => {}, navigate: () => {}, openSession: () => {}
}
export const Tip = p => p.children
export const cn = (...a) => a.filter(Boolean).join(' ')
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const PALETTE_AREA = 'palette'
export const ROUTES_AREA = 'routes'
`
const reactStub = `
export const useState = v => [typeof v === 'function' ? v() : v, () => {}]
export const useEffect = () => {}
export const useCallback = f => f
export const useRef = v => ({ current: v })
export const useMemo = f => f()
`
const jsxStub = `export const jsx = (type, props) => ({ type, props: props || {} })
export const jsxs = jsx`

const tmp = path.join(HERE, `.__zh_${process.pid}.mjs`)
writeFileSync(tmp, readFileSync(BUNDLE, 'utf8')
  .replace(/from '@hermes\/plugin-sdk'/g, `from './__sdk_${process.pid}.mjs'`)
  .replace(/from 'react\/jsx-runtime'/g, `from './__jsx_${process.pid}.mjs'`)
  .replace(/from 'react'/g, `from './__react_${process.pid}.mjs'`), 'utf8')
writeFileSync(path.join(HERE, `__sdk_${process.pid}.mjs`), stub, 'utf8')
writeFileSync(path.join(HERE, `__jsx_${process.pid}.mjs`), jsxStub, 'utf8')
writeFileSync(path.join(HERE, `__react_${process.pid}.mjs`), reactStub, 'utf8')

const cleanup = () => {
  // `tmp` is already an absolute path; joining it with HERE again produced a
  // nonsense path and silently left the temp module on disk.
  const paths = [
    tmp,
    path.join(HERE, `__sdk_${process.pid}.mjs`),
    path.join(HERE, `__jsx_${process.pid}.mjs`),
    path.join(HERE, `__react_${process.pid}.mjs`)
  ]
  for (const f of paths) {
    try { unlinkSync(f) } catch { /* ignore */ }
  }
}

let mod
try {
  mod = await import(pathToFileURL(tmp).href)
} finally {
  cleanup()
}

const tZh = makeT('zh')
const tEn = makeT('en')

const collectText = (n, out) => {
  if (n === null || n === undefined) return out
  if (typeof n === 'string') { out.push(n); return out }
  if (typeof n !== 'object') return out
  const kids = n.props && n.props.children
  if (Array.isArray(kids)) kids.forEach(k => collectText(k, out))
  else collectText(kids, out)
  return out
}

await check('① zh 词表里没有英文残留（除 HTTP 等技术词）', () => {
  const bad = Object.entries(MESSAGES.zh)
    .filter(([, v]) => /^[\x00-\x7F]+$/.test(v) && /[a-zA-Z]{3,}/.test(v) && !v.includes('HTTP'))
    .map(([k]) => k)
  assert(bad.length === 0, bad.join(', '))
  return `${Object.keys(MESSAGES.zh).length} 条全中文`
})

await check('② 传 zh 翻译器 → 引导卡片渲染中文按钮', () => {
  const card = mod.__setupProbe.SetupCard({
    setup: { status: 'pending', mode: null, at: null },
    reload: () => {},
    t: tZh
  })
  const texts = collectText(card, [])
  const fresh = MESSAGES.zh['setup.fresh']
  const imported = MESSAGES.zh['setup.imported']
  assert(texts.includes(fresh), `缺「${fresh}」，实际：${texts.join(' | ')}`)
  assert(texts.includes(imported), `缺「${imported}」，实际：${texts.join(' | ')}`)
  assert(texts.some(x => x.includes('还没初始化')), `缺中文标题，实际：${texts.join(' | ')}`)
  return `${fresh} / ${imported}`
})

await check('③ 传 en 翻译器 → 同一组件渲染英文（语言真的可切换）', () => {
  const card = mod.__setupProbe.SetupCard({
    setup: { status: 'pending', mode: null, at: null },
    reload: () => {},
    t: tEn
  })
  const texts = collectText(card, [])
  assert(texts.includes(MESSAGES.en['setup.fresh']), `实际：${texts.join(' | ')}`)
  assert(!texts.some(x => /[\u4e00-\u9fa5]/.test(x)), `en 里混进了中文：${texts.join(' | ')}`)
  return MESSAGES.en['setup.fresh']
})

await check('④ 关键界面词在 zh 下确实是中文', () => {
  const keys = ['plugin.name', 'mid.newSession', 'right.tasks', 'common.loading', 'mid.empty']
  for (const k of keys) {
    const v = tZh(k)
    assert(/[\u4e00-\u9fa5]/.test(v), `${k} → "${v}" 不含中文`)
  }
  return keys.map(k => `${k}=${tZh(k)}`).join(' · ')
})

await check('⑤ {var} 在 zh 下插值正确', () => {
  const out = tZh('stat.projects', { n: 14 })
  assert(out.includes('14') && /项目/.test(out), out)
  assert(!out.includes('{n}'), out)
  return out
})


await check('⑥ 切语言后侧栏 label 真的变中文（onLocaleChange 重注册）', () => {
  // Fake the host context the way runtime-loader hands it to register():
  // registerMany returns a disposer, i18n.t reads the active locale, and
  // onLocaleChange lets us flip it. This is what makes the sidebar label
  // follow the UI language instead of freezing at registration time.
  let locale = 'en'
  const localeListeners = []
  const live = new Map() // area -> [{id, data}]
  let registrations = 0

  const ctx = {
    i18n: {
      register: () => {},
      t: (key, vars) => makeT(locale)(key, vars),
      onLocaleChange: (cb) => { localeListeners.push(cb) },
      locale: () => locale
    },
    register: (contrib) => {
      const arr = live.get(contrib.area) || []
      arr.push(contrib)
      live.set(contrib.area, arr)
      registrations++
      return () => {
        const cur = live.get(contrib.area) || []
        live.set(contrib.area, cur.filter((c) => c !== contrib))
      }
    },
    registerMany: (contribs) => {
      const disposers = contribs.map((c) => ctx.register(c))
      return () => disposers.forEach((d) => d())
    },
    onDispose: () => {}
  }

  mod.default.register(ctx)

  const navOf = () => (live.get('sidebar.nav') || []).map((c) => c.data.label)
  assert(navOf().includes('Project Hub'), `初始应为英文，实际：${navOf().join(' | ')}`)

  // Switch the host language and fire the hook the host fires.
  locale = 'zh'
  localeListeners.forEach((cb) => cb())

  const after = navOf()
  assert(after.length === 1, `切语言后侧栏应只有 1 项（旧项必须 dispose），实际 ${after.length}: ${after.join(' | ')}`)
  assert(after[0] === MESSAGES.zh['plugin.name'], `应显示「${MESSAGES.zh['plugin.name']}」，实际：${after[0]}`)
  const palette = (live.get('palette') || []).map((c) => c.data.label)
  assert(palette.includes(MESSAGES.zh['plugin.open']), `palette 未切中文：${palette.join(' | ')}`)
  return `${after[0]} / ${palette[0]}（共 ${registrations} 次注册）`
})

console.log(`\n${failed === 0 ? 'all passed' : `${failed} failed`}  (${6 - failed}/6)`)
process.exit(failed === 0 ? 0 : 1)
