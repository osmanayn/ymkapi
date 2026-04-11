from fastapi import FastAPI, HTTPException
from recipe_scrapers import scrape_me, scrape_html
from bs4 import BeautifulSoup
import httpx
import cloudscraper
import json
import re

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


@app.get("/")
def home():
    return {"mesaj": "Tarif API çalışıyor!"}


def fetch_html(url: str) -> str:
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


@app.get("/tarif-getir")
def get_recipe(url: str):
    # 1. recipe-scrapers direkt dene
    try:
        scraper = scrape_me(url, wild_mode=True)
        title        = scraper.title()
        ingredients  = scraper.ingredients()
        instructions = scraper.instructions()
        if title and ingredients and instructions:
            return {
                "baslik":         title,
                "malzemeler":     ingredients,
                "yapis_adimlari": instructions,
                "resim_url":      scraper.image(),
                "toplam_sure":    scraper.total_time(),
                "site":           scraper.host(),
            }
    except Exception:
        pass

    # 2. HTML indir (cloudscraper ile 403 engeli aş)
    html = fetch_html(url)

    # 3. scrape_html ile dene
    try:
        scraper = scrape_html(html, org_url=url)
        title        = scraper.title()
        ingredients  = scraper.ingredients()
        instructions = scraper.instructions()
        if title and ingredients and instructions:
            return {
                "baslik":         title,
                "malzemeler":     ingredients,
                "yapis_adimlari": instructions,
                "resim_url":      scraper.image(),
                "toplam_sure":    scraper.total_time(),
                "site":           scraper.host(),
            }
    except Exception:
        pass

    # 4. BeautifulSoup manuel scraping
    return manual_scrape(html, url)


def manual_scrape(html: str, url: str) -> dict:
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

    # JSON-LD dene
    ingredients, instructions = extract_json_ld(soup)

    # Malzeme selectors
    if not ingredients:
        for sel in [
            "ul.recipe-materials li",
            "[itemprop='recipeIngredient']",
            "[class*='ingredient'] li",
            "[class*='malzeme'] li",
            "[class*='ingre'] li",
            ".ingredients li",
            ".recipe-ingredients li",
        ]:
            try:
                items = soup.select(sel)
                if len(items) >= 2:
                    result = [el.get_text(strip=True) for el in items if el.get_text(strip=True)]
                    if result:
                        ingredients = result
                        break
            except Exception:
                continue

    # Yapılış selectors
    if not instructions:
        for sel in [
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
        ]:
            try:
                items = soup.select(sel)
                if items:
                    result = [el.get_text(strip=True) for el in items if len(el.get_text(strip=True)) > 10]
                    if result:
                        instructions = "\n".join(result)
                        break
            except Exception:
                continue

    if not title and not ingredients and not instructions:
        raise HTTPException(status_code=422, detail="Bu sayfadan tarif çıkarılamadı.")

    return {
        "baslik":         title or "İsimsiz Tarif",
        "malzemeler":     ingredients,
        "yapis_adimlari": instructions,
        "resim_url":      image,
        "toplam_sure":    None,
        "site":           host,
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
