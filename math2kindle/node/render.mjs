// math2kindle batch maths renderer.
//
// Protocol: the whole job arrives as ONE JSON document on stdin:
//   {config: {em_px, width_em, inline_split_em, stroke, out_dir, keep_svg},
//    macros: ["\\newcommand{\\Z}{\\mathbb{Z}}", ...],
//    items:  [{id: "m12", tex: "...", display: true}, ...]}
// and results stream back as JSON lines on stdout:
//   {"type":"ready","mathjax":"4.1.3","x_height":0.442,"macro_errors":[...]}
//   {"type":"result","id":"m12","ok":true,"pieces":[{file,w_em,h_em,va_em,w_px,h_px,full}],"breaks":[...]}
//   {"type":"result","id":"m13","ok":false,"error":"Undefined control sequence \\foo"}
//   {"type":"done"}
// Each PNG is written to config.out_dir/<id>-<n>.png.  Nothing from the
// document is executed: TeX goes only through MathJax's parser.

import fs from 'node:fs';
import path from 'node:path';
import MathJax from '@mathjax/src';
import {Resvg} from '@resvg/resvg-js';

function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.on('data', (c) => chunks.push(c));
    process.stdin.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    process.stdin.on('error', reject);
  });
}

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

const job = JSON.parse(await readStdin());
const cfg = Object.assign({
  em_px: 48, width_em: 16, inline_split_em: 9, stroke: 0, scale: 1, script_min: 0.6, out_dir: '.', keep_svg: false,
}, job.config || {});

// TeX extensions that ship with @mathjax/src and need no extra font packages.
const EXT = ['boldsymbol', 'braket', 'cancel', 'cases', 'centernot', 'color', 'enclose',
  'extpfeil', 'mathtools', 'textcomp', 'textmacros', 'upgreek', 'amscd', 'bbox', 'verb',
  'unicode', 'gensymb', 'colortbl', 'empheq', 'units', 'action'];

await MathJax.init({
  loader: {load: ['input/tex', 'output/svg', 'adaptors/liteDOM', ...EXT.map((p) => '[tex]/' + p)]},
  tex: {
    packages: {'[+]': EXT, '[-]': ['noundefined']},
    // Throw instead of rendering red error boxes, so Python can fall back to LaTeX.
    formatError: (jax, err) => { throw err; },
  },
  svg: {
    fontCache: 'none',             // every SVG is standalone (no <use> refs)
    // NB: any conversion made while displayOverflow is 'linebreak' leaves
    // state behind that makes later \right-delimiters measure too narrow
    // (clipped images).  We therefore stay in 'overflow' mode and break
    // displays ourselves; MathJax's own breaker is used only in a separate
    // process (cfg.autobreak) as a last resort.
    displayOverflow: cfg.autobreak ? 'linebreak' : 'overflow',
    linebreaks: {inline: false, width: '100%'},
  },
});

const adaptor = MathJax.startup.adaptor;

// Minimum glyph size: second-level scripts (fractions in exponents, limits of
// limits) would otherwise be 50% of the text size, too small on e-ink.
try {
  MathJax._.core.MmlTree.MmlNodes.math.MmlMath.defaults.scriptminsize = String(cfg.script_min || 0.6) + 'em';
} catch (e) { /* internals moved: keep MathJax's default (.4em) */ }
const output = MathJax.startup.output;
const XH = output.font.params.x_height;   // ex / em
const TEXT_EM = cfg.em_px;          // raster px per em of the surrounding text
const SCALE = cfg.scale;             // maths size relative to the text
const EM = TEXT_EM * SCALE;          // raster px per em of the maths font
const EX = EM * XH;
const CONTAINER = cfg.width_em * TEXT_EM;

