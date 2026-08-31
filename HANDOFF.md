# New Orleans Shop — handoff, 2026-08-31

For a fresh session. Load the `brain` skill first; this file is the state on top of it.

Owner: John (Vaicos). He verifies by **looking**, not by reading rationale. Show him a
render or a screenshot, not a description of one.

---

## 1. What this is

A Minecraft-market Discord bot + website with its own economy: shop, orders, wallet,
casino, prediction markets. `CONTRACT.md` in the repo is the **binding spec** — code that
contradicts it is a defect, and if a change must contradict it, the file changes first.

- Repo (his machine): `C:\Users\Vaicos\Desktop\AI\New Orleans Shop`
- GitHub: `https://github.com/Vaicosek/New-Orleans-Shop` (public, branch `main`)
- Live site: `https://neworleansshop.org` — Cloudflare proxied A record → Origin Rule
  rewriting to port **9543** → Flexible SSL. Verified by reading back, not assumed.
- Host: Wispbyte, panel at **`wispbyte.com/client`** (NOT `panel.wispbyte.com`, which does
  not resolve). Server `bb1e15db`, pages at `/client/servers/bb1e15db/{console,github,startup,files}`.
- Brain: `C:\AI Brain\brain` (live, authoritative). The packaged `references/` copy inside
  the skill is a STALE cache — never answer from it.

## 2. How to deploy (this exact sequence, it has failed every other way)

1. Edit on his machine via `device_bash` at `$HOME/mnt/New Orleans Shop`.
2. `python3 run_tests.py` — **never pytest**, it dies with INTERNALERROR. 17 files, all
   must pass.
3. Commit with `git -c user.name=... -c user.email=...`. The mount forbids `unlink`, so
   `mv` any `.git/*.lock` into `_to_delete/` first; the object-file warnings are harmless.
4. **Push via the GitKraken MCP with the WINDOWS path** (`C:\Users\Vaicos\...`). Pushing
   from the mounted shell fails: "could not read Username for github".
5. Wispbyte → GitHub tab → Pull → Pull Changes. The first click after page load often
   does not register; click, then confirm the modal is open before clicking Pull Changes,
   or drive it with `javascript_tool` by button text. **A successful pull already restarts
   the server** — do not restart again.
6. Verify by reading the pull log for `Updating <old>..<new>` and `Fast-forward`, then the
   live page or the console. A clicked button is not a deploy.

## 3. Current state — live and working

`31fb9d8` is deployed. 17/17 tests pass.

- **Website**: storefront, `/inventory` (`/stock` still served, same handler, so old links
  work), `/me` hub, `/ledger` (staff). Design: cast-iron quatrefoil SVG tile as the page
  ground, the New Orleans flag's red/white/blue band replacing the masthead rule and
  marking each price-sheet category, a gold fleur-de-lis mark, condensed sans headings
  (Arial Narrow stack) with Helvetica body at 18px. Contrast measured against the tile's
  lightest pixel, not the flat ground: text 14.4:1, dim 10.9:1, inert 7.5:1. `--loss` is a
  stated exception at 7.05:1 — red cannot be both red and that bright on this navy.
- **Bot**: 8 cogs, 8 slash commands, `/setup` provisions 12 channels and roles. Embed
  colour bar now carries meaning (neutral / brand / gain / loss / warn) instead of gold on
  everything; red is only on genuinely destructive steps.
- **Reference market** (`CONTRACT.md` §12): pulls DiplomaticaMC's public market every 6h
  for pricing and demand. **Currently blocked — HTTP 403, Cloudflare at their edge.** Their
  robots.txt grants `use=reference`; the edge refuses anyway. Do NOT spoof a browser
  User-Agent to get past it. The fix is John asking their operator to allow the client.
  Everything downstream is built and tested against a real captured payload.

## 4. THE WORK QUEUE — 19 confirmed defects, ranked

From audit round `wf_14c26046-616`: 29 raised, **19 survived blind refutation** (66%).
Nothing below is fixed. Fix in this order; 1–3 are money and trust.

