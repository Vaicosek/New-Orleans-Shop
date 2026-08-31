"""CSS tokens for the New Orleans site.

Reference cousin: a French Quarter enamel street plaque and a Paris bourse
notice. New Orleans was founded French and still signs itself that way -- the
fleur-de-lis, the tricolour band, the Didone lettering on the corner tiles.
That is where the type and the ornament come from, not from a dashboard.
Plain rules, tabular figures, one ink colour (`--accent`) plus the text
colour. Tokens are lifted verbatim from `abex_theme.py`'s THEME_CSS, which
the owner has already accepted, with one deliberate change noted in
CONTRACT.md section 10: base font 17px, not 15px.

Nothing here is a card, a chip, a gradient or a glow. There is exactly one
interactive colour on the page -- two would mean neither reads as
clickable.
"""
from __future__ import annotations

CSS = r"""
/* Palette drawn from the flag of New Orleans: blue #111B4C, gold #EDB41D,
   red #D52C11, white. Each one is pulled toward the page rather than used
   raw -- a flag is printed on cloth at arm's length, a price sheet is read
   at 60cm, and the saturation that reads as civic on a pole reads as a glow
   on a screen.

   --ground is the flag's blue taken well below it: dark enough to be a
   ground rather than a blue panel, blue enough that the gold on top of it is
   the flag's pairing and not a generic dark theme. --accent is the
   fleur-de-lis gold, desaturated so it does not bloom on dark. --loss is the
   flag's red at the same treatment. --gain has no flag equivalent and stays
   green, because up and down must be tellable apart at a glance and that is
   a function, not a brand decision. */
:root{
  --ground: #0f1328;
  --raised: rgba(240,238,232,.055);
  --line:   #2b3050;
  --text:   #f0eee8;
  --dim:    #a3a6bd;
  --inert:  #6e7189;
  --gain:   #7fb56a;
  --loss:   #d0503a;
  --accent: #d9b544;

  /* The flag's band, at flag saturation. It is drawn 1px tall -- at that
     size a colour has to be itself or it disappears, which is the opposite
     of the rule for a surface. */
  --band-red:   #b8341f;
  --band-white: #efece4;
  --band-blue:  #4257a8;   /* lifted off the flag's #111B4C: that blue IS
                              this page's ground, and a stripe the colour of
                              the paper is not a stripe */
}
*{box-sizing:border-box;margin:0;padding:0}
html{color-scheme:dark;background:var(--ground)}
body{
  background:var(--ground);
  color:var(--text);
  font-family:Georgia,'Times New Roman',serif;
  font-size:17px;
  line-height:1.6;
  font-variant-numeric:tabular-nums;
}
a{color:var(--accent);text-decoration:underline;text-underline-offset:3px;
  text-decoration-thickness:1px;text-decoration-color:rgba(201,179,122,.45)}
a:hover{text-decoration-color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

h1,h2,h3,h4,.wordmark{
  font-family:'Playfair Display',Didot,'Bodoni MT',Georgia,serif;
  font-weight:400;
}
h1{font-size:28px;margin-bottom:8px}
h2{font-size:20px;margin:34px 0 12px}
h4{font-size:15px;color:var(--dim);margin:18px 0 6px}
p{color:var(--dim);max-width:70ch}
p+p{margin-top:8px}

.masthead{
  display:flex;align-items:baseline;gap:36px;flex-wrap:wrap;
  padding:26px 48px 18px;
}
.brand{display:flex;align-items:center;gap:11px;text-decoration:none;color:inherit}
.brand .wordmark{font-size:22px;color:var(--text);letter-spacing:.01em}
.lis{width:22px;height:22px;flex:none;color:var(--accent)}

/* The flag of New Orleans carries a red/white/blue band across its middle.
   Three 1px rules stacked is that band at the size a rule can be without
   becoming ornament for its own sake. It REPLACES the masthead's grey line
   rather than joining it, and marks each category on the price sheet.
   Nowhere else -- a motif used twice is a motif, used everywhere it is
   wallpaper. */
.band{display:block}
.band i{display:block;height:2px}
.band i:nth-child(1){background:var(--band-red)}
.band i:nth-child(2){background:var(--band-white)}
.band i:nth-child(3){background:var(--band-blue)}
h3{font-size:19px;margin:34px 0 0}
h3+.band{margin:7px 0 14px;max-width:660px}
.nav{display:flex;gap:26px;flex-wrap:wrap}
.navlink{color:var(--dim);text-decoration:none;font-size:16px}
.navlink:hover{color:var(--text)}
.navlink[aria-current="page"]{color:var(--accent)}
.who-wrap{margin-left:auto;display:flex;align-items:center;gap:18px;font-size:15px}
.who{color:var(--dim)}

main{max-width:1040px;margin:0 auto;padding:40px 48px 80px}

.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:480px}
.sheet table{max-width:660px}
th{text-align:left;font-weight:400;font-size:15px;color:var(--dim);
  padding:8px 18px 8px 0;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 18px 9px 0;border-bottom:1px solid var(--line);font-size:17px}
th.num,td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tbody tr:hover td{background:var(--raised)}

/* Status is the one place this page spends colour on words. Each one names
   a state somebody acts on: gold means a move is owed, green means the money
   moved, red means it stopped, dim means it is nobody's in particular. A
   fifth colour would have to mean a fifth thing, and there isn't one. */
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

.foot{padding:22px 48px;border-top:1px solid var(--line);color:var(--inert);
  font-size:14px;display:flex;align-items:center;gap:10px}
.foot .lis{width:15px;height:15px;color:var(--inert)}

/* The price sheet on a phone. A price you have to scroll sideways to read
   is a price the customer did not read, so below 560px the row stops being
   a table row and becomes name-left / price-right, and the price is allowed
   to wrap instead of being clipped. */
@media(max-width:560px){
  .sheet table{min-width:0}
  .sheet thead{display:none}
  .sheet tr{display:flex;justify-content:space-between;gap:16px;
    border-bottom:1px solid var(--line)}
  .sheet td{border:none;padding:8px 0}
  .sheet td.num{white-space:normal;text-align:right}
}

@media(max-width:720px){
  .masthead{padding:20px 20px 16px}
  main{padding:26px 20px 60px}
  .who-wrap{margin-left:0;width:100%}
}
"""