// Compatibility shims for common LaTeX commands MathJax lacks.  User macros
// (sent afterwards) may override these.
const COMPAT = [
  String.raw`\def\hdots{\ldots}`,
  String.raw`\def\ensuremath#1{#1}`,
  String.raw`\def\textsc#1{\text{#1}}`,
  String.raw`\def\textsl#1{\textit{#1}}`,
  String.raw`\def\vcentcolon{\mathrel{:}}`,
  String.raw`\def\raisebox#1#2{\text{#2}}`,
  String.raw`\def\P{\unicode{xB6}}`,
  String.raw`\def\copyright{\unicode{xA9}}`,
  String.raw`\def\llbracket{[\![}`,
  String.raw`\def\rrbracket{]\!]}`,
  String.raw`\def\mathbbm#1{\mathbb{#1}}`,
  String.raw`\def\mathds#1{\mathbb{#1}}`,
  String.raw`\def\mathbold#1{\boldsymbol{#1}}`,
  String.raw`\def\bm#1{\boldsymbol{#1}}`,
  String.raw`\def\qedhere{}`,
  String.raw`\def\nobreakdash{}`,
  String.raw`\def\sfrac#1#2{{}^{#1}\!/{}_{#2}}`,
  String.raw`\def\lefteqn#1{\rlap{$\displaystyle #1$}}`,
  String.raw`\def\upshape{\rm}`,
  String.raw`\def\itshape{\it}`,
  String.raw`\def\bfseries{\bf}`,
  String.raw`\def\mathversion#1{}`,
  String.raw`\def\displaybreak{}`,
  String.raw`\def\allowdisplaybreaks{}`,
  String.raw`\def\protect{}`,
  String.raw`\def\relax{}`,
];

const macroErrors = [];
for (const [i, def] of [...COMPAT, ...(job.macros || [])].entries()) {
  try {
    await MathJax.tex2svgPromise(def, {display: false});
  } catch (e) {
    if (i >= COMPAT.length) macroErrors.push({def: def.slice(0, 200), error: String(e.message || e)});
  }
}

emit({type: 'ready', mathjax: MathJax.version, x_height: XH, macro_errors: macroErrors});

const exNum = (s) => parseFloat(s);

// MathJax 4 annotates nodes with data-latex="..." whose values the lite DOM
// does not escape (a "<" breaks the SVG), and we don't need them anyway.
function stripDataAttrs(node) {
  if (adaptor.kind(node) === '#text' || adaptor.kind(node) === '#comment') return;
  for (const name of adaptor.allAttributes(node).map((a) => a.name)) {
    if (name.startsWith('data-') && name !== 'data-mjx-viewBox') adaptor.removeAttribute(node, name);
  }
  for (const child of adaptor.childNodes(node)) stripDataAttrs(child);
}

// Turn one MathJax <svg> into a fixed-size SVG string plus metrics (in em).
function finishSvg(svgNode, forceWidthPx) {
  stripDataAttrs(svgNode);
  let svg = adaptor.outerHTML(svgNode);
  const open = svg.match(/^<svg[^>]*>/)[0];
  const style = (open.match(/ style="([^"]*)"/) || [, ''])[1];
  const va = exNum((style.match(/vertical-align:\s*(-?[\d.]+)ex/) || [, '0'])[1]);
  const minw = exNum((style.match(/min-width:\s*([\d.]+)ex/) || [, '0'])[1]);
  const hEx = exNum((open.match(/height="([\d.]+)ex"/) || [, '0'])[1]);
  const wAttr = (open.match(/ width="([^"]+)"/) || [, '0ex'])[1];
  let wPx;
  let full = false;
  if (wAttr.endsWith('%')) {
    // Tagged equation: MathJax wants the full container width, tag at the right.
    full = true;
    wPx = Math.max(forceWidthPx || CONTAINER, minw * EX);
  } else {
    wPx = exNum(wAttr) * EX;
  }
  wPx = Math.max(1, Math.ceil(wPx));
  const hPx = Math.max(1, Math.ceil(hEx * EX));
  let newOpen = open
    .replace(/ width="[^"]*"/, ` width="${wPx}"`)
    .replace(/ height="[^"]*"/, ` height="${hPx}"`)
    .replace(/ style="[^"]*"/, '');
  svg = svg.replace(open, newOpen).replace(/currentColor/g, '#000000');
  let pad = 0;
  if (cfg.stroke > 0) {
    // Slightly embolden the thin Computer-Modern-style strokes for e-ink, and
    // pad the canvas so the thicker outlines are not clipped at the edges.
    svg = svg.replace(/stroke-width="0"/g, `stroke-width="${cfg.stroke}"`);
    pad = Math.ceil(cfg.stroke / 1000 * EM / 2) + 1;
    svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${wPx + 2 * pad}" height="${hPx + 2 * pad}" ` +
      `viewBox="0 0 ${wPx + 2 * pad} ${hPx + 2 * pad}">` + svg.replace(/^<svg/, `<svg x="${pad}" y="${pad}"`) + '</svg>';
  }
  return {
    svg,
    w_px: wPx + 2 * pad,
    h_px: hPx + 2 * pad,
    w_em: (wPx + 2 * pad) / TEXT_EM,
    h_em: (hPx + 2 * pad) / TEXT_EM,
    va_em: va * XH * SCALE - pad / TEXT_EM,
    full,
  };
}

