"""Telegram Instagram profile monitor. All displayed profile stats come from verified lookups.
No random/fabricated account stats. Instagram access is unofficial and may be limited.
"""
import os, re, json, time, asyncio, logging
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (Application, CommandHandler, MessageHandler, ContextTypes, filters)

load_dotenv()
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
log = logging.getLogger('ig-watchdog')
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
SESSION_ID = os.getenv('IG_SESSIONID', '').strip()
CHECK_SECONDS = max(120, int(os.getenv('CHECK_SECONDS', '180')))
STORE_PATH = Path(os.getenv('STORE_PATH', 'watchdog_store.json'))
IG = None
STARTED = time.time()

# Reply keyboard means the user can operate the bot by tapping buttons.
BUTTONS = [
    ['📸 Profile Check', '👁 Live Monitor'],
    ['🔨 Ban Monitor', '🔓 Unban Monitor'],
    ['📋 Watchlist', '📊 Stats'],
    ['🧹 Remove Monitor', '📡 Status'],
    ['🏠 Home', '❔ Help'],
]
KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton(x) for x in row] for row in BUTTONS], resize_keyboard=True, is_persistent=True)
FIELDS = ('full_name', 'biography', 'followers', 'following', 'posts')


def clean_username(raw):
    s = (raw or '').strip().strip('<>').lower()
    s = re.sub(r'^https?://(www\.)?', '', s)
    if s.startswith('instagram.com/'): s = s[len('instagram.com/'):]
    return s.lstrip('@').split('?')[0].split('/')[0].strip()


def valid_username(u): return bool(re.fullmatch(r'[a-z0-9._]{1,30}', u or ''))


