# AutoCycle AI — Full VPS Setup Guide
**Read this from top to bottom. Do every step in order.**
*Skip Step 2 (Supabase SQL) — you already have that done.*

---

## BEFORE YOU START — Important Notes

- Your **analyzer** (gold scalper dashboard) runs on port **5000**
- Your **signals website** (where users sign up) runs on port **8000**
- Both run on the **same Windows VPS** at the same time — two separate terminal windows
- The **bot connection section** in the analyzer lets users pay and connect their MT5. The scalper bot (`scalper_bot.py`) in `autocycle_gold\` will execute trades on all active subscriber accounts when you run it.
- All paths on your VPS are under `C:\Users\Administrator\Documents\autocycle\`

---

## STEP 1 — Find Your VPS Public IP Address (Your URL)

Your VPS IP is your website address until you buy a domain.

**How to find it:**
1. Log into your VPS (Remote Desktop or your VPS control panel)
2. Open Command Prompt (CMD) on the VPS
3. Type this and press Enter:
   ```
   ipconfig
   ```
4. Look for the line that says **IPv4 Address** under your main network adapter
5. That number (e.g. `102.45.67.89`) is your VPS public IP

**Your URLs will be:**
- Analyzer dashboard: `http://102.45.67.89:5000` ← replace with your real IP
- Signals website: `http://102.45.67.89:8000` ← replace with your real IP

> **Want a proper domain name like autocycleai.com?**
> Buy one from Namecheap or GoDaddy (~$10/year), then go to DNS settings and add an **A Record** pointing to your VPS IP. It takes a few hours to work. This is optional but makes you look professional.

---

## STEP 2 — Supabase SQL Schema

**SKIP THIS — you already have it done.**

---

## STEP 3 — Upload Your Updated Files to the VPS

You need to copy 4 files from your local computer to the VPS. These are the files we updated that your VPS doesn't have yet (they will fix the `get_gold_context` import error you saw in the logs).

**Files to upload:**

| Copy this file FROM your computer | Paste it TO your VPS at this path |
|---|---|
| `autocycle-analyzer\news_calendar.py` | `C:\Users\Administrator\Documents\autocycle\news_calendar.py` |
| `autocycle-analyzer\gold_scalper.py` | `C:\Users\Administrator\Documents\autocycle\gold_scalper.py` |
| `autocycle-analyzer\app.py` | `C:\Users\Administrator\Documents\autocycle\app.py` |
| `autocycle-analyzer\templates\dashboard.html` | `C:\Users\Administrator\Documents\autocycle\templates\dashboard.html` |

**How to upload (easiest method — WinSCP):**
1. Download WinSCP free from winscp.net if you don't have it
2. Open WinSCP → click New Site
3. Protocol: SFTP (or SCP if your VPS uses that)
4. Hostname: your VPS IP address
5. Username: `Administrator`
6. Password: your VPS password
7. Click Login
8. Left side = your computer, Right side = your VPS
9. Navigate right side to `C:\Users\Administrator\Documents\autocycle\`
10. Drag and drop each file from left to right — click Yes to replace when asked

**After uploading, restart the analyzer on the VPS:**
Open CMD on the VPS and run:
```
taskkill /F /IM python.exe
cd C:\Users\Administrator\Documents\autocycle
python app.py
```
The `get_gold_context` error in your logs will stop immediately.

---

## STEP 4 — Set Up the Signals Website Folder on VPS

The signals website (`subscription_server.py`) needs its own folder on the VPS.

On the VPS, open CMD and create the folder:
```
mkdir C:\Users\Administrator\Documents\autocycle-signals
```

Then upload ALL files from your local `autocycle-signals` folder to `C:\Users\Administrator\Documents\autocycle-signals\` on the VPS using WinSCP:
- `autocycle_signals.html`
- `subscription_server.py`
- `env.example` (you will rename this to `.env` and fill it in — see Step 6)

---

## STEP 5 — Install Python Dependencies for the Signals Server

On the VPS, open CMD and run:
```
cd C:\Users\Administrator\Documents\autocycle-signals
pip install flask flask-cors stripe requests apscheduler python-dotenv supabase
```

Wait for it to finish. If you get an error about pip not found, run `python -m pip install ...` instead.

---

## STEP 6 — Create Your .env File on the VPS

This is where all your secret keys go. **Never share this file with anyone.**

On the VPS in `C:\Users\Administrator\Documents\autocycle-signals\`, create a file called `.env` (no other extension — just `.env`).

**How to create it:**
1. Open Notepad on the VPS
2. Paste the template below and fill in every value
3. Go to File → Save As
4. Navigate to `C:\Users\Administrator\Documents\autocycle-signals\`
5. In "File name" type: `.env`
6. In "Save as type" select: **All Files** (very important — otherwise Notepad adds .txt)
7. Click Save

**Template — fill in every line:**
```
# Supabase — from supabase.com → your project → Settings → API
SUPABASE_URL=https://xxxxxxxx.supabase.co
SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
SUPABASE_SERVICE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...

