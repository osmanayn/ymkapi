from fastapi import FastAPI, HTTPException
from bs4 import BeautifulSoup
from recipe_scrapers import scrape_me, scrape_html
import re
import json
import cloudscraper
import httpx
import asyncio
from playwright.async_api import async_playwright

app = FastAPI()

scraper_client = cloudscraper.create_scraper(
    browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Referer": "https://www.google.com/",
}

# JavaScript gerektiren siteler
JS_REQUIRED_SITES = [
    "nefisyemektarifleri.com",
]


@app.get("/")
def home():
    return {"mesaj": "Tarif API çalışıyor!"}


def needs_js(url: str) -> bool:
    return any(site in url for site in JS_REQUIRED_SITES)


def fetch_html(url: str) -> str:
    """Cloudscraper → httpx sırasıyla dene."""
    try:
        resp = scraper_client.get(url, timeout=25)
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    try:
        with httpx.Client(headers=HEADERS, timeout=25, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Sayfa indirilemedi: {str(e)}")


async def fetch_html_js(url: str) -> str:
    """Playwright ile JavaScript render edilmiş HTML döndürür."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36",
            locale="tr-TR",
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Sayfanın dinamik içeriği yüklensin diye bekle
            await page.wait_for_timeout(3000)
            # nefisyemektarifleri için tarif bloğunu bekle
            try:
                await page.wait_for_selector(".recipe-directions, .directions, ol.steps, .recipe-steps", timeout=5000)
            except Exception:
                pass
            html = await page.content()
        finally:
            await browser.close()
        return html


@app.get("/tarif-getir")
async def get_recipe(url: str):
    # 1. recipe-scrapers direkt
    try:
        sc = scrape_me(url, wild_mode=True)
        t, i, ins = sc.title(), sc.ingredients(), sc.instructions()
        if t and i and ins:
            return out(t, i, ins, sc.image(), sc.total_time(), sc.host())
    except Exception:
        pass

    # 2. JavaScript gerektiriyor mu?
    if needs_js(url):
        try:
            html = await fetch_html_js(url)
            result = parse_html(html, url)
            if result.get("yapis_adimlari"):
                return result
        except Exception as e:
            pass  # Playwright başarısız oldu, normal yönteme geç

    # 3. Normal HTML
    html = fetch_html(url)

    # 4. scrape_html
    try:
        sc = scrape_html(html, org_url=url)
        t, i, ins = sc.title(), sc.ingredients(), sc.instructions()
        if t and i and ins:
            return out(t, i, ins, sc.image(), sc.total_time(), sc.host())
    except Exception:
        pass

    # 5. Manuel BeautifulSoup
    return parse_html(html, url)


def parse_html(html: str, url: str) -> dict:
    """HTML'den tarif bilgilerini çıkarır."""
    soup = BeautifulSoup(html, "html.parser")
    host = re.sub(r"https?://(www\.)?", "", url).split("/")[0]

    # Başlık
    title = ""
    og = soup.find("meta", property="og:title")
    if og:
        title = og.get("content", "").strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)

    # Resim
    image = ""
    og_img = soup.find("meta", property="og:image")
    if og_img:
        image = og_img.get("content", "")

    # JSON-LD
    ingredients, instructions = extract_json_ld(soup)

    # Malzeme selectors
    if not ingredients:
        ingredients = try_list(soup, [
            "ul.recipe-materials li",           # nefisyemektarifleri
            "[itemprop='recipeIngredient']",
            "[class*='ingredient'] li",
            "[class*='malzeme'] li",
            "[class*='ingre'] li",
            "[id*='ingredient'] li",
            "[id*='malzeme'] li",
            ".ingredients li",
            ".recipe-ingredients li",
        ])

    # Yapılış selectors — nefisyemektarifleri dahil
    if not instructions:
        instructions = try_steps(soup, [
            # nefisyemektarifleri (JS sonrası yüklenebilir)
            ".recipe-directions li",
            ".recipe-directions p",
            ".directions li",
            ".directions p",
            "ol.steps li",
            ".recipe-steps li",
            ".recipe-steps p",
            # Genel
            "[itemprop='recipeInstructions']",
            "[class*='instruction'] li",
            "[class*='instruction'] p",
            "[class*='direction'] li",
            "[class*='direction'] p",
            "[class*='step'] li",
            "[class*='step'] p",
            "[class*='yapilis'] li",
            "[class*='yapilis'] p",
            "[class*='adim'] li",
            ".recipe-instructions li",
            ".recipe-instructions p",
            "ol li",
        ])

    if not title and not ingredients and not instructions:
        raise HTTPException(
            status_code=422,
            detail=f"Tarif çıkarılamadı: {url}"
        )

    return out(title or "İsimsiz Tarif", ingredients, instructions, image, None, host)


