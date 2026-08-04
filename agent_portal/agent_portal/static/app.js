function _sortKey(text) {
  text = text.trim();
  // MM/DD/YYYY or M/D/YYYY (the CRM export's usual date format). Checked before
  // the numeric case below — parseFloat("06/10/2026") silently returns 6 (it
  // stops at the first "/"), which used to sort every date column by month
  // number alone instead of chronologically.
  var us = text.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (us) {
    return { num: true, value: new Date(+us[3], +us[1] - 1, +us[2]).getTime() };
  }
  // YYYY-MM-DD (seen in some CRM export rows for the same date columns)
  var iso = text.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (iso) {
    return { num: true, value: new Date(+iso[1], +iso[2] - 1, +iso[3]).getTime() };
  }
  // Plain numbers / currency — require the WHOLE cell to be numeric (after
  // stripping $ and ,), not just a numeric prefix, so things like dates or
  // "10 days" never get silently misread as numbers again.
  var cleaned = text.replace(/[$,]/g, "");
  if (cleaned !== "" && /^-?\d+(\.\d+)?$/.test(cleaned)) {
    return { num: true, value: parseFloat(cleaned) };
  }
  return { num: false, value: text };
}

/* ---------- Loading UI (uploads / deletes) ----------
 * Every form submit gets a top progress bar + a spinner on the button that
 * triggered it. Forms flagged with class="js-heavy-action" (uploads, and
 * deletes/resets that touch the DB) also get a full-screen "processing"
 * overlay, with the message pulled from data-loading-text/data-loading-hint.
 * Runs after any inline onsubmit="return confirm(...)" — if the user hits
 * Cancel, that sets e.defaultPrevented and we skip the loading UI entirely.
 */
(function () {
  var bar = document.getElementById("loading-bar");
  var overlay = document.getElementById("loading-overlay");
  var overlayText = document.getElementById("loading-overlay-text");
  var overlayHint = document.getElementById("loading-overlay-hint");

  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return; // user cancelled a confirm() dialog
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;

    if (bar) {
      bar.style.transition = "none";
      bar.style.width = "0%";
      bar.classList.add("active");
      // Force a reflow so the width reset above actually applies before the
      // transition to 85% kicks in on the next frame.
      // eslint-disable-next-line no-unused-expressions
      bar.offsetHeight;
      bar.style.transition = "";
      requestAnimationFrame(function () { bar.style.width = "85%"; });
    }

    var btn = (e.submitter && e.submitter.tagName === "BUTTON")
      ? e.submitter
      : form.querySelector('button[type="submit"]');
    if (btn && !btn.classList.contains("is-loading")) {
      btn.classList.add("is-loading");
      btn.disabled = true;
    }

    if (form.classList.contains("js-heavy-action") && overlay) {
      if (overlayText) overlayText.textContent = form.dataset.loadingText || "Processing…";
      if (overlayHint) overlayHint.textContent = form.dataset.loadingHint || "This can take a few seconds for larger files.";
      overlay.classList.add("active");
    }
  }, false);
})();

/* ---------- Manage Agents: inline auto-save, chips, overflow menu ----------
 * Every form here still POSTs to its normal Flask route (routes_admin.py is
 * untouched) — this layer only changes HOW: intercepted via fetch instead of
 * a full-page navigation, so the admin never leaves the page. Each AJAX call
 * follows the same redirect-to-GET the browser would, parses the flash
 * message out of the returned HTML (Flask's normal post/redirect/get flow —
 * nothing server-side changed to support this), and for anything that
 * changes what's on screen (a new/removed CRM chip, a password's on/off
 * status, an admin badge), pulls the freshly-rendered fragment for THIS
 * agent out of that same response and swaps it in. That keeps the client
 * dumb — it never re-implements chip/badge rendering, it just borrows the
 * server's own markup — so there's a single source of truth for what an
 * agent card looks like.
 *
 * Every "bind" function is idempotent (guarded by dataset.bound) so it's
 * safe to call again on a swapped-in fragment without double-attaching
 * listeners to the parts that weren't replaced.
 */