# Flutterwave — from dashboard.flutterwave.com → Settings → API Keys
FLUTTERWAVE_PUBLIC_KEY=FLWPUBK-xxxxxxxxxxxxxxxx
FLUTTERWAVE_SECRET_KEY=FLWSECK-xxxxxxxxxxxxxxxx
FLUTTERWAVE_WEBHOOK_HASH=makethisanypassword123

# Stripe — from dashboard.stripe.com → Developers → API Keys
STRIPE_PUBLISHABLE_KEY=pk_live_xxxxxxxxxxxxxxxx
STRIPE_SECRET_KEY=sk_live_xxxxxxxxxxxxxxxx
STRIPE_WEBHOOK_SECRET=whsec_xxxxxxxxxxxxxxxx

# Telegram — your BOT TOKEN from BotFather (NOT the channel ID)
TELEGRAM_BOT_TOKEN=your_new_bot_token_here

# Your channel ID — confirmed working
TELEGRAM_CHANNEL_ID=-1003900504820

# Your personal Telegram user ID (get from @userinfobot on Telegram)
TELEGRAM_ADMIN_CHAT_ID=your_telegram_user_id_here

# Telegram public join link for your channel
TELEGRAM_GROUP_LINK=https://t.me/+lli4QvX3Rkw5N2Q0

# Your VPS URLs — replace the IP with your real VPS IP
FRONTEND_URL=http://YOUR_VPS_IP:8000
ANALYZER_URL=http://YOUR_VPS_IP:5000

# Admin secret — make up any long random password, keep it private
ADMIN_SECRET=make_up_a_long_random_password_here_do_not_share
```

**How to get your TELEGRAM_ADMIN_CHAT_ID:**
1. Open Telegram on your phone
2. Search for `@userinfobot`
3. Send it any message (just type /start)
4. It replies with your user ID number — paste that as `TELEGRAM_ADMIN_CHAT_ID`

---

## STEP 7 — Get Your Stripe Keys and Set Up Webhook

**Getting the keys:**
1. Go to dashboard.stripe.com and log in
2. Click **Developers** (top right) → **API Keys**
3. Copy **Publishable key** → paste as `STRIPE_PUBLISHABLE_KEY` in your .env
4. Click **Reveal test key** (or live key) → paste as `STRIPE_SECRET_KEY` in your .env

**Setting up the Stripe webhook (so Stripe tells your server when someone pays):**
1. Still in Stripe → Developers → **Webhooks**
2. Click **Add endpoint**
3. In "Endpoint URL" type: `http://YOUR_VPS_IP:8000/webhook/stripe`
4. Click **Select events** → search for and add these two events:
   - `invoice.paid`
   - `customer.subscription.deleted`
5. Click **Add endpoint**
6. Click on the webhook you just created → under "Signing secret" click **Reveal**
7. Copy that value → paste as `STRIPE_WEBHOOK_SECRET` in your .env

> Note: If you later get a domain with HTTPS, update the webhook URL to use your domain instead of the raw IP.

---

## STEP 8 — Get Your Flutterwave Keys and Set Up Webhook

**Getting the keys:**
1. Go to dashboard.flutterwave.com and log in
2. Settings (gear icon, bottom left) → **API Keys**
3. Copy **Public Key** → paste as `FLUTTERWAVE_PUBLIC_KEY` in your .env
4. Copy **Secret Key** → paste as `FLUTTERWAVE_SECRET_KEY` in your .env

**Setting up the Flutterwave webhook:**
1. Settings → **Webhooks**
2. Set URL to: `http://YOUR_VPS_IP:8000/webhook/flutterwave`
3. Set a Secret Hash — make up any password (e.g. `flwsecret2026`)
4. Save it
5. Put that same password as `FLUTTERWAVE_WEBHOOK_HASH` in your .env

---

## STEP 9 — Fix Your Telegram Bot Token (IMPORTANT — DO THIS NOW)

The old bot token was exposed in a chat. It must be replaced immediately before going live.