def out(title, ingredients, instructions, image, total_time, host):
    return {
        "baslik": title,
        "malzemeler": ingredients,
        "yapis_adimlari": instructions,
        "resim_url": image,
        "toplam_sure": total_time,
        "site": host,
    }


def extract_json_ld(soup):
    ingredients, instructions = [], ""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0]
            if not isinstance(data, dict):
                continue
            if "@graph" in data:
                for item in data["@graph"]:
                    if isinstance(item, dict) and item.get("@type") == "Recipe":
                        data = item
                        break
            if data.get("@type") not in ("Recipe", ["Recipe"]):
                continue
            raw_ing = data.get("recipeIngredient", [])
            if isinstance(raw_ing, list):
                ingredients = [str(i).strip() for i in raw_ing if str(i).strip()]
            raw_ins = data.get("recipeInstructions", "")
            if isinstance(raw_ins, list):
                steps = []
                for step in raw_ins:
                    text = step.get("text", step.get("name", "")) if isinstance(step, dict) else str(step)
                    text = BeautifulSoup(text, "html.parser").get_text(strip=True)
                    if text:
                        steps.append(text)
                instructions = "\n".join(steps)
            elif isinstance(raw_ins, str):
                instructions = BeautifulSoup(raw_ins, "html.parser").get_text(separator="\n", strip=True)
            if ingredients or instructions:
                return ingredients, instructions
        except Exception:
            continue
    return ingredients, instructions


def try_list(soup, selectors):
    for sel in selectors:
        try:
            items = soup.select(sel)
            if len(items) >= 2:
                result = [el.get_text(strip=True) for el in items if el.get_text(strip=True)]
                if result:
                    return result
        except Exception:
            continue
    return []


def try_steps(soup, selectors):
    for sel in selectors:
        try:
            items = soup.select(sel)
            if items:
                result = [el.get_text(strip=True) for el in items if len(el.get_text(strip=True)) > 10]
                if result:
                    return "\n".join(result)
        except Exception:
            continue
    return ""


@app.get("/nefis-js-debug")
async def nefis_js_debug(url: str):
    """Playwright ile yüklenen sayfadaki tüm elementleri gösterir."""
    html = await fetch_html_js(url)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "nav"]):
        tag.decompose()

    cooking = re.compile(
        r'(kavur|ekle|pişir|karıştır|doğra|ilave|kızart|haşla|ısıt|tencere)',
        re.IGNORECASE
    )

    # Pişirme kelimesi geçen kısa elementler
    results = []
    seen = set()
    for el in soup.find_all(["p", "li", "div", "span"]):
        text = el.get_text(strip=True)
        if cooking.search(text) and 20 < len(text) < 400 and text not in seen:
            seen.add(text)
            results.append({
                "tag": el.name,
                "class": el.get("class", []),
                "id": el.get("id", ""),
                "text": text[:150]
            })
        if len(results) >= 20:
            break

    return {"js_rendered_cooking_blocks": results}
