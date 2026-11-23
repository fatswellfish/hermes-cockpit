// Verify the i18n bundles against the keys plugin.js actually uses.
// Run: node desktop-plugins/project-hub/__i18n_check.mjs
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

import { MESSAGES, makeT, normalizeLocale, DEFAULT_LOCALE } from './src/i18n.js'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SRC = readFileSync(path.join(HERE, 'src', 'plugin.js'), 'utf8')

const en = Object.keys(MESSAGES.en)
const zh = Object.keys(MESSAGES.zh)

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
const assert = (cond, msg) => { if (!cond) throw new Error(msg) }

await check('① 两个 locale 的 key 集合完全一致', () => {
  const onlyEn = en.filter(k => !MESSAGES.zh[k])
  const onlyZh = zh.filter(k => !MESSAGES.en[k])
  assert(onlyEn.length === 0, `只在 en：${onlyEn.join(', ')}`)
  assert(onlyZh.length === 0, `只在 zh：${onlyZh.join(', ')}`)
  return `${en.length} 个 key × 2`
})

// Keys referenced as t('x') / tr('x'), plus the dynamic ones built at call sites.
const used = new Set()
for (const m of SRC.matchAll(/\b(?:t|tr)\(\s*'([\w.]+)'/g)) used.add(m[1])
for (const s of ['doing', 'todo', 'done', 'blocked']) used.add(`status.${s}`)
for (const c of ['software', 'media', 'infra', 'agent', 'knowledge', 'other']) used.add(`category.${c}`)
// keys passed as a variable: t(cat) where cat came from projectCategoryKey
used.add('category.other')

await check('② plugin.js 用到的每个 key 都有定义', () => {
  const missing = [...used].filter(k => !(k in MESSAGES.en)).sort()
  assert(missing.length === 0, `未定义：${missing.join(', ')}`)
  return `${used.size} 个使用点`
})

await check('③ 没有定义了但永远用不到的 key', () => {
  const unused = en.filter(k => !used.has(k)).sort()
  assert(unused.length === 0, `闲置：${unused.join(', ')}`)
  return '全部被引用'
})

await check('④ plugin.js 里不再有硬编码的中文界面文案', () => {
  // The single allowed exception is the BLOCKER_EMPTY data set: the literal
  // values TASKS.md writes to mean "no blocker". Anything else is copy that
  // belongs in i18n.js.
  const bad = SRC.split('\n')
    .map((line, i) => [i + 1, line])
    .filter(([, line]) => /[\u4e00-\u9fa5]/.test(line))
    .filter(([, line]) => !line.includes('BLOCKER_EMPTY'))
  assert(bad.length === 0, bad.map(([n, l]) => `${n}: ${l.trim().slice(0, 60)}`).join(' | '))
  return '0 处（仅 BLOCKER_EMPTY 数据常量保留）'
})

await check('⑤ zh 译文里没有残留英文占位', () => {
  const suspicious = zh.filter(k => {
    const v = MESSAGES.zh[k]
    // Allow {vars}, emoji and punctuation; flag values that are pure ASCII words.
    return /^[\x00-\x7F]+$/.test(v) && /[a-zA-Z]{3,}/.test(v) && !v.includes('HTTP')
  })
  assert(suspicious.length === 0, `疑似未翻译：${suspicious.join(', ')}`)
  return '全部为中文'
})

await check('⑥ {var} 占位符在两种语言里一致', () => {
  const varsOf = s => [...String(s).matchAll(/\{(\w+)\}/g)].map(m => m[1]).sort().join(',')
  const bad = en.filter(k => varsOf(MESSAGES.en[k]) !== varsOf(MESSAGES.zh[k]))
  assert(bad.length === 0, bad.map(k => `${k}: en=${varsOf(MESSAGES.en[k])} zh=${varsOf(MESSAGES.zh[k])}`).join(' | '))
  return '一致'
})

await check('⑦ normalizeLocale：zh / zh-CN / zh_SG → zh；zh-hant 与其余 → en', () => {
  assert(normalizeLocale('zh') === 'zh')
  assert(normalizeLocale('zh-CN') === 'zh')
  assert(normalizeLocale('zh_SG') === 'zh')
  // Traditional Chinese is its own UI language; falling back to the Simplified
  // bundle would show 简体 to a 繁體 user. English until it has a bundle.
  assert(normalizeLocale('zh-hant') === 'en')
  assert(normalizeLocale('zh-Hant') === 'en')
  assert(normalizeLocale('en-US') === 'en')
  assert(normalizeLocale('ja') === 'en')
  assert(normalizeLocale('') === 'en')
  assert(normalizeLocale(undefined) === 'en')
  return 'ok'
})

await check('⑧ 未知 key 回退到 key 本身，不渲染 undefined', () => {
  assert(makeT('en')('nope.not.a.key') === 'nope.not.a.key')
  assert(makeT('zh')('nope.not.a.key') === 'nope.not.a.key')
  return 'ok'
})

await check('⑨ {var} 插值真的替换（两种语言）', () => {
  for (const loc of ['en', 'zh']) {
    const out = makeT(loc)('stat.projects', { n: 14 })
    assert(out.includes('14'), `${loc}: ${out}`)
    assert(!out.includes('{n}'), `${loc} 未替换：${out}`)
  }
  // Unknown var name must stay literal rather than print "undefined".
  assert(makeT('en')('stat.projects', {}).includes('{n}'))
  return 'ok'
})

await check('⑩ 缺省 bundle 是 en（README/CLI 等非界面场合的兜底）', () => {
  assert(DEFAULT_LOCALE === 'en')
  assert(makeT('fr')('plugin.name') === MESSAGES.en['plugin.name'])
  return 'ok'
})

console.log(`\n${failed === 0 ? 'all passed' : `${failed} failed`}  (${11 - failed}/11)`)
process.exit(failed === 0 ? 0 : 1)
