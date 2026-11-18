(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  // mobile nav
  var mb = $("#menu-btn"), nav = $("#nav");
  if (mb && nav) mb.addEventListener("click", function () {
    var open = nav.classList.toggle("open"); mb.setAttribute("aria-expanded", open);
  });

  // print / back / confirm / copy (no inline handlers: strict CSP)
  $$("[data-print]").forEach(function (b) { b.addEventListener("click", function () { window.print(); }); });
  $$("[data-back]").forEach(function (b) { b.addEventListener("click", function () { history.back(); }); });
  $$("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) { if (!window.confirm(f.getAttribute("data-confirm"))) e.preventDefault(); });
  });
  $$("[data-copy]").forEach(function (b) {
    b.addEventListener("click", function () {
      var t = b.getAttribute("data-copy");
      if (navigator.clipboard) navigator.clipboard.writeText(t).then(function () { b.textContent = "Copied"; setTimeout(function () { b.textContent = "Copy"; }, 1500); });
    });
  });

  // live quote (progressive enhancement: the form still works without JS via normal POST)
  var form = $("[data-live-quote]"), panel = $("#quote-panel");
  if (form && panel) {
    var token = ($('meta[name="csrf-token"]') || {}).content;
    var timer = null, seq = 0;
    var fields = ["country", "weight_kg", "length_cm", "width_cm", "height_cm", "service", "insure", "declared_value"];
    var el = function (n) { return form.elements[n]; };
    var set = function (k, v) { $$('[data-q="' + k + '"]', panel).forEach(function (n) { n.textContent = v; }); };
    var show = function (which, msg) {
      $("[data-q-empty]", panel).hidden = which !== "empty";
      var er = $("[data-q-error]", panel); er.hidden = which !== "error"; if (msg) er.textContent = msg;
      $("[data-q-result]", panel).hidden = which !== "result";
    };
    var run = function () {
      var body = {};
      fields.forEach(function (n) { var e = el(n); if (e) body[n] = e.type === "checkbox" ? e.checked : e.value; });
      if (!body.country || !body.weight_kg) { show("empty"); return; }
      var my = ++seq;
      fetch("/api/quote", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": token }, body: JSON.stringify(body), credentials: "same-origin" })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
        .then(function (res) {
          if (my !== seq) return;
          if (!res.ok || !res.j.ok) { show("error", (res.j && res.j.error) || "Could not calculate a quote."); return; }
          var q = res.j.quote;
          ["total", "service_name", "country_name", "zone_name", "actual_weight", "volumetric_weight", "chargeable_weight", "shipping_charge", "fuel_surcharge", "insurance_fee"].forEach(function (k) { set(k, q[k]); });
          set("eta", q.days_min + "-" + q.days_max);
          var link = $("#book-link");
          if (link) {
            var p = new URLSearchParams(); fields.forEach(function (n) { if (body[n] && body[n] !== false) p.set(n, body[n]); });
            link.href = "/book?" + p.toString();
          }
          show("result");
        })
        .catch(function () { if (my === seq) show("error", "Network problem - the calculator couldn't reach the server."); });
    };
    form.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(run, 250); });
    form.addEventListener("change", function () { clearTimeout(timer); timer = setTimeout(run, 100); });
    if (el("country") && el("country").value && el("weight_kg") && el("weight_kg").value) run();
  }
})();
