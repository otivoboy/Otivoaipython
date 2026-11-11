import os, json, re, random, time, asyncio, itertools, logging, copy, httpx, websockets
from contextlib import AsyncExitStack
from telegram import Update, BotCommand, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
                          ConversationHandler, ContextTypes, filters)

try:
    from cryptography.fernet import Fernet
except ImportError:          # encryption is optional
    Fernet = None

logging.getLogger("httpx").setLevel(logging.WARNING)   # keep the bot token out of logs

APP_ID = "34SRQBBtI5nwEFQqmc3se"   # hardcoded
MAX_STAKE = 5000              # cap for the base stake
MAX_MART_STAKE = 1000000      # hidden safety cap on martingale stakes
TG_TOKEN = next((os.environ[k] for k in
                 ("TG_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN",
                  "TELEGRAM_TOKEN", "TOKEN") if os.environ.get(k)), None)
API = "https://api.derivws.com/trading/v1/options"

RISK = ("Trading is risky and most users lose money. Martingale can wipe a balance "
        "quickly, and Fast execution opens the next trade the moment the last one "
        "closes, so losses can pile up quicker. Use a DEMO account first and a Trade-scope-only token. No financial advice and no guarantees.")


def _pick_db():
    """First writable location: DB_PATH env var, /data (host volume), home."""
    for p in (os.environ.get("DB_PATH"),
              "/data/deriv_bot_users.json",
              os.path.expanduser("~/.deriv_bot_users.json")):
        if p and os.access(os.path.dirname(p) or ".", os.W_OK):
            return p
    return os.path.abspath("deriv_bot_users.json")


DB = _pick_db()

# Optional encryption of saved tokens: set DB_KEY in the host settings to turn on
FERNET = None
_key = os.environ.get("DB_KEY")
if Fernet and _key:
    try:
        FERNET = Fernet(_key.encode())
    except Exception:
        logging.warning("DB_KEY is not a valid key, saving tokens unencrypted.")

# ---- Virtual mode account (demo account) ----
# Better: set VIRTUAL_PAT in the host environment and delete the fallback below.
VIRTUAL_PAT = os.environ.get(
    "VIRTUAL_PAT",
    "pat_0cfa7f96d364eb299f47ea5e5fbdc2a5fff787ab85cdab4342e92e0b98fd0653")
VIRTUAL_ACC = os.environ.get("VIRTUAL_ACC", "DOT92647098")
VIRTUAL_STAKE = 0.35          # fixed virtual stake (per leg for Dual)
VIRTUAL_ACCU_STAKE = 1        # fixed virtual stake for accumulators

MARKETS = {
    "Vol 10": "R_10", "Vol 10 (1s)": "1HZ10V",
    "Vol 15 (1s)": "1HZ15V",
    "Vol 25": "R_25", "Vol 25 (1s)": "1HZ25V",
    "Vol 50": "R_50", "Vol 50 (1s)": "1HZ50V",
    "Vol 75": "R_75", "Vol 75 (1s)": "1HZ75V",
    "Vol 90 (1s)": "1HZ90V",
    "Vol 100": "R_100", "Vol 100 (1s)": "1HZ100V",
}
SYM_NAME = {v: k for k, v in MARKETS.items()}
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

NEW_STRAT = "New strategy"
RESERVED_NAMES = {"skip", "new strategy", "back", "continue trading", "rename",
                  "delete", "cancel", "yes", "no"}

(PAT, ACC, MARKET, EXEC, MODE, TTYPE, HTYPES, DIGIT, BARRIER, GROWTH,
 ATP, STAKE, DUR, MART, MULT, VIRT, VLOSS, CONT, TP, SL, SAVE, OUT,
 STRAT_PICK, STRAT_ACT, RENAME, DELCONF,
 EDIT_STAKE, EDIT_TP, EDIT_SL) = range(29)
sessions = {}
CF_CACHE = {}
RID = itertools.count(1)


def norm(text):
    return TYPE_LOOKUP.get(text.strip().lower())


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


