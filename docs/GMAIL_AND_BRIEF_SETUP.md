# Gmail (read-only) and the morning brief: one-time setup

JARVIS reads Gmail through Google's official Gmail API. You sign in on Google's own page,
so JARVIS never sees your password. It asks for one permission only, **read your email**
(`gmail.readonly`). It can't send, forward, delete, archive, label or mark anything read.

## 1. Make your own Google OAuth client (about 5 minutes, once)

1. Open <https://console.cloud.google.com/> and create a project, for example "JARVIS".
2. Go to **APIs & Services > Library**, search for **Gmail API**, and press **Enable**.
3. Open **Google Auth Platform** (the OAuth consent screen) and press **Get started**.
   Use the app name "JARVIS", your email as the support email, and **External** as the
   audience.
4. Under **Audience > Test users**, add both Gmail addresses you want in the brief.
5. Under **Clients**, press **Create client**, choose **Desktop app**, name it "JARVIS",
   and press **Create**. Copy the **Client ID** and **Client secret**. Don't put the
   downloaded JSON file in the repository (`.gitignore` blocks `client_secret*.json`).

## 2. Sign in from JARVIS

1. Open **Workflows**, then any Gmail node or the **Morning brief** node, and press
   **Connect** on the Gmail card.
2. Paste the client ID and secret, then press **Sign in with Google**.
3. Pick your first account. Google warns that it "hasn't verified this app". It's your
   own app, so press **Continue**. Then allow **Read your email**.
4. When the browser tab says "JARVIS is connected to Gmail", press **Sign in with Google**
   again for the second account.

The sign-ins are kept in Windows Credential Manager ("JARVIS Gmail: …"). **Remove
selected** forgets an account and revokes JARVIS's access at Google.

**About re-signing in.** While the Google project's status is **Testing**, Google ends
the sign-in after 7 days. The brief then says the sign-in expired; sign in again on the
Gmail card. To stop that, press **Publish app** under **Audience**. Google may then show
the unverified-app warning or ask about verification, because reading Gmail is a
restricted permission. Testing mode with a weekly re-sign-in always works for your own
use.

## 3. Z.ai key

1. At <https://z.ai>, open **API keys**, create a key, and copy it.
2. In the Morning brief node, press **Connect** on the Z.ai card, paste the key, and
   save. Don't paste the key into a chat.

The models tried, in order, are `brief.models` in settings.json (default
`glm-5.2`, then `glm-4.7-flash`). The general API is tried first, then the GLM Coding
Plan endpoint, so a Coding Plan key works too.

**What Z.ai receives.** For each email from the last 24 hours, Z.ai gets the sender,
subject, time, read or unread, which account ("Gmail 1" or "Gmail 2"), and the first 700
characters of the text. It also gets your open tasks, today's reminders and calendar,
headlines and prices. It never gets your own email addresses, Gmail links, passwords or
Google sign-in. Z.ai is an external service and processes that data under its own
terms. Every email is screened by Prompt Guard first, and text that looks like an
attempt to steer the AI is withheld.

## 4. Telegram

Press **Connect** on the Morning brief node's Telegram card. Message @BotFather, send
`/newbot`, and paste the token. Then send your new bot any message and press
**Find my chat**.

## 5. Turn it on

Open **Templates**, pick **Morning brief to Telegram** (daily at 8:00), and press
**Save**. Then press **Test workflow** to get one now. You can also say "send me my
morning brief" at any time. The PDF is saved in Documents\JARVIS Briefs, and a short
summary goes to the memory folder (Notes/Morning briefs.md).

To follow stocks, list them in the node ("Reliance", "TCS"…) or in `brief.watchlist`
in settings.json.
