// The player pages. Every number that matters comes from the server: the
// reels only look like they are spinning while the server already knows the
// result, and nothing here decides a win, a balance or a hold.

import {
  api, ApiError, clear, el, formValues, num, onLangChange, pct, pick,
  roleName, shortTime, t, toggleLang,
} from "/common.js";
import { BANNERS, SITE, SYMBOLS, TICKER } from "/strings.js";

const $ = (id) => document.getElementById(id);
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const state = {
  me: null,
  balance: null,
  game: null, // the open game's rules, or null
  credits: null,
  bet: null,
  lastSpin: null,
  held: new Set(),
  busy: false,
  sound: false,
  bannerIndex: 0,
};

// ---------------------------------------------------------------------------
// Chrome: ticker, banners, header, footer, support
// ---------------------------------------------------------------------------

function renderChrome() {
  clear($("site-name"), SITE.nameParts.map(([initial, rest]) => [el("span", { class: "initial", text: initial }), rest]));
  $("site-name-zh").textContent = SITE.nameZh;
  $("tagline").textContent = t("tagline");
  $("lang-toggle").textContent = t("langToggle");
  $("sound-toggle").textContent = state.sound ? t("soundOn") : t("soundOff");
  $("support-button").textContent = t("supportButton");
  $("support-bubble").textContent = t("support");
  clear($("footer"), el("div", { class: "age-badge", text: "18+" }), el("div", { text: t("footer") }));
  renderTicker();
  renderBanner();
  renderNav();
}

function renderTicker() {
  const items = shuffle([...pick(TICKER)]);
  // Twice over, so the scroll can loop at the halfway point without a jump.
  clear($("ticker"), [...items, ...items].map((text) => el("span", { text })));
}

function renderBanner() {
  const banners = pick(BANNERS);
  const [headline, subline] = banners[state.bannerIndex % banners.length];
  clear($("banner"), el("div", { class: "fade-in" },
    el("div", { class: "headline", text: headline }),
    el("div", { class: "subline", text: subline })));
}

function shuffle(items) {
  // Cosmetic only: Math.random is fine for ordering jokes, never for a spin.
  for (let i = items.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [items[i], items[j]] = [items[j], items[i]];
  }
  return items;
}

function renderNav() {
  const nav = $("nav");
  if (!state.me || !state.me.signed_in || state.me.must_change_password) {
    clear(nav);
    return;
  }
  const view = currentView();
  const item = (name, key, extra = "") =>
    el("button", { type: "button", class: `${view === name ? "active" : ""} ${extra}`, text: t(key), onclick: () => go(name) });
  clear(nav,
    item("lobby", "navLobby"),
    state.game ? item("play", "navPlay", "attention") : null,
    item("wallet", "navWallet"),
    item("stats", "navStats"),
    item("account", "navAccount"),
    el("span", { class: "spacer" }),
    el("span", { class: "wallet-chip", text: `${t("balance")} ${state.balance ? state.balance.text : "…"}` }),
    el("button", { type: "button", text: t("navLogout"), onclick: logout }));
}

function toast(message, ok = false) {
  const box = $("toast");
  box.textContent = message;
  box.className = `toast show ${ok ? "ok" : "bad"}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.className = "toast"; }, 3600);
}

// ---------------------------------------------------------------------------
// Sound: a synthesised "ding", off until the player turns it on
// ---------------------------------------------------------------------------

let audio = null;

function ding(notes = [880, 1320]) {
  if (!state.sound) return;
  try {
    audio = audio || new AudioContext();
    notes.forEach((frequency, index) => {
      const start = audio.currentTime + index * 0.09;
      const osc = audio.createOscillator();
      const gain = audio.createGain();
      osc.type = "triangle";
      osc.frequency.value = frequency;
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.25, start + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.35);
      osc.connect(gain).connect(audio.destination);
      osc.start(start);
      osc.stop(start + 0.4);
    });
  } catch {
    // No audio on this browser; the game does not need it.
  }
}

function coinRain(count) {
  if (reducedMotion) return;
  const layer = $("coins");
  for (let i = 0; i < count; i += 1) {
    const coin = el("span", { class: "coin", text: i % 5 === 0 ? "💰" : "🪙" });
    coin.style.left = `${Math.random() * 100}vw`;
    coin.style.animationDuration = `${1.4 + Math.random() * 1.4}s`;
    coin.style.animationDelay = `${Math.random() * 0.6}s`;
    layer.append(coin);
    setTimeout(() => coin.remove(), 3600);
  }
}

function bigWinFlash() {
  const flash = el("div", { class: "big-win", text: t("bigWin") });
  document.body.append(flash);
  setTimeout(() => flash.remove(), 1900);
}

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------

const VIEWS = { login: viewLogin, change: viewChange, lobby: viewLobby, wallet: viewWallet, play: viewPlay, stats: viewStats, account: viewAccount };

function currentView() {
  const name = location.hash.replace(/^#\/?/, "");
  return name in VIEWS ? name : "lobby";
}

function go(name) {
  if (location.hash === `#/${name}`) route();
  else location.hash = `#/${name}`;
}

