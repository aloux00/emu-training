// 编译 index.html 里所有组件模板，验证无语法错误（配合 jsdom 不需要，直接用 vendor Vue 的编译器）
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const root = path.dirname(__dirname) === path.parse(__dirname).root ? __dirname : path.dirname(__dirname);
const PROJ = __dirname.includes("_test") ? path.resolve(__dirname) : path.resolve(__dirname);
// 本脚本固定位于项目根目录
const html = fs.readFileSync(path.join(__dirname, "static", "index.html"), "utf8");
const vueSrc = fs.readFileSync(path.join(__dirname, "static", "vendor", "vue.global.prod.js"), "utf8");

const ctx = { console };
// 最小 DOM shim：vendor Vue 的 decodeEntities 需要——
//   属性分支: p.innerHTML='<div foo="...">'; p.children[0].getAttribute("foo")
//   文本分支: p.innerHTML=e; p.textContent
const ENT = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": "\"", "&#39;": "'", "&nbsp;": " " };
ctx.document = {
  createElement() {
    let h = "";
    const el = {};
    Object.defineProperties(el, {
      innerHTML: { set(v) { h = String(v);
        const m = /^<div foo="([\s\S]*)">$/.exec(h);
        el._foo = m ? m[1].replace(/&quot;/g, '"').replace(/&(amp|lt|gt|#39|nbsp);/g, s => ENT[s] || s) : null;
      } },
      children: { get() { return [{ getAttribute: k => (k === "foo" ? el._foo : null) }]; } },
      textContent: { get() { return String(h).replace(/&(amp|lt|gt|quot|#39|nbsp);/g, s => ENT[s] || s); } }
    });
    return el;
  }
};
vm.createContext(ctx);
vm.runInContext(vueSrc, ctx);
const Vue = ctx.Vue || (ctx.window && ctx.window.Vue);
if (!Vue || !Vue.compile) { console.log("FAIL Vue compile 不可用"); process.exit(1); }

// 抽出所有 template: `...` 块
const re = /template:\s*`/g;
let m, idx = 0, fail = 0;
while ((m = re.exec(html)) !== null) {
  idx++;
  let i = m.index + m[0].length, depth = 1, buf = "";
  // 模板内不会出现反引号（Vue 模板用单双引号），扫到下一个反引号即结束
  while (i < html.length && html[i] !== "`") { buf += html[i]; i++; }
  try {
    Vue.compile(buf);
  } catch (e) {
    fail++;
    console.log("FAIL 模板 #" + idx + " 编译错误：" + e.message);
    console.log("  附近内容: " + buf.slice(-160));
  }
}
console.log((fail ? "FAIL " : "PASS ") + "共编译 " + idx + " 个组件模板" + (fail ? "，" + fail + " 个失败" : "，全部通过"));
process.exit(fail ? 1 : 0);