**Revoke the old token and get a new one:**
1. Open Telegram → search for `@BotFather`
2. Send: `/mybots`
3. Select your bot `@autocycle_favour_bot`
4. Click **API Token** → click **Revoke current token**
5. Confirm when asked — BotFather gives you a brand new token
6. Copy the new token → paste as `TELEGRAM_BOT_TOKEN` in your .env

**Register the webhook (so Telegram sends messages to your server):**
Open this URL in your browser — replace both placeholders with your real values:
```
https://api.telegram.org/botYOUR_NEW_TOKEN/setWebhook?url=http://YOUR_VPS_IP:8000/telegram/webhook
```

Example: if your new token is `1234567890:ABCdef` and your VPS IP is `102.45.67.89`:
```
https://api.telegram.org/bot1234567890:ABCdef/setWebhook?url=http://102.45.67.89:8000/telegram/webhook
```

You should see: `{"ok":true,"result":true}` — that means it worked.

---

## STEP 10 — Update the URL References in Your HTML Files

Two HTML files have `localhost` hardcoded that need to point to your real VPS IP.

**File 1: `autocycle_signals.html`** (in your autocycle-signals folder)

Open the file in Notepad, find these two lines near the bottom in the `<script>` section:
```javascript
const SERVER       = 'http://localhost:8000';
const ANALYZER_URL = 'http://localhost:5000';
```
Change them to your VPS IP:
```javascript
const SERVER       = 'http://YOUR_VPS_IP:8000';
const ANALYZER_URL = 'http://YOUR_VPS_IP:5000';
```

**File 2: `dashboard.html`** (in autocycle-analyzer/templates/)

Find this line near the very bottom:
```javascript
const SIGNALS_SERVER = 'http://localhost:8000';
```
Change it to:
```javascript
const SIGNALS_SERVER = 'http://YOUR_VPS_IP:8000';
```

After making these changes, upload both files to the VPS again using WinSCP.

---

## STEP 11 — Open the Firewall Ports on Your VPS

By default, Windows Firewall blocks ports 5000 and 8000. Users will not be able to reach your site until you open them.

On the VPS, open CMD **as Administrator** (right-click CMD → Run as administrator) and run:
```
netsh advfirewall firewall add rule name="AutoCycle Analyzer" dir=in action=allow protocol=TCP localport=5000

netsh advfirewall firewall add rule name="AutoCycle Signals" dir=in action=allow protocol=TCP localport=8000
```

**Also check your VPS provider's firewall panel.** Contabo, DigitalOcean, Vultr, and similar all have a separate firewall in their web dashboard. Log into your VPS provider's website → find Firewall or Network settings → add inbound rules allowing TCP on port 5000 and port 8000.

---

## STEP 12 — Run the Signals Server

On the VPS, open a **NEW CMD window** (a second one, separate from the analyzer) and run:
```
cd C:\Users\Administrator\Documents\autocycle-signals
python subscription_server.py
```

You should see:
```
AutoCycle Subscription Server starting on :8000
```

You now have two CMD windows open:
- **Window 1**: Analyzer running on port 5000
- **Window 2**: Signals server running on port 8000

Test both by going to these URLs in your browser from any device:
- `http://YOUR_VPS_IP:5000` → should show the analyzer dashboard
- `http://YOUR_VPS_IP:8000` → should show the signals landing page

---

## STEP 13 — Keep Both Running Permanently (So They Don't Stop When You Close the Window)

If you just run them in CMD windows and close the window, they stop. Here is how to make them run permanently as Windows services using **NSSM** (a free tool).