function rasterize(svg, file) {
  const r = new Resvg(svg, {
    background: '#ffffff',
    fitTo: {mode: 'original'},
    font: {loadSystemFonts: true, defaultFontFamily: 'serif'},
    shapeRendering: 2,
    textRendering: 1,
  });
  fs.writeFileSync(file, r.render().asPng());
}

async function convert(tex, display) {
  // MathJax keeps a reference to the last top-level labelled table on the
  // output jax and may reuse it (stale) for min-width; clear it every time.
  output.table = null;
  return MathJax.tex2svgPromise(tex, {display, em: EM, ex: EX, containerWidth: CONTAINER});
}

function firstSvg(node) {
  return adaptor.childNodes(node).filter((n) => adaptor.kind(n) === 'svg');
}

// ------------------------------------------------------------------ displays
// Display layout (see math2kindle/displaymath.py for the spec format):
// equation numbers are NOT drawn into the images (they become HTML text that
// floats right of, or wraps below, the image); formulas wider than the line
// width W are broken at the best measured break points (relations, then
// top-level +/-, then +/- inside brackets), never inside \left...\right or
// braces and never between a big operator and its operand.

const W = CONTAINER;
const widthCache = new Map();

async function natural(tex) {
  return finishSvg(firstSvg(await convert(tex, true))[0]);
}

async function widthOf(tex) {
  if (!widthCache.has(tex)) {
    let w = Infinity;
    try { w = (await natural(tex)).w_px; } catch (e) { /* unmeasurable piece */ }
    widthCache.set(tex, w);
  }
  return widthCache.get(tex);
}

const QUAD = EM;                   // \quad = 1em of the maths font, in px
const THICK = 5 / 18 * EM;         // relation spacing
const PAD = cfg.stroke > 0 ? Math.ceil(cfg.stroke / 1000 * EM / 2) + 1 : 0;
// a row that starts with + or - (continuation of a multline) keeps binary spacing
const binfix = (t) => (/^\s*(\+|-|\\pm|\\mp)(?![A-Za-z])/.test(t) ? '{}' + t : t);
const isBin = (seg) => seg && seg.prio >= 2;
const inner = async (t) => (await widthOf(t)) - 2 * PAD;

// TeX of segments a..b-1, restoring separators (\qquad) dropped at breaks
function joinSegs(segs, a, b) {
  let t = '';
  for (let k = a; k < b; k++) {
    t += (k === a ? '' : (segs[k].sep ? ' ' + segs[k].sep + ' ' : ' ')) + segs[k].tex;
  }
  return (a === 0 || isBin(segs[a])) ? binfix(t) : t;
}

// x (px) just past the bracket that a break at segment s sits inside
async function bracketX(segs, lines, s) {
  const op = segs[s].open;
  if (!op) return null;
  const [j, off] = op;
  const L = lines.find((l) => l.a <= j && j < l.b);
  if (!L) return null;
  const full = joinSegs(segs, L.a, j + 1);
  const tail = segs[j].tex.slice(off);
  const prefix = tail ? full.slice(0, full.length - tail.length) : full;
  try {
    return L.x + (await inner(prefix)) + 3 / 18 * EM;
  } catch (e) {
    return null;
  }
}

