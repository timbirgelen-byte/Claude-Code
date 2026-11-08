/* J.A.R.V.I.S. – Oberfläche: Chat-Streaming, Sprache, Marktmonitor, Protokoll */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const chatEl = $("#chat"), inputEl = $("#input"), sendBtn = $("#send"), micBtn = $("#mic");
  const voiceBtn = $("#voice"), logEl = $("#log"), marketsEl = $("#markets"), sourcesEl = $("#sources");
  const quickEl = $("#quick"), stateLabel = $("#state-label");

  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* egal */ } },
  };

  let sessionId = null;
  let busy = false;
  let voiceOn = store.get("jarvis.voice", false);
  const seenSources = new Set();
  const marketCards = new Map();

  /* ---------------- Boot-Sequenz ---------------- */
  async function boot(info) {
    const lines = [
      "> INITIALISIERE KERNSYSTEME ............ OK",
      `> NEURONALER KERN: ${info?.model ?? "unbekannt"} ......... OK`,
      "> MODULE: WEBSUCHE · WEB-ANALYSE · MÄRKTE  OK",
      "> SPRACHSCHNITTSTELLE .................. " + (SpeechRec ? "OK" : "N/V"),
      info?.api_key === false ? "> WARNUNG: KEIN API-KEY GESETZT" : "> UPLINK ZUR CLAUDE API .............. OK",
      "> ALLE SYSTEME BEREIT. WILLKOMMEN, SIR.",
    ];
    const log = $("#boot-log"), bar = $(".boot-bar span"), bootEl = $("#boot");
    let skip = false;
    bootEl.addEventListener("click", () => (skip = true), { once: true });
    for (let i = 0; i < lines.length && !skip; i++) {
      log.textContent += lines[i] + "\n";
      bar.style.width = `${((i + 1) / lines.length) * 100}%`;
      await sleep(260);
    }
    await sleep(skip ? 0 : 350);
    bootEl.classList.add("done");
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  /* ---------------- Uhr & Status ---------------- */
  function tick() {
    const now = new Date();
    $("#clock").textContent = now.toLocaleTimeString("de-DE");
    $("#date").textContent = now.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" });
  }
  setInterval(tick, 1000); tick();

  const STATE_TEXT = {
    idle: "BEREIT", thinking: "ANALYSIERE", searching: "DURCHSUCHE NETZ",
    analyzing: "LADE MARKTDATEN", speaking: "ANTWORTE", listening: "HÖRE ZU",
  };
  function setState(s) {
    document.body.className = document.body.className.replace(/state-\S+/g, "").trim() + ` state-${s}`;
    stateLabel.textContent = STATE_TEXT[s] ?? s.toUpperCase();
  }

  /* ---------------- Protokoll ---------------- */
  function log(text, cls = "") {
    const li = document.createElement("li");
    if (cls) li.className = cls;
    const t = document.createElement("time");
    t.textContent = new Date().toLocaleTimeString("de-DE");
    li.append(t, document.createTextNode(text));
    logEl.append(li);
    while (logEl.children.length > 150) logEl.firstChild.remove();
    logEl.scrollTop = logEl.scrollHeight;
    return li;
  }

  /* ---------------- Markdown ---------------- */
  marked.setOptions({ gfm: true, breaks: false });
  function renderMarkdown(md) {
    const html = DOMPurify.sanitize(marked.parse(md), { ADD_ATTR: ["target"] });
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    tpl.content.querySelectorAll("a").forEach((a) => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
    tpl.content.querySelectorAll("table").forEach((t) => {
      const w = document.createElement("div"); w.className = "table-wrap";
      t.replaceWith(w); w.append(t);
    });
    return tpl.content;
  }

  /* ---------------- Nachrichten ---------------- */
  function addMessage(role, text = "") {
    const wrap = document.createElement("div");
    wrap.className = `msg ${role}`;
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = role === "user" ? "Sir" : "J.A.R.V.I.S.";
    const body = document.createElement("div");
    body.className = "body";
    wrap.append(who, body);
    if (role === "user") body.textContent = text;
    else {
      const activity = document.createElement("div"); activity.className = "activity";
      const content = document.createElement("div"); content.className = "content";
      body.append(activity, content);
      if (text) content.replaceChildren(renderMarkdown(text));
    }
    chatEl.append(wrap);
    scrollChat(true);
    return wrap;
  }
  function scrollChat(force = false) {
    const nearBottom = chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight < 140;
    if (force || nearBottom) chatEl.scrollTop = chatEl.scrollHeight;
  }

  const TOOL_LABEL = {
    web_search: "Websuche", web_fetch: "Seite lesen", market_data: "Marktdaten",
    symbol_search: "Tickersuche", crypto_overview: "Krypto",
  };

  /* ---------------- Senden & Streaming ---------------- */
  async function send(text) {
    text = text.trim();
    if (!text || busy) return;
    busy = true; sendBtn.disabled = true;
    quickEl.classList.add("hidden");
    stopSpeaking();
    addMessage("user", text);
    inputEl.value = ""; autoGrow();
    beep(880, 0.05);
    log(`Befehl: ${text.slice(0, 90)}${text.length > 90 ? "…" : ""}`);

    const msg = addMessage("bot");
    msg.classList.add("streaming");
    const content = msg.querySelector(".content"), activity = msg.querySelector(".activity");
    const chips = new Map();
    let md = "", thinkLi = null, thinkText = "", renderQueued = false;

    const render = () => {
      if (renderQueued) return;
      renderQueued = true;
      requestAnimationFrame(() => { renderQueued = false; content.replaceChildren(renderMarkdown(md)); scrollChat(); });
    };

    const handle = (ev) => {
      switch (ev.type) {
        case "session": sessionId = ev.id; break;
        case "status": setState(ev.state); break;
        case "thinking":
          thinkText += ev.text;
          if (!thinkLi) thinkLi = log("", "think");
          thinkLi.lastChild.textContent = "Überlegung: " + thinkText.slice(-280);
          break;
        case "text":
          thinkLi = null; thinkText = "";
          md += ev.text; render(); break;
        case "tool": {
          thinkLi = null; thinkText = "";
          const label = `${TOOL_LABEL[ev.name] ?? ev.name}${ev.detail ? ": " + ev.detail : ""}`;
          const chip = document.createElement("span");
          chip.className = "chip"; chip.textContent = label; chip.title = label;
          activity.append(chip); chips.set(ev.id, chip);
          log(`▸ ${label}`, "tool"); scrollChat();
          break;
        }
        case "tool_done": {
          const chip = chips.get(ev.id);
          if (chip) chip.classList.add(ev.ok ? "ok" : "err");
          if (!ev.ok) log(`✕ ${chip?.textContent ?? "Tool"} – ${ev.error ?? "fehlgeschlagen"}`, "err");
          else log(`✓ ${chip?.textContent ?? "Tool"}`, "ok");
          break;
        }
        case "sources": ev.items.forEach(addSource); break;
        case "market": addMarket(ev.data); break;
        case "error":
          md += `${md ? "\n\n" : ""}> ⚠ ${ev.message}`; render();
          msg.classList.add("error"); log(`Fehler: ${ev.message}`, "err");
          break;
        case "done": break;
      }
    };

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, session_id: sessionId }),
      });
      if (!res.ok || !res.body) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail ?? `HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
          for (const line of chunk.split("\n")) {
            if (line.startsWith("data: ")) {
              try { handle(JSON.parse(line.slice(6))); } catch (e) { console.warn("Event", e, line); }
            }
          }
        }
      }
    } catch (e) {
      handle({ type: "error", message: `Verbindung fehlgeschlagen: ${e.message}` });
    } finally {
      msg.classList.remove("streaming");
      if (!md) { content.replaceChildren(renderMarkdown("*Keine Antwort erhalten.*")); }
      busy = false; sendBtn.disabled = false;
      setState("idle");
      beep(660, 0.06);
      if (voiceOn && md && !msg.classList.contains("error")) speak(md);
      inputEl.focus({ preventScroll: true });
    }
  }

  /* ---------------- Quellen ---------------- */
  function addSource({ title, url }) {
    if (seenSources.has(url)) return;
    seenSources.add(url);
    sourcesEl.querySelector(".empty")?.remove();
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = title;
    const small = document.createElement("small");
    try { small.textContent = new URL(url).hostname.replace(/^www\./, ""); } catch { small.textContent = url; }
    li.append(a, small);
    sourcesEl.prepend(li);
    while (sourcesEl.children.length > 30) sourcesEl.lastChild.remove();
  }

  /* ---------------- Marktmonitor ---------------- */
  const fmt = (n, d = 2) => n == null ? "—" : Number(n).toLocaleString("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d });
  const pct = (n) => n == null ? "—" : `${n >= 0 ? "▲ +" : "▼ "}${fmt(n)} %`;

  function addMarket(d) {
    marketsEl.querySelector(".empty")?.remove();
    const key = `${d.symbol}|${d.range}`;
    marketCards.get(key)?.remove();
    const card = document.createElement("div");
    card.className = "mcard";
    const ch = d.change_pct ?? 0;
    card.innerHTML = `
      <div class="mcard-top"><span class="mcard-sym"></span><span class="mcard-price"></span></div>
      <div class="mcard-top"><span class="mcard-name"></span><span class="mcard-chg ${ch >= 0 ? "up" : "down"}"></span></div>
      <canvas></canvas>
      <div class="mcard-meta"><span class="m-range"></span><span class="m-rsi"></span><span class="m-vol"></span></div>`;
    card.querySelector(".mcard-sym").textContent = d.symbol;
    card.querySelector(".mcard-name").textContent = d.name;
    card.querySelector(".mcard-price").textContent = `${fmt(d.price, d.price < 10 ? 4 : 2)} ${d.currency ?? ""}`;
    card.querySelector(".mcard-chg").textContent = `${pct(d.change_pct)} (${d.range})`;
    card.querySelector(".m-range").textContent = `Tag ${pct(d.day_change_pct)}`;
    card.querySelector(".m-rsi").textContent = `RSI ${fmt(d.rsi14, 1)}`;
    card.querySelector(".m-vol").textContent = `Vol ${fmt(d.volatility, 1)} %`;
    marketsEl.prepend(card);
    marketCards.set(key, card);
    while (marketsEl.children.length > 8) {
      const last = marketsEl.lastChild;
      for (const [k, v] of marketCards) if (v === last) marketCards.delete(k);
      last.remove();
    }
    requestAnimationFrame(() => drawChart(card.querySelector("canvas"), d.series, ch >= 0));
  }

  function drawChart(canvas, series, up) {
    if (!series?.length) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    canvas.width = w * dpr; canvas.height = h * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    const ys = series.map((p) => p[1]);
    const min = Math.min(...ys), max = Math.max(...ys), span = max - min || 1;
    const x = (i) => (i / (series.length - 1)) * (w - 2) + 1;
    const y = (v) => h - 4 - ((v - min) / span) * (h - 10);

    ctx.strokeStyle = "rgba(62,230,255,.08)"; ctx.lineWidth = 1;
    for (let i = 1; i < 4; i++) { ctx.beginPath(); ctx.moveTo(0, (h / 4) * i); ctx.lineTo(w, (h / 4) * i); ctx.stroke(); }

    const color = up ? "69,255,179" : "255,84,104";
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, `rgba(${color},.32)`); grad.addColorStop(1, `rgba(${color},0)`);
    ctx.beginPath();
    series.forEach((p, i) => (i ? ctx.lineTo(x(i), y(p[1])) : ctx.moveTo(x(i), y(p[1]))));
    ctx.lineTo(x(series.length - 1), h); ctx.lineTo(x(0), h); ctx.closePath();
    ctx.fillStyle = grad; ctx.fill();

    ctx.beginPath();
    series.forEach((p, i) => (i ? ctx.lineTo(x(i), y(p[1])) : ctx.moveTo(x(i), y(p[1]))));
    ctx.strokeStyle = `rgb(${color})`; ctx.lineWidth = 1.6; ctx.shadowColor = `rgb(${color})`; ctx.shadowBlur = 6;
    ctx.stroke();
    ctx.shadowBlur = 0;
    const lx = x(series.length - 1), ly = y(ys[ys.length - 1]);
    ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(lx - 1, ly, 2.4, 0, Math.PI * 2); ctx.fill();
  }

  /* ---------------- Sprachausgabe ---------------- */
  const synth = window.speechSynthesis;
  let germanVoice = null;
  function pickVoice() {
    if (!synth) return;
    const voices = synth.getVoices().filter((v) => v.lang?.toLowerCase().startsWith("de"));
    germanVoice = voices.find((v) => /male|männ|daniel|markus|yannick|conrad|google/i.test(v.name)) ?? voices[0] ?? null;
  }
  if (synth) { pickVoice(); synth.onvoiceschanged = pickVoice; }

  function plainText(md) {
    return md
      .replace(/```[\s\S]*?```/g, " ")
      .replace(/^\|.*\|$/gm, " ")
      .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
      .replace(/[#>*_`~|]/g, "")
      .replace(/https?:\/\/\S+/g, "")
      .replace(/\n{2,}/g, ". ").replace(/\n/g, " ")
      .replace(/\s{2,}/g, " ").trim();
  }
  function speak(md) {
    if (!synth) return;
    let text = plainText(md);
    if (text.length > 900) text = text.slice(0, 900).replace(/[^.!?]*$/, "") || text.slice(0, 900);
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "de-DE"; u.rate = 1.03; u.pitch = 0.9;
    if (germanVoice) u.voice = germanVoice;
    u.onstart = () => setState("speaking");
    u.onend = u.onerror = () => { if (!busy) setState("idle"); };
    synth.cancel(); synth.speak(u);
  }
  function stopSpeaking() { if (synth?.speaking) synth.cancel(); }

  function updateVoiceUi() {
    voiceBtn.classList.toggle("active", voiceOn);
    const el = $("#tts-state");
    el.textContent = !synth ? "N/V" : voiceOn ? "AKTIV" : "AUS";
    el.className = voiceOn ? "ok" : "";
  }
  voiceBtn.addEventListener("click", () => {
    voiceOn = !voiceOn; store.set("jarvis.voice", voiceOn);
    if (!voiceOn) stopSpeaking();
    updateVoiceUi();
  });
  if (!synth) voiceBtn.disabled = true;
  updateVoiceUi();

  /* ---------------- Spracheingabe ---------------- */
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  let rec = null, listening = false;
  if (SpeechRec) {
    rec = new SpeechRec();
    rec.lang = "de-DE"; rec.interimResults = true; rec.continuous = false;
    rec.onresult = (e) => {
      let txt = "";
      for (const r of e.results) txt += r[0].transcript;
      inputEl.value = txt; autoGrow();
    };
    rec.onend = () => {
      listening = false; micBtn.classList.remove("active");
      if (!busy) setState("idle");
      if (inputEl.value.trim()) send(inputEl.value);
    };
    rec.onerror = (e) => { log(`Spracheingabe: ${e.error}`, "err"); };
    $("#stt-state").textContent = "BEREIT"; $("#stt-state").className = "ok";
  } else {
    micBtn.disabled = true; micBtn.title = "Spracheingabe wird von diesem Browser nicht unterstützt";
    $("#stt-state").textContent = "N/V"; $("#stt-state").className = "warn";
  }
  function toggleMic() {
    if (!rec || busy) return;
    if (listening) { rec.stop(); return; }
    stopSpeaking();
    inputEl.value = "";
    try { rec.start(); listening = true; micBtn.classList.add("active"); setState("listening"); beep(1040, 0.04); }
    catch (e) { log(`Mikrofon: ${e.message}`, "err"); }
  }
  micBtn.addEventListener("click", toggleMic);

  /* ---------------- UI-Töne ---------------- */
  let audioCtx = null;
  function beep(freq, dur) {
    if (!voiceOn) return;
    try {
      audioCtx ??= new (window.AudioContext || window.webkitAudioContext)();
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.type = "sine"; o.frequency.value = freq;
      g.gain.setValueAtTime(0.05, audioCtx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + dur);
      o.connect(g).connect(audioCtx.destination); o.start(); o.stop(audioCtx.currentTime + dur);
    } catch { /* ohne Ton weiter */ }
  }

  /* ---------------- Eingabe ---------------- */
  function autoGrow() { inputEl.style.height = "auto"; inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + "px"; }
  inputEl.addEventListener("input", autoGrow);
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(inputEl.value); }
  });
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "m") { e.preventDefault(); toggleMic(); }
    if (e.key === "Escape") stopSpeaking();
  });
  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); send(inputEl.value); });
  quickEl.addEventListener("click", (e) => { const q = e.target.closest("button")?.dataset.q; if (q) send(q); });

  $("#reset").addEventListener("click", async () => {
    if (busy) return;
    stopSpeaking();
    if (sessionId) fetch("/api/reset", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: sessionId }) });
    sessionId = null;
    chatEl.replaceChildren();
    quickEl.classList.remove("hidden");
    log("Neue Sitzung gestartet.");
    greet();
  });

  function greet() {
    const h = new Date().getHours();
    const tod = h < 5 ? "Guten Abend" : h < 11 ? "Guten Morgen" : h < 18 ? "Guten Tag" : "Guten Abend";
    addMessage("bot", `${tod}, Sir. Alle Systeme sind online.\n\nIch kann für Sie **im Web recherchieren**, **Nachrichten zusammenfassen** und **Marktanalysen** mit Live-Kursdaten erstellen – für Aktien, Indizes, Währungen und Krypto. Tippen Sie einen Befehl oder nutzen Sie das Mikrofon.`);
  }

  /* ---------------- Start ---------------- */
  (async () => {
    let info = null;
    try { info = await (await fetch("/api/info")).json(); } catch { /* offline */ }
    const up = $("#uplink");
    up.textContent = info ? (info.api_key ? "ONLINE" : "KEIN KEY") : "OFFLINE";
    up.classList.add(info?.api_key ? "on" : "off");
    $("#model").textContent = info ? `${info.model.toUpperCase()} · ${info.effort.toUpperCase()}` : "—";
    log(`Kern verbunden: ${info?.model ?? "?"}`);
    if (info && !info.api_key) log("ANTHROPIC_API_KEY fehlt – siehe README.", "err");
    greet();
    await boot(info);
    inputEl.focus({ preventScroll: true });
  })();
})();
