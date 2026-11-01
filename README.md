# Benjamin Cowen YouTube monitor

This is a free cloud-based monitor that checks Benjamin Cowen's official YouTube RSS feed every 30 minutes using GitHub Actions. When a new video is found, it downloads English captions when available, summarizes the video with Gemini's free API tier, and sends the result to Telegram, email, or both.

The monitor does not require your computer to be on. GitHub Actions runs it in the cloud. Scheduled GitHub Actions jobs can be delayed occasionally, so this is a polling monitor rather than an instant webhook.

## Setup

1. Create a GitHub repository. A public repository avoids consuming private-repository Actions minutes.
2. Copy this folder's contents into the repository root.
3. Create a free Gemini API key in [Google AI Studio](https://aistudio.google.com/apikey).
4. For Telegram, create a bot with `@BotFather`, send the bot one message, and obtain the destination chat ID. Add these GitHub repository secrets:

   - `GEMINI_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

5. For email instead, add SMTP secrets for an account with an app password:

   - `GEMINI_API_KEY`
   - `SMTP_HOST`
   - `SMTP_PORT` (usually `587`)
   - `SMTP_USERNAME`
   - `SMTP_PASSWORD`
   - `EMAIL_FROM`
   - `EMAIL_TO`

6. Open **Actions**, select **Benjamin Cowen YouTube monitor**, and run it once manually. The first run initializes `state.json` without sending an old video. Later runs notify only for uploads not already recorded.

## Notes

- The default summarizer is `gemini-2.5-flash-lite`, which Google currently lists with a free tier; free-tier quotas and model availability can change.
- Never put API keys in the repository files. Store them only as GitHub Actions secrets.
- The workflow needs repository `contents: write` permission so it can remember which video IDs were already sent.
- If you configure both Telegram and email, both notifications are sent.