// cost of ending a line before a break of this priority (lower is better):
// relations and top-level +/- are cheap; breaking inside any bracket group is
// dearer than letting a top-level line run ~40% past the width
const PRIO_COST = {'-1': 0, 0: -1, 1: 0.5, 2: 1, 3: 9, 4: 10, 5: 11, 6: 12, 7: 13, 8: 16, 9: 18};

// Greedy, cost-driven line breaking of one row.
// geo = {first, rel, bin, lhsFirst}: x (px) of the first line, of lines that
// start with a relation, and of lines that start with + or -.  Lines that
// start inside a bracket are indented past the bracket (never left of `bin`).
async function layoutLines(segs, geo) {
  const n = segs.length;
  const lines = [];
  let worst = -1;
  let s = 0;
  while (s < n) {
    let x = geo.first;
    if (lines.length) {
      const p = segs[s].prio;
      if (p === 0) x = geo.first;
      else if (p === 1) x = geo.rel;
      else if (p === 2) x = geo.bin;
      else {
        // inside a bracket: start just past the opening bracket if that leaves
        // room, else at least one quad right of the +/- column
        const bx = await bracketX(segs, lines, s);
        x = (bx != null && bx > geo.bin && W - bx >= 0.6 * W) ? bx : geo.bin + QUAD;
      }
    }
    const avail = Math.max(W - x, 0.35 * W);
    const cands = [];
    // keep "lhs = ..." together: the first line runs at least past the first relation
    let e0 = s + 1;
    if (!lines.length && geo.lhsFirst) {
      const r = segs.findIndex((g, i) => i > 0 && g.prio === 1);
      if (r > 0) e0 = r + 1;
    }
    for (let e = e0; e <= n; e++) {
      const w = (await widthOf(joinSegs(segs, s, e))) - 2 * PAD;
      const prio = e < n ? segs[e].prio : -1;
      if (w > 1.5 * avail && cands.length) break;
      cands.push({e, w, prio});
      if (w > 1.5 * avail || e === n) break;
    }
    const cost = (c) => (PRIO_COST[c.prio] ?? 18) + (c.w > avail ? 20 * (c.w / avail - 1) : 0) +
      (c.w < 0.3 * avail && c.e < n ? 6 : 0) + (c.e < n ? 2 * Math.max(0, 1 - c.w / avail) : 0);
    const pick = cands.reduce((best, c) => (cost(c) < cost(best) - 1e-9 ||
      (Math.abs(cost(c) - cost(best)) < 1e-9 && c.e > best.e)) ? c : best);
    if (process.env.M2K_DEBUG) {
      process.stderr.write(JSON.stringify({x: Math.round(x), avail: Math.round(avail),
        cands: cands.map((c) => [c.e, Math.round(c.w), c.prio, +cost(c).toFixed(1)]), pick: pick.e}) + '\n');
    }
    lines.push({a: s, b: pick.e, x, w: pick.w});
    if (pick.e < n) worst = Math.max(worst, segs[pick.e].prio);
    s = pick.e;
  }
  const right = Math.max(...lines.map((l) => l.x + l.w));
  return {lines, worst, right};
}

// Geometry for a row whose left-hand side (if any) is lhsTex.
async function geometry(lhsTex, col) {
  if (col != null) {           // shared relation column of an alignment
    const lw = lhsTex ? (await inner(lhsTex)) + THICK : 0;
    return {first: lhsTex ? Math.max(0, col - lw) : col, rel: col, bin: col + QUAD, lhsFirst: !!lhsTex};
  }
  return {first: 0, rel: 1.5 * QUAD, bin: 2.5 * QUAD, lhsFirst: false};
}