def enc(pat):
    return FERNET.encrypt(pat.encode()).decode() if FERNET else pat


def get_creds(uid):
    c = load_db().get(uid)
    if not c:
        return None
    pat = c["pat"]
    if FERNET:
        try:
            pat = FERNET.decrypt(pat.encode()).decode()
        except Exception:
            pass                 # login saved before encryption was turned on
    elif pat.startswith("gAAAA"):
        return None              # saved encrypted but DB_KEY is not set now
    return {"pat": pat, "acc": c["acc"]}


def hdr(pat):
    return {"Authorization": f"Bearer {pat}", "Deriv-App-ID": APP_ID}


def running(uid):
    s = sessions.get(uid)
    return bool(s and "task" in s and not s["task"].done())


def virtual_stake(t):
    return VIRTUAL_ACCU_STAKE if t == "Accumulator" else VIRTUAL_STAKE


async def safe_send(bot, chat_id, text):
    """Send a Telegram message and retry on network errors, never raise."""
    for attempt in range(5):
        try:
            await bot.send_message(chat_id, text)
            return
        except Exception:
            await asyncio.sleep(2 * (attempt + 1))


# ---------- saved strategies ----------
# Stored in the same DB file under "strat:<uid>", so they survive /logout.
def strat_key(uid):
    return f"strat:{uid}"


def get_strats(uid):
    return load_db().get(strat_key(uid), [])


def put_strats(uid, lst):
    db = load_db()
    db[strat_key(uid)] = lst
    save_db(db)


def find_strat(lst, name):
    name = (name or "").strip().lower()
    return next((x for x in lst if x["name"].lower() == name), None)


def clean_settings(s):
    """JSON-safe copy of session settings without wizard leftovers."""
    c = json.loads(json.dumps(s))
    c.pop("pending", None)
    c.setdefault("exec", "normal")
    return c


def valid_name(name, lst, ignore=None):
    """Returns an error text, or None when the name is fine."""
    n = name.strip()
    if not n or len(n) > 30:
        return "Send a name of 1 to 30 characters."
    if n.lower() in RESERVED_NAMES:
        return "That name is reserved, pick another."
    for x in lst:
        if x["name"].lower() == n.lower() and x["name"] != ignore:
            return "You already have a strategy with that name."
    return None


def describe(s):
    types = s["types"]
    parts = []
    for t in types:
        if t in DIGIT_TYPES:
            d = s["digits"].get(t)
            parts.append(f"{t} {'random' if d is None else d}")
        elif t in BARRIER_TYPES:
            parts.append(f"{t} {s['barriers'][t]}")
        elif t == "Dual H/L":
            parts.append(f"Dual H{s['barriers']['Higher']}/L{s['barriers']['Lower']}")
        elif t == "Accumulator":
            parts.append(f"Accumulator {int(round(s['growth'] * 100))}% (limit TP {s['atp']})")
        else:
            parts.append(t)
    lines = [
        f"Market: {SYM_NAME.get(s['market'], s['market'])}",
        ("Hybrid: " + " > ".join(parts)) if len(parts) > 1 else f"Type: {parts[0]}",
    ]
    if s.get("exec") == "fast":
        lines.append("Execution: FAST (next trade opens right after the last closes)")
    else:
        lines.append("Execution: Normal")
    lines.append(f"Stake: {s['stake']}")
    if s.get("ticks"):
        lines.append(f"Duration: {s['ticks']} ticks")
    lines.append(f"Martingale: x{s['mult']}" if s.get("mart") else "Martingale: off")
    if s.get("virtual"):
        lines.append(f"Virtual: real after {s['vneed']} virtual losses, "
                     f"continuous real {'on' if s.get('cont') else 'off'}")
    else:
        lines.append("Virtual: off")
    lines.append(f"Take profit: {s['tp']}")
    lines.append(f"Stop loss: {s['sl']}")
    return "\n".join(lines)


