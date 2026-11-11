# Run the bot on a free Oracle Cloud server

ZebPay blocks GitHub's US servers, so the bot needs a home that ZebPay accepts. Oracle Cloud gives you a small Linux server free forever. We'll put it in India (Mumbai or Hyderabad) and run the bot there 24/7 in **paper mode**.

**What you need:**
- A laptop for the setup (about 45 minutes, once). After that, your phone is enough: Telegram for the bot, the Oracle app or website if you ever need it.
- A debit or credit card. Oracle uses it only to check you're a real person. Always Free servers are not charged.

**What stays the same:** the bot runs `--paper-only`, so it can never place a real order from this server. No ZebPay API key is used.

---

## Step 1: Create an Oracle Cloud account

1. Go to https://signup.cloud.oracle.com and sign up.
2. **Home Region: pick `India West (Mumbai)` or `India South (Hyderabad)`.** This matters: free servers can only be created in your home region, and **you can't change it later.**
3. Finish the card check and wait for the "your account is ready" email (usually a few minutes, sometimes longer).

## Step 2: Create the server

1. Sign in at https://cloud.oracle.com. Open the menu (top left) > **Compute** > **Instances** > **Create instance**.
2. **Name:** `zebpay-bot`.
3. **Image and shape** > **Edit**:
   - **Image:** Change image > **Ubuntu** > **Canonical Ubuntu 24.04**.
   - **Shape:** Change shape > **AMD** > **VM.Standard.E2.1.Micro** (it shows an "Always Free-eligible" tag).
   - If it says *out of capacity*, try a different **Availability domain** at the top, or pick **Ampere** > **VM.Standard.A1.Flex** with 1 OCPU and 6 GB memory (also free).
4. **Networking:** leave the defaults. Make sure **Assign a public IPv4 address** is ticked.
5. **Add SSH keys:** choose **Generate a key pair for me** and tap **Save private key**. This file is your only way into the server. Keep it safe and never upload it anywhere.
6. Tap **Create**. When the status turns green (**Running**), copy the **Public IP address** shown on the page.

## Step 3: Connect to the server

On your laptop, open **Terminal** (Mac) or **PowerShell** (Windows). Replace the file name and IP with yours:

```bash
cd ~/Downloads
chmod 600 ssh-key-*.key          # Mac only, skip on Windows
ssh -i ssh-key-2026-11-11.key ubuntu@YOUR_PUBLIC_IP
```

Type `yes` the first time it asks. You're in when the prompt shows `ubuntu@zebpay-bot`. **Every command from here on runs on the server.**

## Step 4: Check ZebPay lets this server in (do this first)

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://sapi.zebpay.com/api/v2/ex/currencies
```

- `200`: ZebPay is reachable. Carry on.
- `403`: ZebPay blocks Oracle too. **Stop here** and tell Claude. The fallback is a cheap Indian VPS or running on your laptop.

## Step 5: Basic server setup

```bash
sudo timedatectl set-timezone Asia/Kolkata      # daily limits reset at Indian midnight
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3-venv git
```

If it shows a purple screen asking about restarting services, just press Enter.

## Step 6: Get the bot's code

Your repo is private, so the server needs its own read-only key to download it.

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 -C "oracle zebpay-bot"
cat ~/.ssh/id_ed25519.pub
```

Copy the line it prints (starts with `ssh-ed25519`). Then on GitHub:

1. Open the **CryptoBot** repo > **Settings** > **Deploy keys** > **Add deploy key**.
2. **Title:** `Oracle server`. **Key:** paste the line. **Leave "Allow write access" unticked.**
3. Tap **Add key**.

Back on the server:

```bash
cd ~
git clone git@github.com:amanbaid99/CryptoBot.git
cd CryptoBot
```

Type `yes` if it asks about GitHub's fingerprint.