// Render one row so that it fits W (+25%) if at all possible.  Returns a list
// of line images with their x offsets (px).
async function fitRow(tex, segs, opts = {}) {
  tex = binfix(tex);
  const nat = await natural(tex);
  if (nat.w_px - 2 * PAD <= 1.25 * W && opts.x == null) return {lines: [{fin: nat, x: 0}], single: true};
  if (nat.w_px - 2 * PAD + (opts.x || 0) <= 1.25 * W) return {lines: [{fin: nat, x: opts.x || 0}], single: true};
  if (!segs || segs.length < 2) {
    return {lines: [{fin: nat, x: opts.x || 0}], single: true, tooWide: nat.w_px > 1.5 * W};
  }
  let layouts = [];
  if (opts.geo) {
    layouts.push(await layoutLines(segs, opts.geo));
  } else {
    if (segs[1].prio === 1) {
      const lw = await inner(segs[0].tex);
      if (lw <= 0.4 * W) layouts.push(await layoutLines(segs, {first: 0, rel: lw + THICK, bin: lw + THICK + QUAD, lhsFirst: true}));
    }
    layouts.push(await layoutLines(segs, await geometry(null, null)));
  }
  // fewest breaks inside brackets, then prefer relation alignment unless clearly wider
  const score = (l) => (l.worst >= 3 ? l.worst : 0);
  const bestScore = Math.min(...layouts.map(score));
  layouts = layouts.filter((l) => score(l) === bestScore);
  const narrow = layouts.reduce((x, y) => (y.right < x.right ? y : x));
  const lay = layouts[0].right <= narrow.right * 1.1 ? layouts[0] : narrow;
  if (lay.lines.length < 2) return {lines: [{fin: nat, x: opts.x || 0}], single: true, tooWide: nat.w_px > 1.5 * W};
  const out = [];
  for (const l of lay.lines) out.push({fin: await natural(joinSegs(segs, l.a, l.b)), x: l.x});
  return {lines: out, single: false, tooWide: false};
}

function phantomCells(rows, ncols) {
  // Every row padded to the column widths of the whole alignment, so rows
  // rendered as separate images still line up.
  const cols = [];
  for (let j = 0; j < ncols; j++) {
    const items = rows.map((r) => (j % 2 ? '{}' : '') + (r.cells[j] || '')).filter((x) => x.replace(/[{}\s]/g, ''));
    cols.push(items.length ? '\\hphantom{\\begin{gathered}' + items.join(' \\\\ ') + '\\end{gathered}}' : '');
  }
  return rows.map((r) => {
    const cells = [];
    for (let j = 0; j < ncols; j++) {
      const c = (r.cells[j] || '').trim();
      if (!c) cells.push(cols[j]);
      else if (j % 2 === 0) cells.push(cols[j] + '\\mathllap{' + c + '}');
      else cells.push('\\mathrlap{{}' + c + '}' + cols[j]);
    }
    return cells.join(' & ');
  });
}

