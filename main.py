from fastapi import FastAPI, HTTPException
import httpx
from bs4 import BeautifulSoup
from recipe_scrapers import scrape_me, scrape_html
import re

app = FastAPI()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

@app.get("/")
def home():
    return {"mesaj": "Tarif API çalışıyor!"}


@app.get("/tarif-getir")
def get_recipe(url: str):

    # ── 1. Önce recipe-scrapers ile dene ─────────────────────────────────────
    try:
        scraper = scrape_me(url, wild_mode=True)
        title       = scraper.title()
        ingredients = scraper.ingredients()
        instructions = scraper.instructions()
        image       = scraper.image()
        total_time  = scraper.total_time()
        host        = scraper.host()

        # Boş sonuç kontrolü
        if title and ingredients and instructions:
            return {
                "baslik":        title,
                "malzemeler":    ingredients,
                "yapis_adimlari": instructions,
                "resim_url":     image,
                "toplam_sure":   total_time,
                "site":          host,
            }
    except Exception:
        pass  # Başarısız oldu, HTML yöntemine geç

    # ── 2. HTML indir, scrape_html ile dene ──────────────────────────────────
    try:
        with httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.text

        try:
            scraper = scrape_html(html, org_url=url)
            title       = scraper.title()
            ingredients = scraper.ingredients()
            instructions = scraper.instructions()
            image       = scraper.image()
            total_time  = scraper.total_time()
            host        = scraper.host()

            if title and ingredients and instructions:
                return {
                    "baslik":        title,
                    "malzemeler":    ingredients,
                    "yapis_adimlari": instructions,
                    "resim_url":     image,
                    "toplam_sure":   total_time,
                    "site":          host,
                }
        except Exception:
            pass  # scrape_html da başarısız, BeautifulSoup'a geç

        # ── 3. BeautifulSoup ile manuel scraping ─────────────────────────────
        return manual_scrape(html, url)

    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=400, detail=f"Sayfa indirilemedi: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Tarif çekilemedi: {str(e)}")


def manual_scrape(html: str, url: str) -> dict:
    """BeautifulSoup ile Türk yemek sitelerine özel manuel scraping."""
    soup = BeautifulSoup(html, "html.parser")
    host = re.sub(r"https?://(www\.)?", "", url).split("/")[0]

    # ── Başlık ────────────────────────────────────────────────────────────────
    title = ""
    for sel in ["h1.recipe-title", "h1.title", "h1"]:
        el = soup.select_one(sel)
        if el:
            title = el.get_text(strip=True)
            break
    # og:title yedek
    if not title:
        og = soup.find("meta", property="og:title")
        if og:
            title = og.get("content", "").strip()

    # ── Resim ─────────────────────────────────────────────────────────────────
    image = ""
    og_img = soup.find("meta", property="og:image")
    if og_img:
        image = og_img.get("content", "")

    # ── Malzemeler ────────────────────────────────────────────────────────────
    ingredients = []

    # Önce JSON-LD schema.org dene
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            import json
            data = json.loads(script.string or "")
            # Liste veya iç içe olabilir
            if isinstance(data, list):
                data = data[0]
            if isinstance(data, dict):
                # @graph içinde olabilir
                if "@graph" in data:
                    for item in data["@graph"]:
                        if item.get("@type") == "Recipe":
                            data = item
                            break
                if data.get("@type") == "Recipe" and data.get("recipeIngredient"):
                    ingredients = [str(i).strip() for i in data["recipeIngredient"] if str(i).strip()]
                    break
        except Exception:
            continue

    # JSON-LD bulunamadıysa HTML selectors dene
    if not ingredients:
        selectors = [
            "[class*='ingredient'] li",
            "[class*='malzeme'] li",
            "[id*='ingredient'] li",
            "[id*='malzeme'] li",
            ".ingredients li",
            ".ingredient-list li",
            "[itemprop='recipeIngredient']",
        ]
        for sel in selectors:
            items = soup.select(sel)
            if len(items) >= 2:
                ingredients = [el.get_text(strip=True) for el in items if el.get_text(strip=True)]
                break

    # ── Yapılış adımları ──────────────────────────────────────────────────────
    instructions = ""

    # JSON-LD schema.org dene
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            import json
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0]
            if isinstance(data, dict):
                if "@graph" in data:
                    for item in data["@graph"]:
                        if item.get("@type") == "Recipe":
                            data = item
                            break
                if data.get("@type") == "Recipe":
                    raw = data.get("recipeInstructions", "")
                    if isinstance(raw, list):
                        steps = []
                        for step in raw:
                            if isinstance(step, dict):
                                steps.append(step.get("text", "").strip())
                            else:
                                steps.append(str(step).strip())
                        instructions = "\n".join(s for s in steps if s)
                    elif isinstance(raw, str):
                        instructions = raw.strip()
                    if instructions:
                        break
        except Exception:
            continue

    # HTML selectors dene
    if not instructions:
        selectors = [
            "[class*='instruction'] li",
            "[class*='direction'] li",
            "[class*='step'] li",
            "[class*='yapilis'] li",
            "[class*='adim'] li",
            "[class*='instruction'] p",
            "[class*='step'] p",
            "[itemprop='recipeInstructions']",
            "ol li",
        ]
        for sel in selectors:
            items = soup.select(sel)
            if len(items) >= 1:
                steps = [el.get_text(strip=True) for el in items if len(el.get_text(strip=True)) > 10]
                if steps:
                    instructions = "\n".join(steps)
                    break

    # Hiçbir şey bulunamadıysa hata ver
    if not title and not ingredients and not instructions:
        raise HTTPException(
            status_code=422,
            detail="Bu sayfadan tarif bilgisi çıkarılamadı. Lütfen farklı bir link deneyin."
        )

    return {
        "baslik":        title or "İsimsiz Tarif",
        "malzemeler":    ingredients,
        "yapis_adimlari": instructions,
        "resim_url":     image,
        "toplam_sure":   None,
        "site":          host,
    }
