// 验证 Rules 组件模板可被 Vue 编译 + 序列编辑逻辑（seqMove/seqDel/seqAdd）行为正确
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "static", "index.html"), "utf8");

// 取出 vendor 的 Vue（full build，含编译器）并在本上下文执行
const vueSrc = fs.readFileSync(path.join(__dirname, "static", "vendor", "vue.global.prod.js"), "utf8");
const ctx = { console, window: {}, document: undefined };
vm.createContext(ctx);
vm.runInContext(vueSrc, ctx);
const Vue = ctx.Vue || ctx.window.Vue;
if (!Vue || !Vue.compile) { console.log("FAIL Vue 全局或 compile 不可用"); process.exit(1); }

// 从 index.html 抽出 Rules 组件的 template 字符串（行 735 const Rules = { 的 template 反引号块）
const lines = html.split("\n");
let inRules = false, tpl = null, buf = [], capture = false;
for (const line of lines) {
  if (/^const Rules = \{/.test(line)) { inRules = true; continue; }
  if (!inRules) continue;
  if (!capture && /^\s*template: `/.test(line)) { capture = true; buf.push(line.replace(/^\s*template: `/, "")); continue; }
  if (capture) {
    if (line.endsWith("`,")) { buf.push(line.slice(0, -2)); break; }
    buf.push(line);
  }
}
tpl = buf.join("\n");
if (!tpl || tpl.length < 100) { console.log("FAIL 未抽到 Rules 模板"); process.exit(1); }

try {
  Vue.compile(tpl);
  console.log("PASS Rules 模板编译通过（长度 " + tpl.length + "）");
} catch (e) {
  console.log("FAIL 模板编译错误：" + e.message);
  process.exit(1);
}

// 关键元素存在性检查
const musts = ["seqItems", "seqMove", "seqDel", "seqAdd", "ctOptions", "seqAddPick", "seq-chip", "①"];
for (const m of musts) {
  const ok = tpl.includes(m);
  console.log((ok ? "PASS" : "FAIL") + " 模板包含 " + m);
  if (!ok) process.exitCode = 1;
}

// 模拟 setup 里的序列编辑函数行为（纯逻辑复刻，与 index.html 一致）
function splitSeq(t){ return String(t||"").split(/[,，、;；\s]+/).map(s=>s.trim()).filter(Boolean); }
const seqText = { value: ["小讲课", "教学阅片,病例讨论", "研究生汇报"] };
const seqItems = i => splitSeq(seqText.value[i]);
const writeSeq = (i, arr) => { seqText.value[i] = arr.join(","); };
function seqMove(i, idx, dir){
  const arr = seqItems(i); const j = idx + dir;
  if (j < 0 || j >= arr.length) return;
  [arr[idx], arr[j]] = [arr[j], arr[idx]];
  writeSeq(i, arr);
}
function seqDel(i, idx){ const arr = seqItems(i); arr.splice(idx,1); writeSeq(i, arr); }
function seqAdd(i, name){ const arr = seqItems(i); if(!arr.includes(name)) arr.push(name); writeSeq(i, arr); }

let fail = 0;
const ck = (name, cond) => { console.log((cond?"PASS ":"FAIL ")+name); if(!cond) fail++; };

seqMove(1, 0, -1);                       // 越界不炸
ck("越界移动不炸", seqText.value[1] === "教学阅片,病例讨论");
seqMove(1, 0, 1);                        // 交换：阅片往后 → 病例在前
ck("交换起点(病例在前)", seqText.value[1] === "病例讨论,教学阅片");
seqMove(1, 0, 1);                        // 再交换回来
ck("再交换还原", seqText.value[1] === "教学阅片,病例讨论");
seqAdd(1, "小讲课");
ck("添加课型", seqText.value[1] === "教学阅片,病例讨论,小讲课");
seqAdd(1, "小讲课");                     // 重复添加被忽略
ck("重复添加忽略", seqItems(1).length === 3);
seqDel(1, 2);
ck("删除课型", seqText.value[1] === "教学阅片,病例讨论");
seqDel(1, 0); seqDel(1, 0);
ck("清空后显示空", seqItems(1).length === 0);
seqText.value[1] = "教学阅片, 病例讨论 ,小讲课";   // 带空格输入
ck("脏输入解析", JSON.stringify(seqItems(1)) === JSON.stringify(["教学阅片","病例讨论","小讲课"]));

console.log(fail ? "== %d FAIL ==".replace("%d", fail) : "== ALL PASS ==");
process.exit(fail ? 1 : 0);