async function renderDisplay(item, base) {
  const spec = item.spec;
  const blocks = [];
  let k = 0;
  const out = (fin, extra) => {
    const file = path.join(cfg.out_dir, `${base}-${k++}.png`);
    rasterize(fin.svg, file);
    if (cfg.keep_svg) fs.writeFileSync(file.replace(/\.png$/, '.svg'), fin.svg);
    blocks.push(Object.assign({file, w_em: fin.w_em, h_em: fin.h_em, w_px: fin.w_px, h_px: fin.h_px,
      tag: null, align: 'center', indent: 0, alt: ''}, extra));
  };
  // emit the lines of one row; the row's number goes on its last line
  const outRow = (res, extra) => {
    const many = res.lines.length > 1 || extra.forceLeft;
    res.lines.forEach((ln, i) => {
      const last = i === res.lines.length - 1;
      out(ln.fin, Object.assign({}, extra, {
        tag: last ? extra.tag : null,
        align: many ? 'left' : extra.align || 'center',
        indent: many ? ln.x / TEXT_EM : 0,
        autobreak: res.tooWide && res.lines.length === 1 ? extra.alt : null,
      }));
    });
  };
  const rows = spec.rows;
  const nTags = rows.filter((r) => r.tag).length + (spec.group_tag ? 1 : 0);
  // a single number belongs to the last line of the whole display
  const soleTag = nTags === 1 ? (spec.group_tag || rows.find((r) => r.tag).tag) : null;
  const tagOf = (i) => (nTags === 1 ? (i === rows.length - 1 ? soleTag : null) : rows[i].tag);

  if (spec.kind === 'single') {
    const row = rows[0];
    const res = await fitRow(row.cells[0], row.segs);
    outRow(res, {tag: spec.group_tag, alt: item.tex});
    return blocks;
  }
  const env = spec.env;
  if (['align', 'flalign', 'alignat', 'xalignat', 'xxalignat', 'eqnarray'].includes(env)) {
    const ncols = Math.max(...rows.map((r) => r.cells.length));
    const outEnv = env === 'eqnarray' ? 'eqnarray*' : (env.includes('alignat') ? 'alignat*' : 'align*');
    const arg = env.includes('alignat') ? (spec.arg || `{${Math.ceil(ncols / 2)}}`) : '';
    const whole = `\\begin{${outEnv}}${arg}` + rows.map((r) => r.cells.join(' & ')).join(' \\\\ ') + `\\end{${outEnv}}`;
    const nat = await natural(whole);
    if (nat.w_px - 2 * PAD <= 1.25 * W) {
      if (nTags <= 1) {
        out(nat, {tag: soleTag, alt: item.tex});
        return blocks;
      }
      if (env !== 'eqnarray') {
        const padded = phantomCells(rows, ncols);
        for (let i = 0; i < rows.length; i++) {
          const fin = await natural(`\\begin{${outEnv}}${arg}` + padded[i] + `\\end{${outEnv}}`);
          out(fin, {tag: tagOf(i), align: 'left', alt: rows[i].cells.join(' ')});
        }
        return blocks;
      }
    }
    // too wide: rows one at a time, broken to fit, aligned on a shared relation column
    const lhsW = [];
    for (const r of rows) lhsW.push((r.cells[0] || '').trim() ? await inner(r.cells[0]) : 0);
    const maxL = Math.max(0, ...lhsW);
    const col = maxL > 0 && maxL <= 0.4 * W ? maxL + THICK : 1.5 * QUAD;
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      const lhs = (r.cells[0] || '').trim();
      const tex = r.cells.filter((c) => c.trim()).join(' ');
      if (!tex.trim()) continue;
      let geo;
      if (maxL > 0 && maxL <= 0.4 * W) geo = await geometry(lhs || null, col);
      else geo = lhs ? {first: 0, rel: col, bin: col + QUAD, lhsFirst: false} : {first: col, rel: col, bin: col + QUAD, lhsFirst: false};
      const res = await fitRow(tex, r.segs, {geo, x: geo.first});
      res.lines[0].x = res.single ? geo.first : res.lines[0].x;
      outRow(res, {tag: tagOf(i), alt: tex, forceLeft: true});
    }
    return blocks;
  }
  // gather / multline: rows are independent
  if (env === 'multline' && rows.length > 1) {
    // Rows after the first continue the first row's expression.  If any row
    // has to be broken, set every continuation row flush at one indent (that
    // of the first row's last line, if it broke) instead of
    // TeX's centred / right-aligned rows, so sibling terms line up.
    const live = rows.map((r, i) => i).filter((i) => rows[i].cells.join(' ').trim());
    const first = await fitRow(rows[live[0]].cells.join(' '), rows[live[0]].segs, {});
    let contX = 2 * QUAD;
    if (!first.single && first.lines.length > 1) contX = Math.min(first.lines[first.lines.length - 1].x, 0.4 * W);
    const geo = {first: contX, rel: contX + 1.5 * QUAD, bin: contX, lhsFirst: false};
    const rest = [];
    for (const i of live.slice(1)) {
      const tex = rows[i].cells.join(' ');
      rest.push(await fitRow(tex, rows[i].segs, {geo, x: contX}));
    }
    const plain = first.single && (await Promise.all(live.slice(1).map(async (i) =>
      (await natural(binfix(rows[i].cells.join(' ')))).w_px - 2 * PAD <= 1.25 * W))).every(Boolean);
    if (plain) {
      // nothing needs breaking: TeX's own multline layout
      for (const [j, i] of live.entries()) {
        const align = j === 0 ? 'left' : (j === live.length - 1 ? 'right' : 'center');
        const res = j === 0 ? first : {lines: [{fin: await natural(binfix(rows[i].cells.join(' '))), x: 0}], single: true};
        outRow(res, {tag: tagOf(i), align, alt: rows[i].cells.join(' ')});
      }
      return blocks;
    }
    outRow(first, {tag: tagOf(live[0]), alt: rows[live[0]].cells.join(' '), forceLeft: true});
    live.slice(1).forEach((i, j) => {
      const res = rest[j];
      if (res.single) res.lines[0].x = contX;
      outRow(res, {tag: tagOf(i), alt: rows[i].cells.join(' '), forceLeft: true});
    });
    return blocks;
  }
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    const tex = r.cells.join(' ');
    if (!tex.trim()) continue;
    const res = await fitRow(tex, r.segs, {});
    outRow(res, {tag: tagOf(i), align: 'center', alt: tex});
  }
  return blocks;
}

