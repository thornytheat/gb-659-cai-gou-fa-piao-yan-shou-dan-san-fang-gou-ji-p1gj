// Assignment / payment forms.
//
// Each submission carries a request_id. It is kept across retries of the
// same intent and only refreshed after a successful post, so a retry never
// creates a second claim or a second payment receipt. When the server
// rejects a post it returns the original request body; the restore button
// puts those exact values back into the form.
const requestIds = new Map();

function newRequestId(kind){
  return `${kind}-${crypto.randomUUID ? crypto.randomUUID() : Date.now() + "-" + Math.random().toString(16).slice(2)}`;
}

function formValues(form){
  return Object.fromEntries(new FormData(form).entries());
}

function setMessage(form, text, level){
  const output = form.querySelector(".ops-msg");
  output.textContent = text;
  output.className = "ops-msg" + (level ? ` ops-msg--${level}` : "");
}

async function postForm(form){
  if(form.dataset.busy === "1") return;
  form.dataset.busy = "1";
  const kind = form.dataset.kind;
  const hidden = form.elements.request_id;
  if(!hidden.value){
    hidden.value = requestIds.get(kind) || newRequestId(kind);
    requestIds.set(kind, hidden.value);
  }
  const payload = formValues(form);
  setMessage(form, "提交中");
  try{
    const response = await fetch(form.dataset.endpoint, {
      method: "POST",
      headers: {"content-type": "application/json", "idempotency-key": hidden.value},
      body: JSON.stringify(payload)
    });
    const data = await response.json();
    if(!response.ok){
      const detail = data.detail || {};
      setMessage(form, `${detail.code || "失败"}：${detail.message || "请求失败"}（原请求已保留，可恢复后重试）`, "bad");
      form.dataset.restore = detail.restore_url || "";
      return;
    }
    setMessage(form, data.replayed ? "已重放同一请求，未新增记录" : "已完成", "ok");
    requestIds.delete(kind);
    hidden.value = "";
    setTimeout(() => window.location.reload(), 350);
  }catch(error){
    setMessage(form, `网络失败：${error.message}（原请求已保留，可重试）`, "bad");
  }finally{
    form.dataset.busy = "0";
  }
}

function restore(form, body){
  for(const [name, value] of Object.entries(body)){
    if(form.elements[name] !== undefined) form.elements[name].value = value;
  }
  setMessage(form, "已恢复原请求，确认后重试", "ok");
}

document.querySelectorAll(".ops-form").forEach((form) => {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    postForm(form);
  });
});

document.querySelectorAll(".ops-form").forEach((form) => {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn";
  button.textContent = "恢复原请求";
  button.addEventListener("click", async () => {
    if(!form.dataset.restore) { setMessage(form, "没有可恢复的失败请求", "bad"); return; }
    const response = await fetch(form.dataset.restore);
    const data = await response.json();
    if(!response.ok || !data.request){ setMessage(form, "无法恢复该请求", "bad"); return; }
    restore(form, data.request);
  });
  form.appendChild(button);
});
