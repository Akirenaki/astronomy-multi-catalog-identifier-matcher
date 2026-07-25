# **astronomy-multi-catalog-cross-matcher**

## **Table of Contents**

1. [App Description](#i-app-description)
2. [Tech Stack & Hosting](#ii-tech-stack--hosting)
3. [User Workflow](#iii-user-workflow)
4. [Application Processing Pipeline](#iv-application-processing-pipeline)
5. [Result Examples](#v-result-examples)
6. [Database Schema](#vi-database-schema)
7. [FAQ](#vii-faq)
8. [Third-Party Data, Services & Legal Notes](#viii-third-party-data-services--legal-notes)

---

## **I. App Description**

An FastAPI WebApp that takes an unstructured, informally-typed star name, resolves it deterministically across independent astronomical catalogs via a cross-identification pipeline, and then, as a distinct, separate step, translates the resulting structured data into a plain-language explanation a non-specialist can actually read.

**Architectural principle:** the resolution engine (deterministic, SQL/ADQL-driven) and the narrative layer (AI-generated) are kept strictly separate: the AI layer can never affect matching logic or introduce a factual error the SQL layer didn't already contain.

The resolver, cross-matcher, and AI narrative are fully usable anonymously. An optional account layer sits on top for anyone who wants to save objects and keep a personal copy of the AI narratives they generate. See [Section IV](#iv-accounts-favorites--personal-ai-summaries).

---

## **II. Tech Stack & Hosting**

| Layer | Choice | Why |
| --- | --- | --- |
| **Web framework** | [FastAPI](https://fastapi.tiangolo.com/) | Async-native Python framework with built-in request/response validation via Pydantic. Chosen so the app can `await` external catalog/AI calls (SIMBAD, NASA Exoplanet Archive, Gemini) without blocking the whole process on a slow network call. |
| **ASGI server** | [Uvicorn](https://www.uvicorn.org/) | The reference ASGI server FastAPI is built to run under. Used locally with `--reload` for hot-reloading during development. |
| **HTTP client** | [httpx](https://www.python-httpx.org/) | Async HTTP client used for all outbound calls to SIMBAD's and NASA's TAP/ADQL endpoints. |
| **ORM / DB toolkit** | [SQLAlchemy](https://www.sqlalchemy.org/) (async) | Defines the schema (`objects`, `identifiers`, `planets`, `users`, `saved_searches`, `user_summary_snapshots`, `rate_limit_events` — see [Section VI](#vi-database-schema)) and handles querying/caching logic without hand-written SQL for most operations. |
| **Database driver** | `aiosqlite` (local dev) / `asyncpg` (production) | SQLAlchemy's async engine needs an async-capable driver. SQLite (`aiosqlite`) is used for local development because it needs zero setup; `asyncpg` talks to the production Postgres database. |
| **Database (production)** | [Neon](https://neon.tech/) (serverless Postgres) | Free-tier, always-on-URL Postgres — no local Postgres install required, and it's what the app's `DATABASE_URL` points at once deployed. Chosen over SQLite-in-production because SQLite's on-disk file cannot safely be relied on in a hosting environment with an ephemeral filesystem (see [hosting note](#hosting-render) below). |
| **Templating** | [Jinja2](https://jinja.palletsprojects.com/) | Server-side HTML rendering for all pages (`result.html`, `history.html`, account pages, etc.), kept deliberately simple/server-rendered rather than adding a separate frontend framework. Autoescaping is explicitly enabled (`select_autoescape(["html"])`) — a plain `jinja2.Environment` defaults to autoescape *off*, unlike FastAPI's `Jinja2Templates`, so this has to be set explicitly rather than assumed. |
| **Data validation** | [Pydantic](https://docs.pydantic.dev/) | Comes bundled with FastAPI; validates request/response shapes and config/environment variables. |
| **AI narrative generation** | [Google Gemini API](https://ai.google.dev/) (`google-genai`, free tier) | Generates the plain-English summary layer described in [Section III](#iii-app--user-workflow), kept strictly separate from the deterministic SQL/ADQL resolution layer so it can never introduce a factual error the catalog data didn't already contain. |
| **Auth** | Starlette `SessionMiddleware` + `bcrypt` | Session-cookie based login (no separate session-token table — see the `users` table in [Section VI](#vi-database-schema)). Passwords are hashed with bcrypt, must be at least 8 characters, and are never stored or logged in plaintext. All mutating POST routes (register/login/logout/favorite/unfavorite/regenerate) are protected by a session-bound CSRF token — see FAQ [G](#g-how-is-csrf-protection-implemented). |
| **Markdown rendering** | `markdown-it-py` | Renders the Gemini-generated summary text (plain Markdown, no HTML) safely into HTML for display. |
| **Testing** | `pytest` / `pytest-asyncio` | Unit and route-level tests across the resolver, cache, rate-limiting, and summary-generation logic. |

<a id="hosting-render"></a>
**Hosting (planned):** The intended deployment target is [Render](https://render.com/) (free-tier web service), connected to this GitHub repo for automatic redeploys on push. **This is not yet live** — deployment is still a planned next step, currently blocked on a free-tier card-verification issue. The app already reads its database connection from a `DATABASE_URL` environment variable specifically so it can point at Neon in production without any code changes.

---

## **III. User Workflow**

Every core feature — search, cross-matching, and AI summaries — works fully anonymously. Logging in adds personalization on top; nothing is gated behind an account except the things listed in III.A below.

### A. Logged-in users

Logging in is session-cookie based (email + password, no third-party auth). Once logged in, you additionally get:

- **Saving objects.** Favorite/unfavorite any resolved or partial object from its result page. Your saved list lives at `/account/saved`.
- **A personal copy of "your" AI summary.** AI summaries are shared globally—one per object, shown to every visitor—but a logged-in user also gets a personal snapshot recording the exact text that existed the moment they generated or last regenerated it. If someone else later regenerates the shared summary, your snapshot doesn't silently change underneath you; you're just shown a note that a newer version exists. (This is the ownership guarantee behind FAQ C.)
- **A personal Gemini API key (optional).** From `/account/settings`, you can add your own Gemini API key and, optionally, a preferred model. This is a *fallback only*; every generation still tries the app's own shared key first, and your key is only used if that specifically comes back rate-limited/quota-exhausted. Because summaries are shared, generating one with your key unblocks it for every future visitor, not just you; the settings page states this explicitly before you save a key. Your key is encrypted before storage and is never shown back to you or anyone else once saved; the field just shows "a key is currently saved" or not. Clearing the field and saving removes it.
- **Fair-use protection**, enforced two ways regardless of login state, but tracked per-account rather than per-browser-session once logged in: a 5-minute per-object cooldown on Regenerate, and a 20-requests/hour limit across all objects (see FAQ D).

### B. Anonymous (not logged in) users

No feature requiring scientific correctness is behind a login wall — search, cross-matching, and generating AI summaries all work the same as for a logged-in user. What's different:

- **No saving.** Clicking Favorite redirects you to `/login` first (see FAQ F), then back to the object page once you've logged in.
- **No personal summary snapshot or personal API key.** You always see the current shared summary as-is; there's nothing to configure a fallback key against without an account to store it on.
- **Rate limiting still applies**, tracked by an anonymous session cookie rather than a user ID — so it resets if you clear cookies, but also can't be raised by adding your own key, since that requires an account.
- **`/history` is global** either way (see FAQ E) — recently resolved objects aren't tied to who searched for them.

### C. Running it yourself

1. **Clone and install.**

```bash
   git clone https://github.com/Akirenaki/astronomy-multi-catalog-cross-matcher.git
   cd astronomy-multi-catalog-cross-matcher
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
```

2. **Create a `.env` file** in `app/` (a repo-root `.env` also works for `GEMINI_API_KEY`/`SESSION_SECRET_KEY` specifically, but `app/.env` is the one that reliably works for everything below; see the caveat under `DATABASE_URL`/`USER_SECRET_ENCRYPTION_KEY`):

   | Variable | Required? | Purpose |
   | --- | --- | --- |
   | `GEMINI_API_KEY` | Optional | The app's own shared Gemini key from [Google AI Studio](https://aistudio.google.com/apikey) (No card needed). Without it, AI summaries just show "No summary available."; everything else works normally. |
   | `DATABASE_URL` | Optional | Defaults to a local SQLite file (`astronomy.db`) if unset — nothing to configure for local dev. Point this at a Postgres connection string (e.g. from [Neon](https://neon.tech/)) for production. **Currently must be set as a real process environment variable, not just `.env`** — see the import-order note above; a `.env`-only value is silently ignored right now. |
   | `SESSION_SECRET_KEY` | Recommended | Signs login session cookies. Without it, the app runs fine but generates a random key per process, so everyone gets logged out on every restart. Generate one with `python -c "import secrets; print(secrets.token_hex(32))"`. This one *does* work via `.env` today. |
   | `USER_SECRET_ENCRYPTION_KEY` | Recommended if you'll use personal Gemini keys | Encrypts personal Gemini API keys at rest. Without it, the app runs fine, but any saved personal keys become unreadable after a restart (a random key is generated per process, with a warning logged). Must be a valid Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. **Same current limitation as `DATABASE_URL`** — set it as a real environment variable, not just `.env`, until the load-order issue above is fixed. |

3. **Reset the database schema** (safe on a fresh clone; this just creates tables, there's nothing to lose yet):

```bash
   python reset_db.py
```

4. **Run it:**

```bash
   uvicorn app.main:app --reload
```

See [Section II](#ii-tech-stack--hosting) for what "deployed" (vs. local dev) looks like — this section is specifically about getting it running on your own machine.

---

## **IV. Application Processing Pipeline**

This is what actually happens between a search request and a rendered result; the backend counterpart to Section III. None of these steps are directly visible in the UI as separate moments; they either happen inside a single page load or inside a single background fetch.

1. **Normalise** the query (fix whitespace/casing, canonicalise catalog prefixes like HD/HIP/GJ/TYC without stripping them).
2. **Check the cache** — first an exact match on the normalized query string, then a fallback through the `query_aliases` table (so a different alias for an already-cached object, or the object's own canonical SIMBAD ID, still hits the cache instead of re-querying). A hit here skips everything below and serves immediately.
3. **[Resolve](#a-what-does-resolve-mean)** identity against SIMBAD via a TAP/ADQL query: canonical name, coordinates, spectral type, and every known alias, in one round trip.
4. **Expand aliases** (adding stripped catalog-prefix variants) to maximize the next step's hit rate.
5. **Cross-match** those aliases against the NASA Exoplanet Archive in a single batched query (not one HTTP request per alias) to find any known orbiting planets.
6. **Classify** the result into one of [five explicit states](#b-what-are-the-five-states)—`RESOLVED`, `PARTIAL`, `AMBIGUOUS`, `UNRESOLVED`, `LOOKUP_FAILED`—rather than quietly picking one answer or silently failing. A `PARTIAL` result is further flagged if the "no planets" conclusion is itself unconfirmed (the Exoplanet Archive lookup failed, rather than a genuine zero-match).
7. **Cache** the result: 14-day TTL for a confirmed result, 1 hour for anything unconfirmed or failed (`UNRESOLVED`/`AMBIGUOUS`/`LOOKUP_FAILED`, or a `PARTIAL` with an unconfirmed planet count), so failures self-heal quickly instead of sitting wrong for two weeks.
8. **Render** the result page with the scientific data only. AI generation is deliberately *not* part of this synchronous path for the HTML `/search` flow; it only runs when the user explicitly clicks Generate (see III.A/III.B), so a slow Gemini call (observed up to ~42s for a single heavily-catalogued star) never blocks the page it's summarizing. **This is asymmetric with the JSON API**: `GET /api/resolve?q=...` *does* generate the AI summary synchronously as part of the same request, since there's no follow-up click available in a pure API context.
9. **On a Generate/Regenerate request**, call Gemini using the app's own shared key; if that's specifically rate-limited and the requesting user has a personal key configured, retry once with theirs (see III.A). Whichever key succeeds, persist the result as the one shared `ai_summary` for that object, kept strictly separate from the scientific data so the AI layer can never corrupt or override what the SQL layer already established.

Programmatic access to steps 1–7 (without the templated HTML) is available via `GET /api/resolve?q=...`, returning the same resolution data as JSON — see step 8's note on the one behavioral difference from the HTML flow.

---

## **V. Result examples**

| Search | Expected state | Why |
| --- | --- | --- |
| `"51 Peg"` | RESOLVED | Famous exoplanet host, well-catalogued |
| `"51 Pegasi"` | RESOLVED | Same star, different name — should resolve to same planet |
| `"HD 217014"` | RESOLVED | Catalog ID for the same star — should match via alias lookup |
| `"Betelgeuse"` | PARTIAL | Real, famous star, but no catalogued planets |
| `"Proxima Centauri"` | RESOLVED | Closest star to the Sun, has confirmed exoplanet(s) |
| `"The Sun"` or `"Sol"` | UNRESOLVED | (Probably — SIMBAD might not index "Sol" as an alternate name) |
| `"Beta Cen"` | AMBIGUOUS | (Possibly — if SIMBAD lists both the primary and companion) |
| `"asdfjkl"` | UNRESOLVED | Gibberish |
| `"HD 217014"` (SIMBAD unreachable — network timeout, firewalled host, etc.) | LOOKUP_FAILED | SIMBAD was never actually reached, so this is not a real "no match" |

---

## **VI. Database Schema**

The application utilizes a relational structure (SQLAlchemy) to cache SIMBAD resolutions, cross-matched planet data, and AI-generated narratives, plus (optionally) accounts, favorites, personal summary snapshots, and rate-limit bookkeeping.

<details>
<summary><b>View Database Table Definitions (Click to expand)</b></summary>

### 1. The `objects` Table

This is the core table of the application. It caches the primary astronomical data fetched from SIMBAD and tracks the lifecycle of the search query.

| Column Name | Data Type | Nullable? | Constraints / Notes |
| :--- | :--- | :--- | :--- |
| **id** | INTEGER | No | PRIMARY KEY |
| **simbad_main_id** | VARCHAR | Yes | UNIQUE |
| **query_text** | VARCHAR | No | |
| **ra_deg** | FLOAT | Yes | Right Ascension (degrees) |
| **dec_deg** | FLOAT | Yes | Declination (degrees) |
| **otype** | VARCHAR | Yes | Object Type |
| **spectral_type** | VARCHAR | Yes | |
| **resolution_state** | VARCHAR | No | CHECK (`RESOLVED`, `AMBIGUOUS`, `PARTIAL`, `UNRESOLVED`, `LOOKUP_FAILED`) |
| **planets_lookup_failed** | BOOLEAN | No | Default `False` |
| **ai_summary** | TEXT | Yes | |
| **ai_summary_generated_at** | DATETIME | Yes | Set on real (re)generation only, never on a cache hit |
| **candidates_json** | TEXT | Yes | |
| **resolved_via_json** | TEXT | Yes | |
| **resolved_at** | DATETIME | No | |
| **expires_at** | DATETIME | No | |

#### Detailed Column Explanations for `objects`

- **`id`**: Unique internal auto-incrementing identifier for each master object record.
- **`simbad_main_id`**: The canonical, standard primary identifier returned by the SIMBAD database. Enforces a `UNIQUE` constraint so we never duplicate the same real-world celestial object in our cache.
- **`query_text`**: The exact, raw (normalized) string this row was most recently resolved from. Repeat searches of the *same* string hit this column directly; a *different* alias for an already-cached object (e.g. searching "51 Pegasi" after "51 Peg") is instead served via the `query_aliases` table (see below) rather than triggering its own live SIMBAD round trip.
- **`ra_deg`**: Right Ascension converted to decimal degrees. Represents the celestial equivalent of longitude. Nullable if the object cannot be resolved or lacks spatial coordinates.
- **`dec_deg`**: Declination converted to decimal degrees. Represents the celestial equivalent of latitude.
- **`otype`**: The astronomical object classification returned by SIMBAD (e.g., Star, High proper-motion Star, White Dwarf).
- **`spectral_type`**: The spectral classification of the star (e.g., `G5V`), indicating its temperature, luminosity, and evolutionary stage.
- **`resolution_state`**: The core state engine value of the application. Restricted by a `CHECK` constraint to exactly five mutually exclusive states: `RESOLVED`, `AMBIGUOUS`, `PARTIAL`, `UNRESOLVED`, or `LOOKUP_FAILED`.
- **`planets_lookup_failed`**: `True` only for a `PARTIAL` row where "no planets" is unconfirmed because the NASA Exoplanet Archive lookup itself failed (timeout/transport error), rather than a genuine, confirmed zero-match. Drives both the shorter 1-hour cache TTL for that case (see `expires_at` below) and an extra caveat shown in the PARTIAL result banner. Always `False` for every other resolution state.
- **`ai_summary`**: The plain-language narrative generated by the Gemini API. Stored here safely as a string text layer so it cannot touch or corrupt the scientific coordinate float data.
- **`ai_summary_generated_at`**: Timestamp of the last real generation (initial Generate or a Regenerate) — never updated on a cache hit. Drives the 5-minute per-object cooldown on regeneration; null until a summary has been generated at least once.
- **`candidates_json`**: A stringified JSON array utilized when a state is `AMBIGUOUS`. It stores the basic data of multiple potential stellar matches so the UI can render a disambiguation selection list. If SIMBAD matched more than 10 objects, only the first 10 are stored and each carries a `candidates_truncated: true` marker so the UI can say so rather than presenting a silently incomplete list.
- **`resolved_via_json`**: A stringified JSON audit trail recording exactly how the app navigated from the raw query to the final match (e.g., `User Input -> SIMBAD Alias -> NASA Exoplanet ID`).
- **`resolved_at`**: The precise timestamp of when the external API lookup was performed and recorded.
- **`expires_at`**: The calculated cache expiration timestamp. Used by the background logic to enforce the 14-day TTL for successful resolutions, and a shorter 1-hour TTL for `UNRESOLVED`/`AMBIGUOUS`/`LOOKUP_FAILED` rows *and* for a `PARTIAL` row with `planets_lookup_failed=True` — none of those are confident enough conclusions to sit uncorrected for two weeks.

---

### 2. The `identifiers` Table

Astronomical objects go by dozens of cross-catalog names. This table stores all known alternate aliases for a cached object, enabling secondary catalog cross-matching.

| Column Name | Data Type | Nullable? | Constraints / Notes |
| :--- | :--- | :--- | :--- |
| **id** | INTEGER | No | PRIMARY KEY |
| **object_id** | INTEGER | No | FOREIGN KEY (`objects.id`) ON DELETE CASCADE |
| **catalog** | VARCHAR | No | |
| **identifier** | VARCHAR | No | |
| **matched_exoplanet_archive** | BOOLEAN | No | |

#### Detailed Column Explanations for `identifiers`

- **`id`**: Internal unique identifier for the alias record.
- **`object_id`**: Foreign key linking the alias back to its parent record in the `objects` table. Includes `ON DELETE CASCADE` so if a cached object is cleared, its alias history is wiped automatically.
- **`catalog`**: The name of the specific star catalog recording this alias (e.g., `HD`, `HIP`, `TYC`).
- **`identifier`**: The specific designation/number assigned within that catalog (e.g., `217014`).
- **`matched_exoplanet_archive`**: A boolean flag indicating whether this specific alias successfully triggered a matching record in the NASA Exoplanet Archive database during the cross-identification process.

*Note: Enforces a composite `UNIQUE (object_id, catalog, identifier)` constraint to prevent duplicate alias mappings for the same object.*

---

### 3. The `planets` Table

Stores structural data for confirmed exoplanets tied to host stars, sourced from the NASA Exoplanet Archive.

| Column Name | Data Type | Nullable? | Constraints / Notes |
| :--- | :--- | :--- | :--- |
| **id** | INTEGER | No | PRIMARY KEY |
| **object_id** | INTEGER | No | FOREIGN KEY (`objects.id`) ON DELETE CASCADE |
| **pl_name** | VARCHAR | No | |
| **pl_letter** | VARCHAR | Yes | |
| **orbital_period_days** | FLOAT | Yes | |
| **planet_radius_earth** | FLOAT | Yes | |
| **discovery_year** | INTEGER | Yes | |
| **discovery_method** | VARCHAR | Yes | |

#### Detailed Column Explanations for `planets`

- **`id`**: Internal unique identifier for the exoplanet record.
- **`object_id`**: Foreign key linking the planet to its host star system in the `objects` table. Enforces `ON DELETE CASCADE`.
- **`pl_name`**: The official, complete canonical name of the exoplanet (e.g., `51 Peg b`).
- **`pl_letter`**: The lower-case letter designation assigned to the planet based on its order of discovery in the system (typically starting at `b`).
- **`orbital_period_days`**: The amount of time (measured in Earth days) the planet takes to complete one full revolution around its host star.
- **`planet_radius_earth`**: The physical size of the exoplanet expressed as a multiple of Earth's radius ($R_\oplus$).
- **`discovery_year`**: The calendar year the exoplanet's discovery was officially confirmed and published (e.g., `1995`).
- **`discovery_method`**: The scientific technique utilized by astronomers to detect the planet (e.g., `Radial Velocity`, `Transit`).

---

### 4. The `users` Table

Registered accounts. Session-cookie based auth (Starlette `SessionMiddleware`) — no separate session-token table.

| Column Name | Data Type | Nullable? | Constraints / Notes |
| :--- | :--- | :--- | :--- |
| **id** | INTEGER | No | PRIMARY KEY |
| **email** | VARCHAR | No | UNIQUE |
| **password_hash** | VARCHAR | No | bcrypt |
| **created_at** | DATETIME | No | |

#### Detailed Column Explanations for `users`

- **`id`**: Internal unique identifier for the account.
- **`email`**: Login identifier. `UNIQUE` constraint enforced at the database level — registration attempts an insert and catches the resulting integrity error rather than pre-checking existence with a separate query, avoiding a check-then-act race between two concurrent registrations for the same email.
- **`password_hash`**: Bcrypt hash of the password. The plaintext password is never stored or logged.
- **`created_at`**: Account creation timestamp.

---

### 5. The `saved_searches` Table

A logged-in user's favorited objects. Minimal MVP shape — just the link and a timestamp, no note/label field yet.

| Column Name | Data Type | Nullable? | Constraints / Notes |
| :--- | :--- | :--- | :--- |
| **id** | INTEGER | No | PRIMARY KEY |
| **user_id** | INTEGER | No | FOREIGN KEY (`users.id`) ON DELETE CASCADE |
| **object_id** | INTEGER | No | FOREIGN KEY (`objects.id`) ON DELETE CASCADE |
| **created_at** | DATETIME | No | |

#### Detailed Column Explanations for `saved_searches`

- **`id`**: Internal unique identifier for the favorite record.
- **`user_id`**: The user who favorited the object. `ON DELETE CASCADE` — deleting a user removes their favorites.
- **`object_id`**: The favorited object. `ON DELETE CASCADE` — deleting a cached object removes any favorites pointing at it.
- **`created_at`**: When the object was favorited; drives the ordering on `/account/saved` (most recent first).

*Note: Enforces a composite `UNIQUE (user_id, object_id)` constraint — a user can favorite a given object at most once. Re-favoriting an already-favorited object is treated as a no-op, not an error.*

---

### 6. The `user_summary_snapshots` Table

A logged-in user's personal copy of the AI summary they most recently generated/regenerated for a given object — the mechanism behind the ownership guarantee described in [Section IV](#iv-accounts-favorites--personal-ai-summaries).

| Column Name | Data Type | Nullable? | Constraints / Notes |
| :--- | :--- | :--- | :--- |
| **id** | INTEGER | No | PRIMARY KEY |
| **user_id** | INTEGER | No | FOREIGN KEY (`users.id`) ON DELETE CASCADE |
| **object_id** | INTEGER | No | FOREIGN KEY (`objects.id`) ON DELETE CASCADE |
| **summary_text** | TEXT | No | |
| **created_at** | DATETIME | No | |

#### Detailed Column Explanations for `user_summary_snapshots`

- **`id`**: Internal unique identifier for the snapshot record.
- **`user_id`** / **`object_id`**: Which user, and which object, this snapshot belongs to. Both `ON DELETE CASCADE`.
- **`summary_text`**: The AI narrative text as it existed at the moment this user generated/regenerated it. Entirely separate storage from `objects.ai_summary` — this table is never the source of the shared canonical summary, only a personal copy of it.
- **`created_at`**: When this user's snapshot was captured/last updated.

*Note: Enforces a composite `UNIQUE (user_id, object_id)` constraint — each user has at most one snapshot per object, their most recent own generation. A user regenerating overwrites their own snapshot; another user's regenerate never touches it.*

---

### 7. The `rate_limit_events` Table

An event log of Gemini-quota-spending actions (Generate/Regenerate clicks), used to enforce the per-client rate limit described in [Section IV](#iv-accounts-favorites--personal-ai-summaries). Deliberately a log table rather than a counter column, so a sliding window can be computed by counting rows rather than resetting a counter on a timer.

| Column Name | Data Type | Nullable? | Constraints / Notes |
| :--- | :--- | :--- | :--- |
| **id** | INTEGER | No | PRIMARY KEY |
| **subject_type** | VARCHAR | No | CHECK (`user`, `session`) |
| **subject_id** | VARCHAR | No | |
| **created_at** | DATETIME | No | |

#### Detailed Column Explanations for `rate_limit_events`

- **`id`**: Internal unique identifier for the log entry.
- **`subject_type`**: `user` for a logged-in request (limited per `users.id`) or `session` for an anonymous request (limited per the Starlette session-cookie id, assigned to every visitor regardless of login state).
- **`subject_id`**: The `users.id` or session id this event counts against, as a string — not a foreign key to `users.id`, since one column needs to hold both kinds of identifier uniformly, and log rows should survive a user account being deleted rather than needing `ON DELETE` handling on what's really just an audit trail.
- **`created_at`**: When the request was made — the sliding window (20 requests/hour) is computed by counting rows newer than `now - 1 hour` for the same `(subject_type, subject_id)` pair.

### 8. The `query_aliases` Table

A lightweight secondary index mapping every (normalized) query string that has ever resolved to a given object -- including its own canonical `simbad_main_id` -- onto that object's `objects.id`. `objects.query_text` only ever holds the *one* string the row was most recently resolved from, so without this table, a different alias for an already-cached object (e.g. "51 Peg" vs. "51 Pegasi" vs. "HD 217014", or looking an object up by its own SIMBAD ID rather than the string originally searched) would miss the cache and trigger its own redundant SIMBAD + Exoplanet Archive round trip.

| Column Name | Data Type | Nullable? | Constraints / Notes |
| :--- | :--- | :--- | :--- |
| **id** | INTEGER | No | PRIMARY KEY |
| **query_text** | VARCHAR | No | UNIQUE |
| **object_id** | INTEGER | No | FOREIGN KEY → `objects.id`, `ON DELETE CASCADE`, indexed |
| **created_at** | DATETIME | No | |

#### Detailed Column Explanations for `query_aliases`

- **`id`**: Internal unique identifier for the alias row.
- **`query_text`**: A normalized query string that has, at some point, resolved to `object_id`. `UNIQUE` so each string points at exactly one (the most recent) object.
- **`object_id`**: The object this string currently resolves to. Repointed in place (not duplicated) if the same string is later resolved and maps to a different object.
- **`created_at`**: When this alias mapping was created or last repointed.

</details>

---

## **VII. FAQ**

### **A. "What does 'resolve' mean?"**

<details>
<summary><b>View Resolve Explanation (Click to expand)</b></summary>

**"Resolve"** = take an informal, ambiguous star name and turn it into a confirmed astronomical identity. The app is answering the question: "**Do we know what object the user is asking about, and can we find it across multiple independent catalogs?**"

Example: the user types `"51 peg"` (lowercase, casual spelling of a real star). The app resolves this by:

1. Normalising it to `"51 Peg"` (correct casing)
2. Asking SIMBAD: "Do you know an object called '51 Peg'?" → yes, it's `"51 Pegasi"` (canonical name)
3. Getting back aliases: `["HD 217014", "HIP 113357", "51 Peg", "51 Pegasi", ...]`
4. Asking NASA Exoplanet Archive: "Do any of these aliases host known planets?" → yes, `"HD 217014"` does
5. Getting back planet data: `51 Peg b` (an exoplanet, discovered 1995)

At each step, gains more certainty about what the user was asking for. "Resolve" is successfully completing that chain.

</details>

### **B. "What are the five states?"**

<details>
<summary><b>View Five States Explanation (Click to expand)</b></summary>

- #### **RESOLVED** ✓✓

    **Meaning:** Identity confirmed *and* planets found.

    **What happened:** SIMBAD matched the query unambiguously (one object), and at least one of its aliases matched in the Exoplanet Archive.

    **What the user sees:** Full profile — spectral type, coordinates, orbital elements of its planets, the AI summary, a "resolved via: 51 Peg → 51 Pegasi → HD 217014 → [51 Peg b found]" trail showing the chain of lookups.

    **Example:** User searches `"51 Peg"` → RESOLVED (it's a real star with a real known planet).

- #### **PARTIAL** ✓✗

    **Meaning:** Identity confirmed, but no planets in the catalog.

    **What happened:** SIMBAD matched unambiguously (one object), but none of its aliases turned up anything in the Exoplanet Archive.

    **What the user sees:** Star's basic data (spectral type, coordinates, object type), but an explicit note: *"No planets were found in the Exoplanet Archive for this object."* (Important: this doesn't mean the star *has no* planets in reality — just that the NASA catalog doesn't list any, which is common for faint/distant/newly-discovered stars.)

    **Example:** User searches `"Barnard's Star"` (a real, nearby star) → PARTIAL (it's confirmed real, but the NASA archive hasn't catalogued any orbiting planets for it, though astronomers suspect there might be one).

- #### **AMBIGUOUS** ⚠️

    **Meaning:** The input matched multiple candidates — we can't safely guess which one you meant.

    **What happened:** SIMBAD returned more than one possible object for the query. (This happens surprisingly often with informal names.)

    **What the user sees:** A **disambiguation list**, showing each candidate with its canonical name, object type, spectral type, and coordinates — clickable links to re-search with the correct SIMBAD identifier, so the user can pick the right one and get a full profile.

    **Example:**

  - User searches `"51"` (way too vague) → AMBIGUOUS (there are hundreds of objects with "51" in their name)
  - User searches `"Beta Cen"` (could mean the primary star Beta Centauri or its close binary companion) → AMBIGUOUS (two distinct objects in SIMBAD)

- #### **UNRESOLVED** ✗

    **Meaning:** Query failed — SIMBAD returned nothing.

    **What happened:** Either the object genuinely doesn't exist in SIMBAD, or it was misspelled/malformed beyond recognition.

    **What the user sees:** A clear message: *"No SIMBAD match was found for this query."* Suggests checking spelling or trying a different identifier.

    **Example:**

  - User searches `"asdkfjhasdf"` (gibberish) → UNRESOLVED
  - User searches `"Foo's Nebula"` (made-up object) → UNRESOLVED
  - User searches `"Amphoreus"` (fiction) → UNRESOLVED

- #### **LOOKUP_FAILED** ⚠

    **Meaning:** The SIMBAD request itself could not be completed — this is not a verdict about the object at all.

    **What happened:** A transport-level failure (connection timeout, DNS failure, connection refused), a bad HTTP status from SIMBAD, or a response body that couldn't be parsed. Crucially, SIMBAD's TAP endpoint was never successfully queried, so nothing was actually checked.

    **Why this is a separate state from UNRESOLVED:** Early versions of this app treated every SIMBAD failure — timeouts included — the same as "no match found," which silently reported network problems as if the object didn't exist. A firewalled or unreachable network (e.g. some school/office networks block `simbad.cds.unistra.fr` outright) would then look identical to a genuinely nonexistent star.

    `LOOKUP_FAILED` keeps that distinction explicit.

    **What the user sees:** A message explaining that the *lookup* failed, not the object, along with a link to retry the same query.

    **Caching:** Same short, self-healing 1-hour TTL as `UNRESOLVED`/`AMBIGUOUS`, rather than the full 14-day TTL, so a transient network issue doesn't get "cached" as a wrong answer for two weeks. No AI summary is generated for this state, since there's no confirmed structured data to describe.

    **Example:**

  - User searches `"HD 217014"` from a network that can't route to SIMBAD at all → LOOKUP_FAILED
  - User searches `"51 Peg"` and SIMBAD's TAP service returns a 503 → LOOKUP_FAILED

#### **Decision Tree**

```text
Was SIMBAD actually reachable, and did it return a usable response?
├─ NO (timeout / transport error / bad status / unparseable response) → LOOKUP_FAILED (end)
└─ YES
   └─ Does SIMBAD know this object?
      ├─ NO  → UNRESOLVED (end)
      └─ YES, but is it ambiguous?
         ├─ YES (multiple candidates) → AMBIGUOUS (show list, end)
         └─ NO (one object)
            └─ Was the Exoplanet Archive itself reachable?
               ├─ NO (timeout / transport error / unparseable response)
               │     → PARTIAL, planets_lookup_failed=True (short 1-hour TTL, retried
               │       automatically -- this is an unconfirmed "no planets", not a
               │       confirmed one; see objects.planets_lookup_failed in §VI.1)
               └─ YES
                  └─ Does it have planets for this object?
                     ├─ YES → RESOLVED (show full profile)
                     └─ NO  → PARTIAL, planets_lookup_failed=False (confirmed no planets)

```

**Each state is mutually exclusive and exhaustive** — every possible search outcome falls into exactly one bucket. `planets_lookup_failed` is an additional flag layered on top of PARTIAL, not a sixth state, since both cases render the same "matched, but no planets" page shape and only differ in how confident that "no planets" claim is and how long it's cached for.

</details>

### **C. "My saved summary looks different from what's shown to everyone else now; is that a bug?"**

<summary><b>View Saved Summary Explanation (Click to expand)</b></summary>

No, this is expected, and it's the point of `user_summary_snapshots`.

There is exactly one canonical AI summary per object (`objects.ai_summary`), shown to every anonymous visitor and to any logged-in user who hasn't generated their own. If you're logged in and you personally clicked Generate or Regenerate, `/account/saved` shows *your* copy from that moment, even if someone else regenerates the shared version afterward.

This is an ownership/no-clobber guarantee, not the AI narrative varying its actual content by user. If you and another user both generate at the same point in time, from the same underlying data, you'll get the same text.

### **D. "Why two separate limits (a 5-minute cooldown AND a 20/hour rate limit) instead of just one?"**

<summary><b>View Limits Explanation (Click to expand)</b></summary>

They stop different failure modes:

- The **cooldown** is per-*object* — it stops rapid double-clicking Regenerate on the same star. It does nothing to stop someone clicking Generate on twenty different stars in a row.
- The **rate limit** is per-*client* (logged-in user, or anonymous session) — it stops exactly that: one visitor spending Gemini quota across many different objects in a short window, which the cooldown alone can't see, since it only ever looks at one object at a time.

Both checks run on every Generate/Regenerate request; either can reject it independently.

</details>

### **E. "Why is /history global instead of per-user?"**

`/history` is a site-wide feed of recently resolved objects, not a personal activity log. The app already has a private per-user list at `/account/saved`, so keeping `/history` global makes it a shared discovery page instead of duplicating the same concept twice.

### **F. "Why do anonymous favorite/unfavorite actions send me to login?"**

Favorites are tied to a user account. If you are not logged in, the app redirects you to `/login` and then brings you back to the object page after authentication so the action can be completed on the right account.

### **G. "How is CSRF protection implemented?"**

<summary><b>View CSRF Protection Implementation Explanation (Click to expand)</b></summary>

Every mutating route (`/register`, `/login`, `/logout`, favorite/unfavorite, and AI-summary regeneration) requires a CSRF token that must match the one minted for the visitor's own session. Form-based routes carry it as a hidden `csrf_token` field; the one JS-driven route (regenerate-summary, a `fetch()` POST with no form body) sends it as an `X-CSRF-Token` header instead, read from a `<meta name="csrf-token">` tag rendered into every page. The token itself lives in the same signed, `itsdangerous`-backed session cookie the app already uses for login state, so it can't be forged or read cross-origin -- see `app.auth.get_csrf_token`/`verify_csrf_token`.

There is no minimum password complexity requirement beyond an 8-character floor (`app.auth._MIN_PASSWORD_LENGTH`) -- reasonable for a portfolio project, but worth knowing if you're reusing this auth code elsewhere.

</details>

### **H. "How do I query the resolver programmatically?"**

Use `GET /api/resolve?q=...`. It returns the same resolution data as the HTML flow, but as JSON for scripts or other tools.

---

## **VIII. Third-Party Data, Services & Legal Notes**

This project depends on a few external data sources and APIs. **None of this project's own code or content is a substitute for reading each service's actual current terms** — this section is a summary, not a legal opinion, and the app itself is a student/portfolio project, not a commercial product.

- **SIMBAD (Strasbourg Astronomical Data Center / CDS).** SIMBAD is the source of the primary object-resolution step (see [Section III](#iii-user-workflow)). SIMBAD data is queried live via its public TAP service and cached temporarily (14-day TTL) purely to avoid re-querying the same object repeatedly; nothing is redistributed as a dataset. If you reuse this project or publish derived results, include SIMBAD attribution/citation as required by [CDS's current data-use guidance](https://cds.unistra.fr/), which generally asks that published work using SIMBAD data acknowledge the CDS.
- **NASA Exoplanet Archive.** Exoplanet cross-match data (orbital period, radius, discovery method/year) comes from NASA's public Exoplanet Archive TAP service, queried live and cached the same way as SIMBAD data. If you reuse this project's catalog output or publish derived results, follow the [Exoplanet Archive's citation guidance](https://exoplanetarchive.ipac.caltech.edu/docs/acknowledge.html).
- **NASA Image and Video Library (background imagery).** The animated space background pulls images from NASA's public Image and Video Library. NASA media is, with some exceptions (e.g. work by contractors, or content that credits a non-NASA source), generally not copyrighted and free to use, but individual images can carry their own credit line or exception — the app shows an on-page credit for the image currently loaded for exactly this reason. Anyone reusing an image outside this project should check that image's own listing on [images.nasa.gov](https://images.nasa.gov/) and follow NASA's [current media usage guidelines](https://www.nasa.gov/nasa-brand-center/images-and-media/), rather than assuming this project's credit line is a complete rights clearance.
- **Google Gemini API.** AI-generated summaries (see [Section III](#iii-app--user-workflow)) are produced through Google's Gemini API on its free tier. Any use of that feature — by this deployment or by anyone running their own copy of this project — is subject to Google's current [Gemini API terms of service](https://ai.google.dev/gemini-api/terms) and related usage policies. AI-generated text is clearly presented as a generated summary, not as an independent authoritative source, and it is deliberately kept unable to alter or override the underlying SIMBAD/NASA scientific data (see the architectural principle in [Section I](#i-app-description)).
- **No warranty.** This project is provided for educational/portfolio purposes. Astronomical data is only as current and accurate as the upstream SIMBAD/NASA services at query time, and AI-generated summaries may contain errors — neither should be relied on for research, publication, or any decision without independently verifying against the primary catalogs.
