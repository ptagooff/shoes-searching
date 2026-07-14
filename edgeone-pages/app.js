const api = String(window.SOLE_API_BASE || "").replace(/\/$/, "");
const input = document.querySelector("#fileInput");
const dropzone = document.querySelector("#dropzone");
const preview = document.querySelector("#preview");
const prompt = document.querySelector("#prompt");
const button = document.querySelector("#searchButton");
const grid = document.querySelector("#grid");
const count = document.querySelector("#resultCount");
const syncState = document.querySelector("#syncState");
let file;

function setFile(next) {
  if (!next || !next.type.startsWith("image/")) return;
  file = next;
  preview.src = URL.createObjectURL(file);
  preview.hidden = false;
  prompt.hidden = true;
  button.disabled = false;
  button.textContent = "开始识图";
}

input.addEventListener("change", () => setFile(input.files[0]));
["dragenter", "dragover"].forEach(name => dropzone.addEventListener(name, event => { event.preventDefault(); dropzone.classList.add("drag"); }));
["dragleave", "drop"].forEach(name => dropzone.addEventListener(name, event => { event.preventDefault(); dropzone.classList.remove("drag"); }));
dropzone.addEventListener("drop", event => setFile(event.dataTransfer.files[0]));

async function syncOnOpen() {
  if (!api || api.includes("example.com")) { syncState.textContent = "请先配置后端地址"; syncState.className = "status error"; return; }
  try {
    const response = await fetch(`${api}/sync`, { method: "POST" });
    if (!response.ok) throw new Error();
    const result = await response.json();
    const inserted = result.inserted || 0;
    syncState.textContent = inserted ? `已更新 ${inserted} 个新品` : (result.reason || "商品库已是最新");
    syncState.className = "status ok";
  } catch { syncState.textContent = "更新失败，仍可搜索已有商品"; syncState.className = "status error"; }
}

button.addEventListener("click", async () => {
  if (!file) return;
  button.disabled = true; button.textContent = "正在比对…";
  const body = new FormData(); body.append("image", file);
  try {
    const response = await fetch(`${api}/search`, { method: "POST", body });
    if (!response.ok) throw new Error(await response.text());
    const data = await response.json();
    count.textContent = `${data.count} 个结果`;
    grid.innerHTML = data.results.length ? data.results.map(item => `
      <article class="card"><div class="thumb"><img loading="lazy" src="${api}${item.proxy_url}" alt="匹配鞋底" /></div>
      <div class="meta"><span class="source">#${item.id}</span><span class="score">${item.similarity}%</span></div></article>`).join("") : '<div class="empty">目录里暂时没有商品</div>';
  } catch { grid.innerHTML = '<div class="empty">识图失败，请检查后端地址或稍后重试</div>'; count.textContent = "请求失败"; }
  finally { button.disabled = false; button.textContent = "重新识图"; }
});

function escapeHtml(value) { const node = document.createElement("span"); node.textContent = value; return node.innerHTML; }
syncOnOpen();