async function route() {
  const me = state.me;
  let view = currentView();
  if (!me || !me.signed_in) view = "login";
  else if (me.must_change_password) view = "change";
  else if (me.role !== "player") {
    location.href = "/admin.html";
    return;
  }
  renderNav();
  const main = $("main");
  clear(main, el("p", { class: "muted", text: t("loading") }));
  try {
    await VIEWS[view](main);
  } catch (error) {
    handleError(error);
  }
  renderNav();
}

function handleError(error) {
  if (error instanceof ApiError && error.status === 401) {
    state.me = { signed_in: false };
    toast(t("sessionExpired"));
    go("login");
    return;
  }
  toast(error instanceof ApiError ? error.message : t("errorGeneric"));
}

async function act(work) {
  try {
    return await work();
  } catch (error) {
    handleError(error);
    return undefined;
  }
}

async function refreshPlayer() {
  if (!state.me || !state.me.signed_in || state.me.role !== "player" || state.me.must_change_password) return;
  const [wallet, current] = await Promise.all([api.post("/api/player/wallet"), api.post("/api/player/game")]);
  state.balance = wallet.balance;
  state.game = current.game;
  state.credits = current.game ? current.credits : null;
  return wallet;
}

async function logout() {
  await act(async () => {
    const reply = await api.post("/api/logout");
    if (reply.cashed_out) toast(t("loggedOutCashed", { amount: reply.cashed_out.text }), true);
    state.me = { signed_in: false };
    state.balance = null;
    state.game = null;
    api.csrf = null;
    go("login");
  });
}

// ---------------------------------------------------------------------------
// Views: signing in
// ---------------------------------------------------------------------------

function viewLogin(main) {
  const login = el("form", { class: "stack", onsubmit: async (event) => {
    event.preventDefault();
    const values = formValues(event.target);
    await act(async () => {
      state.me = await api.post("/api/login", { username: values.username, password: values.password });
      await refreshPlayer();
      go(state.game ? "play" : "lobby");
    });
  } },
  field(t("username"), { name: "username", autocomplete: "username", maxlength: 32, required: true }),
  field(t("password"), { name: "password", type: "password", autocomplete: "current-password", maxlength: 1024, required: true }),
  el("button", { class: "btn", type: "submit", text: t("loginButton") }),
  el("p", { class: "muted", text: t("staffHint") }));

  const register = el("form", { class: "stack", onsubmit: async (event) => {
    event.preventDefault();
    const values = formValues(event.target);
    if (values.password !== values.confirm) {
      toast(t("passwordsDiffer"));
      return;
    }
    await act(async () => {
      await api.post("/api/register", { username: values.username, password: values.password });
      event.target.reset();
      toast(t("registered"), true);
    });
  } },
  field(t("username"), { name: "username", autocomplete: "username", maxlength: 32, required: true }, t("usernameHint")),
  field(t("password"), { name: "password", type: "password", autocomplete: "new-password", maxlength: 1024, required: true }, t("passwordHint")),
  field(t("confirmPassword"), { name: "confirm", type: "password", autocomplete: "new-password", maxlength: 1024, required: true }),
  el("button", { class: "btn red", type: "submit", text: t("registerButton") }));

  clear(main, el("div", { class: "grid" },
    el("div", { class: "card" }, el("h2", { text: t("loginTitle") }), login),
    el("div", { class: "card" }, el("h2", { text: t("registerTitle") }), register)));
}

