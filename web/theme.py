"""CSS tokens for the New Orleans site.

Reference cousin: a printed commodity-exchange price sheet, rendered dark.
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
:root{
  --ground: #1b1d20;
  --raised: rgba(239,236,229,.05);
  --line:   #3b3e43;
  --text:   #efece5;
  --dim:    #aaa59b;
  --inert:  #6f6c66;
  --gain:   #8fbf6a;
  --loss:   #d87a6a;
  --accent: #c9b37a;
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

h1,h2,h3,h4{font-weight:400}
h1{font-size:28px;margin-bottom:8px}
h2{font-size:20px;margin:34px 0 12px}
h4{font-size:15px;color:var(--dim);margin:18px 0 6px}
p{color:var(--dim);max-width:70ch}
p+p{margin-top:8px}

.masthead{
  display:flex;align-items:baseline;gap:36px;flex-wrap:wrap;
  padding:26px 48px 18px;border-bottom:1px solid var(--line);
}
.brand{display:flex;align-items:baseline;gap:10px;text-decoration:none;color:inherit}
.brand .wordmark{font-size:20px;color:var(--text)}
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

.foot{padding:24px 48px;border-top:1px solid var(--line);color:var(--inert);font-size:14px}

@media(max-width:720px){
  .masthead{padding:20px 20px 16px}
  main{padding:26px 20px 60px}
  .who-wrap{margin-left:0;width:100%}
}
"""