**Install NSSM:**
1. Download NSSM from nssm.cc/download → get the 64-bit zip
2. Extract it → open the `win64` folder inside
3. Copy `nssm.exe` to `C:\Windows\System32\` — this makes it work from anywhere in CMD

**Set up the Analyzer as a service:**
Open CMD as Administrator and run:
```
nssm install AutoCycleAnalyzer
```
A window opens. Fill in:
- **Path**: `C:\Python311\python.exe` ← find your Python location first (run `where python` in CMD to get the path)
- **Startup directory**: `C:\Users\Administrator\Documents\autocycle`
- **Arguments**: `app.py`

Click **Install Service**.

**Set up the Signals Server as a service:**
```
nssm install AutoCycleSignals
```
Fill in:
- **Path**: `C:\Python311\python.exe` ← same Python path
- **Startup directory**: `C:\Users\Administrator\Documents\autocycle-signals`
- **Arguments**: `subscription_server.py`

Click **Install Service**.

**Start both services:**
```
nssm start AutoCycleAnalyzer
nssm start AutoCycleSignals
```

Now both servers start automatically when the VPS reboots and keep running forever.

**To restart after updating files:**
```
nssm restart AutoCycleAnalyzer
nssm restart AutoCycleSignals
```

**To check if they're running:**
```
nssm status AutoCycleAnalyzer
nssm status AutoCycleSignals
```

---

## STEP 14 — Add the Token Link Between the Signals Site and Analyzer

For the Bot Connection section inside the analyzer to know who is logged in, we need to save the login token when a user signs up or logs in on the signals site.

Open `autocycle_signals.html` on your local computer. Find the `handleSignup` function. Inside it, right after the line that reads `authToken = data.access_token`, add this line:
```javascript
localStorage.setItem('ac_signals_token', data.access_token);
```

Do the same if there is a `handleLogin` function — add the same line after the token is set.

Save the file and upload it to the VPS. Now when a user logs into the signals site, their login automatically carries over to the analyzer bot panel.

---

## STEP 15 — Bot Connection Flow (Now Fully Automatic)

**No manual activation needed.** When a subscriber connects their bot, the system handles everything on its own:

1. User opens the analyzer → clicks **Bot Connect** button
2. User pays for a plan (2 Weeks / Monthly)
3. User fills in their broker name, server, account number, password → clicks **Connect**
4. The server **immediately** checks their subscription, sets status = `active`, and sends the user a Telegram message saying their bot is now live
5. **Your Telegram receives a notification** (informational only — no action needed from you):
   - User's email, broker, account number
   - "No action needed — already active."
6. The scalper bot picks up the new account on its next 5-second scan cycle and starts trading

**Renewals are also automatic.** The scalper bot checks `expires_at` live on every cycle. When a subscription expires, the account stops getting trades. When they renew and pay again, the new expiry date is saved in Supabase and trading resumes automatically on the next scan.

### If You Ever Need to Override (Edge Cases)

There is a manual admin endpoint you can use for special situations (e.g., manually activating a user who had a payment issue and you sorted it out offline):

**On Windows CMD:**
```
curl -X POST http://YOUR_VPS_IP:8000/bot/admin/activate -H "Content-Type: application/json" -H "X-Admin-Secret: YOUR_ADMIN_SECRET" -d "{\"conn_id\": \"the-conn-id-from-telegram\"}"
```

You can find the `conn_id` in your Telegram notification or in Supabase under the `bot_connections` table.

---

## STEP 15B — How to Give Someone Free Access

To give a user the bot for free (beta tester, gift, yourself, etc.), you insert a manual subscription row in Supabase. No payment needed.

### Step 1 — Find Their User ID

1. Go to your Supabase project dashboard: [https://app.supabase.com](https://app.supabase.com)
2. Click **Authentication** in the left sidebar
3. Click **Users**
4. Search for the person's email address in the search bar
5. Click on their row
6. Copy the **UUID** shown at the top (it looks like: `a1b2c3d4-e5f6-7890-abcd-ef1234567890`)
   - This is their `user_id`

### Step 2 — Insert the Free Subscription

1. In Supabase, click **Table Editor** in the left sidebar
2. Click the `subscriptions` table
3. Click **Insert row** (top right button)
4. Fill in these values:

| Column | Value |
|---|---|
| `user_id` | The UUID you copied in Step 1 |
| `plan` | `bot_monthly` |
| `status` | `active` |
| `expires_at` | `2099-12-31T23:59:59+00:00` |

5. Leave all other columns empty or at their defaults
6. Click **Save**

The scalper bot will pick up this subscription on its next scan and start trading their account (as long as they have also submitted their MT5 details via the Bot Connect form). If they haven't submitted MT5 details yet, ask them to do so — the connection will auto-activate immediately because they now have an active subscription.

> **Note:** The `expires_at` value `2099-12-31T23:59:59+00:00` means the subscription never expires in practice. Change it to any real date if you want it to expire (e.g., `2026-12-31T23:59:59+00:00` for end of year).

---

## STEP 16 — Understanding Your Two Bots

You now have **two separate bots** in the `autocycle_gold` folder. They serve completely different purposes. Do not mix them up.

---

### Bot 1: Gold Reversal Bot (`reversal_bot.py`)
- **Who it trades for:** You only (your personal MT5 account)
- **What it does:** Runs the gold reversal strategy — detects reversal setups and trades your personal account
- **When to run it:** When you want personal automated trading on your own MT5
- **MT5 connection:** Holds a persistent connection to your account at all times

### Bot 2: Gold Scalper Bot (`scalper_bot.py`)
- **Who it trades for:** All active subscribers (people who paid for the bot plan)
- **What it does:** Every 5 seconds it polls the analyzer for a FIRE scalp signal. When a signal fires, it loops through every active subscriber account in your database, connects to each one, executes the trade with proper SL/TP, manages the positions (breakeven, partial close, trail stop), then disconnects — and moves on to the next account.
- **When to run it:** When you are running the subscriber service and want to trade on subscriber accounts
- **MT5 connection:** Connects and disconnects to each account sequentially

### ⚠️ IMPORTANT — You Cannot Run Both Bots at the Same Time
Both bots use the same MT5 Python library on the VPS. MT5 can only be connected to one account at a time in the same process. Running both simultaneously will cause conflicts. Choose which service to run based on what you are doing:

| Situation | Which bot to run |
|---|---|
| Personal trading session | `reversal_bot.py` |
| Running the subscriber service | `scalper_bot.py` |
| Platform is live with paying subscribers | `scalper_bot.py` |

You can keep both files on the VPS — just don't run them at the same time.

---

## STEP 17 — Set Up the Scalper Bot on the VPS

### 17a — Upload the Scalper Bot Files

Upload these files from your local `autocycle_gold` folder to a new folder on the VPS:

**Create the folder on VPS first:**
```
mkdir C:\Users\Administrator\Documents\autocycle_gold
mkdir C:\Users\Administrator\Documents\autocycle_gold\data
mkdir C:\Users\Administrator\Documents\autocycle_gold\logs
```

**Then upload using WinSCP:**

| From your computer | To your VPS |
|---|---|
| `autocycle_gold\scalper_bot.py` | `C:\Users\Administrator\Documents\autocycle_gold\scalper_bot.py` |
| `autocycle_gold\scalper_config.py` | `C:\Users\Administrator\Documents\autocycle_gold\scalper_config.py` |

---

### 17b — Install Scalper Bot Dependencies

On the VPS, open CMD and run:
```
cd C:\Users\Administrator\Documents\autocycle_gold
pip install MetaTrader5 requests python-dotenv supabase
```

> **Note:** MetaTrader5 Python library only works on Windows — this is why the bot must run on your VPS, not on your local Mac or Linux machine.

---

### 17c — Create the .env File for the Scalper Bot

The scalper bot reads from a `.env` file inside the `autocycle_gold` folder. This is **separate** from the one in `autocycle-signals`.

Create `C:\Users\Administrator\Documents\autocycle_gold\.env` in Notepad (Save As → All Files → name it `.env`):

```
# Supabase — same values as your signals server .env
SUPABASE_URL=https://xxxxxxxx.supabase.co
SUPABASE_SERVICE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...