def load_store():
    try:
        data = json.loads(STORE_PATH.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            for k in ('monitor', 'ban', 'unban', 'counters'):
                data.setdefault(k, {} if k != 'counters' else {'checks': 0, 'changes': 0, 'unavailable': 0, 'recoveries': 0})
            return data
    except (OSError, ValueError): pass
    return {'monitor': {}, 'ban': {}, 'unban': {}, 'counters': {'checks': 0, 'changes': 0, 'unavailable': 0, 'recoveries': 0}}

STORE = load_store()

def save_store():
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(STORE, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(STORE_PATH)


def normalize(obj):
    if isinstance(obj, dict): return obj
    if hasattr(obj, 'model_dump'): return obj.model_dump()
    if hasattr(obj, 'dict') and callable(obj.dict): return obj.dict()
    if hasattr(obj, '__dict__'): return vars(obj)
    return {k: getattr(obj, k, None) for k in ('username','full_name','biography','follower_count','following_count','media_count','is_private','is_verified','profile_pic_url_hd','profile_pic_url')}


async def login_instagram():
    global IG
    if not SESSION_ID:
        log.warning('IG_SESSIONID not set; live profile checks will not work without an authenticated session.')
        return
    try:
        from instagrapi import Client
        def connect():
            c = Client(); c.delay_range = [2, 5]
            settings = os.getenv('IG_SETTINGS_PATH', 'ig_settings.json')
            if os.path.exists(settings):
                try: c.load_settings(settings)
                except Exception: pass
            c.login_by_sessionid(SESSION_ID)
            try: c.dump_settings(settings)
            except Exception: pass
            return c
        IG = await asyncio.to_thread(connect)
        log.info('Instagram session initialized.')
    except Exception as e:
        log.warning('Instagram login failed (%s); status checks are unavailable.', type(e).__name__)


async def lookup(username):
    username = clean_username(username)
    if not valid_username(username): return {'state': 'error', 'username': username, 'reason': 'invalid_username'}
    if IG is None: return {'state': 'error', 'username': username, 'reason': 'instagram_session_not_configured_or_login_failed'}
    try:
        obj = await asyncio.to_thread(IG.user_info_by_username_v1, username)
        u = normalize(obj)
        returned = str(u.get('username') or '').lower()
        counts = (u.get('follower_count'), u.get('following_count'), u.get('media_count'))
        if returned != username: return {'state':'error','username':username,'reason':'username_mismatch_or_missing'}
        if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in counts):
            return {'state':'error','username':username,'reason':'profile_counts_missing'}
        return {'state':'ok','username':username,'full_name':str(u.get('full_name') or ''),
                'biography':str(u.get('biography') or ''),'followers':counts[0], 'following':counts[1], 'posts':counts[2],
                'is_private':bool(u.get('is_private')), 'is_verified':bool(u.get('is_verified')),
                'pic_url':str(u.get('profile_pic_url_hd') or u.get('profile_pic_url') or ''), 'fetched_at':int(time.time())}
    except Exception as e:
        # Deliberately do not treat errors/429/challenges as proof of ban.
        reason = type(e).__name__
        low = (reason + ' ' + str(e)).lower()
        if any(x in low for x in ('429','rate limit','too many','feedback_required','please wait')): reason = 'rate_limited'
        return {'state':'error','username':username,'reason':reason}


def stamp(ts=None):
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def profile_message(p, heading='Verified Instagram profile'):
    return (f'{heading}\n@{p["username"]}\n\nName: {p.get("full_name") or "(empty)"}\n'
            f'Followers: {p["followers"]:,}\nFollowing: {p["following"]:,}\nPosts: {p["posts"]:,}\n'
            f'Bio: {p.get("biography") or "(empty)"}\n\nVerified snapshot: {stamp(p.get("fetched_at"))}')


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text('IG WATCHDOG\nTap a button below to begin. Profile stats are only shown after a verified Instagram lookup; lookup errors are not treated as bans.', reply_markup=KEYBOARD)

async def show_help(update, context):
    await update.effective_message.reply_text('Tap buttons to use the bot. For each action, send an Instagram username when prompted.\n\nLive monitor compares name, bio, followers, following and post count against the last verified snapshot. Instagram can rate-limit unofficial monitoring; checks are not guaranteed instant.', reply_markup=KEYBOARD)

async def show_status(update, context):
    uptime = int(time.time()-STARTED)
    await update.effective_message.reply_text(f'Bot process: online\nUptime: {uptime//3600}h {(uptime%3600)//60}m\nInstagram session: {"initialized" if IG else "not initialized"}\nCheck interval: {CHECK_SECONDS}s (minimum 120s)', reply_markup=KEYBOARD)

async def prompt(update, context, action):
    context.user_data['awaiting'] = action
    prompts = {'profile':'Send the Instagram username to check.', 'monitor':'Send username(s) to monitor, separated by spaces or commas.',
               'ban':'Send username(s) for availability/ban-watch. Current verified stats will be saved.',
               'unban':'Send username(s) for recovery-watch. Current verified stats will be saved.',
               'remove':'Send a username to remove, or type ALL to stop all monitoring.'}
    await update.effective_message.reply_text(prompts[action], reply_markup=KEYBOARD)

async def do_profile(message, username):
    p = await lookup(username)
    if p['state'] == 'ok': await message.reply_text(profile_message(p), reply_markup=KEYBOARD)
    else: await message.reply_text(f'Could not verify @{clean_username(username)}.\nReason: {p.get("reason")}\nThis is not proof of a ban.', reply_markup=KEYBOARD)

async def add_monitors(message, context, action, raw):
    names = []
    for piece in re.split(r'[\s,]+', raw.strip()):
        u = clean_username(piece)
        if valid_username(u) and u not in names: names.append(u)
    if not names:
        await message.reply_text('No valid usernames found. Try again.', reply_markup=KEYBOARD); return
    added, skipped = [], []
    bucket = 'monitor' if action == 'monitor' else action
    for u in names:
        p = await lookup(u)
        if p['state'] != 'ok':
            skipped.append(f'@{u} (not verified: {p.get("reason")})'); continue
        prior = STORE[bucket].get(u, {})
        STORE[bucket][u] = {'chat_id': message.chat_id, 'known': p, 'last_state': 'ok',
                            'added_at': prior.get('added_at', int(time.time())), 'last_success': int(time.time()),
                            'last_error_notice': 0}
        added.append('@'+u)
    save_store()
    msg = f'Saved verified baseline for {bucket.upper()}: {", ".join(added) if added else "none"}.'
    if skipped: msg += '\nSkipped (no fabricated baseline):\n' + '\n'.join(skipped[:10])
    msg += '\nChanges will be compared to saved verified snapshots. A lookup failure alone is not a ban confirmation.'
    await message.reply_text(msg, reply_markup=KEYBOARD)

async def show_watchlist(update, context):
    rows=[]
    for kind in ('monitor','ban','unban'):
        rows.append(kind.upper()+': '+(', '.join('@'+u for u in STORE[kind]) or 'empty'))
    await update.effective_message.reply_text('\n'.join(rows), reply_markup=KEYBOARD)

async def show_stats(update, context):
    c=STORE['counters']
    await update.effective_message.reply_text(
        f'📊 ACTUAL BOT STATS\nProfile monitors: {len(STORE["monitor"])}\nBan/availability watches: {len(STORE["ban"])}\nRecovery watches: {len(STORE["unban"])}\n\nSuccessful checks: {c.get("checks",0)}\nVerified profile changes: {c.get("changes",0)}\nUnavailable events observed: {c.get("unavailable",0)}\nRecovery events observed: {c.get("recoveries",0)}\n\nThese are persisted counters; no random stats.', reply_markup=KEYBOARD)

async def remove_watch(message, raw):
    target = raw.strip().lower(); removed=0
    for bucket in ('monitor','ban','unban'):
        keys=list(STORE[bucket]) if target == 'all' else [clean_username(target)]
        for u in keys:
            if u in STORE[bucket]: del STORE[bucket][u]; removed += 1
    save_store(); await message.reply_text(f'Removed {removed} monitor(s).', reply_markup=KEYBOARD)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message=update.effective_message
    text=(message.text or '').strip()
    # The visible reply keyboard is the primary interface; no slash commands needed after Start.
    actions={'📸 Profile Check':'profile','👁 Live Monitor':'monitor','🔨 Ban Monitor':'ban','🔓 Unban Monitor':'unban','🧹 Remove Monitor':'remove'}
    if text in actions:
        await prompt(update, context, actions[text]); return
    if text=='📋 Watchlist': await show_watchlist(update,context); return
    if text=='📊 Stats': await show_stats(update,context); return
    if text=='📡 Status': await show_status(update,context); return
    if text=='❔ Help': await show_help(update,context); return
    if text=='🏠 Home': await start(update,context); return
    action=context.user_data.pop('awaiting',None)
    if not action:
        await message.reply_text('Tap a menu button to choose an action.', reply_markup=KEYBOARD); return
    if action=='profile': await do_profile(message,text)
    elif action in ('monitor','ban','unban'): await add_monitors(message,context,action,text)
    elif action=='remove': await remove_watch(message,text)

async def monitor_loop(app: Application):
    while True:
        await asyncio.sleep(CHECK_SECONDS)
        # Snapshot keys because the dictionaries can be updated by handlers.
        for bucket in ('monitor','ban','unban'):
            for username, rec in list(STORE[bucket].items()):
                try:
                    p=await lookup(username)
                    if p['state']=='ok':
                        STORE['counters']['checks']=STORE['counters'].get('checks',0)+1
                        old=rec.get('known') or {}
                        was_down=rec.get('last_state')=='down'
                        if was_down:
                            STORE['counters']['recoveries']=STORE['counters'].get('recoveries',0)+1
                            await app.bot.send_message(rec['chat_id'], f'🔓 ACCOUNT ACCESSIBLE AGAIN\n@{username}\n\nPrevious verified snapshot:\nName: {old.get("full_name","(not saved)")}\nFollowers: {old.get("followers","unknown")}\nFollowing: {old.get("following","unknown")}\nPosts: {old.get("posts","unknown")}\nBio: {old.get("biography","(not saved)")}\n\nCurrent verified snapshot:\n{profile_message(p)}', reply_markup=KEYBOARD)
                        changed=[k for k in FIELDS if old.get(k)!=p.get(k)] if old else []
                        if changed and not was_down:
                            STORE['counters']['changes']=STORE['counters'].get('changes',0)+1
                            labels={'full_name':'Name','biography':'Bio','followers':'Followers','following':'Following','posts':'Post count'}
                            lines=[f'📈 VERIFIED PROFILE CHANGE · @{username}']
                            for k in changed:
                                a=old.get(k); b=p.get(k)
                                if k in ('followers','following','posts'): a=f'{a:,}' if isinstance(a,int) else str(a); b=f'{b:,}' if isinstance(b,int) else str(b)
                                lines.append(f'{labels[k]}: {a} → {b}')
                            lines.append('\nUpdated verified snapshot: '+stamp(p['fetched_at']))
                            await app.bot.send_message(rec['chat_id'],'\n'.join(lines), reply_markup=KEYBOARD)
                        rec['known']=p; rec['last_state']='ok'; rec['last_success']=int(time.time()); rec['last_error_notice']=0
                    else:
                        # Never interpret transport/login/rate-limit errors as a ban. Keep old stats.
                        rec['last_error']=p.get('reason','unknown'); rec['last_error_at']=int(time.time())
                        if rec.get('last_state')!='down' and p.get('reason') in ('UserNotFound','UserNotFoundError','user_not_found'):
                            rec['last_state']='down'
                            STORE['counters']['unavailable']=STORE['counters'].get('unavailable',0)+1
                            known=rec.get('known') or {}
                            await app.bot.send_message(rec['chat_id'], f'⚠️ INSTAGRAM PROFILE UNAVAILABLE · @{username}\nThis response may indicate deleted, renamed, suspended or unavailable account; it is not conclusive proof of a ban.\n\nLast verified stats saved at {stamp(known.get("fetched_at"))}:\nName: {known.get("full_name","unknown")}\nFollowers: {known.get("followers","unknown")}\nFollowing: {known.get("following","unknown")}\nPosts: {known.get("posts","unknown")}\nBio: {known.get("biography","unknown")}', reply_markup=KEYBOARD)
                    STORE[bucket][username]=rec
                except Exception as e:
                    log.warning('Monitor loop error for one record (%s).', type(e).__name__)
        save_store()

async def post_init(app):
    await login_instagram()
    app.create_task(monitor_loop(app))

def main():
    if not TOKEN: raise SystemExit('Set TELEGRAM_BOT_TOKEN in the hosting environment or .env file.')
    app=Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler('start',start))
    app.add_handler(CommandHandler('help',show_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(drop_pending_updates=False)

if __name__=='__main__': main()