function field(label, attrs, hint) {
  return el("label", {}, label, el("input", attrs), hint ? el("span", { class: "muted", text: hint }) : null);
}

function changeForm(after) {
  return el("form", { class: "stack", onsubmit: async (event) => {
    event.preventDefault();
    const values = formValues(event.target);
    if (values.new !== values.confirm) {
      toast(t("passwordsDiffer"));
      return;
    }
    await act(async () => {
      state.me = await api.post("/api/password", { current: values.current, new: values.new });
      event.target.reset();
      toast(t("changed"), true);
      after();
    });
  } },
  field(t("currentPassword"), { name: "current", type: "password", autocomplete: "current-password", maxlength: 1024, required: true }),
  field(t("newPassword"), { name: "new", type: "password", autocomplete: "new-password", maxlength: 1024, required: true }, t("passwordHint")),
  field(t("confirmPassword"), { name: "confirm", type: "password", autocomplete: "new-password", maxlength: 1024, required: true }),
  el("button", { class: "btn", type: "submit", text: t("changeButton") }));
}

function viewChange(main) {
  clear(main, el("div", { class: "card" },
    el("h2", { text: t("changeTitle") }),
    el("p", { class: "notice", text: t("mustChange") }),
    changeForm(async () => {
      if (state.me.role !== "player") location.href = "/admin.html";
      else {
        await refreshPlayer();
        go("lobby");
      }
    })));
}

// ---------------------------------------------------------------------------
// Lobby
// ---------------------------------------------------------------------------

async function viewLobby(main) {
  await refreshPlayer();
  const { games } = await api.post("/api/player/games");
  const parts = [el("h2", { class: "section-title", text: t("lobbyTitle") })];
  if (state.game) {
    parts.push(el("div", { class: "notice row" },
      el("strong", { text: `${t("resumeTitle")}: ${state.game.name} · ${t("credits")} ${state.credits.text}` }),
      el("button", { class: "btn small", type: "button", text: t("resumeButton"), onclick: () => go("play") })));
  }
  if (!games.length) parts.push(el("p", { class: "muted", text: t("lobbyEmpty") }));
  parts.push(el("div", { class: "grid" }, games.map(gameCard)));
  clear(main, parts);
}

function gameCard(game) {
  const buyIn = el("div");
  const rtpText = game.rtp_with_holds ? `${pct(game.rtp)} · ${t("rtpHolds")} ${pct(game.rtp_with_holds)}` : pct(game.rtp);
  const open = () => {
    const suggested = game.bets[0].cents * 20;
    clear(buyIn, el("form", { class: "stack", onsubmit: async (event) => {
      event.preventDefault();
      const values = formValues(event.target);
      await act(async () => {
        await api.post("/api/player/buyin", { game_id: game.id, amount: values.amount });
        state.lastSpin = null;
        state.held.clear();
        state.bet = null;
        go("play");
      });
    } },
    el("h3", { text: t("buyInTitle") }),
    field(t("buyInAmount"), { name: "amount", inputmode: "decimal", value: (suggested / 100).toFixed(2), maxlength: 16, required: true }),
    el("div", { class: "row" },
      el("button", { class: "btn", type: "submit", text: t("buyInButton") }),
      el("button", { class: "btn ghost", type: "button", text: t("cancel"), onclick: () => clear(buyIn) }))));
  };
  return el("div", { class: "card game-card" },
    el("h2", { text: game.name }),
    el("div", { class: "symbol-strip", text: game.symbols.map(icon).join(" ") }),
    el("div", {},
      el("span", { class: "tag", text: `${game.reels}×${game.rows}` }),
      el("span", { class: "tag", text: `${game.paylines.length} ${t("lines")}` }),
      el("span", { class: game.holds.enabled ? "tag" : "tag off", text: game.holds.enabled ? t("holdsYes") : t("holdsNo") })),
    el("dl", {},
      el("dt", { text: t("bets") }), el("dd", { text: game.bets.map((b) => b.text).join(" / ") }),
      el("dt", { text: t("rtp") }), el("dd", { text: rtpText })),
    state.game
      ? null
      : el("button", { class: "btn red", type: "button", text: t("playNow"), onclick: open }),
    buyIn);
}