## Step 7: Install Python packages and run the tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python simulate.py
```

You should see `7/7 scenarios passed`. Then one real check on live prices:

```bash
.venv/bin/python bot.py --once --paper-only
```

It should **not** say `price check failed`. If it does, stop and tell Claude.

## Step 8: Add your Telegram details

```bash
nano .env
```

Type these two lines with your own values (no quotes, no `< >`):

```
TELEGRAM_BOT_TOKEN=123456:ABC-your-token
TELEGRAM_CHAT_ID=123456789
```

Save with **Ctrl+O**, Enter, then exit with **Ctrl+X**. Then lock the file so only you can read it:

```bash
chmod 600 .env
```

## Step 9: Make the bot run all the time

This installs the bot as a service: it starts by itself when the server boots and restarts if it crashes.

```bash
sudo cp deploy/zebpay-bot.service /etc/systemd/system/
sudo cp deploy/zebpay-bot.logrotate /etc/logrotate.d/zebpay-bot
sudo systemctl daemon-reload
sudo systemctl enable --now zebpay-bot
```

Check it's working:

```bash
systemctl status zebpay-bot      # should say "active (running)". Press q to exit.
tail -n 20 bot.log               # should say "Bot started in PAPER mode"
```

Now send `/status` to your bot in Telegram. It should reply within a minute.

You can close the terminal. The bot keeps running on the server.

---

## Everyday use

**From your phone:** use Telegram. `/status` for P&L and open trades, `/pause` and `/resume`, `/help` for the rest. Replies now come within a minute, not 30.

**From the server** (connect as in Step 3, then `cd ~/CryptoBot`):

| What | Command |
|---|---|
| See what the bot did | `tail -n 50 bot.log` |
| Watch it live | `tail -f bot.log` (Ctrl+C to stop watching) |
| Positions and tax estimate | `.venv/bin/python bot.py --status` |
| Stop the bot | `sudo systemctl stop zebpay-bot` |
| Start it again | `sudo systemctl start zebpay-bot` |
| Get new code after a change on GitHub | `git pull && sudo systemctl restart zebpay-bot` |
| Download all trades to your laptop | on the laptop: `scp -i ssh-key-....key ubuntu@YOUR_PUBLIC_IP:CryptoBot/trades.csv .` |

**About the STOP file:** it still works (`touch STOP` and the bot shuts down cleanly). But it stays stopped until you delete the file: `rm STOP && sudo systemctl start zebpay-bot`.

## Good to know

- **GitHub's 30 minute schedule is now off.** Don't press "Run workflow" in the Actions tab while the server bot is running: two bots would fight over your Telegram commands.
- **STATUS.md on GitHub stops updating.** Telegram `/status` replaces it.
- **Oracle may reclaim a server it thinks is idle** (very low CPU, network and memory use for 7 days). The bot is light, so this can happen. If the server disappears, you'll notice when Telegram goes quiet. Two fixes: upgrade the account to **Pay As You Go** in Billing (Always Free servers stay free, and reclaiming no longer applies), or just recreate the server with this guide.
- **The bot's memory lives on the server** (`state_paper.json`, `trades.csv`). If you delete the server, they're gone. Copy `trades.csv` to your laptop now and then (command above).
- **Going live later** is a separate decision. If you ever do, create the ZebPay key as trading-only (never withdrawals) and whitelist this server's public IP.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Permission denied (publickey)` when connecting | Wrong key file, or you used `opc@` instead of `ubuntu@`. On Mac, run `chmod 600` on the key. |
| Connection times out | Check the server is **Running** and you copied the **public** IP, not the private one. |
| `git clone` says permission denied | The deploy key wasn't saved on GitHub, or you copied it only partly. Redo Step 6. |
| Telegram doesn't reply | `cat .env` to check both values, then `sudo systemctl restart zebpay-bot`. |
| `systemctl status` shows failed | Run `journalctl -u zebpay-bot -n 30` and send the output to Claude. |
