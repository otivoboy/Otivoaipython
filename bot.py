import os, json, re, random, asyncio, itertools, logging, httpx, websockets
from contextlib import AsyncExitStack
from cryptography.fernet import Fernet, InvalidToken
from telegram import Update, BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
                          ConversationHandler, ContextTypes, filters)

logging.getLogger("httpx").setLevel(logging.WARNING)   # keep the bot token out of logs

APP_ID = "34SRQBBtI5nwEFQqmc3se"   # hardcoded
MAX_STAKE = 5000              # cap for the base stake
MAX_MART_STAKE = 1000000      # hidden safety cap on martingale stakes

# Bot token: works with TG_TOKEN or the names FadeHost provides
TG_TOKEN = (os.environ.get("TG_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
            or os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
            or os.environ.get("TOKEN"))
if not TG_TOKEN:
    raise SystemExit("Missing bot token. Set TG_TOKEN in the host settings.")

API = "https://api.derivws.com/trading/v1/options"

# Data folder: the bot's own folder (writable on FadeHost), or DATA_DIR if set
DATA_DIR = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(DATA_DIR, ".deriv_bot_users.json")
KEY_FILE = os.path.join(DATA_DIR, ".deriv_bot_key")

# ---- Virtual mode account (hardcoded demo account) ----
VIRTUAL_PAT = "pat_0cfa7f96d364eb299f47ea5e5fbdc2a5fff787ab85cdab4342e92e0b98fd0653"
VIRTUAL_ACC = "DOT92647098"
VIRTUAL_STAKE = 0.35          # fixed virtual stake (per leg for Dual)
VIRTUAL_ACCU_STAKE = 1        # fixed virtual stake for accumulators

# Optional: restrict the bot to certain Telegram user IDs (empty = public)
ALLOWED = {x for x in os.environ.get("ALLOWED_IDS", "").split(",") if x}

RISK = ("Trading is risky and most users lose money. Martingale can wipe a balance "
        "quickly. Use a DEMO account first and a Trade-scope-only token. "
        "No financial advice and no guarantees.")

MARKETS = {
    "Vol 10": "R_10", "Vol 10 (1s)": "1HZ10V",
    "Vol 15 (1s)": "1HZ15V",
    "Vol 25": "R_25", "Vol 25 (1s)": "1HZ25V",
    "Vol 50": "R_50", "Vol 50 (1s)": "1HZ50V",
    "Vol 75": "R_75", "Vol 75 (1s)": "1HZ75V",
    "Vol 90 (1s)": "1HZ90V",
    "Vol 100": "R_100", "Vol 100 (1s)": "1HZ100V",
}
TYPES = {
    "Differs": "DIGITDIFF", "Over": "DIGITOVER", "Under": "DIGITUNDER",
    "Even": "DIGITEVEN", "Odd": "DIGITODD",
    "Rise": "CALL", "Fall": "PUT",
    "Higher": "HIGHER", "Lower": "LOWER",
    "Accumulator": "ACCU",
    "Dual O5/U4": "DUAL", "Dual H/L": "DUAL",
}
TYPE_LOOKUP = {k.lower(): k for k in TYPES}
DUAL = {"Dual O5/U4", "Dual H/L"}
DIGIT_TYPES = {"Differs", "Over", "Under"}
BARRIER_TYPES = {"Higher", "Lower"}          # need a signed barrier, min 5 ticks
VALID = {"Differs": range(0, 10), "Over": range(0, 9), "Under": range(1, 10)}
BARRIER_RE = re.compile(r"^[+-][0-9]+(\.[0-9]+)?$")

(PAT, ACC, MARKET, MODE, TTYPE, HTYPES, DIGIT, BARRIER, GROWTH, ATP,
 STAKE, DUR, MART, MULT, VIRT, VLOSS, CONT, TP, SL, OUT) = range(20)
sessions = {}
CF_CACHE = {}
RID = itertools.count(1)


# ---------- encryption for saved tokens ----------
def load_key():
    k = os.environ.get("DB_KEY")
    if k:
        return k.encode()
    try:
        if os.path.exists(KEY_FILE):
            with open(KEY_FILE, "rb") as f:
                return f.read().strip()
    except OSError:
        pass
    k = Fernet.generate_key()
    try:
        with open(KEY_FILE, "wb") as f:
            f.write(k)
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        logging.warning("Could not save key file. Set the DB_KEY variable.")
    return k


FERNET = Fernet(load_key())


def load_db():
    try:
        with open(DB) as f:
            return json.load(f)
    except Exception:
        return {}


def save_db(db):
    with open(DB, "w") as f:
        json.dump(db, f)
    try:
        os.chmod(DB, 0o600)
    except OSError:
        pass


def get_creds(uid):
    c = load_db().get(uid)
    if not c:
        return None
    try:
        pat = FERNET.decrypt(c["pat"].encode()).decode()
    except InvalidToken:
        pat = c["pat"]          # older login saved before encryption
    return {"pat": pat, "acc": c["acc"]}


def norm(text):
    return TYPE_LOOKUP.get(text.strip().lower())


def hdr(pat):
    return {"Authorization": f"Bearer {pat}", "Deriv-App-ID": APP_ID}


def running(uid):
    s = sessions.get(uid)
    return bool(s and "task" in s and not s["task"].done())


def virtual_stake(t):
    return VIRTUAL_ACCU_STAKE if t == "Accumulator" else VIRTUAL_STAKE


def allowed(update):
    return (not ALLOWED) or str(update.effective_user.id) in ALLOWED


# ---------- login / help ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return ConversationHandler.END
    uid = str(update.effective_user.id)
    if uid in load_db():
        await update.message.reply_text(
            "Welcome back, you're logged in.\n"
            "/trade - start trading\n/balance - show account balance\n"
            "/stop - stop current session\n/logout - log out\n/help - help")
        return ConversationHandler.END
    await update.message.reply_text(
        RISK + "\n\nSend your Deriv PAT (Trade scope, DEMO account recommended). "
        "I'll delete your message right away.")
    return PAT


async def help_cmd(update, ctx):
    await update.message.reply_text(
        "/start - log in\n/trade - start a trading session\n/balance - show balance\n"
        "/stop - end the current session\n/logout - delete your saved token\n\n" + RISK)


async def got_pat(update, ctx):
    ctx.user_data["pat"] = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    await update.effective_chat.send_message("Token received. Send your Deriv account ID:")
    return ACC


async def got_acc(update, ctx):
    uid = str(update.effective_user.id)
    pat = ctx.user_data["pat"]
    acc = update.message.text.strip()
    db = load_db()
    db[uid] = {"pat": FERNET.encrypt(pat.encode()).decode(), "acc": acc}
    try:
        save_db(db)
    except OSError as e:
        ctx.user_data.clear()
        await update.message.reply_text(
            f"Could not save your login ({e}). The host storage is not writable. "
            "Please contact the bot owner.")
        return ConversationHandler.END
    ctx.user_data.clear()
    msg = "Logged in and saved."
    try:
        async with websockets.connect(await get_ws_url(acc, pat)) as ws:
            b, c = await get_balance(ws)
        msg += f"\nBalance: {b:,.2f} {c}"
    except Exception as e:
        msg += (f"\nCould not read your balance ({e}). Check your token and "
                f"account ID, or /logout and log in again.")
    await update.message.reply_text(msg + "\nSend /trade to start.")
    return ConversationHandler.END


async def balance_cmd(update, ctx):
    uid = str(update.effective_user.id)
    creds = get_creds(uid)
    if not creds:
        await update.message.reply_text("Please /start to log in first.")
        return
    try:
        async with websockets.connect(
                await get_ws_url(creds["acc"], creds["pat"])) as ws:
            b, c = await get_balance(ws)
        await update.message.reply_text(f"Balance: {b:,.2f} {c}")
    except Exception as e:
        await update.message.reply_text(f"Could not read balance: {e}")


# ---------- trade setup ----------
async def trade(update, ctx):
    if not allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return ConversationHandler.END
    uid = str(update.effective_user.id)
    if uid not in load_db():
        await update.message.reply_text("Please /start to log in first.")
        return ConversationHandler.END
    if running(uid):
        await update.message.reply_text("A session is already running. Send /stop first.")
        return ConversationHandler.END
    ctx.user_data["s"] = {}
    names = list(MARKETS)
    rows = [names[i:i + 2] for i in range(0, len(names), 2)]
    kb = ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Choose a market:", reply_markup=kb)
    return MARKET


async def got_market(update, ctx):
    name = update.message.text.strip()
    if name not in MARKETS:
        await update.message.reply_text("Please pick a market from the buttons.")
        return MARKET
    ctx.user_data["s"]["market"] = MARKETS[name]
    kb = ReplyKeyboardMarkup([["Single type", "Hybrid"]],
                             one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Trading mode?\n- Single type: one contract type all session\n"
        "- Hybrid: switch to the next type after each real loss", reply_markup=kb)
    return MODE


async def got_mode(update, ctx):
    m = update.message.text.strip().lower()
    if m.startswith("single"):
        kb = ReplyKeyboardMarkup(
            [["Differs", "Over", "Under"], ["Even", "Odd"],
             ["Rise", "Fall"], ["Higher", "Lower"], ["Accumulator"],
             ["Dual O5/U4", "Dual H/L"]],
            one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text(
            "Trade type?\n\nDual O5/U4 = Over 5 + Under 4 opened together.\n"
            "Dual H/L = Higher + Lower opened together.", reply_markup=kb)
        return TTYPE
    if m.startswith("hybrid"):
        await update.message.reply_text(
            "Send the trade types in order, separated by commas.\n"
            "Example: Over, Under, Dual O5/U4\n\n"
            f"Options: {', '.join(TYPES)}\n\n"
            "The bot starts with the first one. After a real loss it moves to the next "
            "(looping back to the first). After a win it stays on the same type.",
            reply_markup=ReplyKeyboardRemove())
        return HTYPES
    await update.message.reply_text("Choose Single type or Hybrid.")
    return MODE


def set_types(s, types):
    s["types"] = types
    s["digits"] = {}
    s["barriers"] = {}
    s["pending"] = []
    queued = set()

    def q(kind, name):
        if (kind, name) not in queued:
            queued.add((kind, name))
            s["pending"].append((kind, name))

    for t in types:
        if t in DIGIT_TYPES:
            q("digit", t)
        elif t in BARRIER_TYPES:
            q("barrier", t)
        elif t == "Accumulator":
            q("growth", t)
            q("atp", t)
        elif t == "Dual H/L":
            q("barrier", "Higher")
            q("barrier", "Lower")


async def next_param(update, ctx):
    """Ask for the next pending parameter, or move on to the stake."""
    s = ctx.user_data["s"]
    if s["pending"]:
        kind, t = s["pending"][0]
        if kind == "digit":
            digits = [str(d) for d in VALID[t]]
            rows = [digits[i:i + 5] for i in range(0, len(digits), 5)]
            rows.append(["Random each trade"])
            kb = ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)
            await update.message.reply_text(
                f"Digit for {t}? ({VALID[t][0]}-{VALID[t][-1]}, or random each trade)",
                reply_markup=kb)
            return DIGIT
        if kind == "barrier":
            kb = ReplyKeyboardMarkup([["+0.1", "+0.5", "+1"], ["-0.1", "-0.5", "-1"]],
                                     one_time_keyboard=True, resize_keyboard=True)
            await update.message.reply_text(
                f"Barrier for {t}? Send an offset from the entry spot WITH a sign, "
                "e.g. +0.5 or -0.5 (tap a button or type your own).",
                reply_markup=kb)
            return BARRIER
        if kind == "growth":
            kb = ReplyKeyboardMarkup([["1%", "2%", "3%"], ["4%", "5%"]],
                                     one_time_keyboard=True, resize_keyboard=True)
            await update.message.reply_text("Accumulator growth rate?", reply_markup=kb)
            return GROWTH
        if kind == "atp":
            await update.message.reply_text(
                "Accumulator limit order (take profit amount, 0.01 or more)?\n"
                "The trade closes with profit when this is reached.",
                reply_markup=ReplyKeyboardRemove())
            return ATP
    note = " (min 1 for Accumulator)" if "Accumulator" in s["types"] else ""
    if any(t in DUAL for t in s["types"]):
        note += " (Dual: this stake is used on EACH of the 2 legs)"
    await update.message.reply_text(f"Stake per trade?{note} (max {MAX_STAKE})",
                                    reply_markup=ReplyKeyboardRemove())
    return STAKE


async def got_type(update, ctx):
    t = norm(update.message.text)
    if t is None:
        await update.message.reply_text("Pick a trade type from the buttons.")
        return TTYPE
    set_types(ctx.user_data["s"], [t])
    return await next_param(update, ctx)


async def got_htypes(update, ctx):
    raw = [p.strip() for p in update.message.text.split(",") if p.strip()]
    parts = [norm(p) for p in raw]
    if None in parts or len(parts) < 2:
        await update.message.reply_text(
            "Send at least 2 valid types separated by commas, e.g. Over, Under, Dual H/L.\n"
            f"Valid: {', '.join(TYPES)}")
        return HTYPES
    set_types(ctx.user_data["s"], parts)
    return await next_param(update, ctx)


async def got_digit(update, ctx):
    s = ctx.user_data["s"]
    _, t = s["pending"][0]
    d = update.message.text.strip()
    if d.lower().startswith("random"):
        s["digits"][t] = None
    elif d.isdigit() and len(d) == 1 and int(d) in VALID[t]:
        s["digits"][t] = int(d)
    else:
        await update.message.reply_text(
            f"Invalid. {t} allows {VALID[t][0]}-{VALID[t][-1]}, or Random.")
        return DIGIT
    s["pending"].pop(0)
    return await next_param(update, ctx)


async def got_barrier(update, ctx):
    s = ctx.user_data["s"]
    _, t = s["pending"][0]
    b = update.message.text.strip()
    if not BARRIER_RE.match(b):
        await update.message.reply_text("Send a signed number like +0.5 or -0.5.")
        return BARRIER
    s["barriers"][t] = b
    s["pending"].pop(0)
    return await next_param(update, ctx)


async def got_growth(update, ctx):
    s = ctx.user_data["s"]
    g = update.message.text.strip().replace("%", "")
    if g not in ("1", "2", "3", "4", "5"):
        await update.message.reply_text("Pick 1%, 2%, 3%, 4% or 5%.")
        return GROWTH
    s["growth"] = int(g) / 100
    s["pending"].pop(0)
    return await next_param(update, ctx)


async def got_atp(update, ctx):
    s = ctx.user_data["s"]
    try:
        v = float(update.message.text)
        assert v >= 0.01
    except Exception:
        await update.message.reply_text("Enter an amount of 0.01 or more.")
        return ATP
    s["atp"] = round(v, 2)
    s["pending"].pop(0)
    return await next_param(update, ctx)


def needs_duration(s):
    return any(t != "Accumulator" for t in s["types"])


def min_ticks(s):
    return 5 if any((t in BARRIER_TYPES or t == "Dual H/L") for t in s["types"]) else 1


async def ask_mart(update):
    kb = ReplyKeyboardMarkup([["Yes", "No"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Use martingale? (raises stake after each real loss, resets after a win)",
        reply_markup=kb)
    return MART


async def got_stake(update, ctx):
    s = ctx.user_data["s"]
    minimum = 1.0 if "Accumulator" in s["types"] else 0.01
    try:
        v = float(update.message.text)
        assert v >= minimum
        assert v <= MAX_STAKE
    except Exception:
        await update.message.reply_text(f"Enter a number from {minimum:g} to {MAX_STAKE}.")
        return STAKE
    s["stake"] = v
    if needs_duration(s):
        lo_t = min_ticks(s)
        note = " (Higher/Lower needs 5+)" if lo_t == 5 else ""
        await update.message.reply_text(f"Duration in ticks? ({lo_t}-10){note}")
        return DUR
    s["ticks"] = 0   # accumulator only, duration not used
    return await ask_mart(update)


async def got_dur(update, ctx):
    s = ctx.user_data["s"]
    lo = min_ticks(s)
    d = update.message.text.strip()
    if not (d.isdigit() and int(d) in range(lo, 11)):
        await update.message.reply_text(f"Enter a whole number from {lo} to 10.")
        return DUR
    s["ticks"] = int(d)
    return await ask_mart(update)


async def ask_virtual(update):
    kb = ReplyKeyboardMarkup([["Yes", "No"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Use virtual mode?\nVirtual trades run first on a demo account (fixed stake, "
        "can't be changed). Real trades only start after the number of virtual losses "
        "you choose.", reply_markup=kb)
    return VIRT


async def got_mart(update, ctx):
    a = update.message.text.strip().lower()
    s = ctx.user_data["s"]
    if a == "yes":
        s["mart"] = True
        await update.message.reply_text(
            "Martingale multiplier? (e.g. 2.1, between 1.1 and 100)",
            reply_markup=ReplyKeyboardRemove())
        return MULT
    if a == "no":
        s["mart"] = False
        return await ask_virtual(update)
    await update.message.reply_text("Answer Yes or No.")
    return MART


async def got_mult(update, ctx):
    try:
        v = float(update.message.text)
        assert v >= 1.1
        assert v <= 100
    except Exception:
        await update.message.reply_text("Enter a number between 1.1 and 100.")
        return MULT
    ctx.user_data["s"]["mult"] = v
    return await ask_virtual(update)


async def got_virt(update, ctx):
    a = update.message.text.strip().lower()
    s = ctx.user_data["s"]
    if a == "yes":
        s["virtual"] = True
        await update.message.reply_text(
            "How many virtual losses in a row before a real trade? (1-50)",
            reply_markup=ReplyKeyboardRemove())
        return VLOSS
    if a == "no":
        s["virtual"] = False
        s["cont"] = False
        await update.message.reply_text("Take profit (total session profit to stop at)?",
                                        reply_markup=ReplyKeyboardRemove())
        return TP
    await update.message.reply_text("Answer Yes or No.")
    return VIRT


async def got_vloss(update, ctx):
    d = update.message.text.strip()
    if not (d.isdigit() and int(d) in range(1, 51)):
        await update.message.reply_text("Enter a whole number from 1 to 50.")
        return VLOSS
    ctx.user_data["s"]["vneed"] = int(d)
    kb = ReplyKeyboardMarkup([["Yes", "No"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Continuous real trading?\n"
        "- Yes: after a real LOSS keep trading real until a WIN, then go back to virtual\n"
        "- No: go back to virtual after every real trade",
        reply_markup=kb)
    return CONT


async def got_cont(update, ctx):
    a = update.message.text.strip().lower()
    if a not in ("yes", "no"):
        await update.message.reply_text("Answer Yes or No.")
        return CONT
    ctx.user_data["s"]["cont"] = (a == "yes")
    await update.message.reply_text("Take profit (total real profit to stop at)?",
                                    reply_markup=ReplyKeyboardRemove())
    return TP


async def got_tp(update, ctx):
    try:
        v = float(update.message.text)
        assert v > 0
    except Exception:
        await update.message.reply_text("Enter a positive number.")
        return TP
    ctx.user_data["s"]["tp"] = v
    await update.message.reply_text("Stop loss (total real loss to stop at)?")
    return SL


async def got_sl(update, ctx):
    try:
        v = float(update.message.text)
        assert v > 0
    except Exception:
        await update.message.reply_text("Enter a positive number.")
        return SL
    s = ctx.user_data["s"]
    s["sl"] = v
    uid = str(update.effective_user.id)
    creds = get_creds(uid)
    if not creds:
        await update.message.reply_text("Please /start to log in first.")
        ctx.user_data.clear()
        return ConversationHandler.END
    state = {"stop": False}
    state["task"] = asyncio.create_task(
        run_session(ctx.bot, update.effective_chat.id, creds, dict(s), state))
    sessions[uid] = state
    ctx.user_data.clear()
    return ConversationHandler.END


# ---------- trading helpers ----------
async def recv_until(ws, key):
    while True:
        data = json.loads(await asyncio.wait_for(ws.recv(), 60))
        if "error" in data or key in data:
            return data


async def collect(ws, rids):
    """Wait until every request id in rids has been answered (by req_id)."""
    pending = set(rids)
    out = {}
    while pending:
        d = json.loads(await asyncio.wait_for(ws.recv(), 60))
        rid = d.get("req_id")
        if rid in pending:
            out[rid] = d
            pending.discard(rid)
    return out


async def get_ws_url(acc, pat):
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{API}/accounts/{acc}/otp", headers=hdr(pat))
        r.raise_for_status()
        return r.json()["data"]["url"]


async def get_balance(ws):
    """Returns (balance as float, currency)."""
    await ws.send(json.dumps({"balance": 1}))
    bal = await recv_until(ws, "balance")
    if "error" in bal:
        raise Exception(bal["error"]["message"])
    return float(bal["balance"]["balance"]), bal["balance"]["currency"]


def pick_param(s, t):
    """Digit for digit types, signed barrier ONLY for Higher/Lower, else None."""
    if t in DIGIT_TYPES:
        fixed = s["digits"].get(t)
        return fixed if fixed is not None else random.choice(VALID[t])
    if t in BARRIER_TYPES:
        return s["barriers"][t]
    return None


def make_label(s, t, param):
    if t == "Accumulator":
        return f"Accumulator {int(round(s['growth'] * 100))}%"
    if t == "Dual H/L":
        return f"Dual H{s['barriers']['Higher']}/L{s['barriers']['Lower']}"
    return f"{t} {param}" if param is not None else t


def _ticks(v):
    v = str(v)
    return int(v[:-1]) if v.endswith("t") and v[:-1].isdigit() else None


async def check_barrier_contract(ws, s, t):
    """contracts_for pre-check: Higher/Lower exists on this market and the
    tick duration is supported."""
    sym = s["market"]
    if sym not in CF_CACHE:
        await ws.send(json.dumps({"contracts_for": sym}))
        r = await recv_until(ws, "contracts_for")
        if "error" in r:
            raise Exception(r["error"]["message"])
        CF_CACHE[sym] = r["contracts_for"]["available"]
    ctype = TYPES[t]
    found = [c for c in CF_CACHE[sym] if c.get("contract_type") == ctype]
    if not found:
        raise Exception(f"{t} is not available on {sym}. Pick another market.")
    for c in found:
        lo = _ticks(c.get("min_contract_duration"))
        hi = _ticks(c.get("max_contract_duration"))
        if lo is not None and hi is not None:
            if s["ticks"] in range(lo, hi + 1):
                return c
    c = found[0]
    raise Exception(
        f"{t} on {sym}: {s['ticks']} ticks not supported. "
        f"Allowed duration: {c.get('min_contract_duration')} to "
        f"{c.get('max_contract_duration')}.")


async def execute(ws, s, t, param, stake, cur):
    """Place one contract on the given connection and return its profit."""
    if t == "Accumulator":
        req = {
            "proposal": 1, "amount": stake, "basis": "stake",
            "contract_type": "ACCU", "currency": cur,
            "growth_rate": s["growth"],
            "underlying_symbol": s["market"],
            "limit_order": {"take_profit": s["atp"]}}
    else:
        req = {
            "proposal": 1, "amount": stake, "basis": "stake",
            "contract_type": TYPES[t], "currency": cur,
            "duration": s["ticks"], "duration_unit": "t",
            "underlying_symbol": s["market"]}
        if param is not None:          # digits and Higher/Lower only
            req["barrier"] = str(param)
    if t in BARRIER_TYPES:
        await check_barrier_contract(ws, s, t)
    await ws.send(json.dumps(req))
    prop = await recv_until(ws, "proposal")
    if "error" in prop:
        msg = prop["error"]["message"]
        if t in BARRIER_TYPES:
            msg += f" (sent {TYPES[t]} {s['market']} barrier {param}, {s['ticks']} ticks)"
        raise Exception(msg)
    await ws.send(json.dumps({"buy": prop["proposal"]["id"], "price": stake}))
    buy = await recv_until(ws, "buy")
    if "error" in buy:
        raise Exception(buy["error"]["message"])
    cid = buy["buy"]["contract_id"]
    await ws.send(json.dumps({"proposal_open_contract": 1,
                              "contract_id": cid, "subscribe": 1}))
    while True:
        m = await recv_until(ws, "proposal_open_contract")
        if "error" in m:
            raise Exception(m["error"]["message"])
        poc = m["proposal_open_contract"]
        if poc.get("contract_id") == cid and poc.get("is_sold"):
            return float(poc["profit"])


def dual_legs(s, t):
    if t == "Dual O5/U4":
        return [("DIGITOVER", "5"), ("DIGITUNDER", "4")]
    return [("HIGHER", s["barriers"]["Higher"]), ("LOWER", s["barriers"]["Lower"])]


async def execute_dual(ws, s, t, stake, cur):
    """Open both legs together (same tick duration, so they expire on the same
    tick) and return a list with each leg's profit."""
    legs = dual_legs(s, t)
    if t == "Dual H/L":
        await check_barrier_contract(ws, s, "Higher")
        await check_barrier_contract(ws, s, "Lower")

    # 1) request both proposals at once
    prids = []
    for ctype, barrier in legs:
        rid = next(RID)
        prids.append(rid)
        await ws.send(json.dumps({
            "proposal": 1, "amount": stake, "basis": "stake",
            "contract_type": ctype, "currency": cur,
            "duration": s["ticks"], "duration_unit": "t",
            "underlying_symbol": s["market"], "barrier": barrier,
            "req_id": rid}))
    props = await collect(ws, prids)
    for rid, (ctype, barrier) in zip(prids, legs):
        if "error" in props[rid]:
            raise Exception(f"{ctype} {barrier}: {props[rid]['error']['message']}")

    # 2) buy both back to back, no waiting in between
    brids = []
    for rid in prids:
        brid = next(RID)
        brids.append(brid)
        await ws.send(json.dumps({"buy": props[rid]["proposal"]["id"],
                                  "price": stake, "req_id": brid}))
    buys = await collect(ws, brids)
    cids, errs = [], []
    for brid in brids:
        b = buys[brid]
        if "error" in b:
            errs.append(b["error"]["message"])
        else:
            cids.append(b["buy"]["contract_id"])
    if not cids:
        raise Exception(errs[0])

    # 3) follow both contracts until both are settled
    for cid in cids:
        await ws.send(json.dumps({"proposal_open_contract": 1,
                                  "contract_id": cid, "subscribe": 1}))
    profits = {}
    while len(profits) < len(cids):
        m = json.loads(await asyncio.wait_for(ws.recv(), 60))
        if "error" in m:
            raise Exception(m["error"]["message"])
        poc = m.get("proposal_open_contract")
        if poc and poc.get("contract_id") in cids and poc.get("is_sold"):
            profits[poc["contract_id"]] = float(poc["profit"])
    if errs:
        raise Exception(
            f"One leg failed to open ({errs[0]}). The other leg settled "
            f"with {sum(profits.values()):+.2f}.")
    return [profits[c] for c in cids]


async def trade_once(ws, s, t, param, stake, cur):
    """Returns (net profit, detail text)."""
    if t in DUAL:
        ps = await execute_dual(ws, s, t, stake, cur)
        return sum(ps), " [legs " + " / ".join(f"{p:+.2f}" for p in ps) + "]"
    return await execute(ws, s, t, param, stake, cur), ""


# ---------- trading loop ----------
async def run_session(bot, chat_id, creds, s, state):
    async def say(t):
        await bot.send_message(chat_id, t)

    total, n, wins, vn = 0.0, 0, 0, 0
    cur = ""
    last_bal = None
    base = s["stake"]
    stake = base
    step = 0
    types = s["types"]
    idx = 0          # current contract type; only changes after a REAL loss
    vloss = 0        # consecutive virtual losses
    try:
        async with AsyncExitStack() as stack:
            ws = await stack.enter_async_context(
                websockets.connect(await get_ws_url(creds["acc"], creds["pat"])))
            last_bal, cur = await get_balance(ws)
            vws, vcur = None, ""
            if s["virtual"]:
                vws = await stack.enter_async_context(
                    websockets.connect(await get_ws_url(VIRTUAL_ACC, VIRTUAL_PAT)))
                _, vcur = await get_balance(vws)

            mode = "Hybrid: " + " > ".join(types) if len(types) > 1 else types[0]
            mart = f", martingale x{s['mult']}" if s["mart"] else ""
            virt = ""
            if s["virtual"]:
                virt = (f", VIRTUAL mode (real after {s['vneed']} virtual losses, "
                        f"continuous real: {'on' if s['cont'] else 'off'})")
            dur = f"{s['ticks']} ticks, " if s["ticks"] else ""
            await say(f"Session started: {s['market']}, {mode}, "
                      f"{dur}stake {base} {cur}{mart}{virt}, "
                      f"TP {s['tp']}, SL {s['sl']}.\n"
                      f"Balance: {last_bal:,.2f} {cur}\nSend /stop to end.")

            while not state["stop"]:
                t = types[idx]
                param = pick_param(s, t)
                label = make_label(s, t, param)
                per = " per leg" if t in DUAL else ""

                # ----- virtual trade (type never switches here) -----
                if s["virtual"] and vloss < s["vneed"]:
                    await ws.send(json.dumps({"ping": 1}))   # keep real link alive
                    await recv_until(ws, "ping")
                    vs = virtual_stake(t)
                    vprofit, vdet = await trade_once(vws, s, t, param, vs, vcur)
                    vn += 1
                    vwon = vprofit > 0
                    vloss = 0 if vwon else vloss + 1
                    await say(f"V#{vn} {label} (virtual {vs}{per}): "
                              f"{'WIN' if vwon else 'LOSS'}{vdet} | "
                              f"virtual losses {vloss}/{s['vneed']}")
                    if vloss >= s["vneed"]:
                        await say(f"Virtual loss target reached. Next trade is REAL ({t}).")
                    await asyncio.sleep(1)
                    continue

                # ----- real trade (same type as the virtual phase) -----
                profit, det = await trade_once(ws, s, t, param, stake, cur)
                total += profit
                n += 1
                won = profit > 0
                wins += won
                try:
                    last_bal, _ = await get_balance(ws)
                    btxt = f" | Balance {last_bal:,.2f} {cur}"
                except Exception:
                    btxt = ""
                await say(f"#{n} REAL {label} (stake {stake}{per}): "
                          f"{'WIN' if won else 'LOSS'} "
                          f"{profit:+.2f}{det} | Total {total:+.2f} {cur}{btxt}")
                if total >= s["tp"]:
                    await say("Take profit reached. Session ended.")
                    break
                if total <= -s["sl"]:
                    await say("Stop loss reached. Session ended.")
                    break

                # type switching: only after a real loss
                if not won and len(types) > 1:
                    idx = (idx + 1) % len(types)
                    await say(f"Real loss, switching to {types[idx]}")

                # martingale
                if s["mart"]:
                    if won:
                        step, stake = 0, base
                    else:
                        step += 1
                        new = round(base * (s["mult"] ** step), 2)
                        if new > MAX_MART_STAKE:
                            step, stake = 0, base
                            await say("Safety cap reached, stake reset to base.")
                        else:
                            stake = new

                # back to virtual or keep trading real
                if s["virtual"]:
                    if won:
                        vloss = 0
                        await say("Real win, back to virtual mode.")
                    elif s["cont"]:
                        await say("Real loss, continuing real trades until a win.")
                    else:
                        vloss = 0
                        await say("Back to virtual mode.")
                await asyncio.sleep(1)
    except Exception as e:
        await say(f"Error: {e}")
    extra = f", {vn} virtual trades" if s["virtual"] else ""
    bal_txt = f"\nBalance: {last_bal:,.2f} {cur}" if last_bal is not None else ""
    await say(f"Summary: {n} real trades, {wins} wins, total {total:+.2f} {cur}{extra}."
              f"{bal_txt}\n/trade to start again.")


async def stop(update, ctx):
    uid = str(update.effective_user.id)
    if running(uid):
        sessions[uid]["stop"] = True
        await update.message.reply_text("Stopping after the current trade finishes...")
    else:
        await update.message.reply_text("No session is running.")


# ---------- logout ----------
async def logout(update, ctx):
    uid = str(update.effective_user.id)
    if uid not in load_db():
        await update.message.reply_text("You're not logged in.")
        return ConversationHandler.END
    kb = ReplyKeyboardMarkup([["Yes", "No"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Log out? This deletes your saved token and stops any running session.",
        reply_markup=kb)
    return OUT


async def confirm_logout(update, ctx):
    uid = str(update.effective_user.id)
    if update.message.text.strip().lower() == "yes":
        if running(uid):
            sessions[uid]["stop"] = True
        db = load_db()
        db.pop(uid, None)
        try:
            save_db(db)
        except OSError:
            pass
        await update.message.reply_text("Logged out. Your token was deleted.",
                                        reply_markup=ReplyKeyboardRemove())
    else:
        await update.message.reply_text("Still logged in.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def cancel(update, ctx):
    ctx.user_data.clear()
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def setup_commands(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Log in"),
        BotCommand("trade", "Start a trading session"),
        BotCommand("balance", "Show your balance"),
        BotCommand("stop", "Stop the current session"),
        BotCommand("logout", "Delete your saved token"),
        BotCommand("help", "How the bot works"),
    ])


txt = filters.TEXT & ~filters.COMMAND
conv = ConversationHandler(
    entry_points=[CommandHandler("start", start), CommandHandler("trade", trade),
                  CommandHandler("logout", logout)],
    states={
        PAT: [MessageHandler(txt, got_pat)],
        ACC: [MessageHandler(txt, got_acc)],
        MARKET: [MessageHandler(txt, got_market)],
        MODE: [MessageHandler(txt, got_mode)],
        TTYPE: [MessageHandler(txt, got_type)],
        HTYPES: [MessageHandler(txt, got_htypes)],
        DIGIT: [MessageHandler(txt, got_digit)],
        BARRIER: [MessageHandler(txt, got_barrier)],
        GROWTH: [MessageHandler(txt, got_growth)],
        ATP: [MessageHandler(txt, got_atp)],
        STAKE: [MessageHandler(txt, got_stake)],
        DUR: [MessageHandler(txt, got_dur)],
        MART: [MessageHandler(txt, got_mart)],
        MULT: [MessageHandler(txt, got_mult)],
        VIRT: [MessageHandler(txt, got_virt)],
        VLOSS: [MessageHandler(txt, got_vloss)],
        CONT: [MessageHandler(txt, got_cont)],
        TP: [MessageHandler(txt, got_tp)],
        SL: [MessageHandler(txt, got_sl)],
        OUT: [MessageHandler(txt, confirm_logout)],
    },
    fallbacks=[CommandHandler("cancel", cancel)])

app = ApplicationBuilder().token(TG_TOKEN).post_init(setup_commands).build()
app.add_handler(conv)
app.add_handler(CommandHandler("stop", stop))
app.add_handler(CommandHandler("balance", balance_cmd))
app.add_handler(CommandHandler("help", help_cmd))
app.run_polling()
