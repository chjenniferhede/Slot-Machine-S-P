// The staff back office. Which tabs show depends on the role, but that is
// only layout: every action is checked again by the server's role gate.

import {
  api, ApiError, clear, el, formValues, num, onLangChange, pct, roleName, shortTime, t, toggleLang,
} from "/common.js";
import { SYMBOLS } from "/strings.js";

const $ = (id) => document.getElementById(id);
const state = { me: null, tab: null, audit: { offset: 0, filters: {} } };

const TABS = {
  technician: [
    ["games", "tabGames", viewGames],
    ["diagnostic", "tabDiagnostic", viewDiagnostic],
    ["audit", "tabAudit", viewAudit],
    ["staff", "tabStaff", viewStaff],
    ["account", "tabAccount", viewAccount],
  ],
  commissioner: [
    ["analysis", "tabAnalysis", viewAnalysis],
    ["test", "tabTest", viewTest],
    ["par", "tabPar", viewParTab],
    ["account", "tabAccount", viewAccount],
  ],
};

function toast(message, ok = false) {
  const box = $("toast");
  box.textContent = message;
  box.className = `toast show ${ok ? "ok" : "bad"}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.className = "toast"; }, 4000);
}

async function act(work) {
  try {
    return await work();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      location.href = "/";
      return undefined;
    }
    toast(error instanceof ApiError ? error.message : t("errorGeneric"));
    return undefined;
  }
}

function table(headers, rows) {
  return el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, headers.map(([text, cls]) => el("th", { class: cls || "", text })))),
    el("tbody", {}, rows)));
}

function td(text, cls = "") {
  return el("td", { class: cls, text });
}

function icon(symbol) {
  return SYMBOLS[symbol] || symbol;
}

function field(label, attrs) {
  return el("label", {}, label, el("input", attrs));
}

function gameSelect(games, name = "game_id") {
  return el("select", { name }, games.map((g) => el("option", { value: g.id, text: `${g.name} (${g.id})` })));
}

// ---------------------------------------------------------------------------
// Frame
// ---------------------------------------------------------------------------

function renderFrame() {
  const role = state.me.role;
  $("title").textContent = role === "technician" ? t("adminTech") : t("adminComm");
  $("who").textContent = `${state.me.username} · ${roleName(role)}`;
  $("lang-toggle").textContent = t("langToggle");
  $("logout").textContent = t("navLogout");
  const tabs = state.me.must_change_password ? [] : TABS[role];
  clear($("side"), tabs.map(([name, key]) => el("button", {
    type: "button",
    class: name === state.tab ? "active" : "",
    text: t(key),
    onclick: () => { state.tab = name; render(); },
  })));
}

async function render() {
  renderFrame();
  const main = $("main");
  clear(main, el("p", { class: "muted", text: t("loading") }));
  if (state.me.must_change_password) {
    clear(main, el("h1", { text: t("changeTitle") }), el("div", { class: "panel" },
      el("p", { class: "result bad", text: t("mustChange") }), passwordForm()));
    return;
  }
  const entry = TABS[state.me.role].find(([name]) => name === state.tab) || TABS[state.me.role][0];
  state.tab = entry[0];
  renderFrame();
  await act(() => entry[2](main));
}

function passwordForm() {
  return el("form", { class: "inline", onsubmit: async (event) => {
    event.preventDefault();
    const values = formValues(event.target);
    if (values.new !== values.confirm) {
      toast(t("passwordsDiffer"));
      return;
    }
    await act(async () => {
      state.me = await api.post("/api/password", { current: values.current, new: values.new });
      toast(t("changed"), true);
      render();
    });
  } },
  field(t("currentPassword"), { name: "current", type: "password", autocomplete: "current-password", required: true }),
  field(t("newPassword"), { name: "new", type: "password", autocomplete: "new-password", required: true }),
  field(t("confirmPassword"), { name: "confirm", type: "password", autocomplete: "new-password", required: true }),
  el("button", { class: "btn", type: "submit", text: t("changeButton") }));
}

function viewAccount(main) {
  clear(main, el("h1", { text: t("tabAccount") }), el("div", { class: "panel" },
    el("p", { text: t("signedInAs", { name: state.me.username, role: roleName(state.me.role) }) }),
    el("h2", { text: t("changeTitle") }),
    passwordForm()));
}

// ---------------------------------------------------------------------------
// Technician
// ---------------------------------------------------------------------------

async function viewGames(main) {
  const { games } = await api.post("/api/tech/games");
  const parBox = el("div");
  const rows = games.map((g) => el("tr", {},
    td(g.name),
    td(g.id, "mono"),
    el("td", {}, el("span", { class: g.enabled ? "badge on" : "badge off", text: g.enabled ? t("enabled") : t("disabled") })),
    td(g.bets.map((b) => b.text).join(" / ")),
    td(g.rtp_with_holds ? `${pct(g.rtp)} (${pct(g.rtp_with_holds)})` : pct(g.rtp), "num"),
    td(shortTime(g.created_at)),
    el("td", {},
      el("button", { class: "btn secondary", type: "button", text: g.enabled ? t("disable") : t("enable"), onclick: () => act(async () => {
        await api.post("/api/tech/games/enable", { game_id: g.id, enabled: !g.enabled });
        render();
      }) }),
      " ",
      el("button", { class: "btn secondary", type: "button", text: t("viewPar"), onclick: () => act(async () => {
        showPar(parBox, await api.post("/api/tech/par", { game_id: g.id }));
      }) }))));

  const install = el("form", { onsubmit: async (event) => {
    event.preventDefault();
    const values = formValues(event.target);
    await act(async () => {
      const reply = await api.post("/api/tech/games/install", { config: values.config });
      toast(t("installedOk", { name: reply.game.name }), true);
      render();
    });
  } },
  el("p", { class: "muted", text: t("installHelp") }),
  el("textarea", { name: "config", spellcheck: "false", required: true, placeholder: "{ \"id\": \"my_game\", ... }" }),
  el("p", {}, el("button", { class: "btn", type: "submit", text: t("installButton") })));

  clear(main,
    el("h1", { text: t("tabGames") }),
    el("div", { class: "panel" }, games.length
      ? table([[t("gameName")], [t("gameId")], [t("status")], [t("bets")], [t("theoryRtp"), "num"], [t("installed")], [""]], rows)
      : el("p", { class: "muted", text: t("noGames") })),
    parBox,
    el("div", { class: "panel" }, el("h2", { text: t("installTitle") }), install));
}

function showPar(box, sheet) {
  clear(box, el("div", { class: "panel" },
    el("h2", { text: `${t("tabPar")} · ${sheet.game.name}` }),
    el("pre", { class: "par", text: sheet.text })));
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function viewDiagnostic(main) {
  const { games } = await api.post("/api/tech/diagnostic");
  const rows = games.map((g) => el("tr", {},
    td(g.name),
    el("td", {}, el("span", { class: g.enabled ? "badge on" : "badge off", text: g.enabled ? t("enabled") : t("disabled") })),
    td(num(g.spins), "num"), td(num(g.wins), "num"), td(num(g.losses), "num"),
    td(g.wagered.text, "num"), td(g.paid.text, "num"), td(g.house_net.text, "num"),
    td(pct(g.observed_rtp), "num"), td(pct(g.theoretical_rtp), "num"), td(pct(g.hit_rate), "num"),
    td(num(g.unique_players), "num"), td(num(g.plays_per_day, 1), "num"), td(g.largest_win.text, "num")));
  clear(main,
    el("h1", { text: t("tabDiagnostic") }),
    el("div", { class: "panel" },
      el("p", { class: "muted", text: t("diagNote") }),
      table([
        [t("gameName")], [t("status")], [t("spins"), "num"], [t("wins"), "num"], [t("losses"), "num"],
        [t("wagered"), "num"], [t("paidOut"), "num"], [t("houseNet"), "num"], [t("observedRtp"), "num"],
        [t("theoryRtp"), "num"], [t("hitRate"), "num"], [t("players"), "num"], [t("perDay"), "num"], [t("largestWin"), "num"],
      ], rows)));
}

async function viewAudit(main) {
  const limit = 50;
  const filters = state.audit.filters;
  const page = await api.post("/api/tech/audit", { ...filters, limit, offset: state.audit.offset });
  const verifyBox = el("div");

  const filterForm = el("form", { class: "inline", onsubmit: (event) => {
    event.preventDefault();
    const values = formValues(event.target);
    state.audit.filters = Object.fromEntries(Object.entries(values).filter(([, v]) => v));
    state.audit.offset = 0;
    render();
  } },
  field(t("actor"), { name: "actor", value: filters.actor || "", maxlength: 64 }),
  field(t("action"), { name: "action", value: filters.action || "", maxlength: 64, placeholder: "game.disabled" }),
  field(t("since"), { name: "since", type: "date", value: filters.since || "" }),
  field(t("until"), { name: "until", type: "date", value: filters.until || "" }),
  el("button", { class: "btn", type: "submit", text: t("filter") }));

  const rows = page.entries.map((e) => el("tr", {},
    td(String(e.id), "num"), td(shortTime(e.ts)), td(e.actor), td(e.action, "mono"), td(e.detail, "wrap mono")));

  const pager = el("div", { class: "pager" },
    el("button", { class: "btn secondary", type: "button", text: t("newer"), disabled: state.audit.offset === 0, onclick: () => {
      state.audit.offset = Math.max(0, state.audit.offset - limit);
      render();
    } }),
    el("span", { class: "muted", text: `${state.audit.offset + 1}–${state.audit.offset + page.entries.length}` }),
    el("button", { class: "btn secondary", type: "button", text: t("older"), disabled: page.entries.length < limit, onclick: () => {
      state.audit.offset += limit;
      render();
    } }));

  clear(main,
    el("h1", { text: t("tabAudit") }),
    el("div", { class: "panel" },
      el("button", { class: "btn", type: "button", text: t("verifyChain"), onclick: () => act(async () => {
        const result = await api.post("/api/tech/audit/verify");
        clear(verifyBox, el("div", {
          class: result.ok ? "result good" : "result bad",
          text: result.ok
            ? t("chainOk", { n: num(result.entries_checked) })
            : t("chainBad", { id: result.first_bad_id, reason: result.reason, n: num(result.entries_checked) }),
        }));
      }) }),
      verifyBox),
    el("div", { class: "panel" }, filterForm),
    el("div", { class: "panel" },
      page.entries.length
        ? table([["#", "num"], [t("time")], [t("actor")], [t("action")], [t("detail")]], rows)
        : el("p", { class: "muted", text: t("noEntries") }),
      pager));
}

async function viewStaff(main) {
  const { staff } = await api.post("/api/tech/staff");
  const secretBox = el("div");
  const rows = staff.map((s) => el("tr", {},
    td(s.username), td(roleName(s.role)), td(s.must_change_password ? t("yes") : t("no")),
    td(s.locked_until ? shortTime(s.locked_until) : "—"), td(shortTime(s.created_at))));
  const create = el("form", { class: "inline", onsubmit: async (event) => {
    event.preventDefault();
    const values = formValues(event.target);
    await act(async () => {
      const made = await api.post("/api/tech/staff/create", { username: values.username, role: values.role });
      event.target.reset();
      const copy = el("button", { class: "btn secondary", type: "button", text: t("copy"), onclick: async () => {
        try {
          await navigator.clipboard.writeText(made.one_time_password);
          copy.textContent = t("copied");
        } catch {
          copy.disabled = true;
        }
      } });
      clear(secretBox, el("div", { class: "panel" },
        el("p", { text: `${made.username} · ${roleName(made.role)}` }),
        el("p", { class: "muted", text: t("oneTime") }),
        el("div", { class: "secret", text: made.one_time_password }),
        copy));
      const list = await api.post("/api/tech/staff");
      clear(tbody, list.staff.map((s) => el("tr", {},
        td(s.username), td(roleName(s.role)), td(s.must_change_password ? t("yes") : t("no")),
        td(s.locked_until ? shortTime(s.locked_until) : "—"), td(shortTime(s.created_at)))));
    });
  } },
  field(t("username"), { name: "username", maxlength: 32, required: true, autocomplete: "off" }),
  el("label", {}, t("role"), el("select", { name: "role" },
    el("option", { value: "commissioner", text: roleName("commissioner") }),
    el("option", { value: "technician", text: roleName("technician") }))),
  el("button", { class: "btn", type: "submit", text: t("createButton") }));

  const listTable = table([[t("username")], [t("role")], [t("mustChangeCol")], [t("locked")], [t("created")]], rows);
  const tbody = listTable.querySelector("tbody");
  clear(main,
    el("h1", { text: t("tabStaff") }),
    el("div", { class: "panel" }, el("h2", { text: t("createStaff") }), create),
    secretBox,
    el("div", { class: "panel" }, el("h2", { text: t("staffList") }), listTable));
}

// ---------------------------------------------------------------------------
// Commissioner
// ---------------------------------------------------------------------------

async function commissionerGames() {
  const { games } = await api.post("/api/comm/games");
  return games;
}

function tile(label, value) {
  return el("div", { class: "tile" }, el("div", { class: "label", text: label }), el("div", { class: "value", text: value }));
}

async function viewAnalysis(main) {
  const games = await commissionerGames();
  const out = el("div");
  const form = el("form", { class: "inline", onsubmit: async (event) => {
    event.preventDefault();
    const values = formValues(event.target);
    await act(async () => {
      const a = await api.post("/api/comm/analyze", { game_id: values.game_id, test: values.source === "test" });
      const s = a.stats;
      const most = Math.max(1, ...a.histogram.map((b) => b.count));
      clear(out,
        el("div", { class: "tiles" },
          tile(t("spins"), num(s.spins)),
          tile(t("wins"), num(s.wins)),
          tile(t("losses"), num(s.losses)),
          tile(t("wagered"), s.wagered.text),
          tile(t("paidOut"), s.paid.text),
          tile(t("houseNet"), s.house_net.text),
          tile(t("theoryRtp"), s.optimal_hold_rtp ? `${pct(s.theoretical_rtp)} / ${pct(s.optimal_hold_rtp)}` : pct(s.theoretical_rtp)),
          tile(t("observedRtp"), pct(s.observed_rtp)),
          tile(t("hitRate"), pct(s.hit_rate)),
          tile(t("largestWin"), s.largest_win.text),
          tile(t("players"), num(s.unique_players)),
          tile(t("stdev"), typeof a.return_stdev === "number" ? a.return_stdev.toFixed(3) : "—")),
        el("div", { class: "panel" },
          el("h2", { text: t("histogram") }),
          el("div", { class: "bars" }, a.histogram.map((b) => {
            const bar = el("div", { class: "bar" });
            bar.style.width = `${(100 * b.count) / most}%`;
            return el("div", { class: "bar-row" },
              el("span", { text: b.bucket }),
              el("div", { class: "bar-track" }, bar),
              el("span", { class: "num", text: num(b.count) }));
          }))));
    });
  } },
  el("label", {}, t("chooseGame"), gameSelect(games)),
  el("label", {}, t("status"), el("select", { name: "source" },
    el("option", { value: "live", text: t("liveData") }),
    el("option", { value: "test", text: t("testData") }))),
  el("button", { class: "btn", type: "submit", text: t("analyze") }));
  clear(main, el("h1", { text: t("tabAnalysis") }), el("div", { class: "panel" }, games.length ? form : el("p", { class: "muted", text: t("noGames") })), out);
  if (games.length) form.requestSubmit();
}

async function viewTest(main) {
  const games = await commissionerGames();
  const out = el("div");
  const betSelect = el("select", { name: "bet" });
  const select = gameSelect(games);
  const fillBets = () => {
    const game = games.find((g) => g.id === select.value);
    clear(betSelect, (game ? game.bets : []).map((b) => el("option", { value: (b.cents / 100).toFixed(2), text: b.text })));
  };
  select.addEventListener("change", fillBets);
  fillBets();

  const run = el("button", { class: "btn", type: "submit", text: t("runTest") });
  const form = el("form", { class: "inline", onsubmit: async (event) => {
    event.preventDefault();
    const values = formValues(event.target);
    run.disabled = true;
    clear(out, el("p", { class: "muted", text: t("running") }));
    await act(async () => {
      const r = await api.post("/api/comm/test", {
        game_id: values.game_id,
        rounds: Number.parseInt(values.rounds, 10),
        bet: values.bet,
        bankroll: values.bankroll,
      });
      showTest(out, r);
    });
    run.disabled = false;
  } },
  el("label", {}, t("chooseGame"), select),
  el("label", {}, t("bet"), betSelect),
  field(t("rounds"), { name: "rounds", type: "number", min: 1, max: 100000, step: 1, value: 100, required: true }),
  field(t("bankroll"), { name: "bankroll", inputmode: "decimal", value: "1000.00", maxlength: 16, required: true }),
  run);
  clear(main, el("h1", { text: t("tabTest") }), el("div", { class: "panel" }, games.length ? form : el("p", { class: "muted", text: t("noGames") })), out);
}

function showTest(out, r) {
  const rows = r.rounds.map((round) => el("tr", {},
    td(String(round.round), "num"),
    td(String(round.spin_id), "num"),
    td(round.seed, "mono"),
    td(round.stops.join(", "), "mono"),
    td(round.grid.map((row) => row.map(icon).join(" ")).join(" / ")),
    td(round.bet.text, "num"),
    td(round.payout.text, "num"),
    td(round.bankroll.text, "num")));
  clear(out,
    el("div", { class: r.replay_ok ? "result good" : "result bad", text: r.replay_ok ? t("replayOk", { n: num(r.replay_checked) }) : t("replayBad") }),
    r.stopped_early ? el("div", { class: "result bad", text: t("stoppedEarly") }) : null,
    el("h2", { text: `${t("testSummary")} · ${r.name}` }),
    el("div", { class: "tiles" },
      tile(t("played"), `${num(r.rounds_played)} / ${num(r.rounds_requested)}`),
      tile(t("totalBet"), r.total_bet.text),
      tile(t("paidOut"), r.total_paid.text),
      tile(t("observedRtp"), pct(r.observed_rtp)),
      tile(t("theoryRtp"), pct(r.theoretical_rtp)),
      tile(t("hitRate"), pct(r.hit_rate)),
      tile(t("largestWin"), r.largest_win.text),
      tile(t("endBankroll"), r.end_bankroll.text)),
    el("div", { class: "panel" },
      el("p", { class: "muted", text: t("shownRounds", { shown: num(r.rounds_shown), total: num(r.rounds_played) }) }),
      table([[t("round"), "num"], ["spin", "num"], [t("seed")], [t("stops")], [t("symbolsCol")], [t("bet"), "num"], [t("payout"), "num"], [t("bankroll"), "num"]], rows)));
}

async function viewParTab(main) {
  const games = await commissionerGames();
  const out = el("div");
  const form = el("form", { class: "inline", onsubmit: async (event) => {
    event.preventDefault();
    const values = formValues(event.target);
    await act(async () => showPar(out, await api.post("/api/comm/par", { game_id: values.game_id })));
  } },
  el("label", {}, t("chooseGame"), gameSelect(games)),
  el("button", { class: "btn", type: "submit", text: t("viewPar") }));
  clear(main, el("h1", { text: t("tabPar") }), el("div", { class: "panel" }, games.length ? form : el("p", { class: "muted", text: t("noGames") })), out);
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

async function boot() {
  $("lang-toggle").addEventListener("click", () => toggleLang());
  $("logout").addEventListener("click", () => act(async () => {
    await api.post("/api/logout");
    location.href = "/";
  }));
  onLangChange(() => render());
  try {
    state.me = await api.me();
  } catch {
    state.me = { signed_in: false };
  }
  if (!state.me.signed_in || state.me.role === "player") {
    location.href = "/";
    return;
  }
  render();
}

boot();
