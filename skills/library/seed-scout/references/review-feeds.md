# Public review feeds used by seed-scout

These are the public, unauthenticated endpoints the upstream scout reads. They are not an official API and can change or rate-limit; read them politely (one request at a time, a pause of about a second between pages, a timeout of about 20 seconds). No sign-in, no key.

| purpose | URL shape |
| --- | --- |
| search the store for apps | `https://itunes.apple.com/search?term=<words>&media=software&entity=software` |
| one app's rating and rating count | `https://itunes.apple.com/lookup?id=<APP_ID>&country=<cc>` |
| reviews, most recent first | `https://itunes.apple.com/<cc>/rss/customerreviews/page=<N>/id=<APP_ID>/sortby=mostrecent/json` |

- `<cc>` is a two-letter country code; reviews are per country, so name the one you read.
- The review feed gives 50 entries a page and up to 10 pages: the 500 most recent reviews.
- Search results carry `trackId`, `trackName`, `averageUserRating`, `userRatingCount`. In the review feed, `feed.entry[]` items with an `im:rating` field are reviews (the first entry is often the app itself, without a rating); the star count is `im:rating.label`, the title `title.label`, the text `content.label`, the version `im:version.label`, the date `updated.label`.
- Rating rounds half up to one decimal (4.88527 is 4.9). Shares are one decimal. Counts are exact.
- A name can match several apps: say which `trackId` you picked.

Fall back, and say so, when a feed does not answer: ask the user for the rivals' review pages or an export, or read a smaller public sample found with `web_search` and state the sample size. Never fill the gap from memory.
