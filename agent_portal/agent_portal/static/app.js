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

// Upload progress overlay: shows a real upload-percentage bar while a file
// upload's bytes are sending, then switches to an indeterminate "Processing…"
// stripe animation while the server parses/calculates (which can take a
// while for a large CRM export) until the redirected response comes back.
document.querySelectorAll("form.js-upload-form").forEach(function (form) {
  form.addEventListener("submit", function (e) {
    e.preventDefault();

    var fileNames = Array.from(form.querySelectorAll('input[type="file"]'))
      .flatMap(function (input) { return Array.from(input.files || []); })
      .map(function (f) { return f.name; })
      .join(", ");

    var overlay = document.createElement("div");
    overlay.className = "upload-overlay";
    overlay.innerHTML =
      '<div class="upload-overlay-card">' +
        '<div class="upload-overlay-label">Uploading…</div>' +
        '<div class="upload-overlay-files"></div>' +
        '<div class="upload-progress-track"><div class="upload-progress-bar"></div></div>' +
        '<div class="upload-overlay-pct">0%</div>' +
      '</div>';
    document.body.appendChild(overlay);
    overlay.querySelector(".upload-overlay-files").textContent = fileNames;
    var bar = overlay.querySelector(".upload-progress-bar");
    var pct = overlay.querySelector(".upload-overlay-pct");
    var label = overlay.querySelector(".upload-overlay-label");

    var xhr = new XMLHttpRequest();
    xhr.open("POST", form.action, true);

    xhr.upload.onprogress = function (evt) {
      if (!evt.lengthComputable) return;
      var percent = Math.round((evt.loaded / evt.total) * 100);
      bar.style.width = percent + "%";
      pct.textContent = percent + "%";
      if (percent >= 100) {
        label.textContent = "Processing…";
        pct.textContent = "";
        bar.classList.add("upload-progress-indeterminate");
      }
    };

    xhr.onload = function () {
      if (xhr.status >= 200 && xhr.status < 400) {
        // Some uploads redirect somewhere other than the page the form is
        // on (e.g. straight to the new period's detail page) — keep the
        // address bar in sync with what's actually being displayed.
        if (xhr.responseURL && xhr.responseURL !== window.location.href) {
          window.history.replaceState({}, "", xhr.responseURL);
        }
        document.open();
        document.write(xhr.responseText);
        document.close();
      } else {
        overlay.remove();
        alert("Upload failed (server error " + xhr.status + "). Please try again.");
      }
    };

    xhr.onerror = function () {
      overlay.remove();
      alert("Upload failed (network error). Please try again.");
    };

    xhr.send(new FormData(form));
  });
});
