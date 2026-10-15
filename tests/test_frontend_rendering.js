const assert = require("node:assert/strict");
const {test} = require("node:test");

globalThis.markdownit = require("../service/static/vendor/markdown-it.min.js");
globalThis.document = {createElement: (tagName) => ({tagName})};
require("../service/static/markdown.js");

function render(answer, extra = {}) {
  const node = {replaceChildren(...children) { this.children = children; }};
  renderModelAnswer(node, {answer, model:"test-model", model_response_id:"test-response", ...extra});
  return node.children;
}

test("report Markdown renders headings, emphasis, lists, tables and fenced code", () => {
  const [body, metadata] = render([
    "## 本次评估", "", "准确性 **97.19**，字段 `\\N` 表示缺失。", "",
    "1. 检查评分", "2. 核对属性", "   - 保留无法确认的值为空", "",
    "| 表 | 得分 |", "| --- | ---: |", "| 评分 | 100 |", "",
    "```json", '{"task_id":"test"}', "```"
  ].join("\n"));
  for (const tag of ["<h2>", "<strong>", "<ol>", "<ul>", "<table>", "<pre>", "language-json"]) {
    assert.ok(body.innerHTML.includes(tag), `missing ${tag}`);
  }
  assert.equal(body.className, "markdown-body");
  assert.ok(metadata.textContent.includes("test-model"));
  assert.equal(metadata.innerHTML, undefined);
});

test("raw HTML and executable links stay inert", () => {
  const [body] = render('<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">\n\n[危险链接](javascript:alert(1))');
  assert.ok(body.innerHTML.includes("&lt;script&gt;"));
  assert.ok(!body.innerHTML.includes("<script"));
  assert.ok(!body.innerHTML.includes("<img"));
  assert.ok(!body.innerHTML.includes('href="javascript:'));
});

test("ordinary links render safely and model metadata remains plain text", () => {
  const [body, metadata] = render("[任务依据](https://example.com/report)", {model:'<img src=x onerror="alert(1)">'});
  assert.ok(body.innerHTML.includes('href="https://example.com/report"'));
  assert.ok(body.innerHTML.includes('rel="noopener noreferrer"'));
  assert.ok(metadata.textContent.includes("<img"));
  assert.equal(metadata.innerHTML, undefined);
});
