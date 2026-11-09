// Astra Watch landing page. No dependencies. Everything animated here is illustrative.
(() => {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const small = matchMedia("(max-width: 899px)");

  // ---- play animations only while visible -------------------------------------------------
  const loops = document.querySelectorAll(".loop");
  if (reduce || !("IntersectionObserver" in window)) loops.forEach(e => e.classList.add("play"));
  else {
    const io = new IntersectionObserver(es => es.forEach(e => e.target.classList.toggle("play", e.isIntersecting)), { threshold: 0.2 });
    loops.forEach(e => io.observe(e));
  }

  // ---- "How it works" diagram ---------------------------------------------------------------
  const ICON = {
    laptop: "M4 6h16v10H4zM2 19h20",
    server: "M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01",
    agent: "M8 3v4M16 3v4M6 7h12v5a6 6 0 0 1-12 0zM12 18v3",
    checks: "M4 12l5 5L20 6",
    hub: "M12 3 3 8v8l9 5 9-5V8zM12 12 3 8M12 12l9-4M12 12v9",
    dash: "M3 4h18v16H3zM3 9h18M8 9v11",
    tg: "M22 3 2 11l7 2.5L11.5 21 14 15l6 4z",
    you: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
  };
  const TXT = {
    mac: ["MacBook", "your laptop"], agent: ["Mac agent", "sends numbers"],
    server: ["Linux server", "runs your apps"], checks: ["Server checks", "system and apps"],
    hub: ["Astra Watch", "keeps and checks"],
    dash: ["Dashboard", "private"], tg: ["Telegram", "message"], you: ["You", "decide"],
  };
  // [id, x, y, w, h, layout (v = stacked, h = icon beside text), appears at step]
  const LAYOUTS = {
    wide: {
      vb: "0 0 960 540", cls: "wide",
      nodes: [["mac", 40, 60, 190, 120, "v", 1], ["agent", 270, 85, 180, 70, "h", 1], ["server", 40, 360, 190, 120, "v", 2], ["checks", 270, 385, 180, 70, "h", 2],
        ["hub", 540, 175, 190, 190, "hub", 0], ["dash", 790, 50, 140, 100, "v", 4], ["tg", 790, 200, 140, 100, "v", 4], ["you", 790, 350, 140, 140, "you", 5]],
      paths: [["M230,120 H270", 1], ["M450,120 C500,120 500,230 540,230", 1], ["M230,420 H270", 2], ["M450,420 C500,420 500,310 540,310", 2],
        ["M730,215 C760,215 760,100 790,100", 4], ["M730,265 H790", 4], ["M860,300 V350", 4], ["M790,450 C700,450 640,430 635,365", 5, "confirm"]],
    },
    tall: {
      vb: "0 0 480 800", cls: "tall",
      nodes: [["mac", 20, 20, 200, 100, "h2", 1], ["server", 260, 20, 200, 100, "h2", 2], ["agent", 20, 170, 200, 64, "h", 1], ["checks", 260, 170, 200, 64, "h", 2],
        ["hub", 145, 300, 190, 190, "hub", 0], ["dash", 20, 550, 190, 84, "h", 4], ["tg", 270, 550, 190, 84, "h", 4], ["you", 110, 690, 260, 96, "youh", 5]],
      paths: [["M120,120 V170", 1], ["M360,120 V170", 2], ["M120,234 C120,275 190,255 190,300", 1], ["M360,234 C360,275 290,255 290,300", 2],
        ["M190,490 C190,525 115,525 115,550", 4], ["M290,490 C290,525 365,525 365,550", 4], ["M365,634 C365,665 320,665 320,690", 4], ["M240,690 V490", 5, "confirm"]],
    },
  };

  function icon(name, x, y, k = 1) {
    return `<path class="ico" d="${ICON[name]}" transform="translate(${x} ${y}) scale(${k})"/>`;
  }
  function node([id, x, y, w, h, mode, from]) {
    const [label, sub] = TXT[id];
    const alertable = id === "dash" || id === "tg";
    const f = from ? ` f${from}` : "";
    let inner = "";
    const ic = id === "mac" ? "laptop" : id;
    if (mode === "hub") {
      inner += icon("hub", 16, 14, 1.25) + `<text class="nl" x="54" y="37">${label}</text><text class="ns" x="54" y="58">${sub}</text>`;
      const bars = [28, 36, 31, 44, 52, 84].map((v, i) => `<rect x="${18 + i * 26}" y="${126 - v * 0.6}" width="16" height="${v * 0.6}" rx="2.5" class="${i === 5 ? "hi" : ""}"/>`).join("");
      inner += `<g class="hub-bars">${bars}</g><line class="thr" x1="14" x2="${w - 14}" y1="81" y2="81"/>`;
      inner += `<text class="st-ok" x="16" y="${h - 14}">Looks fine</text><text class="st-bad" x="16" y="${h - 14}">Memory staying high</text>`;
    } else if (mode === "h" || mode === "h2") {
      inner += icon(ic, 14, h / 2 - 14, 1.15) + `<text class="nl" x="50" y="${h / 2 - 1}">${label}</text><text class="ns sub-a" x="50" y="${h / 2 + 20}">${sub}</text>`;
      if (alertable) inner += `<text class="ns sub-b" x="50" y="${h / 2 + 20}">${id === "dash" ? "1 problem" : "message sent"}</text><circle class="pulse" cx="${w - 18}" cy="18" r="4"/>`;
    } else if (mode === "you" || mode === "youh") {
      const wide = mode === "youh";
      inner += icon("you", 16, 16, 1.15) + `<text class="nl" x="${wide ? 52 : 16}" y="${wide ? 36 : 70}">${wide ? "You decide" : label}</text>`;
      if (!wide) inner += `<text class="ns" x="16" y="88">${sub}</text>`;
      const bx = wide ? w - 118 : 16, by = wide ? 28 : h - 38, bw = wide ? 100 : w - 32;
      inner += `<g class="confirm-btn"><rect x="${bx}" y="${by}" width="${bw}" height="32" rx="8"/><text x="${bx + bw / 2}" y="${by + 21}" text-anchor="middle">Confirm</text></g>`;
    } else {
      inner += icon(ic, 16, 14, 1.25) + `<text class="nl" x="16" y="${h - 40}">${label}</text><text class="ns sub-a" x="16" y="${h - 18}">${sub}</text>`;
      if (alertable) inner += `<text class="ns sub-b" x="16" y="${h - 18}">${id === "dash" ? "1 problem" : "message sent"}</text><circle class="pulse" cx="${w - 18}" cy="18" r="4"/>`;
    }
    return `<g class="node${f}${id === "hub" ? " hubn" : ""}${alertable ? " alertable" : ""}" transform="translate(${x} ${y})"><rect class="nbox" width="${w}" height="${h}" rx="16"/>${inner}</g>`;
  }
  function stageSvg(L) {
    const lines = L.paths.map(([d, from, kind]) => `<path class="line f${from}${kind ? " " + kind : ""}" d="${d}"/>`).join("");
    const pk = L.paths.filter(p => p[1] !== 3).map(([d, from, kind]) =>
      [0, 1].map(i => `<circle class="pk f${from}${kind ? " " + kind : ""}${i ? " d2" : ""}" r="5" style="offset-path:path('${d}')"/>`).join("")).join("");
    return `<svg class="${L.cls}" viewBox="${L.vb}" focusable="false" aria-hidden="true">${lines}${L.nodes.map(node).join("")}${pk}</svg>`;
  }

  const stage = document.getElementById("stage");
  const steps = [...document.querySelectorAll(".step")];
  const dots = document.getElementById("dots");
  if (stage && steps.length) {
    stage.innerHTML = stageSvg(LAYOUTS.wide) + stageSvg(LAYOUTS.tall);
    dots.innerHTML = steps.map((_, i) => `<button type="button" aria-label="Show step ${i + 1}"></button>`).join("");
    const btns = [...dots.children];
    let cur = 0, restart = () => {};
    const show = n => {
      if (n === cur) return;
      cur = n;
      for (let i = 1; i <= 5; i++) { stage.classList.toggle("s" + i, i <= n); stage.classList.toggle("c" + i, i === n); }
      steps.forEach((s, i) => s.classList.toggle("active", i + 1 === n));
      btns.forEach((b, i) => b.setAttribute("aria-current", String(i + 1 === n)));
    };
    btns.forEach((b, i) => b.addEventListener("click", () => { show(i + 1); restart(); }));

    if (reduce) {
      document.getElementById("how").classList.add("static-steps");
      show(5);
    } else {
      show(1);
      // desktop: the step nearest the middle of the screen is the current one
      const io = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting && !small.matches) show(+e.target.dataset.step); }),
        { rootMargin: "-45% 0px -45% 0px" });
      steps.forEach(s => io.observe(s));
      // mobile: no sticky scrolling, so cycle through the steps while the diagram is on screen
      let timer = null, visible = false;
      const tick = () => show(cur % 5 + 1);
      restart = () => {
        clearInterval(timer);
        if (small.matches && visible) timer = setInterval(tick, 4200);
      };
      new IntersectionObserver(es => { visible = es[0].isIntersecting; restart(); }, { threshold: 0.3 }).observe(stage);
      small.addEventListener("change", () => restart());
    }
  }
})();
