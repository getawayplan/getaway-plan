# Getaway Plan

Watches real round-trip fares from your home airport to the places you want to go, only on
dates when you're free, and tells you when one drops under the price you set.

```
config.json  ->  tracker.py (Google Flights via SerpApi)  ->  docs/data/*.json  ->  docs/index.html
 (your inputs)    runs by itself every day on GitHub        (price history)        (your website)
```

The website shows one poster tile per destination, a price chart, and the exact flights
(airline, flight numbers, times, layovers) for every trip it checked. Prices include airport
taxes; bag fees are extra.

## Put it online (about 20 minutes, all free)

You need two free accounts: **GitHub** (hosts the website and runs the price checks) and
**SerpApi** (provides the flight prices).

### 1. Create a GitHub account and a repository

1. Go to github.com and sign up.
2. Click the **+** at the top right, then **New repository**.
3. Name it `getaway-plan`. Choose **Public** (free websites on GitHub need a public repository).
   Leave "Add a README" unchecked. Click **Create repository**.

### 2. Upload the project

1. Unzip `getaway-plan.zip` on your computer and open the `getaway-plan` folder.
2. On your new repository page, click **uploading an existing file**.
3. Drag everything from inside the folder into the browser window. Include the hidden
   `.github` folder: on a Mac, press **Cmd + Shift + .** in Finder to show hidden folders first.
4. Click **Commit changes**.
5. Check that `.github/workflows/track.yml` appears in the file list. If it didn't upload,
   click **Add file > Create new file**, type `.github/workflows/track.yml` as the name
   (typing `/` makes folders), paste in the contents of that file from your computer, and commit.

### 3. Get the flight-price key

1. Sign up at serpapi.com (free plan: 250 searches a month).
2. Copy your **API key** from your SerpApi dashboard.
3. In your GitHub repository, go to **Settings > Secrets and variables > Actions >
   New repository secret**. Name it exactly `SERPAPI_API_KEY`, paste the key as the value, and
   click **Add secret**. Don't put the key anywhere else.

### 4. Turn on the website

1. In the repository, go to **Settings > Pages**.
2. Under "Build and deployment", set Source to **Deploy from a branch**, Branch to **main**,
   folder to **/docs**, and click **Save**.
3. After a minute or two the page shows your link: `https://YOUR-USERNAME.github.io/getaway-plan/`.

### 5. Run it the first time

1. Click the **Actions** tab. If GitHub asks, click the green button to enable workflows.
2. Click **Track fares** in the list, then **Run workflow**, then the green **Run workflow** button.
3. Wait one or two minutes for the green check mark, then refresh your website. The yellow
   "Sample data" banner disappears and real fares appear. From now on it runs by itself every day, and checks each trip only when it is due.

When a fare drops under one of your limits, or hits the lowest price that trip has had so far,
GitHub opens an issue in your repository with the flights and a Google Flights link, and emails
you. To get it on your phone too, see the next section.

## Phone alerts (optional, free, about 3 minutes)