for (const item of job.items || []) {
  try {
    if (cfg.autobreak) {
      // separate process: MathJax 4's own line breaking for a row we could not break
      const fin = finishSvg(firstSvg(await convert(item.tex, true))[0]);
      const file = path.join(cfg.out_dir, `${item.id}.png`);
      rasterize(fin.svg, file);
      emit({type: 'result', id: item.id, ok: true, breaks: [], pieces: [{file, w_em: fin.w_em, h_em: fin.h_em,
        va_em: 0, w_px: fin.w_px, h_px: fin.h_px, full: fin.full}]});
      continue;
    }
    if (item.display && item.spec) {
      const blocks = await renderDisplay(item, item.id);
      emit({type: 'result', id: item.id, ok: true, breaks: [], pieces: [], blocks});
      continue;
    }
    let node = await convert(item.tex, !!item.display);
    let svgs = firstSvg(node);
    const breaks = [];
    if (!item.display && svgs.length === 1) {
      const w = exNum((adaptor.getAttribute(svgs[0], 'width') || '0').replace('ex', '')) * XH * SCALE;
      if (w > cfg.inline_split_em) {
        // Long inline formula: let MathJax mark its allowed break points and
        // emit one image per piece, so the reader can wrap it across lines.
        output.options.linebreaks.inline = true;
        try {
          const n2 = await convert(item.tex, false);
          const kids = adaptor.childNodes(n2);
          const s2 = kids.filter((n) => adaptor.kind(n) === 'svg');
          if (s2.length > 1) {
            node = n2;
            svgs = s2;
            for (const k of kids) {
              if (adaptor.kind(k) === 'mjx-break') {
                breaks.push(adaptor.getAttribute(k, 'newline') ? 'newline' : (adaptor.getAttribute(k, 'size') || '0'));
              }
            }
          }
        } finally {
          output.options.linebreaks.inline = false;
        }
      }
    }
    const pieces = [];
    svgs.forEach((s, k) => {
      const fin = finishSvg(s);
      const file = path.join(cfg.out_dir, `${item.id}-${k}.png`);
      rasterize(fin.svg, file);
      if (cfg.keep_svg) fs.writeFileSync(file.replace(/\.png$/, '.svg'), fin.svg);
      pieces.push({file, w_em: fin.w_em, h_em: fin.h_em, va_em: fin.va_em,
        w_px: fin.w_px, h_px: fin.h_px, full: fin.full});
    });
    emit({type: 'result', id: item.id, ok: true, pieces, breaks});
  } catch (e) {
    emit({type: 'result', id: item.id, ok: false, error: String((e && e.message) || e)});
  }
}
emit({type: 'done'});
