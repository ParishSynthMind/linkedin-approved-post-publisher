# What is needed for the live LinkedIn test

Everything except the real post has been built and tested. The last step needs a free LinkedIn developer app, which only the profile owner can create.

## What you (the profile owner) do, about 15 minutes, free

1. **Open** https://www.linkedin.com/developers/apps/new while signed in to your own LinkedIn.
2. **Fill in the form:**
   - App name: `Approved Post Publisher` (any name works)
   - LinkedIn Page: pick your business page. LinkedIn will not create an app without one. If you have none, create a basic company page first at https://www.linkedin.com/company/setup/new/ (name, public URL, industry, size, type, logo, then tick that you are authorized to act for it). It takes two minutes. The page only appears in this list once it exists.
   - Privacy policy URL: required. This repo publishes one on GitHub Pages: https://parishsynthmind.github.io/linkedin-approved-post-publisher/privacy.html (from `docs/privacy.html`; update it if you fork the tool).
   - App logo: required, a square image of at least 100 x 100 pixels (the business logo, the same one as the Page, is fine)
   - Tick the legal agreement and press **Create app**.
3. **Settings tab:** press **Verify** next to the Page and approve the link as the page admin. Some products stay locked until this is done.
4. **Products tab:** press **Request access** on both of these. They are self-serve and usually approved at once:
   - **Share on LinkedIn**: gives the `w_member_social` scope, which is what lets the app post as you
   - **Sign In with LinkedIn using OpenID Connect**: gives `openid` and `profile`, which tells the app your member id
   - Do **not** request Community Management or Marketing products. They are for company pages and need a review.
5. **Auth tab:**
   - Under "Authorized redirect URLs for your app", add exactly `http://localhost:8765/callback` and save.
   - Check that "OAuth 2.0 scopes" now lists `openid`, `profile`, `w_member_social`.
6. **Save the app keys in the Keychain yourself.** Never paste them into chat or email. In Terminal, run each line. It asks for the value, so it never appears on screen:
   ```
   security add-generic-password -U -a neo -s linkedin-client-id -w
   security add-generic-password -U -a neo -s linkedin-client-secret -w
   ```
   Paste the Client ID, then the Primary Client Secret, from the Auth tab.

## The live test, once the app exists

1. `python -m li_publisher auth`: the browser opens LinkedIn's consent page, you press **Allow**, the token and member id are saved to the Keychain, and the terminal shows how many days the token is valid (60).
2. `python -m li_publisher whoami --live`: confirms the token belongs to your profile.
3. `python -m li_publisher draft briefs/<a brief you approve>.md`, then open `queue/review.html`. You read the draft and approve it with `approve <id> --by <name>`. Nothing posts without that.
4. `python -m li_publisher publish --dry-run`, then `python -m li_publisher publish`: one real post goes on your profile. Check the post URL it prints, open it, and confirm it matches the approved text.
5. Run `publish` again to prove the duplicate guard skips it. Then check `queue/posted-log.jsonl`. You can delete the test post on LinkedIn afterwards if you like.

If `/rest/posts` answers 403 for a Share on LinkedIn app, set `LINKEDIN_API=ugcposts` (the endpoint shown on LinkedIn's own Share on LinkedIn page) and repeat step 4. The live test settles which one the app gets.

## Good to know

- Tokens last 60 days. Self-serve apps get no refresh token, so every 60 days you run `auth` again. It takes one click if you are still signed in.
- Posting limit for members is 150 requests a day per LinkedIn's Share on LinkedIn page, which is far beyond anything this tool needs.