// ---------------------------------------------------------------------------
// The machine
// ---------------------------------------------------------------------------

function icon(symbol) {
  return SYMBOLS[symbol] || symbol;
}

function symbolNode(symbol) {
  if (symbol === "BAR") return el("span", { class: "sym-bar", text: "BAR" });
  if (symbol === "SEVEN") return el("span", { class: "sym-seven", text: "7" });
  if (SYMBOLS[symbol]) return el("span", { text: SYMBOLS[symbol] });
  return el("span", { class: "sym-other", text: symbol });
}

async function viewPlay(main) {
  await refreshPlayer();
  const game = state.game;
  if (!game) {
    go("lobby");
    return;
  }
  if (!state.bet || !game.bets.some((b) => b.cents === state.bet.cents)) state.bet = game.bets[0];

  const credits = el("div", { class: "led", text: state.credits.text });
  const reels = el("div", { class: `reels rows-${game.rows}` });
  reels.style.gridTemplateColumns = `repeat(${game.reels}, 1fr)`;
  const columns = [];
  for (let r = 0; r < game.reels; r += 1) {
    const cells = [];
    for (let row = 0; row < game.rows; row += 1) cells.push(el("div", { class: "cell" }, symbolNode(game.symbols[(r + row) % game.symbols.length])));
    const column = el("div", { class: "reel" }, cells);
    columns.push({ column, cells });
    reels.append(column);
  }

  const holdRow = el("div", { class: "hold-row" });
  holdRow.style.gridTemplateColumns = `repeat(${game.reels}, 1fr)`;
  const result = el("div", { class: "result", "aria-live": "polite" });
  const bets = el("div", { class: "bets" });
  const spinButton = el("button", { class: "btn red spin-button", type: "button", text: t("spin") });
  const respinButton = el("button", { class: "btn", type: "button" });
  const cashButton = el("button", { class: "btn ghost", type: "button", text: t("cashOut") });

  if (state.lastSpin) paint(state.lastSpin.grid, []);

  function paint(grid, wins) {
    columns.forEach(({ cells }, r) => {
      cells.forEach((cell, row) => {
        clear(cell, symbolNode(grid[row][r]));
        cell.classList.remove("win");
      });
    });
    for (const win of wins) {
      const line = game.paylines[win.line];
      for (let r = 0; r < win.count; r += 1) columns[r].cells[line[r]].classList.add("win");
    }
  }

  function renderBets() {
    clear(bets, game.bets.map((b) => el("button", {
      type: "button",
      class: b.cents === state.bet.cents ? "on" : "",
      text: b.text,
      onclick: () => { state.bet = b; renderBets(); refreshButtons(); },
    })));
  }

  function canHold() {
    return game.holds.enabled && state.lastSpin && state.lastSpin.holds_left > 0 && state.lastSpin.respin_cost;
  }

  function renderHolds() {
    if (!game.holds.enabled) {
      clear(holdRow);
      return;
    }
    clear(holdRow, columns.map((_, r) => el("button", {
      type: "button",
      class: state.held.has(r) ? "on" : "",
      text: state.held.has(r) ? t("holding") : t("holdReel"),
      disabled: !canHold() || state.busy,
      onclick: () => {
        if (state.held.has(r)) state.held.delete(r);
        else if (state.held.size < game.holds.max_reels_held) state.held.add(r);
        renderHolds();
        refreshButtons();
      },
    })));
    columns.forEach(({ column }, r) => column.classList.toggle("held", state.held.has(r)));
  }

  function refreshButtons() {
    const cents = state.credits ? state.credits.cents : 0;
    spinButton.disabled = state.busy || cents < state.bet.cents;
    cashButton.disabled = state.busy;
    if (canHold()) {
      respinButton.hidden = false;
      respinButton.textContent = t("respin", { cost: state.lastSpin.respin_cost.text });
      respinButton.disabled = state.busy || state.held.size === 0 || cents < state.lastSpin.respin_cost.cents;
    } else {
      respinButton.hidden = true;
    }
  }

  function showOutcome(outcome) {
    const parts = [];
    if (outcome.payout.cents > 0) {
      parts.push(el("span", { class: "win-text", text: t("win", { amount: outcome.payout.text }) }));
      for (const w of outcome.wins) {
        parts.push(el("span", { class: "line", text: t("lineWin", { line: w.line + 1, count: w.count, symbol: icon(w.symbol), amount: w.payout.text }) }));
      }
      const big = outcome.payout.cents >= outcome.charged.cents * 10;
      coinRain(big ? 60 : 18);
      ding(big ? [784, 988, 1175, 1568] : [880, 1320]);
      if (big) bigWinFlash();
    } else {
      parts.push(el("span", { text: t("noWin") }));
    }
    if (canHold()) parts.push(el("span", { class: "line", text: `${t("holdsLeft", { n: outcome.holds_left })} · ${t("holdHelp", { n: game.holds.max_reels_held })}` }));
    if (outcome.credits.cents < game.bets[0].cents) parts.push(el("span", { class: "line", text: t("outOfCredits") }));
    clear(result, parts);
  }

  async function play(path, body, spinning) {
    state.busy = true;
    refreshButtons();
    renderHolds();
    const started = performance.now();
    const timer = setInterval(() => {
      for (const r of spinning) {
        for (const cell of columns[r].cells) clear(cell, symbolNode(game.symbols[Math.floor(Math.random() * game.symbols.length)]));
      }
    }, 70);
    for (const r of spinning) columns[r].column.classList.add("spinning");
    clear(result);
    try {
      const outcome = await api.post(path, body);
      const wait = Math.max(0, (reducedMotion ? 0 : 650) - (performance.now() - started));
      await new Promise((resolve) => setTimeout(resolve, wait));
      clearInterval(timer);
      // Stop the reels one after another, left to right.
      for (const r of spinning) {
        columns[r].column.classList.remove("spinning");
        columns[r].cells.forEach((cell, row) => clear(cell, symbolNode(outcome.grid[row][r])));
        if (!reducedMotion) await new Promise((resolve) => setTimeout(resolve, 160));
      }
      paint(outcome.grid, outcome.wins);
      state.lastSpin = outcome;
      state.credits = outcome.credits;
      state.held.clear();
      credits.textContent = outcome.credits.text;
      showOutcome(outcome);
    } catch (error) {
      clearInterval(timer);
      for (const r of spinning) columns[r].column.classList.remove("spinning");
      if (state.lastSpin) paint(state.lastSpin.grid, []);
      handleError(error);
    } finally {
      state.busy = false;
      renderHolds();
      refreshButtons();
    }
  }

  spinButton.addEventListener("click", () => {
    state.held.clear();
    play("/api/player/spin", { bet: (state.bet.cents / 100).toFixed(2) }, columns.map((_, r) => r));
  });
  respinButton.addEventListener("click", () => {
    const held = [...state.held].sort((a, b) => a - b);
    play("/api/player/hold", { reels: held }, columns.map((_, r) => r).filter((r) => !state.held.has(r)));
  });
  cashButton.addEventListener("click", () => act(async () => {
    state.busy = true;
    refreshButtons();
    const done = await api.post("/api/player/cashout");
    state.busy = false;
    state.lastSpin = null;
    state.held.clear();
    toast(t("cashedOut", { amount: done.amount.text, net: done.net.text, spins: done.spins }), true);
    if (done.net.cents > 0) coinRain(30);
    go("lobby");
  }).finally(() => { state.busy = false; }));

  const paytable = el("details", { class: "paytable" },
    el("summary", { text: t("paytable") }),
    el("div", { class: "table-wrap" }, el("table", {},
      el("tbody", {}, game.paytable
        .slice()
        .sort((a, b) => b.multiplier - a.multiplier)
        .map((rule) => el("tr", {},
          el("td", { text: `${rule.count} × ${icon(rule.symbol)}` }),
          el("td", { class: "num", text: `${rule.multiplier}×` })))))),
    el("p", { class: "muted", text: `${t("rtp")}: ${pct(game.rtp)}${game.rtp_with_holds ? ` · ${t("rtpHolds")} ${pct(game.rtp_with_holds)}` : ""}` }));

  renderBets();
  renderHolds();
  refreshButtons();

  clear(main, el("div", { class: "machine" }, el("div", { class: "machine-inner" },
    el("div", { class: "machine-head" },
      el("div", { class: "machine-title", text: `🎰 ${game.name}` }),
      el("div", {}, el("span", { class: "muted", text: `${t("credits")} ` }), credits)),
    reels,
    holdRow,
    result,
    el("div", { class: "muted", text: t("bet") }),
    bets,
    el("div", { class: "actions" }, spinButton, respinButton, cashButton),
    paytable)));
}

