/* NexaPilot Telegram Mini App — Modular Vanilla JS SPA */
(function () {
  "use strict";

  const I = window.NexaIcons.icon;
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

  // Initialize native Telegram WebApp chrome
  if (tg) {
    try {
      tg.ready();
      tg.expand();
      if (tg.setHeaderColor) tg.setHeaderColor("#0B0F19");
      if (tg.setBackgroundColor) tg.setBackgroundColor("#0B0F19");
    } catch (_) {}
  }

  function haptic(type = "light") {
    try {
      if (!tg || !tg.HapticFeedback) return;
      if (type === "success" || type === "error" || type === "warning") {
        tg.HapticFeedback.notificationOccurred(type);
      } else {
        tg.HapticFeedback.impactOccurred(type);
      }
    } catch (_) {}
  }

  // Global App State
  const state = {
    boot: null,
    tab: "dashboard",
    subTab: {
      campaigns: "campaigns",
      templates: "templates",
      plans: "plans",
    },
    skipTrialGate: false,
    loginStep: 1, // 1=phone, 2=otp, 3=2fa
    loginPhone: "",
    loginDelivery: "APP",
    loginHint: "",
    healthResult: null,
    logoutConfirm: false,
    templates: null,
    campaigns: null,
    schedules: null,
    autoReplies: null,
    plans: null,
    payments: null,
    referrals: null,
    countdownTimer: null,
  };

  // DOM helpers
  const $ = (id) => document.getElementById(id);
  const esc = (str) =>
    String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  // Toast Notification
  function toast(msg, kind = "info") {
    const host = $("toast-container");
    if (!host) return;
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    const ic = kind === "success" ? I("check", 16) : kind === "error" ? I("alert", 16) : I("bolt", 16);
    el.innerHTML = `<span style="color:${kind === "success" ? "#34D399" : kind === "error" ? "#F87171" : "#60A5FA"}">${ic}</span><span>${esc(msg)}</span>`;
    host.appendChild(el);
    haptic(kind === "success" ? "success" : kind === "error" ? "error" : "light");
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transition = "opacity 0.2s ease";
      setTimeout(() => el.remove(), 220);
    }, 3200);
  }

  // Copy to Clipboard
  async function copyText(text, label = "Copied") {
    try {
      await navigator.clipboard.writeText(text);
      toast(`${label} copied to clipboard`, "success");
    } catch (_) {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
      toast(`${label} copied`, "success");
    }
  }

  // Bottom Sheet Modal
  function openSheet(title, html, onBind) {
    $("sheet-title").textContent = title;
    $("sheet-body").innerHTML = html;
    $("sheet-backdrop").classList.add("open");
    haptic("light");
    if (typeof onBind === "function") onBind($("sheet-body"));
  }

  function closeSheet() {
    $("sheet-backdrop").classList.remove("open");
  }

  // Authenticated API Client
  async function api(path, options = {}) {
    const initData = (tg && tg.initData) || "";
    const headers = Object.assign(
      {
        "Content-Type": "application/json",
        "X-Telegram-Init-Data": initData,
      },
      options.headers || {}
    );
    const res = await fetch(`/api/webapp${path}`, {
      method: options.method || "GET",
      headers,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    let data = {};
    try {
      data = await res.json();
    } catch (_) {}

    if (!res.ok) {
      const detail = data && data.detail;
      const msg =
        typeof detail === "object" && detail !== null
          ? detail.message || "Action restricted."
          : detail || `Request failed (${res.status})`;
      const err = new Error(msg);
      err.status = res.status;
      err.detail = detail;
      throw err;
    }
    return data;
  }

  // Countdown Formatter
  function formatCountdown(isoStr) {
    if (!isoStr) return "Inactive";
    const target = Date.parse(isoStr);
    if (Number.isNaN(target)) return "Inactive";
    const diff = Math.max(0, Math.floor((target - Date.now()) / 1000));
    if (diff <= 0) return "Expired";
    const d = Math.floor(diff / 86400);
    const h = Math.floor((diff % 86400) / 3600);
    const m = Math.floor((diff % 3600) / 60);
    const s = diff % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return `${d}d ${pad(h)}h ${pad(m)}m ${pad(s)}s`;
  }

  function startCountdownLoop() {
    if (state.countdownTimer) clearInterval(state.countdownTimer);
    state.countdownTimer = setInterval(() => {
      const el = $("live-countdown");
      if (el && state.boot && state.boot.entitlement) {
        el.textContent = formatCountdown(state.boot.entitlement.expires_at);
      }
    }, 1000);
  }

  // Plan Pill Helper
  function planBadgeHtml(plan) {
    const p = String(plan || "free").toLowerCase();
    if (p === "ultra") return `<span class="pill pill-gold">${I("crown", 13)} Ultra Premium</span>`;
    if (p === "premium") return `<span class="pill pill-blue">${I("bolt", 13)} Premium</span>`;
    if (p === "trial") return `<span class="pill pill-emerald">${I("gift", 13)} Starter Trial</span>`;
    return `<span class="pill pill-crimson">${I("lock", 13)} Expired / Free</span>`;
  }

  // Render Bottom Navigation
  function renderBottomNav(visible) {
    const nav = $("bottom-nav");
    const inner = $("bottom-nav-inner");
    if (!visible) {
      nav.classList.add("hidden");
      return;
    }
    nav.classList.remove("hidden");
    const tabs = [
      { id: "dashboard", label: "Home", icon: "dashboard" },
      { id: "account", label: "Account", icon: "account" },
      { id: "campaigns", label: "Campaigns", icon: "megaphone" },
      { id: "templates", label: "Templates", icon: "template" },
      { id: "plans", label: "Plans", icon: "crown" },
    ];
    inner.innerHTML = tabs
      .map(
        (t) => `<button class="nav-item ${state.tab === t.id ? "active" : ""}" data-tab="${t.id}" type="button">
          ${I(t.icon, 20)}
          <span>${t.label}</span>
        </button>`
      )
      .join("");
    inner.querySelectorAll(".nav-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        const next = btn.getAttribute("data-tab");
        if (next && next !== state.tab) {
          haptic("light");
          state.tab = next;
          render();
        }
      });
    });
  }

  // Bootstrap Loader
  async function loadBootstrap(refresh = false) {
    try {
      const data = await api(`/bootstrap${refresh ? "?refresh=true" : ""}`);
      state.boot = data;
      // Clear cached tab datasets on explicit refresh
      if (refresh) {
        state.templates = null;
        state.campaigns = null;
        state.schedules = null;
        state.autoReplies = null;
        state.plans = null;
        state.payments = null;
        state.referrals = null;
      }
      const user = data.user || {};
      $("brand-sub").textContent = user.username ? `@${user.username}` : user.first_name || "Control Center";
      render();
      startCountdownLoop();
    } catch (err) {
      renderBottomNav(false);
      $("app-root").innerHTML = `
        <div class="gate-wrap">
          <div class="gate-card">
            <div class="gate-badge" style="background:var(--crimson-soft);color:#F87171;">${I("shield", 28)}</div>
            <h2 style="margin:0 0 8px;font-size:18px;">Telegram Verification Required</h2>
            <p style="color:var(--text-muted);margin:0 0 18px;font-size:13px;">${esc(err.message)}</p>
            <p style="color:var(--text-dim);margin:0 0 18px;font-size:12px;">Open this Mini App directly from the NexaPilot Telegram Bot using the <strong>Open NexaPilot App</strong> button.</p>
            <button id="btn-retry-boot" class="btn btn-blue btn-full" type="button">${I("refresh", 16)} Retry Connection</button>
          </div>
        </div>`;
      const retry = $("btn-retry-boot");
      if (retry) retry.addEventListener("click", () => loadBootstrap(true));
    }
  }

  // Main Router
  function render() {
    if (!state.boot) return;
    const { force_join, entitlement } = state.boot;

    // Gate 1: Force-Join Channel
    if (force_join && force_join.required) {
      renderBottomNav(false);
      renderForceJoinGate(force_join);
      return;
    }

    // Gate 2: Claim 7-Day Free Trial
    if (!entitlement.is_active && !entitlement.trial_claimed && !state.skipTrialGate) {
      renderBottomNav(false);
      renderTrialGate(entitlement);
      return;
    }

    renderBottomNav(true);
    if (state.tab === "dashboard") renderDashboard();
    else if (state.tab === "account") renderAccount();
    else if (state.tab === "campaigns") renderCampaignsTab();
    else if (state.tab === "templates") renderTemplatesTab();
    else if (state.tab === "plans") renderPlansTab();
  }

  // =========================================================================
  // GATEKEEPERS
  // =========================================================================

  function renderForceJoinGate(fj) {
    $("app-root").innerHTML = `
      <div class="gate-wrap">
        <div class="gate-card">
          <div class="gate-badge" style="background:var(--blue-soft);color:#60A5FA;">${I("shield", 28)}</div>
          <h2 style="margin:0 0 8px;font-size:19px;">Join Official Channel</h2>
          <p style="color:var(--text-muted);margin:0 0 18px;font-size:13px;">
            Join <strong style="color:var(--text);">${esc(fj.channel)}</strong> to unlock NexaPilot automation and claim your free starter trial.
          </p>
          <div style="display:grid;gap:10px;">
            ${fj.join_url ? `<a href="${esc(fj.join_url)}" target="_blank" rel="noopener" class="btn btn-blue btn-full">${I("send", 16)} Join ${esc(fj.channel)}</a>` : ""}
            <button id="btn-verify-fj" class="btn btn-emerald btn-full" type="button">${I("check", 16)} Verify Membership</button>
          </div>
        </div>
      </div>`;

    $("btn-verify-fj").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      try {
        await api("/verify-channel", { method: "POST" });
        toast("Channel membership verified!", "success");
        await loadBootstrap(true);
      } catch (err) {
        toast(err.message, "error");
        btn.disabled = false;
      }
    });
  }

  function renderTrialGate() {
    $("app-root").innerHTML = `
      <div class="gate-wrap">
        <div class="gate-card">
          <div class="gate-badge" style="background:var(--emerald-soft);color:#34D399;">${I("gift", 28)}</div>
          <h2 style="margin:0 0 8px;font-size:20px;">Claim Your 7-Day Free Trial</h2>
          <p style="color:var(--text-muted);margin:0 0 18px;font-size:13px;">
            Start automating Telegram broadcasts immediately with 1 account, 2 templates, and 1 active campaign — zero card required.
          </p>
          <div style="display:grid;gap:10px;">
            <button id="btn-claim-trial" class="btn btn-emerald btn-full" type="button">${I("gift", 17)} Claim 7-Day Starter Trial</button>
            <button id="btn-skip-trial" class="btn btn-ghost btn-full" type="button">${I("crown", 16)} View Premium Plans</button>
          </div>
        </div>
      </div>`;

    $("btn-claim-trial").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      try {
        await api("/claim-trial", { method: "POST" });
        toast("7-Day Starter Trial activated!", "success");
        await loadBootstrap(true);
      } catch (err) {
        toast(err.message, "error");
        btn.disabled = false;
      }
    });

    $("btn-skip-trial").addEventListener("click", () => {
      state.skipTrialGate = true;
      state.tab = "plans";
      render();
    });
  }

  // =========================================================================
  // TAB 1: DASHBOARD
  // =========================================================================

  function renderDashboard() {
    const { entitlement, account, counts, timezone } = state.boot;
    const isOnline = Boolean(account && account.is_online);

    $("app-root").innerHTML = `
      <section class="card card-hero">
        <div class="card-header" style="margin-bottom:8px;">
          ${planBadgeHtml(entitlement.effective_plan)}
          <span class="pill pill-muted">${I("clock", 12)} ${esc(timezone || "UTC")}</span>
        </div>
        <div style="font-size:13px;color:var(--text-muted);">
          ${entitlement.is_active ? "All eligible automation modules are unlocked and ready." : "Your plan is currently inactive. Upgrade or refer friends to resume."}
        </div>
        <div class="countdown-box">
          <div>
            <div class="countdown-label">Subscription Countdown</div>
            <div id="live-countdown" class="countdown-value">${esc(formatCountdown(entitlement.expires_at))}</div>
          </div>
          ${!entitlement.is_active || entitlement.effective_plan === "trial"
            ? `<button id="dash-upgrade" class="btn btn-blue btn-sm" type="button">${I("crown", 14)} Upgrade</button>`
            : `<span class="pill pill-emerald">${I("check", 12)} Active</span>`}
        </div>
      </section>

      <section class="card">
        <div class="card-header" style="margin-bottom:8px;">
          <h3 class="card-title">${I("account", 17)} Connected Userbot</h3>
          ${account
            ? `<span class="pill ${isOnline ? "pill-emerald" : "pill-crimson"}"><span class="status-dot ${isOnline ? "online" : "offline"}"></span>${isOnline ? "Online" : "Offline"}</span>`
            : `<span class="pill pill-muted">Not Connected</span>`}
        </div>
        ${account
          ? `<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;">
               <div>
                 <div style="font-weight:700;font-size:14.5px;">${account.username ? "@" + esc(account.username) : "User " + esc(account.telegram_user_id)}</div>
                 <div class="mono" style="color:var(--text-muted);">${esc(account.phone_number || "ID: " + account.telegram_user_id)}</div>
               </div>
               <button id="dash-open-acc" class="btn btn-ghost btn-sm" type="button">Manage ${I("chevron", 14)}</button>
             </div>`
          : `<p style="margin:0 0 12px;color:var(--text-muted);font-size:13px;">Connect your Telegram account directly inside the Mini App — no spaced OTP required.</p>
             <button id="dash-connect-acc" class="btn btn-emerald btn-full" type="button">${I("key", 16)} Connect Telegram Account</button>`}
      </section>

      <div class="kpi-grid">
        <div class="kpi-card">
          <div class="kpi-top"><span>Active Campaigns</span>${I("megaphone", 15)}</div>
          <div class="kpi-val">${counts.active_campaigns}<span style="font-size:13px;color:var(--text-dim);">/${entitlement.max_campaigns}</span></div>
          <div class="kpi-sub">${counts.total_campaigns} total created</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-top"><span>Templates</span>${I("template", 15)}</div>
          <div class="kpi-val">${counts.templates}<span style="font-size:13px;color:var(--text-dim);">/${entitlement.max_templates}</span></div>
          <div class="kpi-sub">Saved message presets</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-top"><span>Scheduled</span>${I("clock", 15)}</div>
          <div class="kpi-val">${counts.schedules}</div>
          <div class="kpi-sub">${entitlement.can_use_schedules ? "Premium unlocked" : "Requires Premium"}</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-top"><span>Auto-Replies</span>${I("bot", 15)}</div>
          <div class="kpi-val">${counts.auto_replies_active}<span style="font-size:13px;color:var(--text-dim);">/3</span></div>
          <div class="kpi-sub">${entitlement.can_use_auto_replies ? "Ultra unlocked" : "Requires Ultra"}</div>
        </div>
      </div>

      <div class="btn-row">
        <button id="dash-go-cmp" class="btn btn-blue" type="button">${I("megaphone", 16)} Campaigns</button>
        <button id="dash-go-ref" class="btn btn-ghost" type="button">${I("users", 16)} Refer & Earn</button>
      </div>`;

    const up = $("dash-upgrade");
    if (up) up.addEventListener("click", () => { state.tab = "plans"; render(); });
    const oa = $("dash-open-acc");
    if (oa) oa.addEventListener("click", () => { state.tab = "account"; render(); });
    const ca = $("dash-connect-acc");
    if (ca) ca.addEventListener("click", () => { state.tab = "account"; render(); });
    $("dash-go-cmp").addEventListener("click", () => { state.tab = "campaigns"; render(); });
    $("dash-go-ref").addEventListener("click", () => {
      state.tab = "plans";
      state.subTab.plans = "referrals";
      render();
    });
  }

  // =========================================================================
  // TAB 2: ACCOUNT (DIRECT 5-BOX OTP LOGIN, HEALTH PING, 2-STEP REAL LOGOUT)
  // =========================================================================

  function renderAccount() {
    const { account } = state.boot;
    if (account) {
      renderConnectedAccount(account);
    } else {
      renderLoginWizard();
    }
  }

  function renderConnectedAccount(acc) {
    const online = Boolean(acc.is_online);
    const hr = state.healthResult;

    $("app-root").innerHTML = `
      <section class="card card-hero">
        <div class="card-header">
          <h3 class="card-title">${I("account", 18)} Active Userbot Session</h3>
          <span class="pill ${online ? "pill-emerald" : "pill-crimson"}">
            <span class="status-dot ${online ? "online" : "offline"}"></span>
            ${online ? "Online" : "Offline"}
          </span>
        </div>
        <div style="display:grid;gap:6px;margin-bottom:14px;">
          <div style="font-size:16px;font-weight:700;">${acc.username ? "@" + esc(acc.username) : "Telegram Account"}</div>
          <div class="mono" style="color:var(--text-muted);">ID: ${esc(acc.telegram_user_id)} ${acc.phone_number ? " · " + esc(acc.phone_number) : ""}</div>
        </div>
        <button id="btn-ping-health" class="btn btn-blue btn-full" type="button">${I("pulse", 16)} Run Live Health Ping</button>
        ${hr ? `
          <div class="countdown-box" style="margin-top:12px;">
            <div>
              <div class="countdown-label">Telegram DC Response</div>
              <div class="countdown-value" style="font-size:14px;color:${hr.online ? "#34D399" : "#F87171"};">
                ${hr.online ? `Healthy · ${hr.latency_ms} ms` : `Offline (${esc(hr.error || "disconnected")})`}
              </div>
            </div>
            <span class="pill ${hr.online ? "pill-emerald" : "pill-crimson"}">${hr.online ? "OK" : "ERR"}</span>
          </div>` : ""}
      </section>

      <section class="card card-danger">
        <div class="card-header">
          <h3 class="card-title" style="color:#FCA5A5;">${I("logout", 17)} Danger Zone — Real Logout</h3>
        </div>
        <p style="margin:0 0 14px;font-size:12.5px;color:var(--text-muted);">
          Logging out calls Telegram's official <code>log_out()</code> RPC to terminate this device session on Telegram servers and removes it from NexaPilot.
        </p>
        ${!state.logoutConfirm
          ? `<button id="btn-logout-step1" class="btn btn-danger-outline btn-full" type="button">${I("logout", 16)} Logout & Terminate Session</button>`
          : `<div class="btn-row">
               <button id="btn-logout-cancel" class="btn btn-ghost" type="button">Cancel</button>
               <button id="btn-logout-confirm" class="btn btn-crimson" type="button">${I("logout", 16)} Confirm Real Logout</button>
             </div>`}
      </section>`;

    $("btn-ping-health").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      try {
        state.healthResult = await api("/account/health");
        toast(
          state.healthResult.online
            ? `Userbot responded in ${state.healthResult.latency_ms} ms`
            : "Userbot is currently offline",
          state.healthResult.online ? "success" : "error"
        );
        renderAccount();
      } catch (err) {
        toast(err.message, "error");
        btn.disabled = false;
      }
    });

    const s1 = $("btn-logout-step1");
    if (s1) {
      s1.addEventListener("click", () => {
        haptic("warning");
        state.logoutConfirm = true;
        renderAccount();
      });
    }
    const cancel = $("btn-logout-cancel");
    if (cancel) {
      cancel.addEventListener("click", () => {
        state.logoutConfirm = false;
        renderAccount();
      });
    }
    const confirmBtn = $("btn-logout-confirm");
    if (confirmBtn) {
      confirmBtn.addEventListener("click", async (e) => {
        e.currentTarget.disabled = true;
        try {
          const res = await api("/account/logout", { method: "POST" });
          state.logoutConfirm = false;
          state.healthResult = null;
          state.loginStep = 1;
          toast(
            res.telegram_device_terminated
              ? "Session terminated on Telegram & logged out."
              : "Disconnected & removed from NexaPilot.",
            "success"
          );
          await loadBootstrap(true);
        } catch (err) {
          toast(err.message, "error");
          e.currentTarget.disabled = false;
        }
      });
    }
  }

  function renderLoginWizard() {
    const step = state.loginStep;
    let body = "";

    if (step === 1) {
      body = `
        <div class="field">
          <label for="inp-phone">Telegram Phone Number (International Format)</label>
          <input id="inp-phone" class="input mono" type="tel" placeholder="+8801700000000" value="${esc(state.loginPhone)}" autocomplete="tel" />
          <div class="field-hint">Enter your phone number with country code. Direct OTP verification inside the Mini App does not require spaces.</div>
        </div>
        <button id="btn-send-otp" class="btn btn-emerald btn-full" type="button">${I("send", 16)} Send Telegram Login Code</button>`;
    } else if (step === 2) {
      body = `
        <p style="margin:0 0 6px;font-size:13px;color:var(--text-muted);">
          Enter the 5-digit code sent to <strong style="color:var(--text);">${esc(state.loginPhone)}</strong> via <strong>${esc(state.loginDelivery)}</strong>.
        </p>
        <div class="otp-grid" id="otp-grid">
          ${[0, 1, 2, 3, 4].map((i) => `<input class="otp-box" data-idx="${i}" type="text" inputmode="numeric" maxlength="1" autocomplete="one-time-code" />`).join("")}
        </div>
        <div class="btn-row">
          <button id="btn-otp-back" class="btn btn-ghost" type="button">Change Phone</button>
          <button id="btn-verify-otp" class="btn btn-emerald" type="button">${I("check", 16)} Verify Code</button>
        </div>`;
    } else {
      body = `
        <p style="margin:0 0 10px;font-size:13px;color:var(--text-muted);">
          Your Telegram account has Two-Step Verification enabled.
          ${state.loginHint ? `<br/>Password hint: <strong style="color:#FBBF24;">${esc(state.loginHint)}</strong>` : ""}
        </p>
        <div class="field">
          <label for="inp-2fa">2FA Cloud Password</label>
          <input id="inp-2fa" class="input" type="password" placeholder="Enter your Telegram 2FA password" />
        </div>
        <div class="btn-row">
          <button id="btn-2fa-back" class="btn btn-ghost" type="button">Cancel</button>
          <button id="btn-verify-2fa" class="btn btn-emerald" type="button">${I("shield", 16)} Unlock & Connect</button>
        </div>`;
    }

    $("app-root").innerHTML = `
      <section class="card card-hero">
        <div class="card-header">
          <h3 class="card-title">${I("key", 18)} Connect Telegram Account</h3>
          <span class="pill pill-blue">Step ${step} / 3</span>
        </div>
        ${body}
      </section>`;

    if (step === 1) {
      $("btn-send-otp").addEventListener("click", async (e) => {
        const phone = $("inp-phone").value.trim();
        if (phone.length < 6) return toast("Enter a valid phone number with country code.", "error");
        const btn = e.currentTarget;
        btn.disabled = true;
        try {
          const res = await api("/account/send-code", { method: "POST", body: { phone_number: phone } });
          state.loginPhone = phone;
          state.loginDelivery = res.delivery_type || "APP";
          state.loginStep = 2;
          toast("OTP code sent to your Telegram app!", "success");
          renderLoginWizard();
        } catch (err) {
          toast(err.message, "error");
          btn.disabled = false;
        }
      });
    } else if (step === 2) {
      const boxes = Array.from(document.querySelectorAll(".otp-box"));
      if (boxes[0]) boxes[0].focus();
      boxes.forEach((box, idx) => {
        box.addEventListener("input", () => {
          const v = box.value.replace(/\D/g, "");
          box.value = v.slice(0, 1);
          if (box.value && idx < boxes.length - 1) boxes[idx + 1].focus();
        });
        box.addEventListener("keydown", (ev) => {
          if (ev.key === "Backspace" && !box.value && idx > 0) {
            boxes[idx - 1].focus();
          }
        });
        box.addEventListener("paste", (ev) => {
          ev.preventDefault();
          const digits = (ev.clipboardData.getData("text") || "").replace(/\D/g, "").slice(0, 5);
          digits.split("").forEach((d, i) => {
            if (boxes[i]) boxes[i].value = d;
          });
          if (boxes[Math.min(digits.length, 4)]) boxes[Math.min(digits.length, 4)].focus();
        });
      });

      $("btn-otp-back").addEventListener("click", () => {
        state.loginStep = 1;
        renderLoginWizard();
      });

      $("btn-verify-otp").addEventListener("click", async (e) => {
        const code = boxes.map((b) => b.value.trim()).join("");
        if (code.length < 4) return toast("Enter all OTP digits.", "error");
        const btn = e.currentTarget;
        btn.disabled = true;
        try {
          const res = await api("/account/verify-otp", { method: "POST", body: { code } });
          if (res["2fa_required"]) {
            state.loginHint = res.password_hint || "";
            state.loginStep = 3;
            toast("2FA password required.", "info");
            renderLoginWizard();
          } else {
            state.loginStep = 1;
            toast("Telegram account connected!", "success");
            await loadBootstrap(true);
          }
        } catch (err) {
          toast(err.message, "error");
          btn.disabled = false;
        }
      });
    } else {
      $("btn-2fa-back").addEventListener("click", () => {
        state.loginStep = 1;
        renderLoginWizard();
      });
      $("btn-verify-2fa").addEventListener("click", async (e) => {
        const password = $("inp-2fa").value;
        if (!password) return toast("Enter your 2FA password.", "error");
        const btn = e.currentTarget;
        btn.disabled = true;
        try {
          await api("/account/verify-2fa", { method: "POST", body: { password } });
          state.loginStep = 1;
          toast("2FA verified & userbot is now online!", "success");
          await loadBootstrap(true);
        } catch (err) {
          toast(err.message, "error");
          btn.disabled = false;
        }
      });
    }
  }

  // =========================================================================
  // TAB 3: CAMPAIGNS & SCHEDULES
  // =========================================================================

  async function renderCampaignsTab() {
    const sub = state.subTab.campaigns;
    $("app-root").innerHTML = `
      <div class="segmented">
        <button class="seg-btn ${sub === "campaigns" ? "active" : ""}" data-sub="campaigns" type="button">Campaigns</button>
        <button class="seg-btn ${sub === "schedules" ? "active" : ""}" data-sub="schedules" type="button">Schedules</button>
      </div>
      <div id="cmp-pane"><div class="skeleton" style="height:120px;"></div></div>`;

    document.querySelectorAll(".seg-btn").forEach((b) => {
      b.addEventListener("click", () => {
        state.subTab.campaigns = b.getAttribute("data-sub");
        haptic("light");
        renderCampaignsTab();
      });
    });

    if (sub === "campaigns") await loadAndRenderCampaigns();
    else await loadAndRenderSchedules();
  }

  async function loadAndRenderCampaigns() {
    try {
      const [cmpData, tplData] = await Promise.all([
        api("/campaigns"),
        state.templates ? Promise.resolve(state.templates) : api("/templates"),
      ]);
      state.campaigns = cmpData;
      state.templates = tplData;
      const list = cmpData.campaigns || [];
      const maxC = cmpData.max_campaigns || 1;
      const pct = Math.min(100, Math.round((list.length / Math.max(1, maxC)) * 100));

      $("cmp-pane").innerHTML = `
        <div class="card">
          <div class="card-header" style="margin-bottom:6px;">
            <span style="font-weight:700;">Campaign Quota</span>
            <span class="mono">${list.length} / ${maxC}</span>
          </div>
          <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
          <button id="btn-new-cmp" class="btn btn-blue btn-full" style="margin-top:10px;" type="button">${I("plus", 16)} New Broadcast Campaign</button>
        </div>
        ${list.length === 0
          ? `<div class="empty-state">No campaigns yet. Tap <strong>New Broadcast Campaign</strong> to launch one.</div>`
          : list.map((c) => {
              const st = String(c.status || "idle").toLowerCase();
              const pill = st === "running" ? "pill-emerald" : st === "paused" ? "pill-gold" : "pill-muted";
              const groupsCount = Array.isArray(c.target_groups) ? c.target_groups.length : 0;
              return `
                <div class="item-card">
                  <div class="item-top">
                    <div>
                      <div class="item-title">${esc(c.name)}</div>
                      <div class="mono" style="color:var(--text-dim);">${esc(c.id)} · TPL ${esc(c.template_id)}</div>
                    </div>
                    <span class="pill ${pill}">${esc(st.toUpperCase())}</span>
                  </div>
                  <div class="item-meta">
                    <span>Mode: ${esc(c.target_mode)}</span>
                    <span>Groups: ${groupsCount}</span>
                    <span>Delay: ${esc(c.delay_seconds)}s</span>
                    <span>Sent: ${esc(c.sent_count || 0)}</span>
                  </div>
                  <div class="btn-row">
                    ${st === "running"
                      ? `<button class="btn btn-gold btn-sm" data-act="pause" data-id="${esc(c.id)}">${I("pause", 13)} Pause</button>`
                      : `<button class="btn btn-emerald btn-sm" data-act="start" data-id="${esc(c.id)}">${I("play", 13)} Start</button>`}
                    <button class="btn btn-ghost btn-sm" data-act="stop" data-id="${esc(c.id)}">${I("stop", 13)} Stop</button>
                    <button class="btn btn-danger-outline btn-sm" data-del="${esc(c.id)}">${I("trash", 13)}</button>
                  </div>
                </div>`;
            }).join("")}`;

      $("btn-new-cmp").addEventListener("click", openNewCampaignSheet);
      $("cmp-pane").querySelectorAll("[data-act]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          try {
            await api(`/campaigns/${btn.dataset.id}/${btn.dataset.act}`, { method: "POST" });
            toast(`Campaign ${btn.dataset.act} executed`, "success");
            await loadAndRenderCampaigns();
          } catch (err) {
            toast(err.message, "error");
            btn.disabled = false;
          }
        });
      });
      $("cmp-pane").querySelectorAll("[data-del]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          try {
            await api(`/campaigns/${btn.dataset.del}`, { method: "DELETE" });
            toast("Campaign deleted", "success");
            await loadAndRenderCampaigns();
          } catch (err) {
            toast(err.message, "error");
            btn.disabled = false;
          }
        });
      });
    } catch (err) {
      $("cmp-pane").innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
    }
  }

  function openNewCampaignSheet() {
    const tpls = (state.templates && state.templates.templates) || [];
    if (!tpls.length) {
      toast("Create at least one Template first.", "error");
      state.tab = "templates";
      render();
      return;
    }
    const opts = tpls.map((t) => `<option value="${esc(t.id)}">${esc(t.id)} — ${esc(String(t.text || "").slice(0, 38))}</option>`).join("");
    openSheet(
      "New Broadcast Campaign",
      `
      <div class="field"><label>Campaign Name</label><input id="c-name" class="input" placeholder="e.g. Daily Promo Blast" /></div>
      <div class="field"><label>Template</label><select id="c-tpl" class="select">${opts}</select></div>
      <div class="field">
        <label>Target Mode</label>
        <select id="c-mode" class="select">
          <option value="buysell">Buy/Sell Groups</option>
          <option value="all_groups">All Groups</option>
          <option value="keywords">Keyword Match</option>
          <option value="folder">Telegram Folder</option>
        </select>
      </div>
      <div class="field"><label>Folder Name / Keywords (optional, comma-separated)</label><input id="c-extra" class="input" placeholder="e.g. crypto, market" /></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
        <div class="field"><label>Delay (sec)</label><input id="c-delay" class="input mono" type="number" value="15" min="5" /></div>
        <div class="field"><label>Repeat (min)</label><input id="c-repeat" class="input mono" type="number" value="60" min="0" /></div>
      </div>
      <button id="c-submit" class="btn btn-emerald btn-full" type="button">${I("check", 16)} Create Campaign</button>`,
      () => {
        $("c-submit").addEventListener("click", async (e) => {
          const name = $("c-name").value.trim();
          const mode = $("c-mode").value;
          const extra = $("c-extra").value.trim();
          if (name.length < 2) return toast("Enter a campaign name.", "error");
          e.currentTarget.disabled = true;
          try {
            await api("/campaigns", {
              method: "POST",
              body: {
                name,
                template_id: $("c-tpl").value,
                target_mode: mode,
                folder_name: mode === "folder" ? extra : null,
                keywords: mode === "keywords" ? extra.split(",").map((s) => s.trim()).filter(Boolean) : [],
                delay_seconds: Number($("c-delay").value) || 15,
                repeat_interval_minutes: Number($("c-repeat").value) || 0,
              },
            });
            closeSheet();
            toast("Campaign created!", "success");
            await loadAndRenderCampaigns();
          } catch (err) {
            toast(err.message, "error");
            e.currentTarget.disabled = false;
          }
        });
      }
    );
  }

  async function loadAndRenderSchedules() {
    try {
      const [scData, tplData] = await Promise.all([
        api("/schedules"),
        state.templates ? Promise.resolve(state.templates) : api("/templates"),
      ]);
      state.schedules = scData;
      state.templates = tplData;

      if (scData.locked) {
        $("cmp-pane").innerHTML = `
          <div class="lock-card">
            <div class="lock-icon-wrap">${I("lock", 24)}</div>
            <h3 style="margin:0 0 6px;font-size:16px;">Scheduled Posts — Premium Feature</h3>
            <p style="margin:0 0 16px;color:var(--text-muted);font-size:13px;">Automate one-time and recurring scheduled broadcasts by upgrading to Premium or Ultra.</p>
            <button id="btn-unlock-sched" class="btn btn-blue" type="button">${I("crown", 16)} Unlock with Premium</button>
          </div>`;
        $("btn-unlock-sched").addEventListener("click", () => { state.tab = "plans"; render(); });
        return;
      }

      const list = scData.schedules || [];
      $("cmp-pane").innerHTML = `
        <button id="btn-new-sched" class="btn btn-blue btn-full" style="margin-bottom:12px;" type="button">${I("plus", 16)} New Scheduled Post</button>
        ${list.length === 0
          ? `<div class="empty-state">No active scheduled posts.</div>`
          : list.map((s) => `
              <div class="item-card">
                <div class="item-top">
                  <div>
                    <div class="item-title">Template ${esc(s.template_id)}</div>
                    <div class="mono" style="color:var(--text-muted);">Chat: ${esc(s.target_chat_id)}</div>
                  </div>
                  <button class="btn btn-danger-outline btn-sm" data-sdel="${esc(s.id)}">${I("trash", 13)}</button>
                </div>
                <div class="item-meta">
                  <span>${s.interval_minutes ? `Every ${s.interval_minutes} min` : `Run at: ${esc(s.run_at)}`}</span>
                </div>
              </div>`).join("")}`;

      $("btn-new-sched").addEventListener("click", openNewScheduleSheet);
      $("cmp-pane").querySelectorAll("[data-sdel]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          try {
            await api(`/schedules/${btn.dataset.sdel}`, { method: "DELETE" });
            toast("Scheduled post removed", "success");
            await loadAndRenderSchedules();
          } catch (err) {
            toast(err.message, "error");
            btn.disabled = false;
          }
        });
      });
    } catch (err) {
      $("cmp-pane").innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
    }
  }

  function openNewScheduleSheet() {
    const tpls = (state.templates && state.templates.templates) || [];
    if (!tpls.length) return toast("Create a Template first.", "error");
    const opts = tpls.map((t) => `<option value="${esc(t.id)}">${esc(t.id)} — ${esc(String(t.text || "").slice(0, 38))}</option>`).join("");
    openSheet(
      "New Scheduled Post",
      `
      <div class="field"><label>Template</label><select id="s-tpl" class="select">${opts}</select></div>
      <div class="field"><label>Target Chat ID (e.g. -1001234567890)</label><input id="s-chat" class="input mono" type="number" placeholder="-1001234567890" /></div>
      <div class="field"><label>Interval Minutes (for recurring) — OR leave empty for one-time</label><input id="s-int" class="input mono" type="number" min="1" placeholder="60" /></div>
      <div class="field"><label>One-Time Run At (YYYY-MM-DD HH:MM)</label><input id="s-run" class="input mono" placeholder="2026-12-01 18:30" /></div>
      <button id="s-submit" class="btn btn-emerald btn-full" type="button">${I("clock", 16)} Save Schedule</button>`,
      () => {
        $("s-submit").addEventListener("click", async (e) => {
          const chatId = Number($("s-chat").value);
          const interval = Number($("s-int").value) || null;
          const runAt = $("s-run").value.trim() || null;
          if (!chatId) return toast("Enter a valid numeric Target Chat ID.", "error");
          e.currentTarget.disabled = true;
          try {
            await api("/schedules", {
              method: "POST",
              body: { template_id: $("s-tpl").value, target_chat_id: chatId, interval_minutes: interval, run_at: runAt },
            });
            closeSheet();
            toast("Scheduled post saved!", "success");
            await loadAndRenderSchedules();
          } catch (err) {
            toast(err.message, "error");
            e.currentTarget.disabled = false;
          }
        });
      }
    );
  }

  // =========================================================================
  // TAB 4: TEMPLATES & AUTO-REPLIES
  // =========================================================================

  async function renderTemplatesTab() {
    const sub = state.subTab.templates;
    $("app-root").innerHTML = `
      <div class="segmented">
        <button class="seg-btn ${sub === "templates" ? "active" : ""}" data-sub="templates" type="button">Templates</button>
        <button class="seg-btn ${sub === "autoreplies" ? "active" : ""}" data-sub="autoreplies" type="button">Auto-Replies</button>
      </div>
      <div id="tpl-pane"><div class="skeleton" style="height:120px;"></div></div>`;

    document.querySelectorAll(".seg-btn").forEach((b) => {
      b.addEventListener("click", () => {
        state.subTab.templates = b.getAttribute("data-sub");
        haptic("light");
        renderTemplatesTab();
      });
    });

    if (sub === "templates") await loadAndRenderTemplates();
    else await loadAndRenderAutoReplies();
  }

  async function loadAndRenderTemplates() {
    try {
      const data = await api("/templates");
      state.templates = data;
      const list = data.templates || [];
      const maxT = data.max_templates || 2;
      const pct = Math.min(100, Math.round((list.length / Math.max(1, maxT)) * 100));

      $("tpl-pane").innerHTML = `
        <div class="card">
          <div class="card-header" style="margin-bottom:6px;">
            <span style="font-weight:700;">Template Quota</span>
            <span class="mono">${list.length} / ${maxT}</span>
          </div>
          <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
          <button id="btn-new-tpl" class="btn btn-blue btn-full" style="margin-top:10px;" type="button">${I("plus", 16)} Create Template</button>
        </div>
        ${list.length === 0
          ? `<div class="empty-state">No templates saved yet. Create one to power campaigns and auto-replies.</div>`
          : list.map((t) => `
              <div class="item-card">
                <div class="item-top">
                  <span class="pill pill-blue mono">${esc(t.id)}</span>
                  <button class="btn btn-danger-outline btn-sm" data-tdel="${esc(t.id)}">${I("trash", 13)}</button>
                </div>
                <div style="font-size:13px;color:var(--text-muted);white-space:pre-wrap;word-break:break-word;">${esc(String(t.text || "").slice(0, 220))}${String(t.text || "").length > 220 ? "..." : ""}</div>
              </div>`).join("")}`;

      $("btn-new-tpl").addEventListener("click", () => {
        openSheet(
          "Create Message Template",
          `<div class="field"><label>Message Text</label><textarea id="t-text" class="textarea" placeholder="Write your promo or auto-reply message..."></textarea></div>
           <button id="t-save" class="btn btn-emerald btn-full" type="button">${I("check", 16)} Save Template</button>`,
          () => {
            $("t-save").addEventListener("click", async (e) => {
              const text = $("t-text").value.trim();
              if (!text) return toast("Template text cannot be empty.", "error");
              e.currentTarget.disabled = true;
              try {
                await api("/templates", { method: "POST", body: { text } });
                closeSheet();
                toast("Template saved!", "success");
                await loadAndRenderTemplates();
              } catch (err) {
                toast(err.message, "error");
                e.currentTarget.disabled = false;
              }
            });
          }
        );
      });

      $("tpl-pane").querySelectorAll("[data-tdel]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          try {
            await api(`/templates/${btn.dataset.tdel}`, { method: "DELETE" });
            toast("Template deleted", "success");
            await loadAndRenderTemplates();
          } catch (err) {
            toast(err.message, "error");
            btn.disabled = false;
          }
        });
      });
    } catch (err) {
      $("tpl-pane").innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
    }
  }

  async function loadAndRenderAutoReplies() {
    try {
      const [arData, tplData] = await Promise.all([
        api("/auto-replies"),
        state.templates ? Promise.resolve(state.templates) : api("/templates"),
      ]);
      state.autoReplies = arData;
      state.templates = tplData;

      if (arData.locked) {
        $("tpl-pane").innerHTML = `
          <div class="lock-card">
            <div class="lock-icon-wrap">${I("crown", 24)}</div>
            <h3 style="margin:0 0 6px;font-size:16px;">Auto-Replies Suite — Ultra Exclusive</h3>
            <p style="margin:0 0 16px;color:var(--text-muted);font-size:13px;">Unlock Mention, Welcome, Offline Auto-Replies, and Keyword Triggers with Ultra Premium.</p>
            <button id="btn-unlock-ar" class="btn btn-gold" type="button">${I("crown", 16)} Upgrade to Ultra</button>
          </div>`;
        $("btn-unlock-ar").addEventListener("click", () => { state.tab = "plans"; render(); });
        return;
      }

      const s = arData.settings || {};
      const kws = arData.keywords || [];
      const tpls = (tplData && tplData.templates) || [];
      const tplOpts = (sel) =>
        `<option value="">— Select Template —</option>` +
        tpls.map((t) => `<option value="${esc(t.id)}" ${t.id === sel ? "selected" : ""}>${esc(t.id)} — ${esc(String(t.text || "").slice(0, 30))}</option>`).join("");

      $("tpl-pane").innerHTML = `
        <div class="card">
          <div class="card-header"><h3 class="card-title">${I("bot", 17)} Smart Auto-Replies</h3></div>
          <div class="toggle-row">
            <div><div style="font-weight:600;">Mention Auto-Reply</div><div style="font-size:11.5px;color:var(--text-dim);">Reply when tagged in groups</div></div>
            <label class="switch"><input id="sw-mention" type="checkbox" ${s.mention_enabled ? "checked" : ""} /><span class="slider"></span></label>
          </div>
          <div class="field" style="margin-top:8px;"><select id="sel-mention" class="select">${tplOpts(s.mention_template_id)}</select></div>

          <div class="toggle-row">
            <div><div style="font-weight:600;">Welcome Auto-Reply</div><div style="font-size:11.5px;color:var(--text-dim);">Greet first-time DM senders</div></div>
            <label class="switch"><input id="sw-welcome" type="checkbox" ${s.welcome_enabled ? "checked" : ""} /><span class="slider"></span></label>
          </div>
          <div class="field" style="margin-top:8px;"><select id="sel-welcome" class="select">${tplOpts(s.welcome_template_id)}</select></div>

          <div class="toggle-row">
            <div><div style="font-weight:600;">Offline Auto-Reply</div><div style="font-size:11.5px;color:var(--text-dim);">Auto-respond while away</div></div>
            <label class="switch"><input id="sw-offline" type="checkbox" ${s.offline_enabled ? "checked" : ""} /><span class="slider"></span></label>
          </div>
          <div class="field" style="margin-top:8px;"><select id="sel-offline" class="select">${tplOpts(s.offline_template_id)}</select></div>

          <button id="btn-save-ar" class="btn btn-emerald btn-full" type="button">${I("check", 16)} Save Auto-Reply Settings</button>
        </div>

        <div class="card">
          <div class="card-header">
            <h3 class="card-title">${I("bolt", 17)} Keyword Rules (${kws.length})</h3>
            <button id="btn-add-kw" class="btn btn-blue btn-sm" type="button">${I("plus", 14)} Add</button>
          </div>
          ${kws.length === 0
            ? `<div class="empty-state" style="padding:14px 0;">No keyword rules yet.</div>`
            : kws.map((k) => `
                <div class="copy-box">
                  <div style="min-width:0;">
                    <div style="font-weight:600;font-size:13px;">"${esc(k.trigger_text)}"</div>
                    <div class="mono" style="color:var(--text-dim);">Reply TPL: ${esc(k.template_id)}</div>
                  </div>
                  <button class="btn btn-danger-outline btn-sm" data-kwdel="${esc(k.id)}">${I("trash", 13)}</button>
                </div>`).join("")}
        </div>`;

      $("btn-save-ar").addEventListener("click", async (e) => {
        e.currentTarget.disabled = true;
        try {
          await api("/auto-replies/settings", {
            method: "POST",
            body: {
              mention_enabled: $("sw-mention").checked,
              mention_template_id: $("sel-mention").value || null,
              welcome_enabled: $("sw-welcome").checked,
              welcome_template_id: $("sel-welcome").value || null,
              offline_enabled: $("sw-offline").checked,
              offline_template_id: $("sel-offline").value || null,
            },
          });
          toast("Auto-reply settings saved!", "success");
          e.currentTarget.disabled = false;
        } catch (err) {
          toast(err.message, "error");
          e.currentTarget.disabled = false;
        }
      });

      $("btn-add-kw").addEventListener("click", () => {
        openSheet(
          "Add Keyword Trigger",
          `<div class="field"><label>Trigger Keyword / Phrase</label><input id="kw-trig" class="input" placeholder="e.g. price" /></div>
           <div class="field"><label>Reply Text</label><textarea id="kw-reply" class="textarea" placeholder="Reply sent when keyword matches..."></textarea></div>
           <button id="kw-save" class="btn btn-emerald btn-full" type="button">${I("check", 16)} Save Rule</button>`,
          () => {
            $("kw-save").addEventListener("click", async (e) => {
              const trigger = $("kw-trig").value.trim();
              const reply = $("kw-reply").value.trim();
              if (!trigger || !reply) return toast("Fill both trigger and reply text.", "error");
              e.currentTarget.disabled = true;
              try {
                await api("/auto-replies/keywords", {
                  method: "POST",
                  body: { trigger_text: trigger, reply_text: reply },
                });
                closeSheet();
                toast("Keyword rule added!", "success");
                await loadAndRenderAutoReplies();
              } catch (err) {
                toast(err.message, "error");
                e.currentTarget.disabled = false;
              }
            });
          }
        );
      });

      $("tpl-pane").querySelectorAll("[data-kwdel]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          try {
            await api(`/auto-replies/keywords/${btn.dataset.kwdel}`, { method: "DELETE" });
            toast("Keyword rule deleted", "success");
            await loadAndRenderAutoReplies();
          } catch (err) {
            toast(err.message, "error");
            btn.disabled = false;
          }
        });
      });
    } catch (err) {
      $("tpl-pane").innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
    }
  }

  // =========================================================================
  // TAB 5: PLANS, PAYMENTS & REFERRALS
  // =========================================================================

  async function renderPlansTab() {
    const sub = state.subTab.plans;
    $("app-root").innerHTML = `
      <div class="segmented">
        <button class="seg-btn ${sub === "plans" ? "active" : ""}" data-sub="plans" type="button">Plans</button>
        <button class="seg-btn ${sub === "history" ? "active" : ""}" data-sub="history" type="button">Payments</button>
        <button class="seg-btn ${sub === "referrals" ? "active" : ""}" data-sub="referrals" type="button">Referrals</button>
      </div>
      <div id="plans-pane"><div class="skeleton" style="height:140px;"></div></div>`;

    document.querySelectorAll(".seg-btn").forEach((b) => {
      b.addEventListener("click", () => {
        state.subTab.plans = b.getAttribute("data-sub");
        haptic("light");
        renderPlansTab();
      });
    });

    if (sub === "plans") await loadAndRenderPlans();
    else if (sub === "history") await loadAndRenderHistory();
    else await loadAndRenderReferrals();
  }

  async function loadAndRenderPlans() {
    try {
      const data = await api("/plans");
      state.plans = data;
      const p = data.plans;
      const cur = data.current_plan;

      $("plans-pane").innerHTML = `
        <div class="plan-card featured">
          <div class="card-header" style="margin-bottom:4px;">
            <h3 class="card-title">${I("bolt", 17)} ${esc(p.premium.name)}</h3>
            ${cur === "premium" ? `<span class="pill pill-emerald">Current</span>` : `<span class="pill pill-blue">Popular</span>`}
          </div>
          <div class="plan-price">৳${p.premium.price_bdt} BDT <span style="font-size:13px;color:var(--text-muted);font-weight:500;">/ $${p.premium.price_usd} USD · ${p.premium.duration_days}d</span></div>
          <ul class="plan-features">
            <li>${I("check", 14)} ${p.premium.max_accounts} Telegram Account</li>
            <li>${I("check", 14)} Up to ${p.premium.max_templates} Saved Templates</li>
            <li>${I("check", 14)} Up to ${p.premium.max_campaigns} Broadcast Campaigns</li>
            <li>${I("check", 14)} Scheduled Posts Unlocked</li>
          </ul>
          <button class="btn btn-blue btn-full" data-buy="premium" type="button">${I("wallet", 16)} Upgrade to Premium</button>
        </div>

        <div class="plan-card ultra">
          <div class="card-header" style="margin-bottom:4px;">
            <h3 class="card-title">${I("crown", 17)} ${esc(p.ultra.name)}</h3>
            ${cur === "ultra" ? `<span class="pill pill-emerald">Current</span>` : `<span class="pill pill-gold">Full Suite</span>`}
          </div>
          <div class="plan-price">৳${p.ultra.price_bdt} BDT <span style="font-size:13px;color:var(--text-muted);font-weight:500;">/ $${p.ultra.price_usd} USD · ${p.ultra.duration_days}d</span></div>
          <ul class="plan-features">
            <li>${I("check", 14)} Up to ${p.ultra.max_accounts} Telegram Accounts</li>
            <li>${I("check", 14)} Up to ${p.ultra.max_templates} Templates & ${p.ultra.max_campaigns} Campaigns</li>
            <li>${I("check", 14)} Scheduled Posts + Full Auto-Replies Suite</li>
            <li>${I("check", 14)} Mention, Welcome, Offline & Keyword Bots</li>
          </ul>
          <button class="btn btn-gold btn-full" data-buy="ultra" type="button">${I("crown", 16)} Upgrade to Ultra Premium</button>
        </div>`;

      $("plans-pane").querySelectorAll("[data-buy]").forEach((btn) => {
        btn.addEventListener("click", () => openCheckoutSheet(btn.dataset.buy));
      });
    } catch (err) {
      $("plans-pane").innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
    }
  }

  function openCheckoutSheet(planKey) {
    const methods = (state.plans && state.plans.payment_methods) || [];
    const plan = state.plans.plans[planKey];
    if (!methods.length) return toast("No payment methods configured by admin yet.", "error");

    let selected = methods[0];
    const renderBody = () => {
      const amt = selected.currency === "BDT" ? `৳${plan.price_bdt} BDT` : `$${plan.price_usd} USD`;
      return `
        <div class="field">
          <label>Select Payment Method</label>
          <select id="pay-method" class="select">
            ${methods.map((m) => `<option value="${esc(m.key)}" ${m.key === selected.key ? "selected" : ""}>${esc(m.label)} (${esc(m.currency)})</option>`).join("")}
          </select>
        </div>
        <div style="font-size:12px;color:var(--text-muted);">Send <strong style="color:var(--text);">${esc(amt)}</strong> to:</div>
        <div class="copy-box">
          <span class="copy-text" id="pay-addr">${esc(selected.details)}</span>
          <button id="btn-copy-addr" class="btn btn-ghost btn-sm" type="button">${I("copy", 14)} Copy</button>
        </div>
        <div class="field" style="margin-top:12px;">
          <label>Transaction ID (TrxID / TxHash)</label>
          <input id="pay-trx" class="input mono" placeholder="Paste TrxID or TxHash here" />
        </div>
        <button id="pay-submit" class="btn btn-emerald btn-full" type="button">${I("check", 16)} Submit Payment Proof</button>`;
    };

    const bind = () => {
      $("pay-method").addEventListener("change", (ev) => {
        selected = methods.find((m) => m.key === ev.target.value) || methods[0];
        $("sheet-body").innerHTML = renderBody();
        bind();
      });
      $("btn-copy-addr").addEventListener("click", () => copyText(selected.details, selected.label));
      $("pay-submit").addEventListener("click", async (e) => {
        const trx = $("pay-trx").value.trim();
        if (trx.length < 2) return toast("Enter your Transaction ID / TxHash.", "error");
        e.currentTarget.disabled = true;
        try {
          await api("/payments/submit", {
            method: "POST",
            body: { plan_type: planKey, payment_method: selected.key, trx_id: trx },
          });
          closeSheet();
          toast("Payment submitted! Admin notified for approval.", "success");
          state.subTab.plans = "history";
          renderPlansTab();
        } catch (err) {
          toast(err.message, "error");
          e.currentTarget.disabled = false;
        }
      });
    };

    openSheet(`Checkout — ${plan.name}`, renderBody(), bind);
  }

  async function loadAndRenderHistory() {
    try {
      const data = await api("/payments/history");
      const rows = data.payments || [];
      $("plans-pane").innerHTML = rows.length === 0
        ? `<div class="empty-state">No payment requests submitted yet.</div>`
        : rows.map((r) => {
            const st = String(r.status || "pending").toLowerCase();
            const cls = st === "approved" ? "pill-emerald" : st === "rejected" ? "pill-crimson" : "pill-gold";
            return `
              <div class="item-card">
                <div class="item-top">
                  <div>
                    <div class="item-title">${esc(String(r.plan_type || "").toUpperCase())} · ${esc(r.amount_display)}</div>
                    <div class="mono" style="color:var(--text-dim);">${esc(r.id)} · ${esc(r.payment_method)}</div>
                  </div>
                  <span class="pill ${cls}">${esc(st.toUpperCase())}</span>
                </div>
                <div class="mono" style="font-size:11.5px;color:var(--text-muted);">TrxID: ${esc(r.trx_id || "—")}</div>
              </div>`;
          }).join("");
    } catch (err) {
      $("plans-pane").innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
    }
  }

  async function loadAndRenderReferrals() {
    try {
      const d = await api("/referrals");
      const req = Math.max(1, Number(d.referrals_required) || 5);
      const prog = Number(d.referral_count || 0) % req;
      const pct = Math.round((prog / req) * 100);

      $("plans-pane").innerHTML = `
        <div class="card card-hero">
          <div class="card-header">
            <h3 class="card-title">${I("users", 18)} Refer & Earn Free ${esc(String(d.reward_plan || "Premium").toUpperCase())}</h3>
            <span class="pill pill-emerald">+${d.reward_days}d / ${req} refs</span>
          </div>
          <p style="margin:0 0 12px;font-size:13px;color:var(--text-muted);">
            Invite ${req} friends who join our official channel and earn <strong>${d.reward_days} days</strong> of free ${esc(d.reward_plan)} automatically.
          </p>
          <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--text-muted);">
            <span>Current Milestone</span>
            <span class="mono" style="color:var(--text);">${prog} / ${req} verified</span>
          </div>
          <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
          <div style="font-size:11.5px;color:var(--text-dim);margin-bottom:12px;">
            Total Verified Referrals: ${d.referral_count} · Rewards Claimed: ${d.referral_rewards_claimed}
          </div>
          <div class="copy-box">
            <span class="copy-text">${esc(d.referral_link)}</span>
            <button id="btn-copy-ref" class="btn btn-ghost btn-sm" type="button">${I("copy", 14)} Copy</button>
          </div>
          <button id="btn-share-ref" class="btn btn-blue btn-full" style="margin-top:8px;" type="button">${I("share", 16)} Share Invite Link</button>
        </div>`;

      $("btn-copy-ref").addEventListener("click", () => copyText(d.referral_link, "Referral link"));
      $("btn-share-ref").addEventListener("click", () => {
        const shareUrl = `https://t.me/share/url?url=${encodeURIComponent(d.referral_link)}&text=${encodeURIComponent("Automate Telegram with NexaPilot! Join with my link and claim a free 7-day trial:")}`;
        if (tg && tg.openTelegramLink) tg.openTelegramLink(shareUrl);
        else window.open(shareUrl, "_blank");
      });
    } catch (err) {
      $("plans-pane").innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
    }
  }

  // Static Shell Bindings & Boot
  $("brand-logo").innerHTML = I("logo", 32);
  $("btn-top-refresh").innerHTML = I("refresh", 17);
  $("sheet-close").innerHTML = I("close", 17);

  $("btn-top-refresh").addEventListener("click", () => {
    haptic("light");
    loadBootstrap(true);
    toast("Synced with server", "info");
  });
  $("sheet-close").addEventListener("click", closeSheet);
  $("sheet-backdrop").addEventListener("click", (ev) => {
    if (ev.target === $("sheet-backdrop")) closeSheet();
  });

  loadBootstrap(false);
})();
