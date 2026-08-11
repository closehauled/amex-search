from playwright.sync_api import sync_playwright
URL = "https://www.americanexpress.com/us/credit-cards/business/business-credit-cards/american-express-business-gold-card-amex/"
SELS = ("button:has-text('Apply Now')","a:has-text('Apply Now')",
        "button:has-text('Apply')","a:has-text('Apply')")
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    ctx = b.new_context(viewport={"width":1920,"height":1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    p = ctx.new_page()
    p.goto(URL, wait_until="commit", timeout=45000)
    p.wait_for_timeout(9000)
    print("=== scanner APPLY_SELECTORS matches ===")
    for s in SELS:
        try: print(f"  {s!r}: {bool(p.query_selector(s))}")
        except Exception as e: print(f"  {s!r}: ERR {e}")
    print("=== elements whose text == 'Apply Now' ===")
    info = p.eval_on_selector_all("*", """els => els
        .filter(e => (e.childElementCount===0) && (e.textContent||'').trim().toLowerCase()==='apply now')
        .slice(0,6)
        .map(e => ({tag:e.tagName, role:e.getAttribute('role'), cls:(e.className||'').toString().slice(0,60),
                    href:e.getAttribute('href'), parentTag:e.parentElement && e.parentElement.tagName,
                    parentRole:e.parentElement && e.parentElement.getAttribute('role')}))""")
    for i in info: print("  ", i)
    print("=== any 'Apply Now' anywhere (closest interactive ancestor) ===")
    info2 = p.eval_on_selector_all("*", """els => {
        let out=[];
        for (const e of els){
          if ((e.textContent||'').trim().toLowerCase()==='apply now' && e.childElementCount<=1){
            let a=e.closest('a,button,[role=button],[onclick]');
            out.push({self:e.tagName, inter: a?({tag:a.tagName,role:a.getAttribute('role'),cls:(a.className||'').toString().slice(0,50),href:a.getAttribute('href')}):null});
          }
        }
        return out.slice(0,6);
    }""")
    for i in info2: print("  ", i)
    b.close()