# The analyzer URL — the bot polls this for signals
ANALYZER_URL=http://YOUR_VPS_IP:5000

# Admin secret — must exactly match ADMIN_SECRET in your analyzer's .env
ADMIN_SECRET=make_up_a_long_random_password_here_do_not_share

# Telegram — for trade notification alerts
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_ADMIN_CHAT_ID=your_telegram_user_id_here

# MT5 path — leave empty to auto-detect, or set manually
# MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
```

**Key points:**
- `SUPABASE_SERVICE_KEY` must be the **service role key** (not the anon key) — the bot needs to read subscriber account credentials from the database
- `ADMIN_SECRET` must be **identical** to the one in the analyzer's environment — the bot uses it as a secret header to call `/internal/scalp-signal`
- `ANALYZER_URL` should use `localhost` if the bot and analyzer are on the same VPS, or the VPS IP if not

---

### 17d — How to Run the Scalper Bot

**Stop the reversal bot first** (if it is running) — see the note in Step 16 about not running both at once.

On the VPS, open CMD:
```
cd C:\Users\Administrator\Documents\autocycle_gold
python scalper_bot.py
```

You will see log output like:
```
[2026-08-02 12:00:00] Scalper bot started. Polling every 5 seconds.
[2026-08-02 12:00:05] No FIRE signal at this time.
[2026-08-02 12:00:10] No FIRE signal at this time.
...
[2026-08-02 12:04:35] 🔥 Signal: BUY at 2318.50 | SL: 2315.20 | TP: 2323.30
[2026-08-02 12:04:35] Processing 3 active accounts...
[2026-08-02 12:04:36] ✅ #12345678 — order opened ticket 9001
[2026-08-02 12:04:37] ✅ #87654321 — order opened ticket 9002
```

The `data/` folder will contain one state file per subscriber account (`states_ACCOUNTNUMBER.json`) tracking their open positions.

---

### 17e — Make the Scalper Bot a Permanent Service (Optional)

If you want the scalper bot to restart automatically (same as the analyzer and signals server):

```
nssm install AutoCycleScalper
```

Fill in:
- **Path**: `C:\Python311\python.exe` (same Python path you used in Step 13)
- **Startup directory**: `C:\Users\Administrator\Documents\autocycle_gold`
- **Arguments**: `scalper_bot.py`

Click Install Service.

```
nssm start AutoCycleScalper
```

To stop it:
```
nssm stop AutoCycleScalper
```

> **Remember:** Only run `AutoCycleScalper` OR the reversal bot — not both at once.

---

### 17f — What Happens When a Signal Fires

Here is the full flow so you understand what the bot is doing:

1. Every 5 seconds, the bot calls `http://localhost:5000/internal/scalp-signal`
2. The analyzer checks the latest gold scalp scan results — if there is a FIRE signal with a direction, entry, SL, and TP, it returns it
3. The bot checks if this signal is new (different signal ID than the last one, or direction changed after 4 minute cooldown)
4. If it is a new signal, the bot queries Supabase for all `bot_connections` where `status='active'` and the user has a valid `bot_biweekly` or `bot_monthly` subscription that hasn't expired
5. For each account, in order: connect to MT5 → manage any existing open trades (breakeven, partial close, trail stop, timeout close) → open the new trade → disconnect
6. After all accounts are processed, the signal is marked as executed and the bot resumes polling
7. Your Telegram receives a summary notification