// ---------------------------------------------------------------------------
// Wallet
// ---------------------------------------------------------------------------

async function viewWallet(main) {
  const wallet = await refreshPlayer();
  const methods = wallet.methods;
  const methodSelect = () => el("select", { name: "method_id", required: true },
    methods.map((m) => el("option", { value: m.id, text: `${m.brand} •••• ${m.last4} (${m.exp})` })));

  const moneyForm = (path, buttonKey, doneKey, red) => el("form", { class: "stack", onsubmit: async (event) => {
    event.preventDefault();
    if (!methods.length) {
      toast(t("chooseCard"));
      return;
    }
    const values = formValues(event.target);
    await act(async () => {
      const reply = await api.post(path, { method_id: Number.parseInt(values.method_id, 10), amount: values.amount });
      toast(t(doneKey, { balance: reply.balance.text }), true);
      if (path.endsWith("deposit")) coinRain(12);
      route();
    });
  } },
  el("label", {}, t("method"), methodSelect()),
  field(t("amount"), { name: "amount", inputmode: "decimal", placeholder: "100.00", maxlength: 16, required: true }),
  el("button", { class: red ? "btn red" : "btn", type: "submit", text: t(buttonKey) }));

  const thisYear = new Date().getFullYear();
  const addForm = el("form", { class: "stack", onsubmit: async (event) => {
    event.preventDefault();
    const values = formValues(event.target);
    await act(async () => {
      const added = await api.post("/api/player/cards/add", {
        number: values.number.replace(/[\s-]/g, ""),
        exp_month: Number.parseInt(values.exp_month, 10),
        exp_year: Number.parseInt(values.exp_year, 10),
        cvv: values.cvv,
      });
      toast(t("cardAdded", added), true);
      route();
    });
  } },
  field(t("cardNumber"), { name: "number", inputmode: "numeric", autocomplete: "cc-number", maxlength: 23, placeholder: "4242 4242 4242 4242", required: true }),
  el("div", { class: "row" },
    el("label", {}, t("expMonth"), el("select", { name: "exp_month", autocomplete: "cc-exp-month" },
      Array.from({ length: 12 }, (_, i) => el("option", { value: i + 1, text: String(i + 1).padStart(2, "0") })))),
    el("label", {}, t("expYear"), el("select", { name: "exp_year", autocomplete: "cc-exp-year" },
      Array.from({ length: 10 }, (_, i) => el("option", { value: thisYear + i, text: thisYear + i, selected: i === 3 })))),
    field(t("cvv"), { name: "cvv", inputmode: "numeric", autocomplete: "cc-csc", maxlength: 4, required: true })),
  el("p", { class: "muted", text: t("testCardHint") }),
  el("button", { class: "btn", type: "submit", text: t("addCardButton") }));

  clear(main,
    el("h2", { class: "section-title", text: t("walletTitle") }),
    el("div", { class: "grid" },
      el("div", { class: "card" },
        el("h3", { text: t("balance") }),
        el("div", { class: "big-number", text: wallet.balance.text }),
        el("p", { class: "muted", text: t("limits", { deposit: wallet.limits.deposit.text, withdraw: wallet.limits.withdraw.text }) })),
      el("div", { class: "card" },
        el("h3", { text: t("cards") }),
        methods.length
          ? el("ul", { class: "list" }, methods.map((m) => el("li", {},
            el("span", { text: `${m.brand} •••• ${m.last4} · ${m.exp}` }),
            el("button", { class: "btn small ghost", type: "button", text: t("remove"), onclick: () => act(async () => {
              await api.post("/api/player/cards/remove", { id: m.id });
              route();
            }) }))))
          : el("p", { class: "muted", text: t("noCards") })),
      el("div", { class: "card" }, el("h3", { text: t("deposit") }), moneyForm("/api/player/deposit", "depositButton", "deposited", true)),
      el("div", { class: "card" }, el("h3", { text: t("withdraw") }), moneyForm("/api/player/withdraw", "withdrawButton", "withdrew", false)),
      el("div", { class: "card" }, el("h3", { text: t("addCard") }), addForm)));
}

