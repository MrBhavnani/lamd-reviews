# Google Reviews Scraper → Wix

Scrapes the L.A. Mortgage Dude Google Maps listing on the **1st and 15th of every month** and keeps only the reviews that mention **"Rajesh"**. The results go into `reviews.json`, which the Wix embed reads. There's no API key and nothing to host yourself: GitHub runs the scraper for free.

```
GitHub Actions (1st & 15th) → scrape_reviews.py → reviews.json → Wix embed fetches it
```

## Setup (about 10 minutes, one time)

1. **Create a GitHub repo.** Go to github.com → New repository, name it `google-reviews`, and set it to **Public**. It has to be public so Wix can read the file; the reviews are already public on Google.
2. **Upload these files.** In the repo, choose "Add file → Upload files" and drag in everything from this folder, **including the `.github` folder**. If drag-and-drop skips the hidden folder, create the file manually: "Add file → Create new file", name it `.github/workflows/scrape-reviews.yml`, and paste in the contents.
3. **Add your listing URL.** Go to Settings → Secrets and variables → Actions → **Variables** tab → New repository variable.
   - Name: `MAPS_URL`
   - Value: `https://share.google/alMa8plwDqF9weM5B`

   If the share link ever stops working, use the full `google.com/maps/place/...` link from your browser instead.
4. **Run it once now.** Go to the Actions tab → "Scrape Google reviews" → **Run workflow**. It takes 2–5 minutes. Afterward, `reviews.json` in the repo should list your Rajesh reviews.
5. **Wix.** Open `wix-embed.html` and change `REVIEWS_URL` to:
   `https://raw.githubusercontent.com/YOUR-GITHUB-NAME/google-reviews/main/reviews.json`

   Then in the Wix Editor go to Add Elements → Embed Code → **Embed HTML** → Code, paste the whole file, and set the box height to about 900px (adjust to fit).

## Options

| Where | Setting | What it does |
|---|---|---|
| Repo variable `KEYWORD` | default `Rajesh` | Word to filter on |
| `wix-embed.html` `MIN_RATING` | default `1` | Set to 4 or 5 to hide lower ratings |
| `wix-embed.html` `MAX_SHOWN` | default `0` (all) | Cap the number of cards |
| `wix-embed.html` `MAX_CHARS` | default `320` | Truncate long reviews with "Read more" |
| Workflow `cron` | `17 15 1,15 * *` | Run times (UTC). GitHub cron can't do "every 14 days" exactly, so the 1st and 15th is the closest fit |

## When it breaks

Google changes the Maps page structure without notice. When that happens:

- The run **fails** instead of writing an empty file, and your current `reviews.json` stays live on the site, so visitors never see a blank widget.
- GitHub emails you about the failed run.
- In the failed run there's a **debug** download containing a screenshot and the page HTML. Send that to Claude or a developer and the selectors can be updated in a few minutes.

If a run loads only part of the reviews, for example because Google cut off scrolling, it **merges** with the last saved set instead of dropping reviews.

## Notes

- Dates are Google's relative text ("3 months ago") as of the scrape date, so they can be up to two weeks stale.
- GitHub pauses scheduled workflows in repos with no activity for 60 days. Every run commits a new `scraped_at` timestamp, which counts as activity and keeps the schedule alive.
- Automated scraping is against Google's Terms of Service. The practical risk for a twice-monthly scrape of your own listing is low, but it isn't zero: Google can block the request or change the page, and the run will fail until it's fixed.
