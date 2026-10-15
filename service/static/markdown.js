/* Render model Markdown locally; raw HTML stays escaped. */
(() => {
  const markdown = typeof globalThis.markdownit === "function"
    ? globalThis.markdownit({html:false, breaks:true, linkify:false, typographer:false})
    : null;
  if (markdown) {
    const linkOpen = markdown.renderer.rules.link_open
      || ((tokens, index, options, env, renderer) => renderer.renderToken(tokens, index, options));
    markdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
      tokens[index].attrSet("target", "_blank");
      tokens[index].attrSet("rel", "noopener noreferrer");
      return linkOpen(tokens, index, options, env, renderer);
    };
  }

  globalThis.renderModelAnswer = (node, result) => {
    const body = document.createElement("div");
    body.className = "markdown-body";
    const answer = String(result.answer ?? "");
    if (markdown) body.innerHTML = markdown.render(answer);
    else body.textContent = answer;
    const metadata = document.createElement("p");
    metadata.className = "answer-meta";
    const sources = Array.isArray(result.evidence) ? result.evidence : (result.evidence ? [result.evidence] : []);
    metadata.textContent = `模型：${result.model || "未记录"}；响应：${result.model_response_id || "未记录"}`
      + (sources.length ? `；依据：${sources.join("、")}` : "");
    node.replaceChildren(body, metadata);
  };
})();
