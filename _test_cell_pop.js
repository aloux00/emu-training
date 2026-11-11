// 验证排课表格就地编辑浮层的定位逻辑（从 index.html 抽取真实代码，用假 DOM 跑）
const fs = require('fs');
const html = fs.readFileSync(require('path').join(__dirname, 'static', 'index.html'), 'utf8');

function grab(name) {
  const i = html.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('找不到函数 ' + name);
  let d = 0, j = html.indexOf('{', i);
  for (let k = j; k < html.length; k++) {
    if (html[k] === '{') d++;
    else if (html[k] === '}') { d--; if (d === 0) return html.slice(i, k + 1); }
  }
  throw new Error('括号不匹配 ' + name);
}

let fails = 0;
function check(name, cond, detail) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : ' | ' + JSON.stringify(detail)));
  if (!cond) fails++;
}

// 抽取真实 placePop，用假 DOM/假锚点跑一次
const body = grab('placePop');
function run(rect, opts) {
  const o = opts || {};
  const el = { offsetWidth: 238, offsetHeight: o.h || 300, style: {} };
  const doc = {
    documentElement: { clientWidth: o.vw || 1280, clientHeight: o.vh || 800 },
    querySelector: () => (o.none ? null : el)
  };
  const win = { innerHeight: o.vh || 800 };
  const fn = new Function('document', 'window', 'cellEdit', 'popAnchorEl', body + '; return placePop;');
  fn(doc, win, { value: { id: 1 } }, { getBoundingClientRect: () => rect })();
  return el.style;
}

// 1. 中间行：浮层放在格子下方
let s = run({ left: 400, right: 600, top: 300, bottom: 368 });
check('中间行 → 贴格子下方', s.top === '372px' && s.left === '400px', s);

// 2. 最后一行（贴底）：下方放不下 → 向上翻
s = run({ left: 400, right: 600, top: 560, bottom: 628 });
check('最后一行 → 向上翻', s.top === (560 - 300 - 4) + 'px', s);

// 3. 靠右列的格子：不超出视口右边
s = run({ left: 1160, right: 1200, top: 100, bottom: 168 });
check('靠右格子 → 夹到视口内', s.left === (1280 - 238 - 8) + 'px', s);

// 4. 超矮视口（上下都放不下）：仍留在视口内
s = run({ left: 300, right: 500, top: 120, bottom: 188 }, { vh: 360, h: 260 });
check('超矮视口 → 顶部不低于 8px', parseInt(s.top) >= 8 && parseInt(s.top) + 260 <= 360, s);

// 5. 浮层未渲染时不报错
try { run({ left: 0, right: 0, top: 0, bottom: 0 }, { none: true }); check('无浮层时静默返回', true); }
catch (e) { check('无浮层时静默返回', false, e.message); }

// ---- 结构检查 ----
check('浮层已 Teleport 到 body', (html.match(/<teleport to="body">/g) || []).length >= 2);
check('排课表格已移除 click-mask', !/class="click-mask" v-if="cellEdit"/.test(html));
check('滚动时跟随重定位', /@scroll\.passive="onBoardScroll"/.test(html));
check('下拉可向上弹', /pp-list" :class="\{up: dropUp\}"/.test(html));

console.log(fails ? '\n== ' + fails + ' FAIL ==' : '\n== ALL PASS ==');
process.exit(fails ? 1 : 0);