# ---------- login / help ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid in load_db():
        await update.message.reply_text(
            "Welcome back, you're logged in.\n"
            "/trade - start trading (or pick a saved strategy)\n"
            "/strategies - manage saved strategies\n"
            "/balance - show account balance\n"
            "/stop - stop current session\n/logout - log out\n/help - help")
        return ConversationHandler.END
    await update.message.reply_text(
        RISK + "\n\nSend your Deriv PAT (Trade scope, DEMO account recommended). "
        "I'll delete your message right away.")
    return PAT


async def help_cmd(update, ctx):
    await update.message.reply_text(
        "/start - log in\n/trade - start a trading session\n"
        "/strategies - saved strategies (continue, edit stake/TP/SL, rename, delete)\n"
        "/balance - show balance\n"
        "/stop - end the current session\n/logout - delete your saved token\n\n"
        "Execution modes:\n"
        "- Normal: one contract at a time, with a short pause between trades.\n"
        "- Fast: one contract at a time, but the next one is bought immediately "
        "after the last one closes, with no pause.\n\n" + RISK)


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
    db[uid] = {"pat": enc(pat), "acc": acc}
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


# ---------- trade entry + saved strategies ----------
async def ask_market(update):
    names = list(MARKETS)
    rows = [names[i:i + 2] for i in range(0, len(names), 2)]
    kb = ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Choose a market:", reply_markup=kb)
    return MARKET


async def trade_list(update, ctx):
    """Show saved strategies, or go straight to a new setup if there are none."""
    uid = str(update.effective_user.id)
    lst = get_strats(uid)
    if not lst:
        ctx.user_data["s"] = {}
        return await ask_market(update)
    names = [x["name"] for x in lst]
    rows = [names[i:i + 2] for i in range(0, len(names), 2)]
    rows.append([NEW_STRAT])
    kb = ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Pick a saved strategy, or start a new one:", reply_markup=kb)
    return STRAT_PICK


async def trade(update, ctx):
    uid = str(update.effective_user.id)
    if uid not in load_db():
        await update.message.reply_text("Please /start to log in first.")
        return ConversationHandler.END
    if running(uid):
        await update.message.reply_text("A session is already running. Send /stop first.")
        return ConversationHandler.END
    ctx.user_data.clear()
    ctx.user_data["s"] = {}
    return await trade_list(update, ctx)


async def show_strat(update, ctx, strat):
    kb = ReplyKeyboardMarkup(
        [["Continue trading"], ["Edit stake / TP / SL"], ["Rename", "Delete"], ["Back"]],
        one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        f"Strategy: {strat['name']}\n\n{describe(strat['settings'])}", reply_markup=kb)
    return STRAT_ACT


async def got_strat_pick(update, ctx):
    uid = str(update.effective_user.id)
    text = update.message.text.strip()
    if text == NEW_STRAT:
        ctx.user_data["s"] = {}
        return await ask_market(update)
    strat = find_strat(get_strats(uid), text)
    if not strat:
        await update.message.reply_text("Pick a strategy from the buttons.")
        return STRAT_PICK
    ctx.user_data["strat"] = strat["name"]
    return await show_strat(update, ctx, strat)


async def got_strat_act(update, ctx):
    uid = str(update.effective_user.id)
    a = update.message.text.strip().lower()
    strat = find_strat(get_strats(uid), ctx.user_data.get("strat"))
    if not strat:
        await update.message.reply_text("That strategy no longer exists.")
        return await trade_list(update, ctx)
    if a.startswith("continue"):
        return await launch(update, ctx, strat["settings"])
    if a.startswith("edit"):
        ctx.user_data["edit"] = copy.deepcopy(strat["settings"])
        return await ask_edit_stake(update, ctx)
    if a.startswith("rename"):
        await update.message.reply_text("Send the new name for this strategy:",
                                        reply_markup=ReplyKeyboardRemove())
        return RENAME
    if a.startswith("delete"):
        kb = ReplyKeyboardMarkup([["Yes", "No"]], one_time_keyboard=True,
                                 resize_keyboard=True)
        await update.message.reply_text(f"Delete strategy '{strat['name']}'?",
                                        reply_markup=kb)
        return DELCONF
    if a == "back":
        return await trade_list(update, ctx)
    await update.message.reply_text("Pick an option from the buttons.")
    return STRAT_ACT


