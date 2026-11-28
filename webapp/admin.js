/* NexaPilot Zero-Trust Admin Panel Workspace (5 Tabs) */
window.NexaAdmin = (function () {
  "use strict";

  const I = window.NexaIcons.icon;
  const esc = (str) =>
    String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  const st = {
    tab: "overview",
    userFilter: "all",
    userQuery: "",
    payStatus: "pending",
    settingsSection: "general",
    settingsItems: [],
  };

  let ctx = null;

  async function adminApi(path, options = {}) {
    const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
    const initData = (tg && tg.initData) || "";
    const res = await fetch(`/api/admin${path}`, {
      method: options.method || "GET",
      headers: {
        "Content-Type": "application/json",
        "X-Telegram-Init-Data": initData,
      },
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    let data = {};
    try {
      data = await res.json();
    } catch (_) {}
    if (!res.ok) {
      const msg = (data && (typeof data.detail === "string" ? data.detail : data.detail && data.detail.message)) || `Error ${res.status}`;
      throw new Error(msg);
    }
    return data;
  }

  function mount(helpers) {
    ctx = helpers;
    renderShell();
  }

  function renderShell() {
    const root = document.getElementById("app-root");
    const tabs = [
      { id: "overview", label: "Overview" },
      { id: "users", label: "Users" },
      { id: "payments", label: "Payments" },
      { id: "settings", label: "Settings" },
      { id: "admins", label: "Admins" },
    ];

    root.innerHTML = `
      <div class="admin-header-bar">
        <div style="display:flex;align-items:center;gap:8px;">
          <span class="pill pill-admin">${I("shield", 13)} ADMIN PANEL</span>
        </div>
        <button id="adm-exit" class="btn btn-ghost btn-sm" type="button">Exit to User App</button>
      </div>
      <div class="admin-tabs">
        ${tabs
          .map(
            (t) => `<button class="seg-btn ${st.tab === t.id ? "active" : ""}" data-atab="${t.id}" type="button">${t.label}</button>`
          )
          .join("")}
      </div>
      <div id="adm-pane"><div class="skeleton" style="height:180px;"></div></div>`;

    document.getElementById("adm-exit").addEventListener("click", () => {
      ctx.haptic("light");
      ctx.exitAdmin();
    });
    root.querySelectorAll("[data-atab]").forEach((btn) => {
      btn.addEventListener("click", () => {
        st.tab = btn.getAttribute("data-atab");
        ctx.haptic("light");
        renderShell();
      });
    });

    if (st.tab === "overview") loadOverview();
    else if (st.tab === "users") loadUsers();
    else if (st.tab === "payments") loadPayments();
    else if (st.tab === "settings") loadSettings();
    else if (st.tab === "admins") loadAdminsAndBroadcast();
  }

  // -------------------------------------------------------------------------
  // 1. OVERVIEW + AI PROXY POOL
  // -------------------------------------------------------------------------
  async function loadOverview(refresh = false) {
    const pane = document.getElementById("adm-pane");
    try {
      const d = await adminApi(`/overview${refresh ? "?refresh=true" : ""}`);
      const pb = d.plan_breakdown || {};
      const proxies = d.proxies || [];
      pane.innerHTML = `
        <div class="kpi-grid">
          <div class="kpi-card">
            <div class="kpi-top"><span>Total Users</span>${I("users", 15)}</div>
            <div class="kpi-val">${d.total_users}</div>
            <div class="kpi-sub">Admins: ${pb.admin || 0} · Banned: ${pb.banned || 0}</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-top"><span>Online Userbots</span>${I("pulse", 15)}</div>
            <div class="kpi-val">${d.online_sessions}</div>
            <div class="kpi-sub">Live Kurigram clients</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-top"><span>Running Campaigns</span>${I("megaphone", 15)}</div>
            <div class="kpi-val">${d.running_campaigns}</div>
            <div class="kpi-sub">Active broadcast loops</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-top"><span>Pending Payments</span>${I("wallet", 15)}</div>
            <div class="kpi-val" style="color:${d.pending_payments > 0 ? "#FBBF24" : "var(--text)"};">${d.pending_payments}</div>
            <div class="kpi-sub">Awaiting review</div>
          </div>
        </div>

        <div class="card">
          <div class="card-header" style="margin-bottom:8px;">
            <h3 class="card-title">${I("crown", 16)} Plan Breakdown</h3>
            <span class="pill ${d.redis_connected ? "pill-emerald" : "pill-crimson"}">Redis: ${d.redis_connected ? "Connected" : "Fallback"}</span>
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:6px;">
            <span class="pill pill-emerald">Trial: ${pb.trial || 0}</span>
            <span class="pill pill-blue">Premium: ${pb.premium || 0}</span>
            <span class="pill pill-gold">Ultra: ${pb.ultra || 0}</span>
            <span class="pill pill-admin">Admin: ${pb.admin || 0}</span>
            <span class="pill pill-muted">Expired: ${pb.expired || 0}</span>
          </div>
        </div>

        <div class="card">
          <div class="card-header">
            <h3 class="card-title">${I("bot", 16)} AI Proxy Pool (${proxies.length})</h3>
            <button id="adm-reset-proxies" class="btn btn-blue btn-sm" type="button">${I("refresh", 13)} Reset Cooldowns</button>
          </div>
          ${proxies.length === 0
            ? `<div class="empty-state" style="padding:12px;">No AI_PROXY_*_URL nodes configured in ENV.</div>`
            : `<div class="proxy-grid">
                ${proxies
                  .map(
                    (p) => `
                  <div class="proxy-item">
                    <div style="display:flex;align-items:center;justify-content:space-between;gap:6px;">
                      <span class="mono" style="font-weight:700;">${esc(p.id.toUpperCase())}</span>
                      <span class="pill ${p.in_cooldown ? "pill-crimson" : "pill-emerald"}">${p.in_cooldown ? "Cooldown" : "Healthy"}</span>
                    </div>
                    <div class="mono" style="font-size:11px;color:var(--text-muted);margin-top:4px;">Served: <strong>${p.requests_served ?? 0}</strong></div>
                    <div class="mono" style="font-size:10.5px;color:var(--text-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px;">${esc(p.url)}</div>
                  </div>`
                  )
                  .join("")}
              </div>`}
        </div>`;

      document.getElementById("adm-reset-proxies").addEventListener("click", async (e) => {
        e.currentTarget.disabled = true;
        try {
          await adminApi("/proxies/reset-cooldowns", { method: "POST" });
          ctx.toast("All AI Proxy cooldowns reset!", "success");
          loadOverview(true);
        } catch (err) {
          ctx.toast(err.message, "error");
          e.currentTarget.disabled = false;
        }
      });
    } catch (err) {
      pane.innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
    }
  }

  // -------------------------------------------------------------------------
  // 2. USERS MANAGEMENT
  // -------------------------------------------------------------------------
  async function loadUsers() {
    const pane = document.getElementById("adm-pane");
    const filters = ["all", "connected", "trial", "premium", "ultra", "expired", "banned"];
    pane.innerHTML = `
      <div class="card" style="padding:12px;">
        <div class="chat-composer" style="margin-bottom:8px;">
          <input id="adm-user-q" class="input" placeholder="Search by Telegram ID or @username..." value="${esc(st.userQuery)}" />
          <button id="adm-user-go" class="btn btn-blue btn-sm" type="button">Search</button>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;">
          ${filters
            .map(
              (f) => `<button class="pill ${st.userFilter === f ? "pill-blue" : "pill-muted"}" style="cursor:pointer;" data-uflt="${f}" type="button">${f[0].toUpperCase() + f.slice(1)}</button>`
            )
            .join("")}
        </div>
      </div>
      <div id="adm-users-list"><div class="skeleton" style="height:120px;"></div></div>`;

    const doSearch = async () => {
      st.userQuery = document.getElementById("adm-user-q").value.trim();
      const listEl = document.getElementById("adm-users-list");
      try {
        const res = await adminApi(`/users?q=${encodeURIComponent(st.userQuery)}&filter=${encodeURIComponent(st.userFilter)}&limit=50`);
        const rows = res.users || [];
        listEl.innerHTML =
          rows.length === 0
            ? `<div class="empty-state">No users matched (${res.total || 0}).</div>`
            : rows
                .map(
                  (u) => `
                <div class="item-card">
                  <div class="item-top">
                    <div>
                      <div class="item-title">${esc(u.first_name || "User")} ${u.username ? `<span style="color:var(--text-muted);font-weight:500;">@${esc(u.username)}</span>` : ""}</div>
                      <div class="mono" style="color:var(--text-dim);">ID: ${u.owner_user_id} · Slots: ${u.connected_count}/${u.max_accounts}</div>
                    </div>
                    <span class="pill ${u.is_banned ? "pill-crimson" : u.is_admin ? "pill-admin" : u.effective_plan === "ultra" ? "pill-gold" : u.effective_plan === "premium" ? "pill-blue" : "pill-muted"}">${esc(u.plan_name)}</span>
                  </div>
                  <div class="item-meta">
                    <span>${esc(u.remaining_text)}</span>
                    <span>${u.is_online ? "Online" : "Offline"}</span>
                    <span>Refs: ${u.referral_count}</span>
                  </div>
                  <button class="btn btn-ghost btn-sm btn-full" data-umanage="${u.owner_user_id}" type="button">Manage User ${I("chevron", 13)}</button>
                </div>`
                )
                .join("");

        listEl.querySelectorAll("[data-umanage]").forEach((btn) => {
          btn.addEventListener("click", () => {
            const target = rows.find((r) => String(r.owner_user_id) === btn.dataset.umanage);
            if (target) openUserActionSheet(target, doSearch);
          });
        });
      } catch (err) {
        listEl.innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
      }
    };

    document.getElementById("adm-user-go").addEventListener("click", doSearch);
    document.getElementById("adm-user-q").addEventListener("keydown", (e) => {
      if (e.key === "Enter") doSearch();
    });
    pane.querySelectorAll("[data-uflt]").forEach((b) => {
      b.addEventListener("click", () => {
        st.userFilter = b.dataset.uflt;
        loadUsers();
      });
    });
    doSearch();
  }

  function openUserActionSheet(u, onDone) {
    ctx.openSheet(
      `Manage User #${u.owner_user_id}`,
      `
      <div class="field">
        <label>Set / Extend Plan</label>
        <select id="au-plan" class="select">
          <option value="trial" ${u.effective_plan === "trial" ? "selected" : ""}>Trial</option>
          <option value="premium" ${u.effective_plan === "premium" ? "selected" : ""}>Premium</option>
          <option value="ultra" ${u.effective_plan === "ultra" ? "selected" : ""}>Ultra Premium</option>
          <option value="unlimited" ${u.effective_plan === "unlimited" ? "selected" : ""}>Unlimited</option>
          <option value="none">Revoke (None)</option>
        </select>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
        <div class="field"><label>Duration (Days)</label><input id="au-days" class="input mono" type="number" value="30" min="0" /></div>
        <div class="field"><label>Max Account Slots</label><input id="au-slots" class="input mono" type="number" value="${u.max_accounts || 1}" min="0" /></div>
      </div>
      <button id="au-save-plan" class="btn btn-emerald btn-full" style="margin-bottom:10px;" type="button">${I("check", 15)} Apply Plan & Slots</button>
      <div class="btn-row">
        <button id="au-logout" class="btn btn-blue btn-sm" type="button">${I("logout", 14)} Force Logout</button>
        <button id="au-ban" class="btn ${u.is_banned ? "btn-emerald" : "btn-crimson"} btn-sm" type="button">${I("lock", 14)} ${u.is_banned ? "Unban User" : "Ban User"}</button>
      </div>`,
      () => {
        document.getElementById("au-save-plan").addEventListener("click", async (e) => {
          e.currentTarget.disabled = true;
          try {
            await adminApi(`/users/${u.owner_user_id}/plan`, {
              method: "POST",
              body: {
                plan_type: document.getElementById("au-plan").value,
                duration_days: Number(document.getElementById("au-days").value) || 0,
                max_accounts: Number(document.getElementById("au-slots").value),
                notify_user: true,
              },
            });
            ctx.closeSheet();
            ctx.toast("User plan updated!", "success");
            onDone();
          } catch (err) {
            ctx.toast(err.message, "error");
            e.currentTarget.disabled = false;
          }
        });
        document.getElementById("au-logout").addEventListener("click", async (e) => {
          e.currentTarget.disabled = true;
          try {
            await adminApi(`/users/${u.owner_user_id}/force-logout`, { method: "POST" });
            ctx.closeSheet();
            ctx.toast("User sessions logged out!", "success");
            onDone();
          } catch (err) {
            ctx.toast(err.message, "error");
            e.currentTarget.disabled = false;
          }
        });
        document.getElementById("au-ban").addEventListener("click", async (e) => {
          e.currentTarget.disabled = true;
          try {
            await adminApi(`/users/${u.owner_user_id}/ban`, {
              method: "POST",
              body: { is_banned: !u.is_banned },
            });
            ctx.closeSheet();
            ctx.toast(u.is_banned ? "User unbanned" : "User banned", "success");
            onDone();
          } catch (err) {
            ctx.toast(err.message, "error");
            e.currentTarget.disabled = false;
          }
        });
      }
    );
  }

  // -------------------------------------------------------------------------
  // 3. PAYMENTS REVIEW QUEUE
  // -------------------------------------------------------------------------
  async function loadPayments() {
    const pane = document.getElementById("adm-pane");
    const statuses = ["pending", "approved", "rejected", "all"];
    pane.innerHTML = `
      <div class="segmented">
        ${statuses
          .map(
            (s) => `<button class="seg-btn ${st.payStatus === s ? "active" : ""}" data-pst="${s}" type="button">${s[0].toUpperCase() + s.slice(1)}</button>`
          )
          .join("")}
      </div>
      <div id="adm-pay-list"><div class="skeleton" style="height:120px;"></div></div>`;

    pane.querySelectorAll("[data-pst]").forEach((b) => {
      b.addEventListener("click", () => {
        st.payStatus = b.dataset.pst;
        loadPayments();
      });
    });

    const listEl = document.getElementById("adm-pay-list");
    try {
      const res = await adminApi(`/payments?status=${encodeURIComponent(st.payStatus)}&limit=50`);
      const rows = res.payments || [];
      listEl.innerHTML =
        rows.length === 0
          ? `<div class="empty-state">No ${esc(st.payStatus)} payment requests.</div>`
          : rows
              .map((p) => {
                const isPend = p.status === "pending";
                const cls = p.status === "approved" ? "pill-emerald" : p.status === "rejected" ? "pill-crimson" : "pill-gold";
                return `
                  <div class="item-card">
                    <div class="item-top">
                      <div>
                        <div class="item-title">${esc(String(p.plan_type || "").toUpperCase())} · ${esc(p.amount_display)}</div>
                        <div class="mono" style="color:var(--text-dim);">${esc(p.id)} · User ${esc(p.user_id)} · ${esc(p.payment_method)}</div>
                      </div>
                      <span class="pill ${cls}">${esc(String(p.status || "").toUpperCase())}</span>
                    </div>
                    <div class="copy-box" style="margin:8px 0;">
                      <span class="copy-text">TrxID: ${esc(p.trx_id || "—")}</span>
                      <button class="btn btn-ghost btn-sm" data-copy-trx="${esc(p.trx_id || "")}" type="button">${I("copy", 13)}</button>
                    </div>
                    ${isPend
                      ? `<div class="btn-row">
                           <button class="btn btn-emerald btn-sm" data-prev="approve" data-pid="${esc(p.id)}" type="button">${I("check", 14)} Approve</button>
                           <button class="btn btn-crimson btn-sm" data-prev="reject" data-pid="${esc(p.id)}" type="button">${I("close", 14)} Reject</button>
                         </div>`
                      : ""}
                  </div>`;
              })
              .join("");

      listEl.querySelectorAll("[data-copy-trx]").forEach((b) => {
        b.addEventListener("click", () => ctx.copyText(b.dataset.copyTrx, "TrxID"));
      });
      listEl.querySelectorAll("[data-prev]").forEach((b) => {
        b.addEventListener("click", async () => {
          b.disabled = true;
          try {
            await adminApi(`/payments/${b.dataset.pid}/review`, {
              method: "POST",
              body: { action: b.dataset.prev },
            });
            ctx.toast(`Payment ${b.dataset.pid} ${b.dataset.prev}d!`, "success");
            loadPayments();
          } catch (err) {
            ctx.toast(err.message, "error");
            b.disabled = false;
          }
        });
      });
    } catch (err) {
      listEl.innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
    }
  }

  // -------------------------------------------------------------------------
  // 4. SETTINGS (LIVE ENV OVERRIDE)
  // -------------------------------------------------------------------------
  async function loadSettings() {
    const pane = document.getElementById("adm-pane");
    try {
      const res = await adminApi("/settings");
      st.settingsItems = res.settings || [];
      renderSettingsForm();
    } catch (err) {
      pane.innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
    }
  }

  function renderSettingsForm() {
    const pane = document.getElementById("adm-pane");
    const sections = [
      { id: "general", label: "General" },
      { id: "plans", label: "Plans" },
      { id: "referrals", label: "Referrals" },
      { id: "payments", label: "Payments" },
      { id: "gating", label: "Gating" },
    ];
    const visible = st.settingsItems.filter((it) => it.section === st.settingsSection);

    pane.innerHTML = `
      <div class="admin-tabs" style="margin-bottom:12px;">
        ${sections
          .map(
            (s) => `<button class="seg-btn ${st.settingsSection === s.id ? "active" : ""}" data-ssec="${s.id}" type="button">${s.label}</button>`
          )
          .join("")}
      </div>
      <div class="card">
        ${visible
          .map((item) => {
            const badge =
              item.source === "override"
                ? `<span class="pill pill-gold" style="font-size:10px;padding:2px 7px;">Admin Override</span>`
                : `<span class="pill pill-muted" style="font-size:10px;padding:2px 7px;">ENV Default</span>`;
            if (item.type === "bool") {
              return `
                <div class="toggle-row">
                  <div>
                    <div style="font-weight:600;font-size:13px;">${esc(item.label)}</div>
                    <div style="margin-top:3px;">${badge}</div>
                  </div>
                  <label class="switch">
                    <input type="checkbox" data-skey="${esc(item.key)}" data-stype="bool" ${item.value ? "checked" : ""} />
                    <span class="slider"></span>
                  </label>
                </div>`;
            }
            return `
              <div class="field">
                <div class="label-row">
                  <label style="margin:0;">${esc(item.label)}</label>
                  ${badge}
                </div>
                <input class="input ${item.type === "int" ? "mono" : ""}" type="${item.type === "int" ? "number" : "text"}" data-skey="${esc(item.key)}" data-stype="${item.type}" value="${esc(item.value)}" />
              </div>`;
          })
          .join("")}
        <button id="adm-save-settings" class="btn btn-emerald btn-full" style="margin-top:10px;" type="button">${I("check", 16)} Save & Apply Live</button>
      </div>`;

    pane.querySelectorAll("[data-ssec]").forEach((b) => {
      b.addEventListener("click", () => {
        st.settingsSection = b.dataset.ssec;
        renderSettingsForm();
      });
    });

    document.getElementById("adm-save-settings").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      const payload = {};
      pane.querySelectorAll("[data-skey]").forEach((el) => {
        const k = el.dataset.skey;
        const t = el.dataset.stype;
        if (t === "bool") payload[k] = el.checked;
        else if (t === "int") payload[k] = Number(el.value);
        else payload[k] = el.value.trim();
      });
      try {
        const res = await adminApi("/settings", { method: "PUT", body: { settings: payload } });
        st.settingsItems = res.settings || st.settingsItems;
        ctx.toast("Live settings saved & Redis refreshed!", "success");
        renderSettingsForm();
      } catch (err) {
        ctx.toast(err.message, "error");
        btn.disabled = false;
      }
    });
  }

  // -------------------------------------------------------------------------
  // 5. ADMINS & BROADCAST
  // -------------------------------------------------------------------------
  async function loadAdminsAndBroadcast() {
    const pane = document.getElementById("adm-pane");
    try {
      const res = await adminApi("/admins");
      const admins = res.admins || [];
      pane.innerHTML = `
        <div class="card">
          <div class="card-header"><h3 class="card-title">${I("shield", 16)} Admin Users (${admins.length})</h3></div>
          <div style="display:grid;grid-template-columns:1fr auto;gap:8px;margin-bottom:12px;">
            <input id="adm-new-id" class="input mono" type="number" placeholder="Telegram User ID" />
            <button id="adm-add-btn" class="btn btn-emerald btn-sm" type="button">${I("plus", 14)} Add</button>
          </div>
          ${admins
            .map(
              (a) => `
            <div class="copy-box">
              <div>
                <div class="mono" style="font-weight:700;">${a.telegram_user_id}</div>
                <div style="font-size:11px;color:var(--text-dim);">${a.immutable ? "Super Admin (ENV Locked)" : esc(a.note || "Added Admin")}</div>
              </div>
              ${a.immutable
                ? `<span class="pill pill-gold">${I("lock", 11)} Super</span>`
                : `<button class="btn btn-crimson btn-sm" data-adel="${a.telegram_user_id}" type="button">Remove</button>`}
            </div>`
            )
            .join("")}
        </div>

        <div class="card">
          <div class="card-header"><h3 class="card-title">${I("megaphone", 16)} Global Bot Broadcast</h3></div>
          <div class="field">
            <label>Target Audience</label>
            <select id="bc-aud" class="select">
              <option value="all">All Users</option>
              <option value="active">Active Subscribers Only</option>
              <option value="trial">Trial Users Only</option>
              <option value="expired">Expired / Free Users</option>
            </select>
          </div>
          <div class="field">
            <label>Announcement Message</label>
            <textarea id="bc-msg" class="textarea" placeholder="Write your announcement message..."></textarea>
          </div>
          <button id="bc-send" class="btn btn-blue btn-full" type="button">${I("send", 16)} Send Broadcast</button>
        </div>`;

      document.getElementById("adm-add-btn").addEventListener("click", async (e) => {
        const id = Number(document.getElementById("adm-new-id").value);
        if (!id) return ctx.toast("Enter a valid Telegram numeric ID.", "error");
        e.currentTarget.disabled = true;
        try {
          await adminApi("/admins", { method: "POST", body: { telegram_user_id: id } });
          ctx.toast(`Admin ${id} added (UNLIMITED plan active)!`, "success");
          loadAdminsAndBroadcast();
        } catch (err) {
          ctx.toast(err.message, "error");
          e.currentTarget.disabled = false;
        }
      });

      pane.querySelectorAll("[data-adel]").forEach((b) => {
        b.addEventListener("click", async () => {
          b.disabled = true;
          try {
            await adminApi(`/admins/${b.dataset.adel}`, { method: "DELETE" });
            ctx.toast("Admin removed", "success");
            loadAdminsAndBroadcast();
          } catch (err) {
            ctx.toast(err.message, "error");
            b.disabled = false;
          }
        });
      });

      document.getElementById("bc-send").addEventListener("click", async (e) => {
        const msg = document.getElementById("bc-msg").value.trim();
        const aud = document.getElementById("bc-aud").value;
        if (!msg) return ctx.toast("Enter a broadcast message.", "error");
        if (!window.confirm(`Send broadcast to '${aud}' users?`)) return;
        e.currentTarget.disabled = true;
        try {
          const job = await adminApi("/broadcast", { method: "POST", body: { audience: aud, message: msg } });
          document.getElementById("bc-msg").value = "";
          ctx.toast(`Broadcast ${job.job_id} queued for ${job.total} users!`, "success");
        } catch (err) {
          ctx.toast(err.message, "error");
        } finally {
          e.currentTarget.disabled = false;
        }
      });
    } catch (err) {
      pane.innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
    }
  }

  return { mount };
})();
