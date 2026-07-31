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
        var av = a.cells[col] ? a.cells[col].innerText.replace(/[$,]/g, "") : "";
        var bv = b.cells[col] ? b.cells[col].innerText.replace(/[$,]/g, "") : "";
        var an = parseFloat(av);
        var bn = parseFloat(bv);
        if (!isNaN(an) && !isNaN(bn)) {
          return sortAsc ? an - bn : bn - an;
        }
        return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
      });
      rows.forEach(function(r) { tbody.appendChild(r); });
    });
  });
});