async def got_rename(update, ctx):
    uid = str(update.effective_user.id)
    lst = get_strats(uid)
    old = ctx.user_data.get("strat")
    strat = find_strat(lst, old)
    if not strat:
        return await trade_list(update, ctx)
    new = update.message.text.strip()
    err = valid_name(new, lst, ignore=strat["name"])
    if err:
        await update.message.reply_text(err)
        return RENAME
    strat["name"] = new
    try:
        put_strats(uid, lst)
    except OSError as e:
        await update.message.reply_text(f"Could not save ({e}).")
        return await show_strat(update, ctx, strat)
    ctx.user_data["strat"] = new
    await update.message.reply_text(f"Renamed to '{new}'.")
    return await show_strat(update, ctx, strat)


async def got_delconf(update, ctx):
    uid = str(update.effective_user.id)
    lst = get_strats(uid)
    strat = find_strat(lst, ctx.user_data.get("strat"))
    if update.message.text.strip().lower() == "yes" and strat:
        lst = [x for x in lst if x is not strat]
        try:
            put_strats(uid, lst)
            await update.message.reply_text("Strategy deleted.")
        except OSError as e:
            await update.message.reply_text(f"Could not delete ({e}).")
        return await trade_list(update, ctx)
    if strat:
        return await show_strat(update, ctx, strat)
    return await trade_list(update, ctx)


# ---- edit only stake / TP / SL of a saved strategy ----
def stake_minimum(s):
    return 1.0 if "Accumulator" in s["types"] else 0.01


async def ask_edit_stake(update, ctx):
    e = ctx.user_data["edit"]
    kb = ReplyKeyboardMarkup([[f"Keep {e['stake']:g}"]], one_time_keyboard=True,
                             resize_keyboard=True)
    await update.message.reply_text(
        f"New stake? ({stake_minimum(e):g} to {MAX_STAKE}) or keep {e['stake']:g}.",
        reply_markup=kb)
    return EDIT_STAKE


async def got_edit_stake(update, ctx):
    e = ctx.user_data["edit"]
    text = update.message.text.strip()
    if not text.lower().startswith("keep"):
        try:
            v = float(text)
            assert stake_minimum(e) <= v <= MAX_STAKE
        except Exception:
            await update.message.reply_text(
                f"Enter a number from {stake_minimum(e):g} to {MAX_STAKE}.")
            return EDIT_STAKE
        e["stake"] = v
    kb = ReplyKeyboardMarkup([[f"Keep {e['tp']:g}"]], one_time_keyboard=True,
                             resize_keyboard=True)
    await update.message.reply_text(
        f"New take profit? or keep {e['tp']:g}.", reply_markup=kb)
    return EDIT_TP


async def got_edit_tp(update, ctx):
    e = ctx.user_data["edit"]
    text = update.message.text.strip()
    if not text.lower().startswith("keep"):
        try:
            v = float(text)
            assert v > 0
        except Exception:
            await update.message.reply_text("Enter a positive number.")
            return EDIT_TP
        e["tp"] = v
    kb = ReplyKeyboardMarkup([[f"Keep {e['sl']:g}"]], one_time_keyboard=True,
                             resize_keyboard=True)
    await update.message.reply_text(
        f"New stop loss? or keep {e['sl']:g}.", reply_markup=kb)
    return EDIT_SL


async def got_edit_sl(update, ctx):
    uid = str(update.effective_user.id)
    e = ctx.user_data["edit"]
    text = update.message.text.strip()
    if not text.lower().startswith("keep"):
        try:
            v = float(text)
            assert v > 0
        except Exception:
            await update.message.reply_text("Enter a positive number.")
            return EDIT_SL
        e["sl"] = v
    lst = get_strats(uid)
    strat = find_strat(lst, ctx.user_data.get("strat"))
    if strat:
        strat["settings"]["stake"] = e["stake"]
        strat["settings"]["tp"] = e["tp"]
        strat["settings"]["sl"] = e["sl"]
        try:
            put_strats(uid, lst)
            await update.message.reply_text(
                f"Saved: stake {e['stake']:g}, TP {e['tp']:g}, SL {e['sl']:g}.")
        except OSError as err:
            await update.message.reply_text(f"Could not save changes ({err}). "
                                            "Trading with them anyway.")
    return await launch(update, ctx, e)


