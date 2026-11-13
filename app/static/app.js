const $ = (s) => document.querySelector(s);
let projects = [];
let current = null;
let lastProposals = [];

async function j(url, opt) {
  const r = await fetch(url, opt);
  const t = await r.text();
  try { return { status: r.status, ...JSON.parse(t) }; }
  catch { return { status: r.status, raw: t }; }
}

function ev(obj) { return JSON.stringify(obj, null, 2); }

function renderCards(el, cards, emptyMsg) {
  if (!cards || !cards.length) {
    el.innerHTML = `<div class="empty">${emptyMsg}</div>`;
    return;
  }
  el.innerHTML = cards.map(c => `
    <article class="card ${c.severity || ""}">
      <div class="kind">${c.kind} · ${c.layer} · ${c.severity || ""}</div>
      <h3>${escapeHtml(c.title)}</h3>
      <p class="hint">${escapeHtml(c.asset_name)} / ${escapeHtml(c.task_type_name)}</p>
      <div class="evidence">${escapeHtml(ev(c.evidence))}</div>
      ${(c.comment_citations||[]).map(q => `<div class="cite">#${escapeHtml(q.comment_id)}：${escapeHtml(q.citation || q.text || "")}</div>`).join("")}
    </article>`).join("");
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

async function boot() {
  const h = await j("/api/health");
  const st = $("#status");
  if (h.kitsu_connected) {
    st.className = "status ok";
    st.textContent = `Kitsu 已连接 ${h.kitsu_host} · Zou ${(h.api&&h.api.version)||""} · as_of ${h.as_of}`;
  } else {
    st.className = "status bad";
    st.textContent = `Kitsu 未连接（${h.kitsu_error||"unknown"}）。扫描将尝试读本地快照，写回不可用。`;
  }
  const ban = $("#banner");
  if (h.banner) { ban.hidden = false; ban.textContent = h.banner; }

  const p = await j("/api/projects");
  projects = p.projects || [];
  const sel = $("#project");
  sel.innerHTML = projects.map(x => `<option value="${x.id}">${escapeHtml(x.name)}</option>`).join("");
  current = projects[0] && projects[0].id;
  sel.onchange = () => { current = sel.value; $("#meta").textContent = ""; };
  renderTemplates();
}

function renderTemplates() {
  $("#tmpl-view").innerHTML = projects.map(p => `
    <div class="tmpl">
      <h3>${escapeHtml(p.name)}</h3>
      <p>${escapeHtml(p.description)}</p>
      <p class="dag">阈值：宽限 ${p.risk.overdue_grace_days} 天 · 超估 ${p.risk.overrun_ratio} · 返修 ≥ ${p.risk.retake_threshold} · SLA ${p.risk.review_sla_days} 天 · 周产能 ${p.team_weekly_hours} 人时</p>
      <ol>${p.task_types.map(t => `<li>${escapeHtml(t.name)}（默认 ${t.default_days} 人天）${t.depends_on.length ? " ← " + t.depends_on.join(", ") : ""}</li>`).join("")}</ol>
    </div>`).join("");
}

$("#btn-scan").onclick = async () => {
  $("#btn-scan").disabled = true;
  try {
    const r = await j("/api/scan/" + current);
    $("#btn-scan").disabled = false;
    if (!r.ok) { $("#risk-rules").innerHTML = `<div class="empty">${escapeHtml(r.warning||"空")}</div>`; return; }
    $("#meta").textContent = `来源 ${r.source} · ${r.n_tasks} 条任务（进行中 ${r.n_open} / 已完成 ${r.n_done}）· 规则 ${r.latency_ms.rule} ms · 关键词 ${r.latency_ms.keyword} ms`;
    renderCards($("#risk-rules"), r.rule_cards, "规则层没有打出风险卡。若任务数为 0，这不是「一切正常」。");
    renderCards($("#risk-kw"), r.keyword_cards, "关键词基线没有打出阻塞。");
    if (!r.llm_ran) {
      $("#risk-llm").innerHTML = `<div class="empty">智能解读暂停（backend=${escapeHtml(r.llm_backend)}），未调用真实模型，不展示 LLM 指标。</div>`;
    } else {
      renderCards($("#risk-llm"), r.llm_cards, "模型没有抽出阻塞。");
    }
    const tasks = r.tasks || [];
    $("#task-table").innerHTML = `<table><thead><tr><th>资产</th><th>工序</th><th>状态</th><th>开始</th><th>截止</th><th>估时(人天)</th><th>实耗(人天)</th><th>返修</th></tr></thead><tbody>${
      tasks.map(t => `<tr><td>${escapeHtml(t.asset_name)} <small>${escapeHtml(t.asset_type_name)}</small></td><td>${escapeHtml(t.task_type_name)}</td><td>${escapeHtml(t.status_short)}</td><td>${t.start_date||""}</td><td>${t.due_date||""}</td><td>${t.estimation_seconds ? (t.estimation_seconds/28800).toFixed(2):""}</td><td>${t.duration_seconds ? (t.duration_seconds/28800).toFixed(2):"0"}</td><td>${t.retake_count}</td></tr>`).join("")
    }</tbody></table>`;
    (r.warnings||[]).forEach(w => console.info(w));
  } catch (e) {
    $("#btn-scan").disabled = false;
    alert(e);
  }
};

$("#btn-sched").onclick = async () => {
  const r = await j("/api/schedule/" + current);
  lastProposals = r.proposals || [];
  if (!lastProposals.length) {
    $("#sched-table").innerHTML = `<div class="empty">没有可建议的未完成任务。可先看「m_prop_newpack」或「c_character_new_npc」。`;
    return;
  }
  $("#sched-table").innerHTML = `<table><thead><tr><th></th><th>资产</th><th>工序</th><th>现有日期</th><th>建议开始</th><th>建议截止</th><th>估时</th><th>来源</th><th>冲突</th></tr></thead><tbody>${
    lastProposals.map((p,i) => {
      const cls = p.conflicts && p.conflicts.length ? "conflict" : (p.estimate_source==="template_default" ? "lowconf":"");
      return `<tr class="${cls}"><td><input type="checkbox" data-i="${i}" ${p.conflicts&&p.conflicts.length?"":"checked"}></td>
        <td>${escapeHtml(p.asset_name)}</td><td>${escapeHtml(p.task_type_name)}</td>
        <td>${p.current_start||"—"} → ${p.current_due||"—"}</td>
        <td>${p.proposed_start}</td><td>${p.proposed_due}</td>
        <td>${p.estimate_days??""}</td>
        <td>${escapeHtml(p.estimate_source)}${p.similar_task_ids.length? " · "+p.similar_task_ids.length+" 条参照":""}</td>
        <td>${p.conflicts&&p.conflicts.length ? escapeHtml(p.conflicts.map(c=>c.message).join("；")) : ""}</td></tr>`;
    }).join("")
  }</tbody></table>`;
};

$("#btn-confirm").onclick = async () => {
  const who = $("#confirmed-by").value.trim();
  const msg = $("#confirm-msg");
  if (!who) { msg.textContent = "请填写制片确认人。没有确认人，接口会拒绝写回。"; return; }
  const boxes = [...document.querySelectorAll("#sched-table input[type=checkbox]:checked")];
  const results = [];
  for (const b of boxes) {
    const p = lastProposals[Number(b.dataset.i)];
    const r = await j("/api/confirm", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ proposal_id: p.proposal_id, confirm_token: p.confirm_token, confirmed_by: who })
    });
    results.push({ asset: p.asset_name, type: p.task_type_name, ...r });
  }
  msg.textContent = JSON.stringify(results, null, 2);
};

$("#btn-deny").onclick = async () => {
  const p = lastProposals[0];
  const msg = $("#confirm-msg");
  if (!p) { msg.textContent = "先生成建议。"; return; }
  const r = await j("/api/confirm-denied-demo", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ proposal_id: p.proposal_id, confirmed_by: "演示" })
  });
  msg.textContent = "无令牌演示：\n" + JSON.stringify(r, null, 2);
};

$("#btn-audit").onclick = async () => {
  const r = await j("/api/audit");
  const rows = r.entries || [];
  $("#audit-table").innerHTML = `<table><thead><tr><th>时间</th><th>事件</th><th>ok</th><th>原因</th><th>确认人</th><th>任务</th><th>日期变化</th></tr></thead><tbody>${
    rows.slice().reverse().map(e => `<tr><td>${escapeHtml(e.ts)}</td><td>${escapeHtml(e.event)}</td><td>${e.ok}</td><td>${escapeHtml(e.reason||"")}</td><td>${escapeHtml(e.confirmed_by||"")}</td><td>${escapeHtml(e.asset_name||"")} ${escapeHtml(e.task_type_name||e.task_id||"")}</td><td>${escapeHtml((e.current_start||"")+" → "+(e.new_start||""))} / ${escapeHtml((e.current_due||"")+" → "+(e.new_due||""))}</td></tr>`).join("")
  }</tbody></table>`;
};

document.querySelectorAll(".tabs button").forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll(".tabs button").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
  };
});

boot();
