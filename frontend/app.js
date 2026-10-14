/* Frontend for the internal RAG chatbot.
 *
 * - Three views: login, chat, admin. We swap visibility, no client routing.
 * - Login posts JSON to /api/login; the cookie is httpOnly and set by the
 *   server, so the JS never sees or stores it.
 * - Chat uses fetch() to POST /api/chat and parses the SSE response stream
 *   manually. (EventSource doesn't support POST; we need POST for the body.)
 * - Admin tab is shown only if /api/me reports is_admin=true. Server still
 *   gates every /api/admin/* call regardless of the UI state.
 *
 * The CSP forbids inline scripts and 'unsafe-eval'. All DOM updates use
 * textContent; no innerHTML, no string-to-HTML, anywhere.
 */
(function () {
  "use strict";

  // ----- DOM refs ---------------------------------------------------------
  const $ = (id) => document.getElementById(id);
  const viewLogin = $("view-login");
  const viewChat  = $("view-chat");
  const viewAdmin = $("view-admin");

  const loginForm = $("login-form");
  const loginErr  = $("login-error");
  const loginBtn  = $("login-btn");
  const emailEl   = $("email");
  const passwordEl= $("password");

  const totpForm       = $("totp-form");
  const totpCode       = $("totp-code");
  const totpSubmitBtn  = $("totp-submit-btn");
  const totpCancelBtn  = $("totp-cancel-btn");
  const totpErr        = $("totp-error");

  const accountBtn     = $("account-btn");
  const accountModal   = $("account-modal");
  const accountClose   = $("account-close-btn");
  const totpStatus     = $("totp-status");
  const totpActions    = $("totp-actions");
  const totpSetup      = $("totp-setup");
  const totpQr         = $("totp-qr");
  const totpSecret     = $("totp-secret");
  const totpVerifyForm = $("totp-verify-form");
  const totpVerifyCode = $("totp-verify-code");
  const totpVerifyBtn  = $("totp-verify-btn");
  const totpVerifyErr  = $("totp-verify-error");
  const totpDisable    = $("totp-disable");
  const totpDisableForm= $("totp-disable-form");
  const totpDisableCode= $("totp-disable-code");
  const totpDisableBtn = $("totp-disable-btn");
  const totpDisableErr = $("totp-disable-error");

  // Holds the short-lived challenge token returned by /api/login when the
  // user has 2FA enabled. Null outside the password→TOTP transition.
  let pendingChallengeToken = null;

  const whoEl     = $("who");
  const adminBtn  = $("admin-btn");
  const logoutBtn = $("logout-btn");
  const messages  = $("messages");
  const chatForm  = $("chat-form");
  const chatInput = $("chat-input");
  const sendBtn   = $("send-btn");
  const chatErr   = $("chat-error");

  const adminWhoEl     = $("admin-who");
  const backToChatBtn  = $("back-to-chat-btn");
  const adminLogoutBtn = $("admin-logout-btn");
  const tabDocs   = $("tab-docs");
  const tabUsers  = $("tab-users");
  const panelDocs  = $("panel-docs");
  const panelUsers = $("panel-users");

  const uploadForm     = $("upload-form");
  const uploadFile     = $("upload-file");
  const uploadFileName = $("upload-file-name");
  const uploadTagPickerEl = $("upload-tag-picker");
  const uploadBtn      = $("upload-btn");
  const uploadStatus   = $("upload-status");
  const uploadErr      = $("upload-error");
  const docsList       = $("docs-list");

  const createUserForm     = $("create-user-form");
  const newUserEmail       = $("new-user-email");
  const newUserPassword    = $("new-user-password");
  const newUserAdmin       = $("new-user-admin");
  const newUserTagPickerEl = $("new-user-tag-picker");
  const createUserBtn      = $("create-user-btn");
  const createUserStatus   = $("create-user-status");
  const createUserErr      = $("create-user-error");
  const usersList          = $("users-list");

  // Department admin section + tag-edit modal.
  const deptList         = $("dept-list");
  const deptCreateForm   = $("dept-create-form");
  const deptCreateName   = $("dept-create-name");
  const deptCreateLabel  = $("dept-create-label");
  const deptCreateBtn    = $("dept-create-btn");
  const deptError        = $("dept-error");

  const tagEditModal   = $("tag-edit-modal");
  const tagEditTitle   = $("tag-edit-title");
  const tagEditSubject = $("tag-edit-subject");
  const tagEditPicker  = $("tag-edit-picker");
  const tagEditCancel  = $("tag-edit-cancel");
  const tagEditSave    = $("tag-edit-save");
  const tagEditClose   = $("tag-edit-close");
  const tagEditErr     = $("tag-edit-error");

  const sidebar         = $("chat-sidebar");
  const sidebarToggle   = $("sidebar-toggle");
  const sidebarBackdrop = $("sidebar-backdrop");
  const newConvBtn      = $("new-conv-btn");
  const convList        = $("conv-list");

  const docViewer       = $("doc-viewer");
  const docViewerTitle  = $("doc-viewer-title");
  const docViewerMeta   = $("doc-viewer-meta");
  const docViewerBody   = $("doc-viewer-body");
  const docViewerClose  = $("doc-viewer-close");

  const tabAnalytics    = $("tab-analytics");
  const panelAnalytics  = $("panel-analytics");
  const aTotals         = $("analytics-totals");
  const aRecent         = $("analytics-recent");
  const aFeedback       = $("analytics-feedback");
  const aQuality        = $("analytics-quality");
  const aCited          = $("analytics-cited");
  const aDaily          = $("analytics-daily");
  const aLogins         = $("analytics-logins");

  const tabBranding     = $("tab-branding");
  const panelBranding   = $("panel-branding");
  const brandingForm    = $("branding-form");
  const brandAppName    = $("brand-app-name");
  const brandSub        = $("brand-sub");
  const brandColor      = $("brand-color");
  const brandTagline    = $("brand-tagline");
  const brandSaveBtn    = $("brand-save-btn");
  const brandStatus     = $("brand-status");
  const brandErr        = $("brand-error");
  const brandLogoInput  = $("brand-logo-file");
  const brandLogoName   = $("brand-logo-name");
  const brandLogoPrev   = $("brand-logo-preview");
  const brandLogoClear  = $("brand-logo-clear");
  // Operator legal identity fields (filled into impressum/datenschutz).
  const brandLegalName  = $("brand-legal-name");
  const brandAddress    = $("brand-address");
  const brandRep        = $("brand-representative");
  const brandContact    = $("brand-contact-email");
  const brandPrivacy    = $("brand-privacy-email");
  const brandRegister   = $("brand-register-info");
  const brandVat        = $("brand-vat-id");
  const brandAuthority  = $("brand-supervisory-authority");
  // Holds the data-URI of a freshly-picked logo (or "" to clear, or null
  // to leave unchanged on save).
  let pendingLogoDataUri = null;

  // Mutable state — refreshed from /api/me on bootstrap and after login.
  let me = null;

  // Server is the source of truth for conversation history. The client
  // tracks only which conversation it's currently viewing; null means
  // "fresh chat — server will create a conversation on first send and
  // tell us its id via the SSE `meta` event".
  let currentConversationId = null;
  let conversations = [];
  let openPopover = null;

  // ----- View management --------------------------------------------------
  function show(view) {
    viewLogin.hidden = view !== "login";
    viewChat.hidden  = view !== "chat";
    viewAdmin.hidden = view !== "admin";
  }

  // ----- Branding (public — needed for login page too) ------------------
  // Cached so a save in the admin panel can refresh just the parts that
  // depend on it (CSS var, document.title, every wordmark).
  let branding = null;

  async function applyBranding() {
    try {
      const r = await fetch("/api/branding");
      if (!r.ok) return;
      branding = await r.json();
    } catch (_) { return; /* keep HTML defaults */ }

    const sub = branding.sub_brand || "";
    document.title = sub ? `${branding.app_name} ${sub}` : branding.app_name;
    if (typeof branding.accent_color === "string" &&
        /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(branding.accent_color)) {
      document.documentElement.style.setProperty("--accent", branding.accent_color);
    }

    // Logo + favicon from the data-URI the server resolved (falls back to
    // the neutral baked-in mark server-side, so this is always populated).
    if (typeof branding.logo_data_uri === "string" && branding.logo_data_uri) {
      document.querySelectorAll("img.logo").forEach((img) => {
        img.src = branding.logo_data_uri;
      });
      const icon = document.querySelector('link[rel="icon"]');
      if (icon) icon.href = branding.logo_data_uri;
    }

    // Update every wordmark unless it's marked static (e.g. the admin
    // header, whose sub-label is a UI noun like "verwaltung", not a brand).
    document.querySelectorAll(".wordmark").forEach((wm) => {
      const inner = wm.querySelector(":scope > span");
      const accentSpan = wm.querySelector(".accent");
      if (!inner || !accentSpan) return;
      // The accent span carries either the (static) UI noun or the dynamic
      // sub-brand. A trailing space after app_name is needed whenever the
      // accent span will render any text.
      const accentWillHaveText = wm.dataset.staticSub
        ? accentSpan.textContent.trim() !== ""
        : sub !== "";
      // Replace the leading text node (app_name) with the configured value.
      for (const node of inner.childNodes) {
        if (node.nodeType === Node.TEXT_NODE) {
          node.textContent = accentWillHaveText
            ? `${branding.app_name} `
            : branding.app_name;
          break;
        }
      }
      if (!wm.dataset.staticSub) {
        accentSpan.textContent = sub;
      }
    });

    // Fill any [data-brand] placeholder spans (legal pages reuse this).
    applyBrandPlaceholders(branding);
  }

  // Shared helper: legal pages (impressum/datenschutz) and any page that
  // embeds <span data-brand="contact_email">…</span> get the value filled
  // here. textContent only — never innerHTML — so no markup injection.
  function applyBrandPlaceholders(b) {
    document.querySelectorAll("[data-brand]").forEach((el) => {
      const key = el.dataset.brand;
      const val = b && typeof b[key] === "string" ? b[key].trim() : "";
      if (val) {
        el.textContent = val;
        if (el.dataset.brandHref) el.setAttribute("href", "mailto:" + val);
        el.removeAttribute("data-brand-empty");
      } else if (el.dataset.brandFallback !== undefined) {
        el.textContent = el.dataset.brandFallback;
      }
    });
  }

  // ----- Bootstrap: am I logged in? --------------------------------------
  async function bootstrap() {
    // Branding is the same for every visitor, fetch in parallel with /api/me.
    const brandingPromise = applyBranding();
    try {
      const r = await fetch("/api/me", { credentials: "same-origin" });
      if (r.ok) {
        me = await r.json();
        applyMe();
        show("chat");
        await refreshConversations();
        chatInput.focus();
        return;
      }
    } catch (_) { /* network error → show login */ }
    await brandingPromise; // ensure login screen renders branded
    show("login");
    emailEl.focus();
  }

  function applyMe() {
    whoEl.textContent = me?.email || "";
    adminWhoEl.textContent = me?.email || "";
    // Show the admin button only for admins. Server still enforces it.
    adminBtn.hidden = !(me && me.is_admin);
  }

  // ----- Login -----------------------------------------------------------
  loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    loginErr.hidden = true;
    loginBtn.disabled = true;
    try {
      const r = await fetch("/api/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: emailEl.value.trim(),
          password: passwordEl.value,
        }),
      });
      if (r.status === 202) {
        // 2FA required — keep the password card hidden until TOTP verifies.
        // Check 202 BEFORE r.ok: r.ok is true for any 2xx including 202, so
        // the password-only branch would otherwise swallow the TOTP step.
        const data = await r.json().catch(() => ({}));
        pendingChallengeToken = data.challenge_token || null;
        passwordEl.value = "";
        loginForm.hidden = true;
        totpForm.hidden = false;
        totpErr.hidden = true;
        totpCode.value = "";
        totpCode.focus();
      } else if (r.ok) {
        passwordEl.value = "";
        await postLoginBootstrap();
      } else if (r.status === 429) {
        loginErr.textContent = "Zu viele Versuche. Bitte in einer Minute erneut versuchen.";
        loginErr.hidden = false;
      } else {
        loginErr.textContent = "E-Mail oder Passwort ist ungültig.";
        loginErr.hidden = false;
      }
    } catch (_) {
      loginErr.textContent = "Netzwerkfehler. Bitte erneut versuchen.";
      loginErr.hidden = false;
    } finally {
      loginBtn.disabled = false;
    }
  });

  // ----- Post-login bootstrap (shared by password-only + TOTP paths) ----
  async function postLoginBootstrap() {
    try {
      const m = await fetch("/api/me", { credentials: "same-origin" });
      if (m.ok) me = await m.json();
    } catch (_) { me = { email: emailEl.value.trim(), is_admin: false }; }
    applyMe();
    show("chat");
    await refreshConversations();
    chatInput.focus();
  }

  // ----- TOTP step (after a 202 from /api/login) -------------------------
  totpForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!pendingChallengeToken) return;
    totpErr.hidden = true;
    totpSubmitBtn.disabled = true;
    try {
      const r = await fetch("/api/login/totp", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          challenge_token: pendingChallengeToken,
          code: totpCode.value.trim(),
        }),
      });
      if (r.ok) {
        pendingChallengeToken = null;
        totpForm.hidden = true;
        loginForm.hidden = false;
        totpCode.value = "";
        await postLoginBootstrap();
      } else if (r.status === 429) {
        totpErr.textContent = "Zu viele Versuche. Bitte einen Moment warten.";
        totpErr.hidden = false;
      } else {
        // Generic — backend doesn't tell us why.
        totpErr.textContent = "Code ungültig oder abgelaufen.";
        totpErr.hidden = false;
        totpCode.select();
      }
    } catch (_) {
      totpErr.textContent = "Netzwerkfehler.";
      totpErr.hidden = false;
    } finally {
      totpSubmitBtn.disabled = false;
    }
  });

  totpCancelBtn.addEventListener("click", () => {
    pendingChallengeToken = null;
    totpForm.hidden = true;
    loginForm.hidden = false;
    totpCode.value = "";
    totpErr.hidden = true;
    emailEl.focus();
  });

  // ----- Logout ----------------------------------------------------------
  async function doLogout() {
    try { await fetch("/api/logout", { method: "POST", credentials: "same-origin" }); }
    catch (_) { /* ignore — we still want to drop client state */ }
    me = null;
    currentConversationId = null;
    conversations = [];
    closePopover();
    convList.replaceChildren();
    messages.replaceChildren();
    closeSidebar();
    show("login");
    emailEl.focus();
  }
  logoutBtn.addEventListener("click", doLogout);
  adminLogoutBtn.addEventListener("click", doLogout);

  // ----- Admin nav -------------------------------------------------------
  adminBtn.addEventListener("click", async () => {
    show("admin");
    // Department list must load FIRST — refreshDocs/Users render tag chips
    // that look up the friendly label from the cached vocabulary, and the
    // chip pickers in the upload + create-user forms read it too.
    await refreshDepartments();
    await Promise.all([refreshDocs(), refreshUsers()]);
  });
  backToChatBtn.addEventListener("click", () => {
    show("chat");
    chatInput.focus();
  });

  function setTab(which) {
    tabDocs.classList.toggle("active", which === "docs");
    tabUsers.classList.toggle("active", which === "users");
    tabAnalytics.classList.toggle("active", which === "analytics");
    tabBranding.classList.toggle("active", which === "branding");
    panelDocs.hidden = which !== "docs";
    panelUsers.hidden = which !== "users";
    panelAnalytics.hidden = which !== "analytics";
    panelBranding.hidden = which !== "branding";
    if (which === "branding") loadBrandingForm();
    if (which === "analytics") loadAnalytics();
  }
  tabDocs.addEventListener("click", () => setTab("docs"));
  tabUsers.addEventListener("click", () => setTab("users"));
  tabAnalytics.addEventListener("click", () => setTab("analytics"));
  tabBranding.addEventListener("click", () => setTab("branding"));

  // ----- Admin: analytics ----------------------------------------------
  function statCard(label, value, sub) {
    const card = document.createElement("div");
    card.className = "stat-card";
    const l = document.createElement("div"); l.className = "stat-label"; l.textContent = label; card.appendChild(l);
    const v = document.createElement("div"); v.className = "stat-value"; v.textContent = String(value); card.appendChild(v);
    if (sub) {
      const s = document.createElement("div"); s.className = "stat-sub"; s.textContent = sub; card.appendChild(s);
    }
    return card;
  }

  async function loadAnalytics() {
    [aTotals, aRecent, aFeedback, aQuality, aCited, aDaily, aLogins].forEach(el => el.replaceChildren());
    try {
      const r = await fetch("/api/admin/analytics", { credentials: "same-origin" });
      if (!r.ok) return;
      const a = await r.json();

      aTotals.appendChild(statCard("Benutzer", a.totals.users));
      aTotals.appendChild(statCard("Dokumente", a.totals.documents));
      aTotals.appendChild(statCard("Unterhaltungen", a.totals.conversations));
      aTotals.appendChild(statCard("Nachrichten gesamt", a.totals.messages));

      aRecent.appendChild(statCard("Unterhaltungen 7T", a.recent.conversations_7d));
      aRecent.appendChild(statCard("Unterhaltungen 30T", a.recent.conversations_30d));
      aRecent.appendChild(statCard("Nachrichten 7T", a.recent.messages_7d));
      aRecent.appendChild(statCard("Aktive Benutzer 7T", a.recent.active_users_7d));
      aRecent.appendChild(statCard("Aktive Benutzer 30T", a.recent.active_users_30d));

      // Stat-card labels are short German nouns instead of emoji thumbs.
      // Reads better in a numeric dashboard ("Hilfreich: 23" beats "👍: 23")
      // and matches the rest of the panel.
      aFeedback.appendChild(statCard("Hilfreich", a.feedback.thumbs_up));
      aFeedback.appendChild(statCard("Nicht hilfreich", a.feedback.thumbs_down));
      aFeedback.appendChild(statCard("Bewertet", a.feedback.rated, `${a.feedback.unrated_assistant_msgs} unbewertet`));
      aFeedback.appendChild(statCard("Mit Kommentar", a.feedback.with_comment));

      const refusalPct = (a.refusal_rate_30d * 100).toFixed(1) + " %";
      aQuality.appendChild(statCard("Refusal-Quote", refusalPct, "„kann nicht beantworten\""));

      if (!a.most_cited_30d.length) {
        const empty = document.createElement("li");
        empty.className = "muted"; empty.textContent = "Keine Daten in den letzten 30 Tagen.";
        aCited.appendChild(empty);
      } else {
        for (const c of a.most_cited_30d) {
          const li = document.createElement("li");
          li.className = "row";
          const left = document.createElement("div");
          const name = document.createElement("div"); name.className = "name"; name.textContent = c.source;
          left.appendChild(name);
          const right = document.createElement("div");
          right.className = "muted"; right.textContent = `${c.count}×`;
          li.appendChild(left); li.appendChild(right);
          aCited.appendChild(li);
        }
      }

      // Bars: scale to max of the series.
      const max = a.daily_chats_30d.reduce((m, d) => Math.max(m, d.count), 0) || 1;
      for (const d of a.daily_chats_30d) {
        const bar = document.createElement("div");
        bar.className = "bar";
        const h = Math.max(2, Math.round((d.count / max) * 76));
        bar.style.height = `${h}px`;
        const lab = document.createElement("span");
        lab.className = "bar-label";
        lab.textContent = `${d.day}: ${d.count}`;
        bar.appendChild(lab);
        aDaily.appendChild(bar);
      }
      if (!a.daily_chats_30d.length) {
        const muted = document.createElement("p");
        muted.className = "muted"; muted.textContent = "Noch keine Aktivität.";
        aDaily.appendChild(muted);
      }

      aLogins.appendChild(statCard("Erfolgreich", a.login_attempts_7d.success));
      aLogins.appendChild(statCard("Fehlgeschlagen", a.login_attempts_7d.failure));
    } catch (_) { /* silent */ }
  }

  // ----- Admin: branding -----------------------------------------------
  async function loadBrandingForm() {
    // Pull fresh from server in case another admin changed it in another tab.
    try {
      const r = await fetch("/api/branding");
      if (r.ok) branding = await r.json();
    } catch (_) { /* fall through to whatever's cached */ }
    if (!branding) return;
    brandAppName.value = branding.app_name;
    brandSub.value = branding.sub_brand || "";
    brandColor.value = branding.accent_color;
    brandTagline.value = branding.tagline || "";
    if (brandLegalName) brandLegalName.value = branding.company_legal_name || "";
    if (brandAddress) brandAddress.value = branding.company_address || "";
    if (brandRep) brandRep.value = branding.company_representative || "";
    if (brandContact) brandContact.value = branding.contact_email || "";
    if (brandPrivacy) brandPrivacy.value = branding.privacy_email || "";
    if (brandRegister) brandRegister.value = branding.register_info || "";
    if (brandVat) brandVat.value = branding.vat_id || "";
    if (brandAuthority) brandAuthority.value = branding.supervisory_authority || "";
    pendingLogoDataUri = null;
    if (brandLogoName) brandLogoName.textContent = "Aktuelles Logo";
    if (brandLogoPrev && branding.logo_data_uri) {
      brandLogoPrev.src = branding.logo_data_uri;
    }
    _paintAccentPreview(branding.accent_color);
    brandStatus.hidden = true;
    brandErr.hidden = true;
  }

  // FileReader-based logo picker with a client-side size cap. The server
  // re-validates (prefix allowlist + 256 KB cap); this is a fast-fail so
  // the admin gets feedback before the round-trip. 192 KB raw → ~256 KB as
  // a base64 data-URI, matching the server cap.
  const _MAX_LOGO_RAW_BYTES = 192 * 1024;
  const _ALLOWED_LOGO_TYPES = ["image/svg+xml", "image/png"];
  if (brandLogoInput) {
    brandLogoInput.addEventListener("change", () => {
      brandErr.hidden = true;
      const file = brandLogoInput.files && brandLogoInput.files[0];
      if (!file) return;
      if (!_ALLOWED_LOGO_TYPES.includes(file.type)) {
        brandErr.textContent = "Nur SVG- oder PNG-Logos werden akzeptiert.";
        brandErr.hidden = false;
        brandLogoInput.value = "";
        return;
      }
      if (file.size > _MAX_LOGO_RAW_BYTES) {
        brandErr.textContent = "Logo ist zu groß (max. 192 KB).";
        brandErr.hidden = false;
        brandLogoInput.value = "";
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        // result is a "data:<mime>;base64,<…>" URI.
        pendingLogoDataUri = String(reader.result || "");
        if (brandLogoName) brandLogoName.textContent = file.name;
        if (brandLogoPrev) brandLogoPrev.src = pendingLogoDataUri;
      };
      reader.onerror = () => {
        brandErr.textContent = "Logo konnte nicht gelesen werden.";
        brandErr.hidden = false;
      };
      reader.readAsDataURL(file);
    });
  }
  if (brandLogoClear) {
    brandLogoClear.addEventListener("click", () => {
      // "" tells the server to clear the custom logo (falls back to neutral).
      pendingLogoDataUri = "";
      if (brandLogoInput) brandLogoInput.value = "";
      if (brandLogoName) brandLogoName.textContent = "Standard-Logo (wird zurückgesetzt)";
    });
  }

  // Live preview of the accent colour while the admin is still picking.
  // Mutating --accent on documentElement is the same path applyBranding
  // takes — every button/chip/focus-ring picks it up immediately. If the
  // admin navigates away without saving, the next applyBranding() on
  // page load reads the persisted value and reverts.
  //
  // HTML5 <input type="color"> always returns `#rrggbb`, but if the
  // input ever becomes a free-text field the regex below stops a
  // CSS-injection (audit C1-LOW-8, 2026-05-12). Mirror of the
  // server-side validator in admin._HEX_COLOR_RE.
  const _HEX_COLOR_RE_FE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;
  const brandColorHex = $("brand-color-hex");
  function _paintAccentPreview(hex) {
    if (!hex || !_HEX_COLOR_RE_FE.test(hex)) return;
    if (brandColorHex) brandColorHex.textContent = hex.toUpperCase();
    document.documentElement.style.setProperty("--accent", hex);
  }
  brandColor.addEventListener("input", () => _paintAccentPreview(brandColor.value));

  brandingForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    brandStatus.hidden = true;
    brandErr.hidden = true;
    brandSaveBtn.disabled = true;
    try {
      const payload = {
        app_name: brandAppName.value.trim(),
        sub_brand: brandSub.value.trim(),
        accent_color: brandColor.value,
        tagline: brandTagline.value.trim(),
        company_legal_name: brandLegalName ? brandLegalName.value.trim() : "",
        company_address: brandAddress ? brandAddress.value.trim() : "",
        company_representative: brandRep ? brandRep.value.trim() : "",
        contact_email: brandContact ? brandContact.value.trim() : "",
        privacy_email: brandPrivacy ? brandPrivacy.value.trim() : "",
        register_info: brandRegister ? brandRegister.value.trim() : "",
        vat_id: brandVat ? brandVat.value.trim() : "",
        supervisory_authority: brandAuthority ? brandAuthority.value.trim() : "",
      };
      // Only send the logo when the admin actually picked/cleared one;
      // null means "leave unchanged".
      if (pendingLogoDataUri !== null) {
        payload.logo_data_uri = pendingLogoDataUri;
      }
      const r = await fetch("/api/admin/branding", {
        method: "PATCH",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (r.ok) {
        branding = await r.json();
        pendingLogoDataUri = null;  // committed — don't resend on next save
        // Re-apply to the live DOM so the change is visible immediately,
        // not just after the next reload.
        await applyBranding();
        brandStatus.textContent = "Markenanpassung gespeichert.";
        brandStatus.hidden = false;
      } else {
        let msg = `Fehler ${r.status}`;
        try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
        brandErr.textContent = msg;
        brandErr.hidden = false;
      }
    } catch (_) {
      brandErr.textContent = "Netzwerkfehler beim Speichern.";
      brandErr.hidden = false;
    } finally {
      brandSaveBtn.disabled = false;
    }
  });

  // ----- Chat helpers ----------------------------------------------------
  function addMessage(role, text) {
    const div = document.createElement("div");
    div.className = "msg " + role;
    // Text lives in its own child so we can later replace it with mixed
    // text/element children (inline citation pills) without nuking siblings
    // like the sources panel or feedback buttons.
    const txt = document.createElement("div");
    txt.className = "msg-text";
    txt.textContent = text;
    div.appendChild(txt);
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
    return div;
  }

  function setMessageText(botEl, text) {
    const txt = botEl.querySelector(".msg-text");
    if (txt) txt.textContent = text;
  }

  // Render assistant-message text with safe inline markdown + citation pills.
  // Replaces both the previous setMessageText (textContent only) AND the
  // standalone applyInlineCitations pass — they were two phases over the
  // same string and the order was awkward.
  //
  // Supported subset (deliberately minimal — LLM output uses these reliably):
  //   **bold**, *italic*, `code`, [filename.ext] citation,
  //   line-leading "- " / "* " bullets, blank-line paragraph breaks.
  //
  // No HTML is ever interpolated as a string. Every node comes from
  // createElement + textContent so injected `<script>` etc. is rendered
  // as text. The system prompt also frames retrieved context as data,
  // but defence in depth: the renderer is the second wall.
  const _CITE_NAME_RE = /^[A-Za-z0-9._-]+\.(?:pdf|md|markdown|txt)$/;

  function _appendInline(container, text, knownSourceNames) {
    let i = 0, plain = "";
    const flushPlain = () => {
      if (plain) {
        container.appendChild(document.createTextNode(plain));
        plain = "";
      }
    };
    while (i < text.length) {
      // **bold** — must come before single-* italic so we don't eat the openers.
      if (text[i] === "*" && text[i + 1] === "*") {
        const end = text.indexOf("**", i + 2);
        if (end !== -1) {
          flushPlain();
          const el = document.createElement("strong");
          _appendInline(el, text.slice(i + 2, end), knownSourceNames);
          container.appendChild(el);
          i = end + 2;
          continue;
        }
      }
      // *italic* — single asterisk, NOT inside a "**" run.
      if (text[i] === "*" && text[i + 1] !== "*" && (i === 0 || text[i - 1] !== "*")) {
        const end = text.indexOf("*", i + 1);
        if (end !== -1 && text[end + 1] !== "*") {
          flushPlain();
          const el = document.createElement("em");
          _appendInline(el, text.slice(i + 1, end), knownSourceNames);
          container.appendChild(el);
          i = end + 1;
          continue;
        }
      }
      // `code` — inline monospace.
      if (text[i] === "`") {
        const end = text.indexOf("`", i + 1);
        if (end !== -1) {
          flushPlain();
          const el = document.createElement("code");
          el.textContent = text.slice(i + 1, end);
          container.appendChild(el);
          i = end + 1;
          continue;
        }
      }
      // [filename.ext] citation — only if the source was actually retrieved.
      if (text[i] === "[" && knownSourceNames && knownSourceNames.size) {
        const end = text.indexOf("]", i + 1);
        if (end !== -1) {
          const name = text.slice(i + 1, end);
          if (_CITE_NAME_RE.test(name) && knownSourceNames.has(name)) {
            flushPlain();
            const pill = document.createElement("span");
            pill.className = "inline-cite";
            pill.textContent = name;
            container.appendChild(pill);
            i = end + 1;
            continue;
          }
        }
      }
      plain += text[i];
      i++;
    }
    flushPlain();
  }

  function renderAssistantContent(msgTextEl, text, knownSourceNames) {
    msgTextEl.replaceChildren();
    msgTextEl.classList.add("rendered");
    const blocks = text.split(/\n{2,}/);
    for (const block of blocks) {
      if (!block.trim()) continue;
      const lines = block.split("\n");
      const isList = lines.every(l => /^\s*[-*]\s+/.test(l));
      if (isList) {
        const ul = document.createElement("ul");
        for (const line of lines) {
          const li = document.createElement("li");
          _appendInline(li, line.replace(/^\s*[-*]\s+/, ""), knownSourceNames);
          ul.appendChild(li);
        }
        msgTextEl.appendChild(ul);
      } else {
        const p = document.createElement("p");
        // Soft line breaks within a paragraph map to <br>.
        lines.forEach((line, idx) => {
          _appendInline(p, line, knownSourceNames);
          if (idx < lines.length - 1) p.appendChild(document.createElement("br"));
        });
        msgTextEl.appendChild(p);
      }
    }
  }

  function renderSources(botEl, sources) {
    if (!sources || !sources.length) return;

    // Pills row (one per unique source filename, in retrieval order).
    // If the source object carries a document_id, the pill becomes a
    // clickable button that opens the doc viewer.
    const pillsWrap = document.createElement("div");
    pillsWrap.className = "sources";
    const label = document.createElement("span");
    label.className = "label";
    label.textContent = "Quellen:";
    pillsWrap.appendChild(label);
    const firstByName = new Map();
    for (const s of sources) {
      if (typeof s !== "object") continue;
      if (!firstByName.has(s.source)) firstByName.set(s.source, s);
    }
    for (const [name, s] of firstByName) {
      const pill = document.createElement(s.document_id ? "button" : "span");
      pill.className = "source-pill" + (s.document_id ? " source-pill-link" : "");
      pill.textContent = name;
      if (s.document_id) {
        pill.type = "button";
        pill.addEventListener("click", () =>
          openDocViewer(s.document_id, s.chunk_index, name));
      }
      pillsWrap.appendChild(pill);
    }
    botEl.appendChild(pillsWrap);

    // Snippet block: one <details> per retrieved chunk, showing the actual
    // text the model saw. Collapsed by default. Snippet head is clickable
    // when document_id is present.
    const objSources = sources.filter((s) => typeof s === "object" && s.text);
    if (!objSources.length) return;

    const det = document.createElement("details");
    det.className = "snippets";
    const sum = document.createElement("summary");
    sum.textContent = `Quelltext anzeigen (${objSources.length})`;
    det.appendChild(sum);
    for (const s of objSources) {
      const item = document.createElement("div");
      item.className = "snippet";
      const head = document.createElement(s.document_id ? "button" : "div");
      head.className = "snippet-head" + (s.document_id ? " snippet-head-link" : "");
      head.textContent = `${s.source} · Abschnitt ${s.chunk_index}`;
      if (s.document_id) {
        head.type = "button";
        head.addEventListener("click", () =>
          openDocViewer(s.document_id, s.chunk_index, s.source));
      }
      const body = document.createElement("div");
      body.className = "snippet-body";
      body.textContent = s.text;
      item.appendChild(head);
      item.appendChild(body);
      det.appendChild(item);
    }
    botEl.appendChild(det);
  }

  // ----- Doc viewer modal -----------------------------------------------
  async function openDocViewer(documentId, highlightChunkIndex, sourceName) {
    docViewerTitle.textContent = sourceName || `Dokument ${documentId}`;
    docViewerMeta.textContent = "Wird geladen…";
    docViewerBody.replaceChildren();
    docViewer.hidden = false;
    document.body.style.overflow = "hidden";
    try {
      const r = await fetch(`/api/documents/${documentId}`, { credentials: "same-origin" });
      if (!r.ok) {
        docViewerMeta.textContent = `Fehler ${r.status}`;
        return;
      }
      const d = await r.json();
      const ts = new Date(d.ingested_at).toLocaleString("de-DE");
      const versionLabel = d.is_current ? `v${d.version} (aktuell)` : `v${d.version} (archiviert)`;
      docViewerTitle.textContent = d.source;
      docViewerMeta.textContent = `${versionLabel} · eingelesen ${ts} · ${d.chunks.length} Abschnitt${d.chunks.length === 1 ? "" : "e"}`;
      let highlightedEl = null;
      for (const c of d.chunks) {
        const div = document.createElement("div");
        div.className = "dv-chunk";
        if (c.chunk_index === highlightChunkIndex) {
          div.classList.add("highlight");
          highlightedEl = div;
        }
        const head = document.createElement("div");
        head.className = "dv-chunk-head";
        head.textContent = `Abschnitt ${c.chunk_index}`;
        const body = document.createElement("div");
        body.textContent = c.content;
        div.appendChild(head);
        div.appendChild(body);
        docViewerBody.appendChild(div);
      }
      if (highlightedEl) {
        // Scroll the highlighted chunk into view inside the modal body.
        // Defer so layout is settled.
        requestAnimationFrame(() => highlightedEl.scrollIntoView({ block: "center" }));
      }
    } catch (_) {
      docViewerMeta.textContent = "Netzwerkfehler.";
    }
  }

  function closeDocViewer() {
    docViewer.hidden = true;
    docViewerBody.replaceChildren();
    document.body.style.overflow = "";
  }
  docViewerClose.addEventListener("click", closeDocViewer);
  docViewer.addEventListener("click", (e) => {
    // Click on the overlay (backdrop) but not on the inner panel closes.
    if (e.target === docViewer) closeDocViewer();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !docViewer.hidden) closeDocViewer();
  });

  // ----- Feedback (thumbs per assistant message) ------------------------
  // Lucide-style outline icons. Each icon = a body shape + a small "wrist"
  // line. Drawing both via two <path> children keeps the SVG simple and
  // resilient to copy/paste truncation (the previous attempt had a
  // fragmented Heroicons path that rendered as squiggles for thumbs-down).
  const _SVG_NS = "http://www.w3.org/2000/svg";
  const _THUMB_UP_PATHS = [
    "M7 10v12",
    "M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z",
  ];
  const _THUMB_DOWN_PATHS = [
    "M17 14V2",
    "M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z",
  ];

  function _thumbIcon(pathDataList) {
    const svg = document.createElementNS(_SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.7");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    svg.classList.add("fb-icon");
    for (const d of pathDataList) {
      const path = document.createElementNS(_SVG_NS, "path");
      path.setAttribute("d", d);
      svg.appendChild(path);
    }
    return svg;
  }

  function renderFeedback(botEl, messageId, currentFeedback) {
    if (!messageId) return; // can't rate something without an id
    botEl.dataset.messageId = String(messageId);

    const wrap = document.createElement("div");
    wrap.className = "msg-feedback";

    const upBtn = document.createElement("button");
    upBtn.type = "button";
    upBtn.className = "fb-btn fb-up";
    upBtn.appendChild(_thumbIcon(_THUMB_UP_PATHS));
    upBtn.setAttribute("aria-label", "Hilfreich");

    const downBtn = document.createElement("button");
    downBtn.type = "button";
    downBtn.className = "fb-btn fb-down";
    downBtn.appendChild(_thumbIcon(_THUMB_DOWN_PATHS));
    downBtn.setAttribute("aria-label", "Nicht hilfreich");

    const status = document.createElement("span");
    status.className = "fb-status";
    status.hidden = true;

    let activeRating = currentFeedback ? currentFeedback.rating : 0;
    function paint() {
      upBtn.classList.toggle("active", activeRating === 1);
      downBtn.classList.toggle("active", activeRating === -1);
    }
    paint();

    async function submit(rating) {
      // Click on the active rating again = clear (DELETE).
      const clearing = activeRating === rating;
      const previous = activeRating;
      // Optimistic update so the UI feels snappy.
      activeRating = clearing ? 0 : rating;
      paint();
      upBtn.disabled = true; downBtn.disabled = true;
      try {
        let comment = null;
        if (!clearing && rating === -1) {
          // Optional: ask for a brief reason on thumbs-down. Empty / cancel
          // skips the comment but still records the rating.
          const c = prompt("Optional: Was war ungenau? (Leer lassen zum Abbrechen)");
          if (c === null) {
            // user cancelled the rating entirely
            activeRating = previous;
            paint();
            return;
          }
          comment = c.trim() || null;
        }
        const url = `/api/messages/${messageId}/feedback`;
        const resp = clearing
          ? await fetch(url, { method: "DELETE", credentials: "same-origin" })
          : await fetch(url, {
              method: "POST",
              credentials: "same-origin",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ rating, comment }),
            });
        if (!resp.ok) throw new Error("feedback failed");
        status.textContent = clearing ? "Bewertung entfernt." : "Danke für das Feedback!";
        status.hidden = false;
        setTimeout(() => { status.hidden = true; }, 2500);
      } catch (_) {
        // Revert optimistic update.
        activeRating = previous;
        paint();
        status.textContent = "Speichern fehlgeschlagen.";
        status.hidden = false;
      } finally {
        upBtn.disabled = false; downBtn.disabled = false;
      }
    }

    upBtn.addEventListener("click", () => submit(1));
    downBtn.addEventListener("click", () => submit(-1));

    wrap.appendChild(upBtn);
    wrap.appendChild(downBtn);
    wrap.appendChild(status);
    botEl.appendChild(wrap);
  }

  // Parse a Server-Sent Events stream from a fetch() Response.
  async function* parseSSE(stream) {
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let sep;
        while ((sep = buf.indexOf("\n\n")) !== -1) {
          const block = buf.slice(0, sep);
          buf = buf.slice(sep + 2);
          let eventName = "message";
          let dataLines = [];
          for (const line of block.split("\n")) {
            if (line.startsWith("event:")) eventName = line.slice(6).trim();
            else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
          }
          if (dataLines.length) {
            const raw = dataLines.join("\n");
            try { yield { event: eventName, data: JSON.parse(raw) }; }
            catch (_) { yield { event: eventName, data: raw }; }
          }
        }
      }
    } finally {
      reader.releaseLock();
    }
  }

  // ----- Send message ----------------------------------------------------
  async function sendMessage(text) {
    chatErr.hidden = true;
    addMessage("user", text);

    const botEl = addMessage("bot", "");
    botEl.classList.add("thinking");
    messages.setAttribute("aria-busy", "true");

    // The server reconstructs history from the conversation_id. Sending
    // null here tells it "create a fresh conversation"; the new id comes
    // back via the SSE `meta` event and we adopt it.
    const wasNew = currentConversationId === null;
    let buffered = "";
    let pendingSources = null;
    try {
      const r = await fetch("/api/chat", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: JSON.stringify({
          message: text,
          conversation_id: currentConversationId,
        }),
      });
      if (r.status === 401) {
        show("login");
        return;
      }
      if (r.status === 429) {
        chatErr.textContent = "Zu viele Anfragen — bitte einen Moment warten.";
        chatErr.hidden = false;
        botEl.remove();
        return;
      }
      if (!r.ok || !r.body) {
        chatErr.textContent = "Da ist etwas schiefgelaufen. Bitte erneut versuchen.";
        chatErr.hidden = false;
        botEl.remove();
        return;
      }

      for await (const ev of parseSSE(r.body)) {
        if (ev.event === "meta") {
          // Adopt the server-assigned conversation id on first send.
          if (ev.data?.conversation_id && currentConversationId === null) {
            currentConversationId = ev.data.conversation_id;
          }
        } else if (ev.event === "sources") {
          pendingSources = ev.data?.sources || [];
        } else if (ev.event === "token") {
          if (botEl.classList.contains("thinking")) {
            botEl.classList.remove("thinking");
          }
          buffered += ev.data?.text || "";
          setMessageText(botEl, buffered);
          messages.scrollTop = messages.scrollHeight;
        } else if (ev.event === "error") {
          if (ev.data?.detail === "upstream_busy") {
            chatErr.textContent =
              "Das Modell ist gerade ausgelastet. Bitte versuche es in ein paar Sekunden erneut.";
          } else {
            chatErr.textContent = "Das Modell hat einen Fehler zurückgegeben.";
          }
          chatErr.hidden = false;
          break;
        } else if (ev.event === "done") {
          // The server attaches the persisted message id here so we can
          // immediately wire up feedback buttons. Null = persist failed.
          if (ev.data?.message_id) {
            renderFeedback(botEl, ev.data.message_id, null);
          }
          break;
        }
      }
      if (pendingSources && pendingSources.length) {
        renderSources(botEl, pendingSources);
      }
      // Re-render the buffered text as markdown + inline citations now
      // that streaming is done. During the stream we used setMessageText
      // (textContent only) so tokens appear instantly; here we replace
      // that flat text with structured DOM (paragraphs, bold, lists,
      // citation pills).
      if (buffered) {
        const txtEl = botEl.querySelector(".msg-text");
        const names = new Set(
          (pendingSources || []).map(s => typeof s === "string" ? s : s.source)
        );
        if (txtEl) renderAssistantContent(txtEl, buffered, names);
      } else {
        setMessageText(botEl, "(keine Antwort)");
      }
      // Refresh the sidebar so the (possibly newly-created, possibly
      // freshly-titled) conversation shows up at the top. Best-effort —
      // a sidebar refresh failure shouldn't break the chat experience.
      if (buffered && (wasNew || pendingSources)) {
        refreshConversations().catch(() => {});
      }
    } catch (_) {
      chatErr.textContent = "Netzwerkfehler. Bitte erneut versuchen.";
      chatErr.hidden = false;
      botEl.remove();
    } finally {
      botEl.classList.remove("thinking");
      messages.setAttribute("aria-busy", "false");
    }
  }

  chatForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = chatInput.value.trim();
    if (!text) return;
    chatInput.value = "";
    sendBtn.disabled = true;
    chatInput.disabled = true;
    try {
      await sendMessage(text);
    } finally {
      sendBtn.disabled = false;
      chatInput.disabled = false;
      chatInput.focus();
    }
  });

  // Shift+Enter inserts a newline; bare Enter submits.
  chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      chatForm.requestSubmit();
    }
  });

  // ----- Account modal (2FA self-service) -------------------------------
  function openAccountModal() {
    accountModal.hidden = false;
    document.body.style.overflow = "hidden";
    refreshTotpStatus();
  }
  function closeAccountModal() {
    accountModal.hidden = true;
    document.body.style.overflow = "";
    totpSetup.hidden = true;
    totpDisable.hidden = true;
    totpVerifyErr.hidden = true;
    totpDisableErr.hidden = true;
    totpVerifyCode.value = "";
    totpDisableCode.value = "";
  }
  accountBtn.addEventListener("click", openAccountModal);
  accountClose.addEventListener("click", closeAccountModal);
  accountModal.addEventListener("click", (e) => {
    if (e.target === accountModal) closeAccountModal();
  });

  async function refreshTotpStatus() {
    totpActions.replaceChildren();
    totpSetup.hidden = true;
    totpDisable.hidden = true;
    totpStatus.textContent = "Wird geladen…";
    try {
      const r = await fetch("/api/me/2fa", { credentials: "same-origin" });
      if (!r.ok) { totpStatus.textContent = "Status konnte nicht geladen werden."; return; }
      const s = await r.json();
      if (s.enabled) {
        totpStatus.textContent = "Zwei-Faktor ist aktiviert.";
        totpDisable.hidden = false;
      } else {
        totpStatus.textContent = "Zwei-Faktor ist deaktiviert. Empfohlen für Admin-Konten.";
        const enableBtn = document.createElement("button");
        enableBtn.type = "button";
        enableBtn.textContent = "Aktivieren";
        enableBtn.addEventListener("click", startTotpSetup);
        totpActions.appendChild(enableBtn);
      }
    } catch (_) {
      totpStatus.textContent = "Netzwerkfehler.";
    }
  }

  async function startTotpSetup() {
    totpActions.replaceChildren();
    totpStatus.textContent = "Setup wird vorbereitet…";
    try {
      const r = await fetch("/api/me/2fa/setup", {
        method: "POST",
        credentials: "same-origin",
      });
      if (!r.ok) { totpStatus.textContent = "Setup fehlgeschlagen."; return; }
      const data = await r.json();
      totpStatus.textContent = "Scannen Sie den QR-Code und bestätigen Sie mit einem aktuellen Code.";
      // Defense-in-depth (audit C2-MED-5, 2026-05-12): only accept a
      // strict base64-PNG data URI. Stops a future server bug from
      // injecting any other scheme into <img src> (the CSP
      // `img-src 'self' data:` is permissive about data: URIs, so the
      // check must happen here).
      if (typeof data.qr_png_data_uri === "string"
          && /^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(data.qr_png_data_uri)) {
        totpQr.src = data.qr_png_data_uri;
      } else {
        totpStatus.textContent = "Setup fehlgeschlagen (ungültiges QR-Format).";
        return;
      }
      totpSecret.textContent = data.secret;
      totpSetup.hidden = false;
      totpVerifyCode.value = "";
      totpVerifyErr.hidden = true;
      totpVerifyCode.focus();
    } catch (_) {
      totpStatus.textContent = "Netzwerkfehler.";
    }
  }

  totpVerifyForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    totpVerifyErr.hidden = true;
    totpVerifyBtn.disabled = true;
    try {
      const r = await fetch("/api/me/2fa/verify", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: totpVerifyCode.value.trim() }),
      });
      if (r.ok) {
        totpSetup.hidden = true;
        await refreshTotpStatus();
      } else {
        totpVerifyErr.textContent = "Code ungültig.";
        totpVerifyErr.hidden = false;
        totpVerifyCode.select();
      }
    } catch (_) {
      totpVerifyErr.textContent = "Netzwerkfehler.";
      totpVerifyErr.hidden = false;
    } finally {
      totpVerifyBtn.disabled = false;
    }
  });

  totpDisableForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    totpDisableErr.hidden = true;
    totpDisableBtn.disabled = true;
    try {
      const r = await fetch("/api/me/2fa/disable", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: totpDisableCode.value.trim() }),
      });
      if (r.ok) {
        totpDisable.hidden = true;
        totpDisableCode.value = "";
        await refreshTotpStatus();
      } else {
        totpDisableErr.textContent = "Code ungültig oder bereits deaktiviert.";
        totpDisableErr.hidden = false;
        totpDisableCode.select();
      }
    } catch (_) {
      totpDisableErr.textContent = "Netzwerkfehler.";
      totpDisableErr.hidden = false;
    } finally {
      totpDisableBtn.disabled = false;
    }
  });

  // Esc key closes whichever modal is open. Doc viewer handler is wired
  // separately above; keep them additive.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !accountModal.hidden) closeAccountModal();
  });

  // ----- Conversation sidebar ------------------------------------------
  async function refreshConversations() {
    try {
      const r = await fetch("/api/conversations", { credentials: "same-origin" });
      if (!r.ok) return;
      conversations = await r.json();
      renderConversationList();
    } catch (_) { /* silent */ }
  }

  function renderConversationList() {
    convList.replaceChildren();
    if (!conversations.length) {
      const empty = document.createElement("li");
      empty.className = "conv-empty";
      empty.textContent = "Noch keine Unterhaltungen.";
      convList.appendChild(empty);
      return;
    }
    for (const c of conversations) {
      const li = document.createElement("li");
      li.className = "conv-item";
      if (c.id === currentConversationId) li.classList.add("active");

      const title = document.createElement("div");
      title.className = "conv-title";
      title.textContent = c.title;
      li.appendChild(title);

      const menu = document.createElement("button");
      menu.type = "button";
      menu.className = "conv-menu-btn";
      menu.setAttribute("aria-label", "Unterhaltungs-Menü");
      menu.textContent = "⋯";
      menu.addEventListener("click", (e) => {
        e.stopPropagation();
        toggleConvMenu(menu, c);
      });
      li.appendChild(menu);

      li.addEventListener("click", () => loadConversation(c.id));
      convList.appendChild(li);
    }
  }

  async function loadConversation(id) {
    closePopover();
    closeSidebar();
    try {
      const r = await fetch(`/api/conversations/${id}`, { credentials: "same-origin" });
      if (!r.ok) {
        if (r.status === 404) await refreshConversations();
        return;
      }
      const conv = await r.json();
      currentConversationId = conv.id;
      messages.replaceChildren();
      for (const m of conv.messages) {
        const el = addMessage(m.role === "user" ? "user" : "bot", m.content);
        if (m.role === "assistant") {
          const txtEl = el.querySelector(".msg-text");
          const names = new Set(
            (Array.isArray(m.sources) ? m.sources : []).map(s => s.source)
          );
          if (txtEl) renderAssistantContent(txtEl, m.content, names);
          if (Array.isArray(m.sources) && m.sources.length) {
            renderSources(el, m.sources);
          }
          renderFeedback(el, m.id, m.feedback || null);
        }
      }
      renderConversationList();
      chatInput.focus();
    } catch (_) { /* silent */ }
  }

  function newConversation() {
    closePopover();
    closeSidebar();
    currentConversationId = null;
    messages.replaceChildren();
    renderConversationList();
    chatInput.focus();
  }

  async function renameConversation(conv) {
    const title = prompt("Neuer Titel:", conv.title);
    if (title === null) return;
    const trimmed = title.trim();
    if (!trimmed) return;
    try {
      const r = await fetch(`/api/conversations/${conv.id}`, {
        method: "PATCH",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: trimmed.slice(0, 200) }),
      });
      if (r.ok) await refreshConversations();
    } catch (_) { /* silent */ }
  }

  async function deleteConversation(conv) {
    if (!confirm(`Unterhaltung „${conv.title}" wirklich löschen?`)) return;
    try {
      const r = await fetch(`/api/conversations/${conv.id}`, {
        method: "DELETE",
        credentials: "same-origin",
      });
      if (r.ok) {
        if (currentConversationId === conv.id) {
          currentConversationId = null;
          messages.replaceChildren();
        }
        await refreshConversations();
      }
    } catch (_) { /* silent */ }
  }

  function toggleConvMenu(triggerBtn, conv) {
    if (openPopover && openPopover.dataset.convId === String(conv.id)) {
      closePopover();
      return;
    }
    closePopover();
    const pop = document.createElement("div");
    pop.className = "conv-popover";
    pop.dataset.convId = String(conv.id);

    const renameBtn = document.createElement("button");
    renameBtn.type = "button";
    renameBtn.textContent = "Umbenennen";
    renameBtn.addEventListener("click", () => {
      closePopover();
      renameConversation(conv);
    });

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "danger";
    delBtn.textContent = "Löschen";
    delBtn.addEventListener("click", () => {
      closePopover();
      deleteConversation(conv);
    });

    pop.appendChild(renameBtn);
    pop.appendChild(delBtn);
    document.body.appendChild(pop);
    // Position relative to the trigger button.
    const rect = triggerBtn.getBoundingClientRect();
    pop.style.top = `${rect.bottom + 4}px`;
    pop.style.left = `${Math.max(8, rect.right - pop.offsetWidth)}px`;
    openPopover = pop;
  }

  function closePopover() {
    if (openPopover) {
      openPopover.remove();
      openPopover = null;
    }
  }
  document.addEventListener("click", (e) => {
    // Click outside an open popover closes it. The trigger button itself
    // calls toggleConvMenu which handles the toggle case.
    if (openPopover && !openPopover.contains(e.target) &&
        !(e.target instanceof Element && e.target.classList.contains("conv-menu-btn"))) {
      closePopover();
    }
  });

  // Sidebar toggle (mobile)
  function openSidebar() {
    sidebar.classList.add("open");
    sidebarBackdrop.hidden = false;
    sidebarToggle.setAttribute("aria-expanded", "true");
  }
  function closeSidebar() {
    sidebar.classList.remove("open");
    sidebarBackdrop.hidden = true;
    sidebarToggle.setAttribute("aria-expanded", "false");
  }
  sidebarToggle.addEventListener("click", () => {
    if (sidebar.classList.contains("open")) closeSidebar(); else openSidebar();
  });
  sidebarBackdrop.addEventListener("click", closeSidebar);

  newConvBtn.addEventListener("click", newConversation);

  // ----- Departments (controlled vocabulary for tags) -------------------
  // Cached list — refreshed whenever a department is created/deleted.
  // Holds [{name, label, created_at}, ...]. The chip pickers read this
  // directly so they don't each have to re-fetch.
  let departments = [];

  async function refreshDepartments() {
    try {
      const r = await fetch("/api/admin/departments", { credentials: "same-origin" });
      if (!r.ok) return;
      departments = await r.json();
    } catch (_) { /* keep stale list — better than blanking the picker */ }
    renderDepartmentList();
  }

  function renderDepartmentList() {
    deptList.replaceChildren();
    if (!departments.length) {
      const empty = document.createElement("div");
      empty.className = "dept-empty";
      empty.textContent = "Noch keine Abteilungen angelegt.";
      deptList.appendChild(empty);
      return;
    }
    for (const d of departments) {
      const pill = document.createElement("span");
      pill.className = "dept-pill";

      const lbl = document.createElement("span");
      lbl.className = "dept-label";
      lbl.textContent = d.label;
      pill.appendChild(lbl);

      const slug = document.createElement("span");
      slug.className = "dept-slug";
      slug.textContent = d.name;
      pill.appendChild(slug);

      const del = document.createElement("button");
      del.type = "button";
      del.setAttribute("aria-label", `Abteilung ${d.label} löschen`);
      del.textContent = "×";
      del.addEventListener("click", () => deleteDepartment(d));
      pill.appendChild(del);

      deptList.appendChild(pill);
    }
  }

  async function deleteDepartment(d) {
    const msg =
      `Abteilung „${d.label}" (${d.name}) wirklich löschen?\n\n` +
      "Sie wird automatisch aus allen Dokumenten und Benutzer-Berechtigungen entfernt.";
    if (!confirm(msg)) return;
    deptError.hidden = true;
    try {
      const r = await fetch(`/api/admin/departments/${encodeURIComponent(d.name)}`, {
        method: "DELETE",
        credentials: "same-origin",
      });
      if (!r.ok && r.status !== 204) {
        let m = `Fehler ${r.status}`;
        try { const j = await r.json(); if (j.detail) m = String(j.detail); } catch (_) {}
        deptError.textContent = m;
        deptError.hidden = false;
        return;
      }
      await refreshDepartments();
      // Re-render any open admin lists that show tag chips, since the
      // server stripped the tag from documents/users in the same call.
      await Promise.all([refreshDocs(), refreshUsers()]);
    } catch (_) {
      deptError.textContent = "Netzwerkfehler beim Löschen.";
      deptError.hidden = false;
    }
  }

  deptCreateForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    deptError.hidden = true;
    deptCreateBtn.disabled = true;
    try {
      const r = await fetch("/api/admin/departments", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: deptCreateName.value.trim(),
          label: deptCreateLabel.value.trim(),
        }),
      });
      if (!r.ok) {
        let m = `Fehler ${r.status}`;
        try { const j = await r.json(); if (j.detail) m = String(j.detail); } catch (_) {}
        deptError.textContent = m;
        deptError.hidden = false;
        return;
      }
      deptCreateName.value = "";
      deptCreateLabel.value = "";
      await refreshDepartments();
    } catch (_) {
      deptError.textContent = "Netzwerkfehler beim Anlegen.";
      deptError.hidden = false;
    } finally {
      deptCreateBtn.disabled = false;
    }
  });

  // Auto-derive a slug from the label as the admin types — saves a step
  // for the common case (label "Personal & Recht" -> slug "personal-recht").
  // The admin can still override the slug field manually.
  deptCreateLabel.addEventListener("input", () => {
    if (deptCreateName.dataset.userEdited === "1") return;
    const slug = deptCreateLabel.value
      .toLowerCase()
      .normalize("NFKD")
      .replace(/[̀-ͯ]/g, "")     // strip combining marks (umlauts -> ascii-ish)
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 32);
    deptCreateName.value = slug;
  });
  deptCreateName.addEventListener("input", () => {
    deptCreateName.dataset.userEdited = "1";
  });

  // ----- Chip picker (multi-select for departments) --------------------
  // Returns a controller {refresh(), getSelected(), setSelected(names)}.
  // Re-renders chips from the current `departments` cache plus the
  // initial selection. Picking toggles the .selected class; the controller
  // reads that on getSelected().
  function chipPicker(container, initialSelected) {
    let selected = new Set(initialSelected || []);

    function render() {
      container.replaceChildren();
      if (!departments.length) {
        const empty = document.createElement("span");
        empty.className = "chip-empty";
        empty.textContent = "Keine Abteilungen verfügbar — oben unter „Abteilungen“ anlegen.";
        container.appendChild(empty);
        return;
      }
      for (const d of departments) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "chip-pick" + (selected.has(d.name) ? " selected" : "");
        btn.dataset.name = d.name;
        btn.textContent = d.label;
        btn.title = d.name;  // tooltip shows the slug
        btn.addEventListener("click", () => {
          if (selected.has(d.name)) selected.delete(d.name);
          else selected.add(d.name);
          btn.classList.toggle("selected");
        });
        container.appendChild(btn);
      }
    }

    render();
    return {
      refresh: render,
      getSelected: () => [...selected],
      setSelected: (names) => { selected = new Set(names || []); render(); },
    };
  }

  // Bind the two persistent pickers (upload + new-user). They're
  // re-rendered every time refreshDepartments() runs because the
  // available list might have grown/shrunk.
  let uploadTagPicker = null;
  let newUserTagPicker = null;
  function rebuildPersistentPickers() {
    uploadTagPicker  = chipPicker(uploadTagPickerEl, uploadTagPicker?.getSelected() || []);
    newUserTagPicker = chipPicker(newUserTagPickerEl, newUserTagPicker?.getSelected() || []);
  }
  // Keep these refs around so refreshDepartments can rebuild after any change.
  const _origRefreshDepartments = refreshDepartments;
  refreshDepartments = async function() {
    await _origRefreshDepartments();
    rebuildPersistentPickers();
  };

  function renderTagChips(target, tags) {
    target.replaceChildren();
    if (!tags || !tags.length) {
      const empty = document.createElement("span");
      empty.className = "muted small";
      empty.textContent = "öffentlich";
      target.appendChild(empty);
      return;
    }
    // Look the slug up in the cached departments list so we can show the
    // friendly label in the chat surface; fall back to the slug.
    const labelOf = new Map(departments.map(d => [d.name, d.label]));
    for (const t of tags) {
      const chip = document.createElement("span");
      chip.className = "tag-chip";
      chip.textContent = labelOf.get(t) || t;
      target.appendChild(chip);
    }
  }

  // ----- Admin: documents -----------------------------------------------
  // Mirror the picked filename into the visible label so the user can see
  // which file is staged. The native input is .visually-hidden so the
  // browser's "No file chosen" text never shows.
  uploadFile.addEventListener("change", () => {
    const f = uploadFile.files && uploadFile.files[0];
    uploadFileName.textContent = f ? f.name : "Keine Datei gewählt";
    uploadFileName.classList.toggle("muted", !f);
  });

  uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    uploadErr.hidden = true;
    uploadStatus.hidden = true;
    const f = uploadFile.files && uploadFile.files[0];
    if (!f) return;
    uploadBtn.disabled = true;
    uploadStatus.textContent = `Wird hochgeladen und eingelesen: ${f.name}…`;
    uploadStatus.hidden = false;
    try {
      const fd = new FormData();
      fd.append("file", f);
      // Tags are sent as a JSON-encoded string in a multipart field. The
      // backend parses, normalises, validates against the departments
      // vocabulary, and either applies or inherits from the previous
      // version when the field is empty (see admin.upload_document).
      const tags = uploadTagPicker ? uploadTagPicker.getSelected() : [];
      if (tags.length) fd.append("tags", JSON.stringify(tags));
      const r = await fetch("/api/admin/documents", {
        method: "POST",
        credentials: "same-origin",
        body: fd,
      });
      if (r.ok) {
        const d = await r.json();
        uploadStatus.textContent = `Eingelesen: ${d.source} (${d.chunks} Abschnitte)`;
        uploadFile.value = "";
        uploadFileName.textContent = "Keine Datei gewählt";
        uploadFileName.classList.add("muted");
        if (uploadTagPicker) uploadTagPicker.setSelected([]);
        await refreshDocs();
      } else {
        let msg = `Fehler ${r.status}`;
        try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
        uploadErr.textContent = msg;
        uploadErr.hidden = false;
        uploadStatus.hidden = true;
      }
    } catch (_) {
      uploadErr.textContent = "Netzwerkfehler beim Hochladen.";
      uploadErr.hidden = false;
      uploadStatus.hidden = true;
    } finally {
      uploadBtn.disabled = false;
    }
  });

  async function refreshDocs() {
    docsList.replaceChildren();
    try {
      const r = await fetch("/api/admin/documents", { credentials: "same-origin" });
      if (!r.ok) return;
      const docs = await r.json();
      if (!docs.length) {
        const empty = document.createElement("li");
        empty.className = "muted";
        empty.textContent = "Noch keine Dokumente vorhanden.";
        docsList.appendChild(empty);
        return;
      }
      for (const d of docs) {
        const li = document.createElement("li");
        li.className = "row";
        const left = document.createElement("div");
        const name = document.createElement("div");
        name.className = "name";
        name.textContent = d.source;
        if ((d.version || 1) > 1) {
          const badge = document.createElement("span");
          badge.className = "version-badge";
          badge.textContent = ` v${d.version}`;
          name.appendChild(badge);
        }
        const meta = document.createElement("div");
        meta.className = "meta muted";
        const ts = new Date(d.ingested_at).toLocaleString("de-DE");
        const archived = d.archived_versions
          ? ` · ${d.archived_versions} ältere Version${d.archived_versions === 1 ? "" : "en"}`
          : "";
        meta.textContent = `${d.chunks} Abschnitte · eingelesen ${ts}${archived}`;
        const tagsRow = document.createElement("div");
        tagsRow.className = "meta tag-row";
        renderTagChips(tagsRow, d.tags);
        left.appendChild(name);
        left.appendChild(meta);
        left.appendChild(tagsRow);
        const actions = document.createElement("div");
        actions.className = "row-actions";
        const tagBtn = document.createElement("button");
        tagBtn.type = "button";
        tagBtn.className = "ghost";
        tagBtn.textContent = "Tags bearbeiten";
        tagBtn.addEventListener("click", () => editDocTags(d, tagBtn));
        const del = document.createElement("button");
        del.type = "button";
        del.className = "ghost danger";
        del.textContent = "Löschen";
        del.addEventListener("click", async () => {
          if (!confirm(`„${d.source}" wirklich löschen? Alle Abschnitte werden entfernt.`)) return;
          del.disabled = true;
          const dr = await fetch(`/api/admin/documents/${d.id}`, {
            method: "DELETE",
            credentials: "same-origin",
          });
          if (dr.ok) {
            await refreshDocs();
          } else {
            del.disabled = false;
            alert("Löschen fehlgeschlagen.");
          }
        });
        actions.appendChild(tagBtn);
        actions.appendChild(del);
        li.appendChild(left);
        li.appendChild(actions);
        docsList.appendChild(li);
      }
    } catch (_) { /* fall through */ }
  }

  // ----- Admin: users ---------------------------------------------------
  createUserForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    createUserErr.hidden = true;
    createUserStatus.hidden = true;
    createUserBtn.disabled = true;
    try {
      const r = await fetch("/api/admin/users", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: newUserEmail.value.trim(),
          password: newUserPassword.value,
          is_admin: newUserAdmin.checked,
          allowed_tags: newUserTagPicker ? newUserTagPicker.getSelected() : [],
        }),
      });
      if (r.ok) {
        const u = await r.json();
        createUserStatus.textContent = `Benutzer angelegt: ${u.email}`;
        createUserStatus.hidden = false;
        newUserEmail.value = "";
        newUserPassword.value = "";
        newUserAdmin.checked = false;
        if (newUserTagPicker) newUserTagPicker.setSelected([]);
        await refreshUsers();
      } else {
        let msg = `Fehler ${r.status}`;
        try { const j = await r.json(); if (j.detail) msg = String(j.detail); } catch (_) {}
        createUserErr.textContent = msg;
        createUserErr.hidden = false;
      }
    } catch (_) {
      createUserErr.textContent = "Netzwerkfehler beim Anlegen.";
      createUserErr.hidden = false;
    } finally {
      createUserBtn.disabled = false;
    }
  });

  async function refreshUsers() {
    usersList.replaceChildren();
    try {
      const r = await fetch("/api/admin/users", { credentials: "same-origin" });
      if (!r.ok) return;
      const users = await r.json();
      for (const u of users) {
        const li = document.createElement("li");
        li.className = "row";

        const left = document.createElement("div");
        const name = document.createElement("div");
        name.className = "name";
        name.textContent = u.email;
        if (u.id === me?.id) {
          const self = document.createElement("span");
          self.className = "muted";
          self.textContent = " (Sie)";
          name.appendChild(self);
        }
        const meta = document.createElement("div");
        meta.className = "meta muted";
        const flags = [];
        flags.push(u.is_admin ? "Admin" : "Benutzer");
        flags.push(u.is_active ? "aktiv" : "deaktiviert");
        flags.push(`erstellt ${new Date(u.created_at).toLocaleDateString("de-DE")}`);
        meta.textContent = flags.join(" · ");
        const tagsRow = document.createElement("div");
        tagsRow.className = "meta tag-row";
        // Admins bypass the ACL anyway, so tag display is informational
        // for them only; non-admins see this as the actual access scope.
        renderTagChips(tagsRow, u.allowed_tags);
        left.appendChild(name);
        left.appendChild(meta);
        left.appendChild(tagsRow);

        const actions = document.createElement("div");
        actions.className = "row-actions";

        // Toggle admin (disabled for self).
        const adminTog = document.createElement("button");
        adminTog.type = "button";
        adminTog.className = "ghost";
        adminTog.textContent = u.is_admin ? "Admin entziehen" : "Zum Admin machen";
        adminTog.disabled = u.id === me?.id;
        adminTog.addEventListener("click", () =>
          patchUser(u.id, { is_admin: !u.is_admin }, adminTog),
        );

        // Toggle active (disabled for self).
        const activeTog = document.createElement("button");
        activeTog.type = "button";
        activeTog.className = "ghost";
        activeTog.textContent = u.is_active ? "Deaktivieren" : "Aktivieren";
        activeTog.disabled = u.id === me?.id;
        activeTog.addEventListener("click", () =>
          patchUser(u.id, { is_active: !u.is_active }, activeTog),
        );

        const tagBtn = document.createElement("button");
        tagBtn.type = "button";
        tagBtn.className = "ghost";
        tagBtn.textContent = "Tags bearbeiten";
        tagBtn.addEventListener("click", () => editUserTags(u, tagBtn));

        // Reset password.
        const resetBtn = document.createElement("button");
        resetBtn.type = "button";
        resetBtn.className = "ghost";
        resetBtn.textContent = "Passwort zurücksetzen";
        resetBtn.addEventListener("click", () => resetUserPassword(u.id, u.email, resetBtn));

        // Delete (disabled for self).
        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "ghost danger";
        delBtn.textContent = "Löschen";
        delBtn.disabled = u.id === me?.id;
        delBtn.addEventListener("click", () => deleteUser(u.id, u.email, delBtn));

        actions.appendChild(adminTog);
        actions.appendChild(activeTog);
        actions.appendChild(tagBtn);
        actions.appendChild(resetBtn);
        actions.appendChild(delBtn);

        li.appendChild(left);
        li.appendChild(actions);
        usersList.appendChild(li);
      }
    } catch (_) { /* fall through */ }
  }

  async function patchUser(id, body, btn) {
    btn.disabled = true;
    try {
      const r = await fetch(`/api/admin/users/${id}`, {
        method: "PATCH",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        let msg = `Fehler ${r.status}`;
        try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
        alert(msg);
      }
    } finally {
      await refreshUsers();
    }
  }

  async function resetUserPassword(id, email, btn) {
    const pw = prompt(`Neues Passwort für ${email} (mind. 12 Zeichen):`);
    if (pw === null) return;
    if (pw.length < 12) { alert("Passwort muss mindestens 12 Zeichen haben."); return; }
    btn.disabled = true;
    try {
      const r = await fetch(`/api/admin/users/${id}/reset-password`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: pw }),
      });
      if (!r.ok) {
        let msg = `Fehler ${r.status}`;
        try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
        alert(msg);
      } else {
        alert(`Passwort für ${email} wurde geändert. Aktive Sitzungen wurden beendet.`);
      }
    } finally {
      btn.disabled = false;
    }
  }

  // Tag-edit modal: shared by "edit doc tags" and "edit user tags".
  // openTagEditModal({title, subject, current, save}) where save(selected)
  // is the async PATCH, returning {ok, errMsg?}.
  let _tagEditPicker = null;
  let _tagEditSubmit = null;
  function openTagEditModal({title, subject, current, save}) {
    tagEditTitle.textContent = title;
    tagEditSubject.textContent = subject;
    tagEditErr.hidden = true;
    _tagEditPicker = chipPicker(tagEditPicker, current || []);
    _tagEditSubmit = save;
    tagEditModal.hidden = false;
    document.body.style.overflow = "hidden";
  }
  function closeTagEditModal() {
    tagEditModal.hidden = true;
    tagEditPicker.replaceChildren();
    document.body.style.overflow = "";
    _tagEditPicker = null;
    _tagEditSubmit = null;
  }
  tagEditClose.addEventListener("click", closeTagEditModal);
  tagEditCancel.addEventListener("click", closeTagEditModal);
  tagEditModal.addEventListener("click", (e) => {
    if (e.target === tagEditModal) closeTagEditModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !tagEditModal.hidden) closeTagEditModal();
  });
  tagEditSave.addEventListener("click", async () => {
    if (!_tagEditPicker || !_tagEditSubmit) return;
    tagEditErr.hidden = true;
    tagEditSave.disabled = true;
    try {
      const result = await _tagEditSubmit(_tagEditPicker.getSelected());
      if (result?.ok) closeTagEditModal();
      else {
        tagEditErr.textContent = result?.errMsg || "Speichern fehlgeschlagen.";
        tagEditErr.hidden = false;
      }
    } finally {
      tagEditSave.disabled = false;
    }
  });

  async function editDocTags(d, btn) {
    openTagEditModal({
      title: "Abteilungen für Dokument",
      subject: d.source,
      current: d.tags || [],
      save: async (tags) => {
        const r = await fetch(`/api/admin/documents/${d.id}/tags`, {
          method: "PATCH",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tags }),
        });
        if (r.ok) { await refreshDocs(); return {ok: true}; }
        let m = `Fehler ${r.status}`;
        try { const j = await r.json(); if (j.detail) m = String(j.detail); } catch (_) {}
        return {ok: false, errMsg: m};
      },
    });
  }

  async function editUserTags(u, btn) {
    openTagEditModal({
      title: "Abteilungs-Berechtigungen für Benutzer",
      subject: u.email,
      current: u.allowed_tags || [],
      save: async (allowed_tags) => {
        const r = await fetch(`/api/admin/users/${u.id}/allowed-tags`, {
          method: "PATCH",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ allowed_tags }),
        });
        if (r.ok) { await refreshUsers(); return {ok: true}; }
        let m = `Fehler ${r.status}`;
        try { const j = await r.json(); if (j.detail) m = String(j.detail); } catch (_) {}
        return {ok: false, errMsg: m};
      },
    });
  }

  async function deleteUser(id, email, btn) {
    if (!confirm(`Benutzer „${email}" wirklich löschen?`)) return;
    btn.disabled = true;
    try {
      const r = await fetch(`/api/admin/users/${id}`, {
        method: "DELETE",
        credentials: "same-origin",
      });
      if (!r.ok) {
        let msg = `Fehler ${r.status}`;
        try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
        alert(msg);
        btn.disabled = false;
      } else {
        await refreshUsers();
      }
    } catch (_) {
      btn.disabled = false;
    }
  }

  bootstrap();
})();