async def launch(update, ctx, settings):
    """Start a trading session with the given settings."""
    uid = str(update.effective_user.id)
    creds = get_creds(uid)
    if not creds:
        await update.message.reply_text("Please /start to log in first.",
                                        reply_markup=ReplyKeyboardRemove())
        ctx.user_data.clear()
        return ConversationHandler.END
    if running(uid):
        await update.message.reply_text("A session is already running. Send /stop first.",
                                        reply_markup=ReplyKeyboardRemove())
        ctx.user_data.clear()
        return ConversationHandler.END
    state = {"stop": False}
    state["task"] = asyncio.create_task(
        run_session(ctx.bot, update.effective_chat.id, creds,
                    clean_settings(settings), state))
    sessions[uid] = state
    ctx.user_data.clear()
    return ConversationHandler.END


# ---------- new strategy setup ----------
async def got_market(update, ctx):
    name = update.message.text.strip()
    if name not in MARKETS:
        await update.message.reply_text("Please pick a market from the buttons.")
        return MARKET
    ctx.user_data["s"]["market"] = MARKETS[name]
    kb = ReplyKeyboardMarkup([["Normal", "Fast"]],
                             one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Execution speed?\n"
        "- Normal: one contract at a time, with a short pause between trades.\n"
        "- Fast: one contract at a time, but the next contract is bought immediately "
        "after the last one closes (no pause, no extra balance checks), so trades "
        "come as quickly as the market allows.", reply_markup=kb)
    return EXEC


async def ask_mode(update):
    kb = ReplyKeyboardMarkup([["Single type", "Hybrid"]],
                             one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Trading mode?\n- Single type: one contract type all session\n"
        "- Hybrid: switch to the next type after each real loss", reply_markup=kb)
    return MODE


