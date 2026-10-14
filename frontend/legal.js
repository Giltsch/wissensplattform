/* Minimal branding loader for the static legal pages (impressum,
 * datenschutz). They don't load the SPA's app.js, so this standalone script
 * fetches /api/branding (public, no auth) and:
 *   - fills the logo <img> + favicon from logo_data_uri,
 *   - sets the wordmark app_name / sub_brand,
 *   - fills every [data-brand="<column>"] placeholder with the operator's
 *     legal-identity value, falling back to the bracketed placeholder text
 *     when the operator hasn't filled that field yet.
 *
 * textContent only — never innerHTML — so a branding value can never inject
 * markup (defence-in-depth; the strict CSP already blocks inline script).
 */
(function () {
  "use strict";

  function fillPlaceholders(b) {
    document.querySelectorAll("[data-brand]").forEach(function (el) {
      var key = el.dataset.brand;
      var val = b && typeof b[key] === "string" ? b[key].trim() : "";
      if (val) {
        el.textContent = val;
        if (el.dataset.brandHref === "mailto") {
          el.setAttribute("href", "mailto:" + val);
        }
      } else if (el.dataset.brandFallback !== undefined) {
        el.textContent = el.dataset.brandFallback;
      }
    });
  }

  function applyWordmark(b) {
    var sub = b.sub_brand || "";
    document.title = document.title.replace(
      /·.*$/,
      "· " + (sub ? b.app_name + " " + sub : b.app_name)
    );
    if (typeof b.logo_data_uri === "string" && b.logo_data_uri) {
      document.querySelectorAll("img.logo").forEach(function (img) {
        img.src = b.logo_data_uri;
      });
      var icon = document.querySelector('link[rel="icon"]');
      if (icon) icon.href = b.logo_data_uri;
    }
    if (typeof b.accent_color === "string" &&
        /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(b.accent_color)) {
      document.documentElement.style.setProperty("--accent", b.accent_color);
    }
    document.querySelectorAll(".wordmark").forEach(function (wm) {
      var inner = wm.querySelector(":scope > span");
      var accentSpan = wm.querySelector(".accent");
      if (!inner || !accentSpan) return;
      for (var i = 0; i < inner.childNodes.length; i++) {
        var node = inner.childNodes[i];
        if (node.nodeType === Node.TEXT_NODE) {
          // Trailing space only when a sub-brand word follows.
          node.textContent = sub ? b.app_name + " " : b.app_name;
          break;
        }
      }
      accentSpan.textContent = sub;
    });
  }

  fetch("/api/branding")
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (b) {
      if (!b) return; // keep the HTML defaults / bracketed placeholders
      applyWordmark(b);
      fillPlaceholders(b);
    })
    .catch(function () { /* offline → keep HTML defaults */ });
})();