(function () {
  var list = document.querySelector('[data-role="agent-list"]');
  if (!list) return; // not on the Manage Agents page

  function extractFlash(doc) {
    var successEl = doc.querySelector(".flash-success");
    if (successEl) return { ok: true, message: successEl.textContent.trim() };
    var singleError = doc.querySelector(".flash-error:not(.flash-collapsible)");
    if (singleError) return { ok: false, message: singleError.textContent.trim() };
    var errorItem = doc.querySelector(".flash-error.flash-collapsible .flash-detail-list li");
    if (errorItem) return { ok: false, message: errorItem.textContent.trim() };
    return { ok: true, message: null };
  }

  function ajaxSubmitForm(form) {
    var fd = new FormData(form);
    return fetch(form.action, { method: "POST", body: fd, credentials: "same-origin" })
      .then(function (resp) { return resp.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var flash = extractFlash(doc);
        return { ok: flash.ok, message: flash.message, doc: doc };
      });
  }

  function setStatus(el, kind, message) {
    if (!el) return;
    el.className = "agent-input-status" + (kind ? " is-" + kind : "");
    el.textContent = message || "";
  }

  function clearStatusSoon(el, delay) {
    if (!el) return;
    var current = el.textContent;
    setTimeout(function () {
      if (el.textContent === current) setStatus(el, "", "");
    }, delay || 2000);
  }

  function flashInputError(input, message) {
    input.classList.add("has-error");
    input.setCustomValidity(message || "Something went wrong.");
    input.reportValidity();
    setTimeout(function () {
      input.setCustomValidity("");
      input.classList.remove("has-error");
    }, 4000);
  }

  // Swaps one or more data-role fragments on `card` for the freshly-rendered
  // version of the same agent pulled out of `freshDoc`, then re-runs
  // initAgentCard so the new nodes (which start unbound) get their listeners.
  function refreshAgentCard(card, freshDoc, roles) {
    var freshCard = freshDoc.querySelector('.agent-card[data-agent-id="' + card.dataset.agentId + '"]');
    if (!freshCard) return;
    roles.forEach(function (role) {
      var freshNode = freshCard.querySelector('[data-role="' + role + '"]');
      var liveNode = card.querySelector('[data-role="' + role + '"]');
      if (freshNode && liveNode) liveNode.replaceWith(document.importNode(freshNode, true));
    });
    initAgentCard(card);
  }

  function bindEmailForm(form) {
    if (!form || form.dataset.bound) return;
    form.dataset.bound = "1";
    var input = form.querySelector('input[name="email"]');
    var status = form.querySelector('[data-role="status"]');

    function save() {
      var val = input.value.trim();
      var original = input.dataset.original;
      if (!val) { setStatus(status, "error", "Email is required."); return; }
      if (val === original) { setStatus(status, "", ""); return; }
      setStatus(status, "saving", "Saving…");
      ajaxSubmitForm(form).then(function (result) {
        if (result.ok) {
          input.dataset.original = val;
          input.classList.remove("has-error");
          setStatus(status, "saved", "Saved");
          clearStatusSoon(status);
        } else {
          input.classList.add("has-error");
          setStatus(status, "error", result.message || "Couldn't save.");
        }
      }).catch(function () {
        setStatus(status, "error", "Network error — try again.");
      });
    }

    form.addEventListener("submit", function (e) { e.preventDefault(); save(); });
    input.addEventListener("blur", save);
    input.addEventListener("input", function () {
      input.classList.remove("has-error");
      if (status.classList.contains("is-error")) setStatus(status, "", "");
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); input.blur(); }
      else if (e.key === "Escape") { input.value = input.dataset.original; setStatus(status, "", ""); input.blur(); }
    });
  }

  function bindAliasAddForm(form) {
    if (!form || form.dataset.bound) return;
    form.dataset.bound = "1";
    var input = form.querySelector(".tag-add-input");
    var card = form.closest(".agent-card");

    function submit() {
      var val = input.value.trim();
      if (!val) return;
      // readOnly, not disabled — a disabled field is excluded from
      // FormData entirely, which would silently submit an empty agent_name.
      input.readOnly = true;
      ajaxSubmitForm(form).then(function (result) {
        if (result.ok) {
          refreshAgentCard(card, result.doc, ["chip-row"]);
        } else {
          input.readOnly = false;
          input.value = val;
          flashInputError(input, result.message);
        }
      }).catch(function () {
        input.readOnly = false;
        flashInputError(input, "Network error — try again.");
      });
    }

    form.addEventListener("submit", function (e) { e.preventDefault(); submit(); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); submit(); }
    });
  }

  function bindAliasRemoveForm(form) {
    if (!form || form.dataset.bound) return;
    form.dataset.bound = "1";
    var card = form.closest(".agent-card");
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var chip = form.closest(".tag-chip");
      if (chip) chip.style.opacity = "0.4";
      ajaxSubmitForm(form).then(function (result) {
        if (result.ok) {
          refreshAgentCard(card, result.doc, ["chip-row"]);
        } else if (chip) {
          chip.style.opacity = "1";
        }
      }).catch(function () {
        if (chip) chip.style.opacity = "1";
      });
    });
  }

  function bindPasswordToggle(card) {
    var toggle = card.querySelector('[data-role="password-toggle"]');
    if (!toggle || toggle.dataset.bound) return;
    toggle.dataset.bound = "1";
    toggle.addEventListener("click", function () {
      var passwordForm = card.querySelector('[data-role="password-form"]');
      var row = card.querySelector('[data-role="password-row"]');
      if (!passwordForm) return;
      var willShow = passwordForm.hasAttribute("hidden");
      passwordForm.hidden = !willShow;
      if (row) row.style.display = willShow ? "none" : "";
      if (willShow) {
        var input = passwordForm.querySelector('input[name="password"]');
        if (input) input.focus();
      }
    });
  }

  function bindPasswordForm(form, card) {
    if (!form || form.dataset.bound) return;
    form.dataset.bound = "1";
    var input = form.querySelector('input[name="password"]');
    var status = form.querySelector('[data-role="status"]');
    var submitBtn = form.querySelector('button[type="submit"]');

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      if (!input.value || input.value.length < 6) {
        setStatus(status, "error", "Min. 6 characters.");
        return;
      }
      submitBtn.disabled = true;
      setStatus(status, "saving", "Saving…");
      ajaxSubmitForm(form).then(function (result) {
        if (result.ok) {
          // The freshly-fetched fragment already renders the reveal form
          // collapsed (hidden, cleared) — swapping it back in IS the
          // "close and reset" step, no manual cleanup needed here.
          refreshAgentCard(card, result.doc, ["password-row", "password-form", "identity-badges"]);
        } else {
          submitBtn.disabled = false;
          setStatus(status, "error", result.message || "Couldn't save.");
        }
      }).catch(function () {
        submitBtn.disabled = false;
        setStatus(status, "error", "Network error — try again.");
      });
    });

    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        form.hidden = true;
        var row = card.querySelector('[data-role="password-row"]');
        if (row) row.style.display = "";
        input.value = "";
        setStatus(status, "", "");
      }
    });
  }

  var menuGlobalHandlersBound = false;
  function ensureMenuGlobalHandlers() {
    if (menuGlobalHandlersBound) return;
    menuGlobalHandlersBound = true;
    function closeAllExcept(exceptMenu) {
      document.querySelectorAll(".agent-menu-dropdown:not([hidden])").forEach(function (dropdown) {
        var menu = dropdown.closest(".agent-menu");
        if (menu === exceptMenu) return;
        dropdown.hidden = true;
        var trigger = menu && menu.querySelector('[data-role="menu-trigger"]');
        if (trigger) trigger.setAttribute("aria-expanded", "false");
      });
    }
    document.addEventListener("click", function (e) {
      var openMenu = e.target.closest && e.target.closest(".agent-menu");
      closeAllExcept(openMenu && openMenu.querySelector(".agent-menu-dropdown:not([hidden])") ? openMenu : null);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeAllExcept(null);
    });
  }

  function bindAgentMenu(menu) {
    if (!menu || menu.dataset.bound) return;
    menu.dataset.bound = "1";
    ensureMenuGlobalHandlers();

    var trigger = menu.querySelector('[data-role="menu-trigger"]');
    var dropdown = menu.querySelector('[data-role="menu-dropdown"]');
    var removeTrigger = menu.querySelector('[data-role="remove-user-trigger"]');
    var confirmBox = menu.querySelector('[data-role="remove-user-confirm"]');
    var cancelBtn = menu.querySelector('[data-role="remove-user-cancel"]');

    function resetConfirm() {
      if (removeTrigger) removeTrigger.hidden = false;
      if (confirmBox) confirmBox.hidden = true;
    }

    trigger.addEventListener("click", function (e) {
      e.stopPropagation();
      var willOpen = dropdown.hidden;
      document.querySelectorAll(".agent-menu-dropdown:not([hidden])").forEach(function (d) { d.hidden = true; });
      dropdown.hidden = !willOpen;
      trigger.setAttribute("aria-expanded", String(willOpen));
      if (!willOpen) resetConfirm();
    });

    if (removeTrigger) {
      removeTrigger.addEventListener("click", function () {
        removeTrigger.hidden = true;
        confirmBox.hidden = false;
      });
    }
    if (cancelBtn) {
      cancelBtn.addEventListener("click", resetConfirm);
    }
  }

  function updateAgentCount(delta) {
    var countEl = document.querySelector('[data-role="agent-count"]');
    if (!countEl) return;
    var n = parseInt(countEl.textContent, 10) || 0;
    countEl.textContent = String(n + delta);
  }

  function bindRemoveUserForm(card) {
    var form = card.querySelector('[data-role="remove-user-form"]');
    if (!form || form.dataset.bound) return;
    form.dataset.bound = "1";
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var confirmBtn = form.querySelector('[data-role="remove-user-confirm-btn"]');
      if (confirmBtn) confirmBtn.disabled = true;
      ajaxSubmitForm(form).then(function (result) {
        if (result.ok) {
          card.style.transition = "opacity .18s ease, transform .18s ease";
          card.style.opacity = "0";
          card.style.transform = "scale(0.98)";
          setTimeout(function () {
            card.remove();
            updateAgentCount(-1);
            var searchInput = document.getElementById("agent-search");
            if (searchInput) searchInput.dispatchEvent(new Event("input"));
          }, 180);
        } else {
          if (confirmBtn) confirmBtn.disabled = false;
          window.alert(result.message || "Couldn't remove this user.");
        }
      }).catch(function () {
        if (confirmBtn) confirmBtn.disabled = false;
        window.alert("Network error — try again.");
      });
    });
  }

  function initAgentCard(card) {
    bindEmailForm(card.querySelector('[data-role="email-form"]'));
    card.querySelectorAll('[data-role="remove-alias-form"]').forEach(bindAliasRemoveForm);
    bindAliasAddForm(card.querySelector('[data-role="add-alias-form"]'));
    bindPasswordToggle(card);
    bindPasswordForm(card.querySelector('[data-role="password-form"]'), card);
    bindAgentMenu(card.querySelector('[data-role="agent-menu"]'));
    bindRemoveUserForm(card);
  }

  function initAgentSearch() {
    var input = document.getElementById("agent-search");
    var emptyState = document.querySelector('[data-role="search-empty"]');
    if (!input) return;
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      var cards = list.querySelectorAll(".agent-card");
      var visibleCount = 0;
      cards.forEach(function (card) {
        var match = !q || (card.dataset.search || "").indexOf(q) !== -1;
        card.style.display = match ? "" : "none";
        if (match) visibleCount++;
      });
      if (emptyState) emptyState.hidden = visibleCount !== 0 || cards.length === 0;
    });
  }

  list.querySelectorAll(".agent-card").forEach(initAgentCard);
  initAgentSearch();
})();

document.querySelectorAll("table.sortable").forEach(function(table) {
  var tbody = table.querySelector("tbody");
  var headers = table.querySelectorAll("th[data-col]");
  var sortCol = -1;
  var sortAsc = true;

  headers.forEach(function(th) {
    th.addEventListener("click", function() {
      var col = parseInt(th.dataset.col);
      if (sortCol === col) {
        sortAsc = !sortAsc;
      } else {
        sortCol = col;
        sortAsc = true;
      }
      headers.forEach(function(h) { h.classList.remove("sort-asc", "sort-desc"); });
      th.classList.add(sortAsc ? "sort-asc" : "sort-desc");

      var rows = Array.from(tbody.querySelectorAll("tr"));
      rows.sort(function(a, b) {
        var aText = a.cells[col] ? a.cells[col].innerText : "";
        var bText = b.cells[col] ? b.cells[col].innerText : "";
        var ak = _sortKey(aText);
        var bk = _sortKey(bText);
        if (ak.num && bk.num) {
          return sortAsc ? ak.value - bk.value : bk.value - ak.value;
        }
        return sortAsc ? aText.localeCompare(bText) : bText.localeCompare(aText);
      });
      rows.forEach(function(r) { tbody.appendChild(r); });
    });
  });
});