async def got_exec(update, ctx):
    a = update.message.text.strip().lower()
    s = ctx.user_data["s"]
    if a == "normal":
        s["exec"] = "normal"
        return await ask_mode(update)
    if a == "fast":
        s["exec"] = "fast"
        return await ask_mode(update)
    await update.message.reply_text("Choose Normal or Fast.")
    return EXEC


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
    minimum = stake_minimum(s)
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
    ctx.user_data["s"]["sl"] = v
    kb = ReplyKeyboardMarkup([["Skip"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Save this as a strategy? Send a name (max 30 characters) to save it and "
        "start trading, or tap Skip to just start.", reply_markup=kb)
    return SAVE


async def got_save(update, ctx):
    uid = str(update.effective_user.id)
    s = ctx.user_data["s"]
    text = update.message.text.strip()
    if text.lower() != "skip":
        lst = get_strats(uid)
        err = valid_name(text, lst)
        if err:
            await update.message.reply_text(err + " Or tap Skip.")
            return SAVE
        lst.append({"name": text, "settings": clean_settings(s)})
        try:
            put_strats(uid, lst)
            await update.message.reply_text(
                f"Saved as '{text}'. Find it under /trade or /strategies.")
        except OSError as e:
            await update.message.reply_text(f"Could not save the strategy ({e}). "
                                            "Starting anyway.")
    return await launch(update, ctx, s)


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


def build_proposal(s, t, param, stake, cur):
    if t == "Accumulator":
        return {
            "proposal": 1, "amount": stake, "basis": "stake",
            "contract_type": "ACCU", "currency": cur,
            "growth_rate": s["growth"],
            "underlying_symbol": s["market"],
            "limit_order": {"take_profit": s["atp"]}}
    req = {
        "proposal": 1, "amount": stake, "basis": "stake",
        "contract_type": TYPES[t], "currency": cur,
        "duration": s["ticks"], "duration_unit": "t",
        "underlying_symbol": s["market"]}
    if param is not None:          # digits and Higher/Lower only
        req["barrier"] = str(param)
    return req


def dual_legs(s, t):
    if t == "Dual O5/U4":
        return [("DIGITOVER", "5"), ("DIGITUNDER", "4")]
    return [("HIGHER", s["barriers"]["Higher"]), ("LOWER", s["barriers"]["Lower"])]


def dual_leg_proposal(s, ctype, barrier, stake, cur):
    return {
        "proposal": 1, "amount": stake, "basis": "stake",
        "contract_type": ctype, "currency": cur,
        "duration": s["ticks"], "duration_unit": "t",
        "underlying_symbol": s["market"], "barrier": barrier}


# ---------- NORMAL execution (one contract at a time) ----------
async def execute(ws, s, t, param, stake, cur):
    """Place one contract on the given connection and return its profit."""
    req = build_proposal(s, t, param, stake, cur)
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
        req = dual_leg_proposal(s, ctype, barrier, stake, cur)
        req["req_id"] = rid
        await ws.send(json.dumps(req))
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


# ---------- FAST execution helper ----------
class Reporter:
    """Batches lines and sends them every few seconds, so Fast mode doesn't
    hit Telegram's rate limit with one message per trade."""

    def __init__(self, bot, chat_id):
        self.bot, self.chat_id, self.lines = bot, chat_id, []

    def add(self, line):
        self.lines.append(line)

    async def flush(self):
        if not self.lines:
            return
        lines, self.lines = self.lines, []
        chunk = ""
        for ln in lines:
            if len(chunk) + len(ln) + 1 > 3800:
                await safe_send(self.bot, self.chat_id, chunk)
                chunk = ""
            chunk += ln + "\n"
        if chunk:
            await safe_send(self.bot, self.chat_id, chunk)


# ---------- trading loop (Normal and Fast) ----------
# Both modes place ONE contract at a time. Normal pauses 1s between trades and
# checks the balance after every trade. Fast buys the next contract the moment
# the previous one closes, skips the balance check, and sends its Telegram
# messages in batches so Telegram's rate limit never slows the trading.
async def run_session(bot, chat_id, creds, s, state):
    fast = s.get("exec") == "fast"
    rep = Reporter(bot, chat_id) if fast else None

    async def say(t):
        if fast:
            rep.add(t)
        else:
            await safe_send(bot, chat_id, t)

    total, n, wins, vn = 0.0, 0, 0, 0
    cur = ""
    last_bal = None
    base = s["stake"]
    stake = base
    step = 0
    types = s["types"]
    idx = 0          # current contract type; only changes after a REAL loss
    vloss = 0        # consecutive virtual losses
    last_ping = 0.0
    last_n = [-1]
    flusher = None

    async def flush_loop():
        tick = 0
        while True:
            await asyncio.sleep(3)
            tick += 1
            if tick % 5 == 0 and n + vn != last_n[0]:
                last_n[0] = n + vn
                v = f" | Virtual {vn}" if s["virtual"] else ""
                rep.add(f"STATS Runs {n} | Won {wins} | Lost {n - wins} | "
                        f"P/L {total:+.2f} {cur} | Stake {stake}{v}")
            await rep.flush()

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
            await safe_send(
                bot, chat_id,
                f"{'FAST session' if fast else 'Session'} started: {s['market']}, {mode}, "
                f"{dur}stake {base} {cur}{mart}{virt}, TP {s['tp']}, SL {s['sl']}.\n"
                f"Balance: {last_bal:,.2f} {cur}\n"
                + ("Each trade opens right after the previous one closes. " if fast else "")
                + "Send /stop to end.")
            if fast:
                flusher = asyncio.create_task(flush_loop())

            while not state["stop"]:
                t = types[idx]
                param = pick_param(s, t)
                label = make_label(s, t, param)
                per = " per leg" if t in DUAL else ""

                # ----- virtual trade (type never switches here) -----
                if s["virtual"] and vloss < s["vneed"]:
                    # keep the real link alive (every trade in Normal, every 30s in Fast)
                    if not fast or time.monotonic() - last_ping > 30:
                        await ws.send(json.dumps({"ping": 1}))
                        await recv_until(ws, "ping")
                        last_ping = time.monotonic()
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
                    await asyncio.sleep(0 if fast else 1)
                    continue

                # ----- real trade (same type as the virtual phase) -----
                profit, det = await trade_once(ws, s, t, param, stake, cur)
                last_ping = time.monotonic()
                total += profit
                n += 1
                won = profit > 0
                wins += won
                btxt = ""
                if not fast:
                    try:
                        last_bal, _ = await get_balance(ws)
                        btxt = f" | Balance {last_bal:,.2f} {cur}"
                    except Exception:
                        pass
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
                await asyncio.sleep(0 if fast else 1)

            if fast:                      # one balance check at the end
                try:
                    last_bal, _ = await get_balance(ws)
                except Exception:
                    pass
    except Exception as e:
        logging.warning("Session error: %s", e)
        await say(f"Error: {e}")
    finally:
        if flusher:
            flusher.cancel()
    if rep:
        await rep.flush()
    extra = f", {vn} virtual trades" if s["virtual"] else ""
    bal_txt = f"\nBalance: {last_bal:,.2f} {cur}" if last_bal is not None else ""
    await safe_send(bot, chat_id,
                    f"Summary{' (FAST)' if fast else ''}: {n} real trades, {wins} wins, "
                    f"total {total:+.2f} {cur}{extra}.{bal_txt}\n/trade to start again.")


async def stop(update, ctx):
    uid = str(update.effective_user.id)
    if running(uid):
        sessions[uid]["stop"] = True
        await update.message.reply_text(
            "Stopping after the current trade finishes...")
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
        "Log out? This deletes your saved token and stops any running session. "
        "Your saved strategies are kept.",
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


async def on_error(update, context):
    logging.warning("Handler error: %s", context.error)


async def setup_commands(application):
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Log in"),
            BotCommand("trade", "Start trading or pick a saved strategy"),
            BotCommand("strategies", "Manage saved strategies"),
            BotCommand("balance", "Show your balance"),
            BotCommand("stop", "Stop the current session"),
            BotCommand("logout", "Delete your saved token"),
            BotCommand("help", "How the bot works"),
        ])
    except Exception as e:
        logging.warning("Could not set commands: %s", e)