1. **A prediction market can never be closed to new stakes — insiders can bet after the outcome is known**
   `core/predictions.py`
   Fix: Call predictions.close() at the start of the resolve flow — the moment staff pick the market to resolve, before the preview and confirm modal — and have resolve() require a closed market so no stake can land inside the confirm window.

2. **Order approval — the one irreversible treasury payout — never shows the amount that will be paid**
   `bot/views/orders.py`
   Fix: Compute the exact total payout (and the per-claim breakdown) before rendering the Approve gate and put the gold figure in the confirmation text and button, so the number staff confirm is the number that leaves treasury.

3. **Fairness verify shows only two of four checks — a tampered round renders True / True / INVALID**
   `bot/views/casino.py`
   Fix: Render all four verify() checks with their pass/fail state and name the failing one in plain words, and show the pre-bet commitment hash from the commitment row (not the round row) so the value the player compares is the one they were actually committed to.

4. **gambling_blocked self-exclusion is enforced but can never be set — money.set_flag has no callers**
   `core/money.py`
   Fix: Add a staff action (and ideally a self-serve one) that writes the wallet_flags row for gambling_blocked and a freeze/unfreeze action that sets wallets.frozen, so both enforced states have a reachable writer.

5. **Orders channel resolved from env only — on a /setup-provisioned server no order card is ever posted**
   `bot/cogs/shop.py`
   Fix: Resolve the orders channel through bot/layout.py (the /setup-provisioned channel) with the env var only as an override, and make posting failure surface an explicit error instead of a success message.

6. **A prediction market with an outcome label over 100 characters can never be resolved**
   `bot/views/admin.py`
   Fix: Cap outcome labels at what the confirmation input can hold when the market is opened, and make the resolve confirmation compare against a short token (outcome index or truncated label) rather than the full label.

7. **The item picker shows only the first 25 catalog items and cannot be searched**
   `bot/views/pickers.py`
   Fix: Paginate the picker (or add a category/search step that narrows to under 25 before the Select is built) so every active item is reachable from customer and admin flows alike.

8. **One commitment is minted per game-pick and consumed by the first settlement, so every later bet fails**
   `bot/views/casino.py`
   Fix: Mint a fresh commitment per bet at the point the bet is submitted rather than once per game-pick, so each round gets its own unrevealed commitment.

9. **/shop replaces the whole price sheet with one sentence when nothing is in stock**
   `bot/views/shop.py`
   Fix: Always render categories, names and prices, marking each item's stock as zero, and reserve the 'not stocked yet' line for a genuinely empty catalog.

10. **An item can never be retired — catalog.deactivate_item has no caller**
   `core/catalog.py`
   Fix: Wire a Retire / Restore action into the admin item flow that calls deactivate_item (and its inverse), or let update_item set active.

11. **Public order card prints the wallet subject u:<discord_id> where a member's name belongs**
   `bot/views/orders.py`
   Fix: Store or resolve the Discord user id from order_claims.worker and render it as a mention or display name on the card, keeping u:<id> as the wallet subject internally only.

12. **Wallet activity prints the ledger's internal reason string, including raw round and market ids**
   `bot/views/wallet.py`
   Fix: Render history entries from their structured fields into human sentences (game name, market question, order number), keeping the raw reason string as an internal detail.

13. **Fairness verify panel prints internal column names and two unreadable 64-char hex blobs**
   `bot/views/casino.py`
   Fix: Label the fields in plain words, show hashes truncated with a copyable full value, and lead with a single clear verdict line instead of the raw columns.

14. **Pre-bet commitment message dumps the 64-char hash under its internal field name**
   `bot/views/casino.py`
   Fix: Say what it is in words ('Committed before you stake') with a truncated hash and a copy-friendly full value, matching however the verify panel presents the same figure.

15. **Order status printed as the raw column enum awaiting_verification in four user-facing places**
   `bot/views/orders.py`
   Fix: Add one status-to-label map and route all five render sites through it.

16. **'Verify a round' picker labels every option with a raw internal id and the raw game key**
   `bot/views/casino.py`
   Fix: Label each option with GAME_LABELS[game], the round's time, stake and result, and carry the round id in the option value where the user never sees it.

