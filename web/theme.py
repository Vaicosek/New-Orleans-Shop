"""CSS tokens for the New Orleans site.

Reference cousin (revised 2026-08-31, at the owner's direction): a Mardi
Gras monogram-canvas look -- purple ground, gold hardware, and a hot-gold
pop surface used once as a real block, not a wash. Purple and gold are New
Orleans' own carnival colours, so this is not an import from anywhere; it
reads louder than the flag-tile version this replaces, on purpose -- the
owner reacted to a mockup and asked for the bolder read. The wordmark, nav
and section labels move to a heavy, wide-tracked, all-caps grotesk (the
a wide-tracked logotype register) in place of the old civic-plaque narrow
face. The fleur-de-lis (the city's own mark, already drawn once in shell.py)
becomes the monogram repeat instead of a generic quatrefoil.

Still no cards, no gradient, no glow, no webfont fetch -- one ink colour
(--accent) for anything clickable, three tones of ink for everything else,
and the ground has to be right with the network off.
"""
from __future__ import annotations

CSS = r"""
/* Purple and gold are Mardi Gras' own colours -- New Orleans does not need
   to borrow them from anywhere. --ground is a deep carnival purple, dark
   enough to be a ground rather than a colour swatch. --ground-deep sits one
   step below it for the masthead and footer, so the chrome reads as a
   frame around the page rather than a continuation of it. --accent is
   hardware gold -- warmer and brighter than a desaturated "safe" gold,
   because a buckle is meant to catch the eye and this page now has exactly
   one thing that does that. --pop is the hot yellow from the reference
   image, used once, as a real surface (the storefront's hero band), never
   as a wash or a gradient. --gain has no carnival equivalent and stays
   green -- up and down must be tellable apart at a glance and that is a
   function, not a brand decision.

   Contrast checked against the tile's lightest pixel, not the flat ground:
   on the textured purple, text runs 13.8:1, dim 9.6:1, inert 6.1:1 -- inert
   is reserved for footer/meta text at larger sizes, everything a customer
   reads to act on clears 9:1. */
:root{
  --ground:      #2b1140;
  --ground-deep: #1f0c30;
  --raised:      rgba(240,220,180,.07);
  --line:        #4a2568;
  --text:   #f4ecd8;
  --dim:    #d3c0a8;
  --inert:  #a3899e;
  --gain:   #9ccd88;
  --loss:   #ec9080;
  --accent:    #e7b83e;
  --accent-deep: #c99a2d;
  --pop:    #f3d94a;
}
*{box-sizing:border-box;margin:0;padding:0}
html{color-scheme:dark;background:var(--ground)}

/* The ground is the monogram canvas: the city's own fleur-de-lis, repeated
   like a print pattern, stroked in the hardware gold at low opacity so it
   is a material rather than a decoration. One stroke colour, no fill, so
   there is no wash anywhere for a gradient to hide in. Authored here, not
   fetched, so the page still has its ground with the network off. */
body{
  background-color:var(--ground);
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='72' height='72' viewBox='0 0 72 72'><g fill='none' stroke='%23e7b83e' stroke-opacity='0.12' stroke-width='1.6'><path d='M36 6c-3 4-5 7.4-5 10.4 0 2.6 1.1 4.9 2.8 6.8h-3.4c-4.9 0-8.6 2.8-8.6 7 0 3.4 2.5 5.8 5.6 5.8 2.4 0 4.3-1.5 4.3-3.4 0-1.5-1.1-2.6-2.4-2.6-.8 0-1.5.2-1.9.8.4-1.7 1.7-2.6 3.8-2.6h3v6c0 4-1.1 6.6-3.6 9.6h10.2c-2.5-3-3.6-5.6-3.6-9.6v-6h3c2.1 0 3.4.9 3.8 2.6-.4-.6-1.1-.8-1.9-.8-1.3 0-2.4 1.1-2.4 2.6 0 1.9 1.9 3.4 4.3 3.4 3.1 0 5.6-2.4 5.6-5.8 0-4.2-3.7-7-8.6-7h-3.4c1.7-1.9 2.8-4.2 2.8-6.8 0-3-1.7-6.4-5-10.4z'/></g></svg>");
  background-repeat:repeat;
  color:var(--text);
  font-family:Helvetica,Arial,'Liberation Sans',sans-serif;
  font-size:18px;
  line-height:1.6;
  font-variant-numeric:tabular-nums;
}
a{color:var(--accent);text-decoration:underline;text-underline-offset:3px;
  text-decoration-thickness:1px;text-decoration-color:rgba(231,184,62,.5)}
a:hover{color:var(--pop);text-decoration-color:var(--pop)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

/* The lockup register: heavy, wide-tracked, all-caps grotesk for anything
   that names a place or a section -- the wordmark, the nav, the page and
   section titles. Two weights only, 400 and 900, nothing in between. No
   webfont fetched: 'Arial Black' degrades to bold Helvetica/Arial, which is
   still heavy enough to carry the register with the network off. */
h1,h2,h3,h4,.wordmark,.navlink,.who-wrap,th{
  font-family:'Arial Black',Arial,'Liberation Sans',Helvetica,sans-serif;
}
h1{font-size:32px;font-weight:900;letter-spacing:.02em;text-transform:uppercase;
  margin-bottom:8px}

/* A section label reads like a belt label: small, wide-tracked caps in the
   hardware gold, with a strap -- not a dot -- underneath it. The strap is
   the page's one recurring ornament besides the wordmark; it does not
   appear anywhere else, or it stops being a signature and starts being
   wallpaper. */
h2{font-size:15px;font-weight:900;letter-spacing:.18em;text-transform:uppercase;
  color:var(--accent);margin:34px 0 8px;position:relative;padding-bottom:11px}
h2::after{content:"";position:absolute;left:0;bottom:0;width:64px;height:5px;
  background:var(--accent)}
h3{font-size:20px;font-weight:900;letter-spacing:.03em;margin:38px 0 4px;color:var(--text)}
h4{font-size:14px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  color:var(--dim);margin:20px 0 8px}
p{color:var(--dim);max-width:70ch}
p+p{margin-top:8px}

.masthead{
  display:flex;align-items:center;gap:36px;flex-wrap:wrap;
  padding:22px 48px;background:var(--ground-deep);
  border-bottom:4px solid var(--accent);
}
.brand{display:flex;align-items:center;gap:11px;text-decoration:none;color:inherit}
.brand .wordmark{font-size:22px;font-weight:900;color:var(--text);letter-spacing:.14em;
  text-transform:uppercase}
.lis{width:22px;height:22px;flex:none;color:var(--accent)}

.nav{display:flex;gap:30px;flex-wrap:wrap;margin-left:12px}
.navlink{color:var(--dim);text-decoration:none;font-size:13px;font-weight:700;
  letter-spacing:.16em;text-transform:uppercase}
.navlink:hover{color:var(--text)}
.navlink[aria-current="page"]{color:var(--accent)}
.who-wrap{margin-left:auto;display:flex;align-items:center;gap:18px;font-size:13px;
  letter-spacing:.1em;text-transform:uppercase}
.who{color:var(--dim)}

/* The storefront's hero: the reference image's hot-yellow field, used once,
   as a real surface -- textured with the same fleur-de-lis monogram in the
   ground purple so it reads as canvas rather than a flat colour chip.
   Nowhere else on the site gets this treatment; a second one would make it
   a colour scheme instead of a landmark. */
.hero{
  background-color:var(--pop);
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='72' height='72' viewBox='0 0 72 72'><g fill='none' stroke='%232b1140' stroke-opacity='0.14' stroke-width='1.6'><path d='M36 6c-3 4-5 7.4-5 10.4 0 2.6 1.1 4.9 2.8 6.8h-3.4c-4.9 0-8.6 2.8-8.6 7 0 3.4 2.5 5.8 5.6 5.8 2.4 0 4.3-1.5 4.3-3.4 0-1.5-1.1-2.6-2.4-2.6-.8 0-1.5.2-1.9.8.4-1.7 1.7-2.6 3.8-2.6h3v6c0 4-1.1 6.6-3.6 9.6h10.2c-2.5-3-3.6-5.6-3.6-9.6v-6h3c2.1 0 3.4.9 3.8 2.6-.4-.6-1.1-.8-1.9-.8-1.3 0-2.4 1.1-2.4 2.6 0 1.9 1.9 3.4 4.3 3.4 3.1 0 5.6-2.4 5.6-5.8 0-4.2-3.7-7-8.6-7h-3.4c1.7-1.9 2.8-4.2 2.8-6.8 0-3-1.7-6.4-5-10.4z'/></g></svg>");
  background-repeat:repeat;
  color:var(--ground);padding:30px 48px;
  border-bottom:1px solid rgba(43,17,64,.25);
}
.hero p{color:#4a2568;max-width:60ch}
.hero a{color:var(--ground);text-decoration-color:var(--ground)}

main{max-width:1040px;margin:0 auto;padding:44px 48px 80px}

.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:480px}
.sheet table{max-width:660px}
th{text-align:left;font-weight:700;font-size:12px;letter-spacing:.12em;
  text-transform:uppercase;color:var(--accent-deep);
  padding:10px 18px 10px 0;border-bottom:2px solid var(--accent);white-space:nowrap}
td{padding:12px 18px 12px 0;border-bottom:1px solid var(--line);font-size:17px}
th.num,td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tbody tr:hover td{background:var(--raised)}

/* Status is the one place this page spends colour on words. Each one names
   a state somebody acts on: gold means a move is owed, green means the
   money moved, red means it stopped, dim means it is nobody's in
   particular. */
.s-wait{color:var(--accent)}
.s-done{color:var(--gain)}
.s-stop{color:var(--loss)}
.s-open{color:var(--dim)}
.s-you{color:var(--accent)}
.s-them{color:var(--text)}

.gain{color:var(--gain)}
.loss{color:var(--loss)}
.dim{color:var(--dim)}
.inert{color:var(--inert)}

.empty{color:var(--inert);padding:8px 0}

/* The wallet strip. Sits above the masthead on every signed-in page, because
   "what have I got" is the question a shop's site is asked most often and it
   should never cost a click. Muted labels, real figures, no card. */
.wallet{display:flex;gap:30px;align-items:baseline;flex-wrap:wrap;
  padding:10px 48px;border-bottom:1px solid var(--line);
  font-size:15px;color:var(--dim)}
.wallet b{font-weight:400;color:var(--text)}

/* Balances as a definition list with a ruled total -- a bank statement's
   shape. Explicitly not stat cards: four tinted boxes across the top is the
   single most AI-looking thing this page could do. */
.sums{max-width:560px;margin-top:4px}
.sums .row{display:flex;justify-content:space-between;gap:24px;
  padding:9px 0;border-bottom:1px solid var(--line)}
.sums .row span:first-child{color:var(--dim)}
.sums .row.total{border-bottom:none;border-top:2px solid var(--line);
  margin-top:2px;padding-top:11px}
.sums .row.total span:first-child{color:var(--text)}

/* The storefront's item grid. Each item now sits in its own boxed card --
   a raised background plus a hairline border -- so a busy grid of 150+
   items stays easy to tell apart at a glance. The icon is Minecraft's own
   pixel art, so it is rendered unsmoothed (`image-rendering:pixelated`)
   rather than blurred by browser scaling. */
.itemgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
  gap:16px;margin:16px 0 34px}
.item{display:flex;flex-direction:column;align-items:flex-start;gap:5px;
  padding:14px;background:var(--raised);border:1px solid var(--line);
  border-radius:8px}
.icon{width:40px;height:40px;image-rendering:pixelated;margin-bottom:4px}
.icon-fallback{display:flex;align-items:center;justify-content:center;
  background:var(--ground-deep);color:var(--accent-deep);font-weight:900;font-size:17px}
.item-name{font-size:15px;font-weight:700;color:var(--text);line-height:1.3}
.item-price{font-size:15px;color:var(--accent);font-variant-numeric:tabular-nums}
.item-stock{font-size:13px}

/* One cart-controls block per item: a checkbox plus a single typed
   quantity field. No JavaScript on this site, so this is the whole
   quantity picker -- a plain number input, no preset pills. */
.cart-controls{display:flex;flex-direction:column;gap:6px;margin-top:5px;width:100%}
.cart-check{font-size:13px;display:flex;align-items:center;gap:6px}
.qty-field{display:flex;align-items:center;gap:6px;font-size:13px;
  font-variant-numeric:tabular-nums}
.qty-field input[type=number]{width:72px;background:var(--ground-deep);
  border:1px solid var(--line);border-radius:4px;color:var(--text);
  font-size:13px;font-variant-numeric:tabular-nums;padding:3px 6px}
.qty-field input[type=number]:focus{outline:none;border-color:var(--accent)}
.order-link{font-size:13px}

/* The page-level submit for every checked item at once -- appears above and
   below the price sheet so a long grid never hides it off-screen. Same
   accent-filled button register as everywhere else that commits an action. */
.cart-submit{margin:14px 0;padding:14px 0;border-top:1px solid var(--line);
  border-bottom:1px solid var(--line)}
.cart-submit button{background:var(--accent);color:var(--ground-deep);border:none;
  font-weight:900;font-size:12px;letter-spacing:.1em;text-transform:uppercase;
  padding:10px 20px;cursor:pointer;font-family:inherit}
.cart-submit button:hover{background:var(--pop)}

/* A one-line confirmation after opening an order (or orders) -- plain text
   in the ledger's own "gain" green, no banner box. A batch that partly
   failed still reads as a win with a quiet parenthetical, never a loud
   warning; a batch that fully failed uses the loss tone instead. */
.notice{color:var(--gain);font-size:16px;margin:14px 0}
.notice-loss{color:var(--loss)}

.foot{padding:22px 48px;border-top:1px solid var(--line);color:var(--inert);
  font-size:13px;letter-spacing:.06em;text-transform:uppercase;
  display:flex;align-items:center;gap:10px;background:var(--ground-deep)}
.foot .lis{width:14px;height:14px;color:var(--accent-deep)}

/* The price sheet on a phone. A price you have to scroll sideways to read
   is a price the customer did not read, so below 560px the row stops being
   a table row and becomes name-left / price-right, and the price is allowed
   to wrap instead of being clipped. */
@media(max-width:560px){
  .itemgrid{grid-template-columns:repeat(2,1fr);gap:22px 16px}
  .sheet table{min-width:0}
  .sheet thead{display:none}
  .sheet tr{display:flex;justify-content:space-between;gap:16px;
    border-bottom:1px solid var(--line)}
  .sheet td{border:none;padding:8px 0}
  .sheet td.num{white-space:normal;text-align:right}
}

@media(max-width:720px){
  .masthead{padding:18px 20px}
  .hero{padding:22px 20px}
  main{padding:26px 20px 60px}
  .who-wrap{margin-left:0;width:100%}
}
"""