[ntfy](https://ntfy.sh) sends push notifications to your phone with no account.

1. Install the **ntfy** app (iPhone or Android).
2. In the app, tap **+** and subscribe to a topic with a long, random name that only you know,
   like `getawayplan-k7q2x9m4vz`. Anyone who knows the name can read the alerts, so treat it
   like a password.
3. In your GitHub repository, go to **Settings > Secrets and variables > Actions > New repository
   secret**. Name it exactly `NTFY_TOPIC` and paste the topic name as the value.
4. Test it: **Actions > Track fares > Run workflow**, tick **Only send a test phone
   notification**, and run it. Your phone should buzz within a minute. This uses no searches.

An alert says the destination, price, and why it fired ("$100 under your limit", "lowest price
yet", Google's low/typical/high rating), and tapping it opens Google Flights. A fare that stays
under your limit does not alert again unless it drops to a new low by at least 3%.

## What the labels mean

- **Low / Typical / High for this route** is Google's own rating of a fare against usual prices
  for that route. It only appears when Google provides one.
- **Lowest yet** means the fare beat every earlier check of the same trip by at least 3%
  (it needs at least 4 checks, so it starts showing after about three weeks).

## Change your destinations, limits, or dates

On your website, scroll to **Settings** (or click the Settings button at the top). Change what
you want, then click **Save to GitHub**. The next run uses it. The panel also shows how many
searches your setup uses, so you don't run out.

**One-time setup for the Save button:** it needs a GitHub key that can edit only this one
repository. Open the **How do I get a GitHub key?** note right above the button in Settings and
follow the six steps, then paste the key in and tick **Remember the key on this device**.
After that, saving is one click. The key stays in your browser. It is never put on the website.
If you lose it or stop using it, delete it on the same GitHub page.

No key? Click **Copy config** instead, then **Open config.json on GitHub**, click the pencil,
select everything, paste, and click **Commit changes**. It does the same thing.

## Buying a flight

Each trip card shows the exact outbound and return flights. Click **Open on Google Flights**,
find those flights, and Google sends you to the airline or a travel site to buy. No tool can
lock a price: Google refreshes prices about once a day and the tracker rechecks each trip every few days, so
always confirm the price on the booking page before you pay.

## How often each trip is checked

Fares are usually lowest in a band of days before departure: roughly 22 to 57 days out for US
trips, and 50 to 133 days out for international trips. Christmas and spring break bottom out
a bit earlier, so those windows have a **Book earlier by** setting. (These numbers come from
Google's published pattern. They are typical, not guaranteed.)

The tracker runs once a day, but each run only checks the trips that are due:

| Where the trip is | Checked every |
|---|---|
| In its best booking window | 3 days |
| Past its best window (still before the trip) | 5 days |
| Window opens within 3 weeks | 7 days |
| Window is still far off | 21 days |

This puts most of your free searches on the trips that matter now, such as the Dec 18 trip,
and very few on May. Each trip card shows its best booking window. You can change the day
counts in `check_every_days` in `config.json`.

## Two one-way tickets vs a round trip

Google Flights shows a round trip as one ticket, so it never shows you the cheaper pair of
one-ways that sometimes exists. The tracker checks for it: it searches the flight out and the
flight back separately, adds the two cheapest prices, and compares that with the round trip.
When two one-ways win, the trip card shows both tickets, both prices, and how much you save,
with a Google Flights button for each ticket, and your alert says so.

To save searches, it only does this for a few trips: ones in their best booking window and
within 25% of your limit, at most 2 trips a day, and the same trip at most once a week. It also
only runs when there is room left in the month's search budget. You can change or turn this off
in Settings (**Two one-way tickets**).

Two separate tickets are riskier than one round trip:
- They are not linked. If the first flight is delayed or cancelled, the airline owes you nothing
  on the second one.
- Bags are paid on each ticket, and changes or cancellations are handled one ticket at a time.
- You book them one after the other, and the second price can change before you finish.

## Searches and the free limit

- Each check is **one search**. The Settings panel estimates the total for the next 30 days.
- For any fare under your limit the tracker does **one extra search** to find the return flight
  (up to 8 per day), and it skips that lookup when the fare hasn't changed.
- The tracker stops itself at the `monthly_search_budget` in `config.json` (240), and it paces
  itself so the budget lasts all month: if it has used more than its share by a given day, it waits.
  Trips in their best booking window go first.

## Things to know

- **Prices are round-trip totals for one traveler.** They include airport taxes and not bag fees.
- **Free-plan limits still apply.** Prices are rechecked every few days, not the moment they
  change, so a fare that appears and disappears between checks can be missed.
- **It samples dates, not every date.** A 31-day window has hundreds of possible trips; the
  tracker tries a few evenly spaced ones (the "dates to try" setting).
- **Trip lengths are assumptions** you can change in Settings: 4 nights for US cities, 7 for
  London, 10 for Delhi. A destination is skipped for any window too short for its fewest nights.
- **Long layovers are skipped.** Any itinerary with a layover over 3 hours (domestic) or 7 hours
  (international) is ignored, both for the flight out and the flight back. Change the limits in
  Settings (**Longest layover**). If every option for a trip is longer than your limit, that trip
  shows no fare.
- **Airline miles are not tracked.** Fare data doesn't show whether a fare earns miles.
- **The repository is public,** so your destinations, dates, and price history are visible to
  anyone with the link. Nothing else is stored.

## If something goes wrong

- **Run failed with "Set SERPAPI_API_KEY":** the secret name has a typo. Re-add it exactly as
  `SERPAPI_API_KEY`.
- **Run failed when saving results:** go to **Settings > Actions > General > Workflow
  permissions**, choose **Read and write permissions**, save, and run it again.
- **The site still says "Sample data":** the first run hasn't finished, or Pages isn't pointed
  at `main` and `/docs`. Check the Actions tab, then Settings > Pages.
- **Runs stopped after a long quiet stretch:** GitHub can pause scheduled runs in repositories
  with no activity for 60 days. Open the Actions tab and click the enable button.

## Run it on your own computer (optional)

```
python tracker.py --plan                 # shows what it would search, uses no searches
SERPAPI_API_KEY=your_key python tracker.py
python tracker.py --demo                 # labeled sample data, no key needed
```
