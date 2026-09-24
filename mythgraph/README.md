# Mythographia

An explorable atlas of Greek myth: gods, mortals, monsters and the events that bind them,
as a force-directed web, a scrubbable timeline and a "six degrees" pathfinder.

This is a separate project from the memecoin screener in the repository root. It shares
nothing with it and deploys as its own Vercel project (root directory `mythgraph/`).

## Where the content comes from

Every bio, image, date, relationship and fun fact comes from **English Wikipedia** and
**Wikimedia Commons**. Nothing is written or drawn by AI.

| Field | Source |
|---|---|
| Bio | Lead section of the article, plain text (`prop=extracts&exintro`) |
| Short description | Wikipedia's short description |
| Fun fact | The first sentences of a named section (Etymology, Epithets, Iconography, …), verbatim, credited to that section |
| Image | First **public-domain or CC0** file in the article (infobox first). Artist, date and file page from Commons `extmetadata`. Other licences are skipped. |
| Parent / consort / sibling | Infobox parameters (`parents`, `children`, `consort`, `siblings`, …) |
| Appears in | An event's lead section links to the figure, or the figure's lead links to the event |
| Linked with | Two articles whose lead sections link to each other |
| Generation (timeline) | *Derived*: a least-squares fit of the parent (+1 generation), consort/sibling (same generation) and event links above. Myth has no calendar, so the axis is relative. Figures outside the main family network stay **unplaced**; nothing is guessed. |

The curated seed list (`ingest/seeds.js`) holds only article titles and a curatorial group.
One-hop expansion adds infobox family members, typed from Wikipedia's own short description.
Every node stores its revision ID, and the detail card links to that exact revision
(CC BY-SA 4.0 attribution).

**Colour:** 8 hues cannot be told apart once they're scattered across a graph. The palette was
validated all-pairs for colour-vision deficiency, and only 3 hues pass. So colour carries the
family (Gods, Mortals, Monsters), events are ink diamonds, and the subtype (Titan, Olympian,
Hero, …) is always shown in text.

## Setup

```sh
cd mythgraph
npm install
npm run build              # vendors d3 + self-hosted fonts into public/vendor
```

Put the Neon connection string in `mythgraph/.env.local` (or the repo-root `.env.local`) as
`DATABASE_URL=…`. It is gitignored and never printed.

```sh
npm run db:schema          # create tables
npm run ingest             # fetch from Wikipedia + Commons, write to Neon (about 5 minutes)
npm run ingest -- --out graph.json   # also write the graph JSON
npm run dev                # http://localhost:4173, reads /api/graph from Neon
```

The ingester needs outbound HTTPS to `en.wikipedia.org`. Image files load in the browser
from `upload.wikimedia.org`. `ingest/last-report.json` lists seed titles that were missing or
turned out to be disambiguation pages, the pages added by expansion, and pages without a
public-domain image.

## Deploy

```sh
cd mythgraph && vercel --prod
```

Set `DATABASE_URL` in the Vercel project's environment variables. `api/graph.js` reads Neon
over HTTP (`@neondatabase/serverless`) and is CDN-cached for an hour.

## Tests and QA

```sh
npm test                   # parser, licence filter, pathfinding and chronology unit tests
npm run qa:fixture         # synthetic stress graph (520 nodes, labelled "Synthetic figure 017")
GRAPH_JSON=qa/fixture-graph.json npm run dev &
npm run qa                 # Playwright: search, hover, expand, frame times, six degrees vs an
                           # independent BFS, timeline scrub/thread/play, dark mode, phone width
QA_URL=https://<deployment>/ npm run qa   # the same checks against a live deployment
```

The fixture is for layout and performance testing only. It is never deployed or stored.