def build_app():
    txt = filters.TEXT & ~filters.COMMAND
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("trade", trade),
                      CommandHandler("strategies", trade),
                      CommandHandler("logout", logout)],
        states={
            PAT: [MessageHandler(txt, got_pat)],
            ACC: [MessageHandler(txt, got_acc)],
            STRAT_PICK: [MessageHandler(txt, got_strat_pick)],
            STRAT_ACT: [MessageHandler(txt, got_strat_act)],
            RENAME: [MessageHandler(txt, got_rename)],
            DELCONF: [MessageHandler(txt, got_delconf)],
            EDIT_STAKE: [MessageHandler(txt, got_edit_stake)],
            EDIT_TP: [MessageHandler(txt, got_edit_tp)],
            EDIT_SL: [MessageHandler(txt, got_edit_sl)],
            MARKET: [MessageHandler(txt, got_market)],
            EXEC: [MessageHandler(txt, got_exec)],
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
            SAVE: [MessageHandler(txt, got_save)],
            OUT: [MessageHandler(txt, confirm_logout)],
        },
        fallbacks=[CommandHandler("cancel", cancel)])

    app = (ApplicationBuilder().token(TG_TOKEN)
           .connect_timeout(20).read_timeout(30)
           .write_timeout(30).pool_timeout(30)
           .post_init(setup_commands).build())
    app.add_error_handler(on_error)
    app.add_handler(conv)
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    return app


if __name__ == "__main__":
    if not TG_TOKEN:
        raise SystemExit("No Telegram token found in the environment.")
    build_app().run_polling(bootstrap_retries=-1)
 
