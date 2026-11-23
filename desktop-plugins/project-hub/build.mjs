#!/usr/bin/env node
/**
 * Build the single-file desktop bundle.
 *
 * Why this exists
 * ---------------
 * The host's runtime loader refuses any import that is not in its SDK map:
 *
 *     unsupported import: ./i18n.js — runtime plugins may only import
 *     @hermes/plugin-sdk and react
 *
 * (apps/desktop/src/contrib/runtime-loader.ts → unsupportedImports). The plugin
 * source is evaluated as a blob URL, so a relative specifier has nothing to
 * resolve against. The desktop side is therefore a SINGLE-FILE target: keeping
 * locale bundles in a second module is fine in the source tree, but the file
 * the host loads must have everything inlined.
 *
 * What it does
 * ------------
 *   src/plugin.js + src/i18n.js  ->  plugin.js (single file, host-ready)
 *
 * Sources stay split (one 1000-line file holding two language tables is
 * unmaintainable). This script is the only piece that knows both worlds; the
 * tests and the installer operate on the built `plugin.js`.
 *
 * Run:  node desktop-plugins/project-hub/build.mjs
 * Then: bash scripts/install.sh
 */
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SRC = path.join(HERE, 'src')
const OUT = path.join(HERE, 'plugin.js')

const LOCALE_MODULE = './i18n.js'
const LOCALE_SRC = path.join(SRC, 'i18n.js')
const PLUGIN_SRC = path.join(SRC, 'plugin.js')

const pluginSrc = readFileSync(PLUGIN_SRC, 'utf8')
const localeSrc = readFileSync(LOCALE_SRC, 'utf8')

// The import must appear exactly once, or the build is operating on a shape it
// does not understand — stop rather than emit a broken bundle.
const importLine = `import { MESSAGES } from '${LOCALE_MODULE}'`
const occurrences = pluginSrc.split(importLine).length - 1
if (occurrences !== 1) {
  console.error(`✗ expected exactly one "${importLine}" in src/plugin.js, found ${occurrences}`)
  process.exit(1)
}

// Drop the locale module's own import/export keywords: the bundle is a single
// module scope, so its declarations simply join the plugin's.
const inlined = localeSrc
  .replace(/^import[^\n]*\n/gm, '')
  .replace(/^export\s+(const|function|let|var)\s/gm, '$1 ')
  .replace(/^export\s*\{[^}]*\}\s*$/gm, '')
  .trim()

const banner = `/**
 * ⚠️ GENERATED FILE — do not edit.
 *
 * Sources: desktop-plugins/project-hub/src/{plugin.js,i18n.js}
 * Built by: desktop-plugins/project-hub/build.mjs
 *
 * The host's runtime loader only accepts @hermes/plugin-sdk and react imports,
 * so the desktop side must be one file and the locale bundles are inlined
 * below. Edit the sources, then run: node build.mjs
 */`

const out = pluginSrc.replace(
  importLine,
  `${banner}\n\n// ── inlined from ${LOCALE_MODULE} ────────────────────────────────────────────\n${inlined}\n// ── end ${LOCALE_MODULE} ─────────────────────────────────────────────────────`
)

// Any remaining relative import would fail at load time with a banner the user
// sees but cannot act on. Fail the build instead.
const leftovers = [...out.matchAll(/^\s*import[^\n]*from\s+'(\.[^']*)'/gm)].map(m => m[1])
if (leftovers.length > 0) {
  console.error(`✗ unresolved relative imports would break the host loader: ${leftovers.join(', ')}`)
  process.exit(1)
}

writeFileSync(OUT, out, 'utf8')

const kb = (Buffer.byteLength(out, 'utf8') / 1024).toFixed(1)
console.log(`✓ built ${path.relative(process.cwd(), OUT)} (${kb} KiB)`)
console.log(`  inlined ${LOCALE_MODULE} (${(Buffer.byteLength(localeSrc, 'utf8') / 1024).toFixed(1)} KiB)`)