// ---------------------------------------------------------------------------
// Statistics and account
// ---------------------------------------------------------------------------

function signed(value) {
  return el("span", { class: value.cents > 0 ? "up" : value.cents < 0 ? "down" : "", text: value.text });
}

async function viewStats(main) {
  await refreshPlayer();
  const s = await api.post("/api/player/stats");
  const tile = (key, value) => el("div", { class: "tile" }, el("div", { class: "label", text: t(key) }), el("div", { class: "value" }, value));
  const parts = [
    el("h2", { class: "section-title", text: t("statsTitle") }),
    el("div", { class: "tiles" },
      tile("statSessions", num(s.sessions)),
      tile("statSpins", num(s.spins)),
      tile("statWagered", s.wagered.text),
      tile("statWon", s.won.text),
      tile("statNet", signed(s.net)),
      tile("statWinRate", pct(s.win_rate, 1)),
      tile("statBiggest", s.biggest_payout.text),
      tile("statTotalLost", s.total_lost.text),
      tile("statFavourite", s.favourite_game || "—")),
  ];
  if (!s.spins) parts.push(el("p", { class: "muted", text: t("noStats") }));
  if (s.by_game.length) {
    parts.push(el("div", { class: "card" }, el("h3", { text: t("byGame") }), el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", { text: t("game") }), el("th", { class: "num", text: t("spins") }), el("th", { class: "num", text: t("staked") }), el("th", { class: "num", text: t("paid") }))),
      el("tbody", {}, s.by_game.map((g) => el("tr", {},
        el("td", { text: g.name }), el("td", { class: "num", text: num(g.spins) }), el("td", { class: "num", text: g.wagered.text }), el("td", { class: "num", text: g.won.text }))))))));
  }
  if (s.recent_sessions.length) {
    parts.push(el("div", { class: "card" }, el("h3", { text: t("recentSessions") }), el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {},
        el("th", { text: t("game") }), el("th", { text: t("started") }), el("th", { text: t("ended") }),
        el("th", { class: "num", text: t("spins") }), el("th", { class: "num", text: t("staked") }), el("th", { class: "num", text: t("paid") }), el("th", { class: "num", text: t("net") }))),
      el("tbody", {}, s.recent_sessions.map((r) => el("tr", {},
        el("td", { text: r.name }), el("td", { text: shortTime(r.started_at) }), el("td", { text: r.ended_at ? shortTime(r.ended_at) : t("open") }),
        el("td", { class: "num", text: num(r.spins) }), el("td", { class: "num", text: r.staked.text }), el("td", { class: "num", text: r.paid.text }),
        el("td", { class: "num" }, signed(r.net)))))))));
  }
  clear(main, parts);
}

async function viewAccount(main) {
  await refreshPlayer();
  clear(main,
    el("h2", { class: "section-title", text: t("accountTitle") }),
    el("div", { class: "grid" },
      el("div", { class: "card" },
        el("p", { text: t("signedInAs", { name: state.me.username, role: roleName(state.me.role) }) }),
        el("button", { class: "btn ghost", type: "button", text: t("navLogout"), onclick: logout })),
      el("div", { class: "card" }, el("h3", { text: t("changeTitle") }), changeForm(() => {}))));
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

async function boot() {
  $("lang-toggle").addEventListener("click", () => toggleLang());
  $("sound-toggle").addEventListener("click", () => {
    state.sound = !state.sound;
    renderChrome();
    ding();
  });
  $("support-button").addEventListener("click", () => {
    const bubble = $("support-bubble");
    bubble.hidden = !bubble.hidden;
  });
  onLangChange(() => {
    renderChrome();
    route();
  });
  window.addEventListener("hashchange", route);
  setInterval(() => {
    state.bannerIndex += 1;
    renderBanner();
  }, 4500);

  renderChrome();
  try {
    state.me = await api.me();
    await refreshPlayer();
  } catch (error) {
    state.me = { signed_in: false };
  }
  route();
}

boot();