---

## QUICK REFERENCE — What Runs Where

| What | File | Port / Notes | Folder on VPS |
|---|---|---|---|
| Gold Scalper Dashboard | `app.py` | port 5000 | `autocycle\` |
| Signals Website | `subscription_server.py` | port 8000 | `autocycle-signals\` |
| Landing Page | `autocycle_signals.html` | served by signals server | `autocycle-signals\` |
| Analyzer Dashboard | `dashboard.html` | served by app.py | `autocycle\templates\` |
| Scalper Bot (subscribers) | `scalper_bot.py` | background process | `autocycle_gold\` |
| Reversal Bot (personal) | `reversal_bot.py` | background process | `autocycle_gold\` |

---

## QUICK REFERENCE — After You Update a File Locally

Whenever we change a file on your local computer, upload it to the VPS and restart:

| Changed file | After uploading to VPS, run |
|---|---|
| `app.py`, `gold_scalper.py`, `news_calendar.py` | `nssm restart AutoCycleAnalyzer` |
| `subscription_server.py` | `nssm restart AutoCycleSignals` |
| `scalper_bot.py`, `scalper_config.py` | `nssm restart AutoCycleScalper` (or stop and start manually) |
| `dashboard.html` | No restart needed — just upload the file |
| `autocycle_signals.html` | No restart needed — just upload the file |

---

## COMPLETE CHECKLIST — First-Time Setup Order

Go through this in order:

- [ ] **Step 1** — Find your VPS public IP
- [ ] **Step 3** — Upload 4 updated analyzer files, restart analyzer (fixes import error)
- [ ] **Step 4** — Create signals folder on VPS, upload signals files
- [ ] **Step 5** — Install Python packages on VPS
- [ ] **Step 6** — Create .env file (in `autocycle-signals\`) with all keys filled in
- [ ] **Step 7** — Get Stripe keys, set up Stripe webhook
- [ ] **Step 8** — Get Flutterwave keys, set up Flutterwave webhook
- [ ] **Step 9** — Revoke old Telegram token, get new one, set webhook URL
- [ ] **Step 10** — Update localhost URLs in both HTML files, upload them
- [ ] **Step 11** — Open firewall ports 5000 and 8000
- [ ] **Step 12** — Run signals server, test both URLs in browser
- [ ] **Step 13** — Install NSSM, make both servers permanent services
- [ ] **Step 14** — Add localStorage line in autocycle_signals.html
- [ ] **Step 17** — Upload scalper bot files to `autocycle_gold\` on VPS
- [ ] **Step 17b** — Install scalper bot pip packages on VPS
- [ ] **Step 17c** — Create `.env` in `autocycle_gold\` folder on VPS
- [ ] **Done** — Both servers running, users can sign up, payments work, scalper bot ready to launch

---

*AutoCycle AI — Built August 2026*