17. **Resolve mints its event id inside the confirm modal, so a second confirm reports a false failure**
   `bot/views/admin.py`
   Fix: Mint the resolve event id once at the preview (the source event) and carry it into the modal, so a repeat submit hits the idempotency guard and reports success.

18. **Admin stock error names the item by database id instead of the item just picked**
   `bot/views/admin.py`
   Fix: Catch the core error at the modal and re-raise it with self.item['name'] and units on both figures.

19. **/setup embed uses a different gold from every other brand-toned panel**
   `bot/views/setup.py`
   Fix: Build the setup embed through bot/ui/embed.py with the brand tone instead of hard-coding 0xC9B37A.

## 5. Also open, not from the audit

- **OAuth Client ID/Secret** for `/me` sign-in. John has never done this; the site's
  "Sign in with Discord" has nothing to talk to until he creates it in the Dev Portal.
  He pastes secrets himself — never ask for or handle one.
- **Fund a treasury.** All three are at 0, so every payout fails. `/admin` → Fund treasury.
- **Give himself the Staff role.** `/setup` created it and assigned it to nobody.
- **DiplomaticaMC 403** — see §3.

## 6. Standing constraints — do not relearn these

- He pastes tokens/secrets himself. Never enter a password, token or payment detail.
- Never permanently delete his files; `mv` into `_to_delete/` and tell him.
- Never write a secret, webhook or access-granting id into the repo or the brain
  (`CONTRACT.md` §11).
- Money: claim first then act, one atomic `UPDATE ... WHERE <whole precondition>` judged by
  rowcount. Idempotency keys minted at the source. On a money screen **the unit is the
  content** — never trim a `/ piece` or `/ stack of 64` clarifier.
- The 64× rule: `price_unit_pieces` is the ONLY divisor. `stack_size` is capacity maths.
- An existence check is not a completeness check. `cancel()` existing in core is not
  `cancel()` reachable from the UI — that exact bug shipped here green behind 16/16 tests.
- Empty states are empty. Real names over internal ids. Never make a user type an id.
- UI: load the `human-ui` skill before any screen. Dark, flat, no glass, no glow is his
  ACCEPTED decision, not something to reconsider.

## 7. Token consumption — read before launching any swarm

The swarm is ~80% of this project's bill. Measured on `wf_14c26046-616`: 36 agents,
**1,128 turns**, median startup **42,497 tokens re-sent every turn** = 67% of the round's
entire cache read. The findings themselves were 44k output tokens.

Per-role, measured:

| role | n | turns/agent | verdict |
|---|---|---|---|
| scout | 1 | **11.0** | LEAN. Cheapest useful agent by an order of magnitude. |
| auditor | 5 | **50.2** | HEAVY. Brief named a lens but no files, so each re-derived the scout's map. |
| refuter | 29 | **29.7** | Handed ONE claim with a file and a line. Worst agent: 63 turns, 6.1M cache read, 509 output tokens. It is re-auditing. |
| synthesist | 1 | 4.0 | `out=8` is a measurement fault, not a cost. Do not use it. |

**Three changes proposed to John, NOT yet made — he decides, never rewrite a brief from a
score alone:**
1. `auditor.md` Corrections: name the files in your lens; the scout's map is the briefing,
   do not re-derive it. (Biggest turn saving.)
2. `refuter.md` Corrections: you are handed one claim, one file, one line. Past ten turns
   you are re-auditing, which is not your job.
3. `audit.js`: pass a narrow `agentType` for the read-only phases. Caveat: the
   13,748-vs-28,013 figure in his notes compares `claude-code-guide` to `general-purpose`;
   the narrow type actually used here has NOT been measured.

Also: `workflow-progress` cannot see a run launched from a `scriptPath` inside the skills
folder — it finds scripts by searching project dirs for `*<run_id>.js`. Copy the script to
`projects/<session>/workflows/scripts/<name>-<run_id>.js` or read `journal.jsonl` directly.

## 8. Ledger

Five rows for `wf_14c26046-616` are appended to `brain/swarm-ledger.jsonl`, including a
`__round__` row carrying the measured startup figure. Corrections were already appended to
`designer.md` and `refuter.md` from the earlier design round `wf_b14e95ab-ec5`.
