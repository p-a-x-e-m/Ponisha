/* Dashboard poller: status + events + logs + toasts + health + search. */
(function () {
  const csrf = window.__CSRF__ || "";
  const $ = (id) => document.getElementById(id);
  let sinceEvent = Number(localStorage.getItem("ps_since") || 0);
  let sinceLog = Number(localStorage.getItem("ps_log") || 0);
  let lastId = null;
  let bidCount = Number(localStorage.getItem("ps_bids") || 0);
  let startedAt = localStorage.getItem("ps_started") || "";
  let serverStartedAt = null;
  let failStreak = 0;
  let wasOffline = false;
  let lastSeq = 0;
  let badgeCount = 0;
  let logLevel = "all";
  let logFollow = true;
  const projects = new Map();
  const metaPlanPrices = {};

  function setStatusbar(msg) {
    const el = $("statusbar");
    if (el) el.textContent = msg;
  }

  function toast(msg, type = "info", ms = 3200) {
    const host = $("toasts");
    if (!host) return;
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = msg;
    host.appendChild(el);
    while (host.children.length > 5) host.removeChild(host.firstChild);
    setTimeout(() => {
      el.classList.add("out");
      setTimeout(() => el.remove(), 200);
    }, ms);
  }

  function confirmDialog(title, body) {
    return new Promise((resolve) => {
      const backdrop = $("modal");
      const ok = $("modal-ok");
      const cancel = $("modal-cancel");
      if (!backdrop || !ok || !cancel) {
        resolve(window.confirm(body));
        return;
      }
      $("modal-title").textContent = title;
      $("modal-body").textContent = body;
      backdrop.classList.remove("hidden");
      const prevFocus = document.activeElement;
      ok.focus();
      function done(v) {
        backdrop.classList.add("hidden");
        ok.removeEventListener("click", onOk);
        cancel.removeEventListener("click", onCancel);
        backdrop.removeEventListener("click", onBg);
        document.removeEventListener("keydown", onKey);
        if (prevFocus && prevFocus.focus) prevFocus.focus();
        resolve(v);
      }
      function onOk() { done(true); }
      function onCancel() { done(false); }
      function onBg(e) { if (e.target === backdrop) done(false); }
      function onKey(e) { if (e.key === "Escape") done(false); }
      ok.addEventListener("click", onOk);
      cancel.addEventListener("click", onCancel);
      backdrop.addEventListener("click", onBg);
      document.addEventListener("keydown", onKey);
    });
  }

  function markPoll(ok) {
    failStreak = ok ? 0 : failStreak + 1;
    const offline = failStreak >= 3;
    document.body.classList.toggle("offline", offline);
    const connText = $("conn-text");
    if (connText) {
      connText.textContent = offline ? "offline" : `online · seq ${lastSeq}`;
    }
    if (offline && !wasOffline) {
      wasOffline = true;
      toast("اتصال به سرور قطع شد", "err");
      setStatusbar("offline — سرور در دسترس نیست");
    } else if (!offline && wasOffline) {
      wasOffline = false;
      toast("اتصال برقرار شد", "ok");
    }
  }

  async function api(path, opts) {
    opts = opts || {};
    const headers = opts.headers || {};
    if (opts.method && opts.method !== "GET") {
      headers["X-CSRFToken"] = csrf;
      headers["Content-Type"] = "application/json";
    }
    const res = await fetch(path, { ...opts, headers, credentials: "same-origin" });
    const data = await res.json().catch(() => ({}));
    return { res, data };
  }

  async function pollStatus() {
    try {
      const { data } = await api("/api/status");
      markPoll(true);
      lastSeq = data.event_seq || lastSeq;
      applyStatus(data);
    } catch (e) {
      markPoll(false);
    }
  }

  function applyStatus(s) {
    document.body.classList.remove("booting");
    const word = $("state-word");
    const sub = $("state-sub");
    const pulse = $("pulse");
    const pill = $("status-pill");
    const start = $("btn-start");
    const stop = $("btn-stop");
    const dry = $("chk-dry");
    const quota = $("g-quota");
    const matches = $("g-matches");

    if (pill && !document.body.classList.contains("offline")) {
      pill.textContent = s.running ? "RUNNING" : "STOPPED";
      pill.classList.toggle("run", !!s.running);
    }
    if (word) word.textContent = s.running ? "Running" : "Idle";
    if (sub) sub.textContent = s.running
      ? "monitoring ponisha.ir"
      : "برای شروع مانیتور Start را بزنید";
    if (pulse) pulse.classList.toggle("on", !!s.running);
    if (start) start.disabled = s.running || (s.busy && s.busy.starting);
    if (stop) stop.disabled = !s.running || (s.busy && s.busy.stopping);
    if (dry) dry.checked = !!s.dry_run;
    if (quota) {
      quota.textContent = s.quota == null ? "—" : String(s.quota);
      quota.parentElement && quota.parentElement.classList.toggle(
        "warn", s.quota != null && s.quota <= 2
      );
    }
    if (matches) matches.textContent = String(s.matches || 0);

    if (typeof s.bids === "number" && s.bids != null) {
      bidCount = s.bids;
      localStorage.setItem("ps_bids", String(bidCount));
    }
    const b0 = $("g-bids");
    if (b0) b0.textContent = String(bidCount);

    const usd = $("g-usd");
    if (usd) {
      usd.textContent = s.usd == null ? "—" : Number(s.usd).toLocaleString("en-US");
    }
    const usdFoot = $("usd-rate");
    if (usdFoot) {
      usdFoot.textContent = s.usd == null
        ? ""
        : `USD ${Number(s.usd).toLocaleString("en-US")} تومان`;
    }

    serverStartedAt = s.started_at || null;
    if (s.running && !startedAt && !serverStartedAt) {
      startedAt = String(Date.now());
      localStorage.setItem("ps_started", startedAt);
    }

    const bidBtn = $("btn-bid");
    const hint = $("bid-hint");
    if (bidBtn) {
      const hasSel = !!lastId && projects.has(lastId);
      bidBtn.disabled = s.dry_run || !hasSel || (s.busy && s.busy.bid);
    }
    if (hint) {
      // `has_cookies` only means something is stored; ponisha may have revoked
      // it. Only the verified verdict may say the bot is ready to bid.
      const ready = s.session_valid === true;
      if (s.dry_run) {
        hint.textContent = "DRY_RUN روشن است — بید واقعی نمی‌زند";
      } else if (s.session_valid === null || s.session_valid === undefined) {
        hint.textContent = "در حال بررسی نشست…";
      } else if (ready) {
        hint.textContent = "آماده بید";
      } else {
        hint.textContent = s.has_cookies
          ? "نشست ذخیره‌شده معتبر نیست — دوباره Login"
          : "ابتدا Login";
      }
    }

    // NOTE: the banner is opened by the Login button (OTP form) — status
    // polling must NOT auto-hide it, only auto-show it when a login runs.
    const banner = $("banner");
    if (banner && s.busy && s.busy.login && !s.has_cookies) {
      banner.classList.remove("hidden");
    }
    updateCount();
  }

  function updateCount() {
    const el = $("project-count");
    if (!el) return;
    let n = 0;
    projects.forEach(() => { n += 1; });
    el.textContent = String(n);
  }

  async function pollEvents() {
    try {
      const { data } = await api(`/api/events?since=${sinceEvent}`);
      markPoll(true);
      if (typeof data.seq === "number" && data.seq < sinceEvent) {
        sinceEvent = 0;
        localStorage.setItem("ps_since", "0");
      }
      for (const ev of data.events || []) {
        sinceEvent = ev.id;
        localStorage.setItem("ps_since", String(sinceEvent));
        handleEvent(ev);
      }
    } catch (e) {
      markPoll(false);
    }
  }

  function bumpBadge() {
    badgeCount += 1;
    const b = $("nav-badge");
    if (b) {
      b.textContent = String(badgeCount);
      b.classList.remove("hidden");
    }
  }

  function clearBadge() {
    badgeCount = 0;
    const b = $("nav-badge");
    if (b) {
      b.textContent = "0";
      b.classList.add("hidden");
    }
  }

  function handleEvent(ev) {
    const t = ev.type;
    const p = ev.payload || {};
    if (t === "new_project" && p.project) {
      const isNew = !projects.has(p.project.id);
      if (p.price != null) {
        lastPlanPrice = p.price;
        metaPlanPrices[p.project.id] = p.price;
      }
      addProject(p.project, p);
      if (isNew) {
        bumpBadge();
        toast(`پروژه جدید: ${(p.project.title || "").slice(0, 48)}`, "info");
        setStatusbar(`پروژه جدید: ${(p.project.title || "").slice(0, 60)}`);
      }
      pollStatus();
    } else if (t === "bid") {
      if (p.success) {
        bidCount += 1;
        localStorage.setItem("ps_bids", String(bidCount));
        toast(`بید ثبت شد: ${(p.title || "").slice(0, 40)}`, "ok");
        setStatusbar(`✅ بید: ${p.title || ""}`);
      } else {
        toast(`بید ناموفق: ${(p.title || "").slice(0, 40)}`, "err");
        setStatusbar(`❌ بید ناموفق: ${p.title || ""}`);
      }
      const b = $("g-bids");
      if (b) b.textContent = String(bidCount);
    } else if (t === "status") {
      if (p.running) startedAt = String(Date.now());
      setStatusbar(p.running ? "در حال اجرا…" : "متوقف");
      toast(p.running ? "مانیتور اجرا شد" : "مانیتور متوقف شد", p.running ? "ok" : "warn");
      pollStatus();
    } else if (t === "login_done") {
      setStatusbar("لاگین انجام شد");
      toast("لاگین انجام شد", "ok");
      pollStatus();
    } else if (t === "login_error") {
      setStatusbar("خطای لاگین: " + (p.error || ""));
      toast("خطای لاگین", "err");
    } else if (t === "quota_low") {
      setStatusbar("⚠ سهمیه پیشنهاد کم است — ربات متوقف شد");
      toast("سهمیه پیشنهاد کم است — ربات متوقف شد", "err", 5000);
    } else if (t === "scan_done") {
      setStatusbar(`اسکن تمام — ${p.count || 0} متچ`);
      pollStatus();
    }
  }

  function fmtBudget(lo, hi) {
    if (lo == null && hi == null) return "نامشخص";
    const f = (n) => n == null ? "?" : Number(n).toLocaleString("en-US");
    return `${f(lo)}–${f(hi)}`;
  }

  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function addProject(pr, meta) {
    if (!pr || !pr.id) return;
    if (projects.has(pr.id)) return;
    projects.set(pr.id, pr);
    const box = $("projects");
    if (!box) return;
    const empty = $("projects-empty") || box.querySelector(".empty-state, .empty");
    if (empty) empty.classList.add("hidden");
    const card = document.createElement("div");
    card.className = "card fresh";
    card.dataset.id = pr.id;
    card.tabIndex = 0;
    card.dataset.title = (pr.title || "").toLowerCase();
    card.dataset.skills = (pr.skills || []).join(" ").toLowerCase();
    const budget = meta && meta.budget
      ? meta.budget
      : fmtBudget(pr.budget_min, pr.budget_max);
    const badges = (pr.badges || []).map((b) => {
      const cls = String(b).includes("فوری") ? "chip urgent" : "chip";
      return `<span class="${cls}">${esc(b)}</span>`;
    }).join("");
    card.innerHTML = `
      ${badges ? `<div class="proj-badges">${badges}</div>` : ""}
      <div class="t">${esc(pr.title || "").slice(0, 90)}</div>
      <div class="m">
        <span><span class="lbl">BUD</span> <span class="num">${esc(budget)}</span></span>
        <span><span class="lbl">BIDS</span> <span class="num">${pr.bid_count ?? "—"}</span></span>
        <span class="skills-inline">${(pr.skills || []).slice(0, 4)
          .map((s) => `<span class="skill-tag">${esc(s)}</span>`).join("")}</span>
      </div>
    `;
    card.addEventListener("click", () => selectProject(pr.id));
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        selectProject(pr.id);
      }
    });
    box.insertBefore(card, box.firstChild);
    setTimeout(() => card.classList.remove("fresh"), 300);
    updateCount();
    filterProjects();
  }

  function filterProjects() {
    const q = (($("project-search") || {}).value || "").trim().toLowerCase();
    let visible = 0;
    document.querySelectorAll("#projects > .card").forEach((c) => {
      const hit = !q
        || (c.dataset.title || "").includes(q)
        || (c.dataset.skills || "").includes(q);
      c.classList.toggle("hidden", !hit);
      if (hit) visible += 1;
    });
    const count = $("project-count");
    if (count) count.textContent = String(projects.size);
  }

  function selectProject(id) {
    lastId = id;
    clearBadge();
    document.querySelectorAll("#projects > .card").forEach((c) => {
      c.classList.toggle("sel", c.dataset.id === id);
    });
    const pr = projects.get(id);
    if (!pr) return;
    // prefer plan price from last new_project meta if available
    if (metaPlanPrices[id] != null) lastPlanPrice = metaPlanPrices[id];
    const d = $("detail");
    if (d) d.classList.remove("hidden");
    setText("d-title", pr.title || "");
    setText("d-budget", "بودجه: " + fmtBudget(pr.budget_min, pr.budget_max));
    setText("d-bids", "پیشنهادها: " + (pr.bid_count ?? "?"));
    const badges = $("d-badges");
    if (badges) {
      badges.innerHTML = (pr.badges || [])
        .map((b) => {
          const cls = String(b).includes("فوری") ? "chip urgent" : "chip";
          return `<span class="${cls}">${esc(b)}</span>`;
        }).join("");
    }
    const extra = $("d-extra");
    const emp = $("d-employer");
    const dl = $("d-deadline");
    const price = $("d-price");
    const parts = [];
    if (emp) {
      emp.textContent = pr.employer ? `کارفرما: ${pr.employer}` : "";
      if (pr.employer) parts.push(emp);
    }
    if (dl) {
      dl.textContent = pr.deadline ? `مهلت: ${pr.deadline}` : "";
      if (pr.deadline) parts.push(dl);
    }
    if (price) {
      const meta = lastPlanPrice;
      price.textContent = meta != null ? `قیمت پیشنهادی: ${Number(meta).toLocaleString("en-US")}` : "";
      if (meta != null) parts.push(price);
    }
    if (extra) extra.classList.toggle("hidden", parts.length === 0);
    const skills = $("d-skills");
    if (skills) {
      skills.innerHTML = (pr.skills || [])
        .map((s) => `<span class="skill-tag">${esc(s)}</span>`).join("");
    }
    setText("d-desc", (pr.description || "").slice(0, 1200));
    const a = $("d-url");
    if (a && pr.url) {
      a.href = pr.url;
      a.onclick = (e) => {
        e.preventDefault();
        api("/api/open_project", {
          method: "POST",
          body: JSON.stringify({ url: pr.url }),
        });
      };
    }
    pollStatus();
  }

  let lastPlanPrice = null;

  function setText(id, v) {
    const el = $(id);
    if (el) el.textContent = v;
  }

  function logLineClass(line) {
    if (/ - ERROR - |ERROR:|Traceback|Exception/i.test(line)) return "err";
    if (/ - WARNING - |WARNING:/i.test(line)) return "warn";
    return "info";
  }

  function appendLogChunk(text) {
    const el = $("log");
    if (!el) return;
    // remove idle placeholder (dim / Waiting)
    const ph = el.querySelector(".log-line.dim, .log-line.info");
    if (ph && el.childNodes.length <= 1 &&
        (ph.classList.contains("dim") || (ph.textContent || "").indexOf("Waiting") === 0 ||
         (ph.textContent || "").indexOf("no events") >= 0)) {
      el.textContent = "";
    }
    el.classList.remove("log-idle");
    const frag = document.createDocumentFragment();
    const lines = text.split(/\r?\n/);
    // if last chunk doesn't end with newline, merge with previous last line — simple append of lines
    for (const line of lines) {
      if (line === "" && lines[lines.length - 1] === "") continue;
      const span = document.createElement("span");
      span.className = "log-line " + logLineClass(line);
      span.dataset.level = span.className.includes("err") ? "error"
        : span.className.includes("warn") ? "warn" : "info";
      span.textContent = line + "\n";
      frag.appendChild(span);
    }
    el.appendChild(frag);
    // trim by line count
    while (el.childElementCount > 4000) el.removeChild(el.firstChild);
    const count = $("log-count");
    if (count) count.textContent = String(el.childElementCount);
    applyLogFilter();
    if (logFollow) el.scrollTop = el.scrollHeight;
  }

  function applyLogFilter() {
    document.querySelectorAll("#log .log-line").forEach((n) => {
      if (logLevel === "all") {
        n.style.display = "";
        return;
      }
      const lv = n.dataset.level || "info";
      n.style.display = lv === logLevel ? "" : "none";
    });
  }

  async function pollLogs() {
    try {
      const { data } = await api(`/api/logs?since=${sinceLog}`);
      markPoll(true);
      if (data.text) {
        appendLogChunk(data.text);
        sinceLog = data.since;
        localStorage.setItem("ps_log", String(sinceLog));
      }
    } catch (e) {
      markPoll(false);
    }
  }

  function tickUptime() {
    const el = $("g-uptime");
    if (!el) return;
    const base = serverStartedAt || (startedAt ? Number(startedAt) : null);
    if (!base) return;
    const sec = Math.max(0, Math.floor((Date.now() - base) / 1000));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    el.textContent = `${h}h ${m}m ${s}s`;
  }

  function bindControls() {
    const start = $("btn-start");
    const stop = $("btn-stop");
    const login = $("btn-login");
    const dry = $("chk-dry");
    const bid = $("btn-bid");
    const clear = $("btn-clear-log");
    const demo = $("btn-demo");
    const loginDone = $("btn-login-done");
    const emptyStart = $("btn-empty-start");
    const search = $("project-search");
    const copyUrl = $("btn-copy-url");
    const projectsCard = $("projects-card");
    const logEl = $("log");

    if (start) start.onclick = async () => {
      setStatusbar("در حال استارت…");
      toast("در حال استارت…", "info");
      await api("/api/start", { method: "POST", body: "{}" });
      setTimeout(pollStatus, 400);
    };
    if (emptyStart) emptyStart.onclick = () => start && start.click();
    if (stop) stop.onclick = async () => {
      setStatusbar("در حال توقف…");
      await api("/api/stop", { method: "POST", body: "{}" });
      setTimeout(pollStatus, 400);
    };
    // Login button handler lives in the OTP section below (opens the form).
    if (dry) dry.onchange = async () => {
      await api("/api/dry_run", {
        method: "POST",
        body: JSON.stringify({ dry_run: dry.checked }),
      });
      toast(dry.checked ? "DRY_RUN روشن" : "DRY_RUN خاموش", dry.checked ? "warn" : "ok");
      pollStatus();
    };
    if (bid) bid.onclick = async () => {
      if (!lastId) return;
      const ok = await confirmDialog("ثبت بید", "آیا بید واقعی روی این پروژه ثبت شود؟");
      if (!ok) return;
      setStatusbar("ارسال بید…");
      toast("ارسال بید…", "info");
      await api("/api/bid", {
        method: "POST",
        body: JSON.stringify({ project_id: lastId }),
      });
      setTimeout(pollStatus, 400);
    };
    if (clear) clear.onclick = async () => {
      const ok = await confirmDialog("پاک کردن لاگ", "نمایش لاگ از ابتدا پاک شود؟");
      if (!ok) return;
      const el = $("log");
      if (el) {
        el.textContent = "";
        const ph = document.createElement("span");
        ph.className = "log-line dim";
        ph.textContent = "— no events yet —";
        el.appendChild(ph);
        el.classList.add("log-idle");
      }
      sinceLog = 0;
      localStorage.setItem("ps_log", "0");
      const c = $("log-count");
      if (c) c.textContent = "0";
      pollLogs();
    };
    if (demo) demo.onclick = async () => {
      setStatusbar("Demo…");
      await api("/api/demo", { method: "POST", body: "{}" });
      toast("Demo در حال اجراست — خروجی در لاگ", "info");
    };
    if (loginDone) loginDone.onclick = async () => {
      await api("/api/login_done", { method: "POST", body: "{}" });
      const banner = $("banner");
      if (banner) banner.classList.add("hidden");
    };

    // ── OTP login ──
    const otpMobile = $("otp-mobile");
    const otpCode = $("otp-code");
    const otpCodeRow = $("otp-code-row");
    const btnReqOtp = $("btn-request-otp");
    const btnVerifyOtp = $("btn-verify-otp");
    const otpMsg = $("otp-msg");
    const otpCountdown = $("otp-countdown");
    let otpTicker = null;

    function otpSay(msg, type) {
      if (otpMsg) otpMsg.textContent = msg || "";
      if (msg && type) toast(msg, type);
    }

    function startOtpCountdown(expiresAt) {
      if (otpTicker) clearInterval(otpTicker);
      if (!expiresAt || !otpCountdown) return;
      const tick = () => {
        const left = expiresAt - Math.floor(Date.now() / 1000);
        if (left <= 0) {
          otpCountdown.textContent = "کد منقضی شد — دوباره ارسال کنید";
          clearInterval(otpTicker);
          otpTicker = null;
          return;
        }
        const m = Math.floor(left / 60), s = left % 60;
        otpCountdown.textContent = `اعتبار: ${m}:${String(s).padStart(2, "0")}`;
      };
      tick();
      otpTicker = setInterval(tick, 1000);
    }

    if (btnReqOtp) btnReqOtp.onclick = async () => {
      const mobile = (otpMobile && otpMobile.value || "").trim();
      if (!mobile) { otpSay("شماره موبایل را وارد کنید", "warn"); return; }
      btnReqOtp.disabled = true;
      otpSay("در حال ارسال کد…", "info");
      await api("/api/auth/request-otp", {
        method: "POST",
        body: JSON.stringify({ mobile }),
      });
      // poll for the worker result
      for (let i = 0; i < 20; i++) {
        await new Promise((r) => setTimeout(r, 700));
        const { data } = await api("/api/auth/otp-status");
        const res = data.request;
        if (res) {
          btnReqOtp.disabled = false;
          if (res.ok) {
            otpSay("کد پیامک شد — کد را وارد کنید", "ok");
            if (otpCodeRow) otpCodeRow.classList.remove("hidden");
            startOtpCountdown(res.expires_at);
            if (otpCode) otpCode.focus();
          } else {
            otpSay(res.error || "ارسال کد ناموفق بود", "err");
          }
          break;
        }
      }
      btnReqOtp.disabled = false;
    };

    if (btnVerifyOtp) btnVerifyOtp.onclick = async () => {
      const mobile = (otpMobile && otpMobile.value || "").trim();
      const code = (otpCode && otpCode.value || "").trim();
      if (!mobile || !code) { otpSay("موبایل و کد را وارد کنید", "warn"); return; }
      btnVerifyOtp.disabled = true;
      otpSay("در حال بررسی کد…", "info");
      await api("/api/auth/verify-otp", {
        method: "POST",
        body: JSON.stringify({ mobile, otp: code }),
      });
      for (let i = 0; i < 20; i++) {
        await new Promise((r) => setTimeout(r, 700));
        const { data } = await api("/api/auth/otp-status");
        const res = data.verify;
        if (res) {
          btnVerifyOtp.disabled = false;
          if (res.ok) {
            otpSay("ورود موفق — نشست ذخیره شد", "ok");
            if (otpTicker) clearInterval(otpTicker);
            if (otpCodeRow) otpCodeRow.classList.add("hidden");
            pollStatus();
          } else {
            otpSay(res.error || "کد نادرست است", "err");
          }
          break;
        }
      }
      btnVerifyOtp.disabled = false;
    };

    // Login button opens the OTP banner
    if (login) login.onclick = () => {
      const banner = $("banner");
      if (banner) banner.classList.remove("hidden");
      if (otpMobile) otpMobile.focus();
    };
    if (search) search.addEventListener("input", filterProjects);
    if (copyUrl) {
      copyUrl.onclick = async () => {
        const a = $("d-url");
        const url = (a && a.href) || "";
        if (!url || url === window.location.href) return;
        try {
          await navigator.clipboard.writeText(url);
          toast("لینک کپی شد", "ok");
        } catch (e) {
          toast("کپی ناموفق", "err");
        }
      };
    }
    if (projectsCard) {
      projectsCard.addEventListener("click", () => {
        if (!projectsCard.contains(document.activeElement)) clearBadge();
      });
    }
    // log filters
    const filter = $("log-filter");
    if (filter) {
      filter.addEventListener("click", (e) => {
        const btn = e.target.closest(".log-filter");
        if (!btn) return;
        logLevel = btn.dataset.level || "all";
        filter.querySelectorAll(".log-filter").forEach((b) => {
          b.classList.toggle("on", b === btn);
        });
        applyLogFilter();
      });
    }
    if (logEl) {
      logEl.addEventListener("scroll", () => {
        const nearBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 40;
        logFollow = nearBottom;
      });
    }

    // password reveal (settings page)
    document.querySelectorAll("[data-reveal]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const input = $(btn.getAttribute("data-reveal"));
        if (!input) return;
        input.type = input.type === "password" ? "text" : "password";
      });
    });

    // keyboard shortcuts
    document.addEventListener("keydown", (e) => {
      const tag = (e.target && e.target.tagName) || "";
      const editable = e.target && (e.target.isContentEditable
        || tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT");
      if (editable || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "s" && start && !start.disabled) start.click();
      else if (e.key === "S" && stop && !stop.disabled) stop.click();
      else if (e.key === "l" && login) login.click();
      else if (e.key === "/" && search) {
        e.preventDefault();
        search.focus();
      } else if (e.key === "Escape" && lastId) {
        lastId = null;
        document.querySelectorAll("#projects > .card").forEach((c) => c.classList.remove("sel"));
        const d = $("detail");
        if (d) d.classList.add("hidden");
        pollStatus();
      }
    });
  }

  bindControls();
  pollStatus();
  pollLogs();
  setInterval(pollStatus, 2000);
  setInterval(pollEvents, 1500);
  setInterval(pollLogs, 2000);
  setInterval(tickUptime, 1000);
})();
