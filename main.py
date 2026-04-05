from fastapi import FastAPI, HTTPException
import httpx
from bs4 import BeautifulSoup
from recipe_scrapers import scrape_me, scrape_html
import re
import json

app = FastAPI()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


@app.get("/")
def home():
    return {"mesaj": "Tarif API çalışıyor!"}


# ── Debug endpoint: Sayfanın class yapısını analiz et ─────────────────────────
@app.get("/debug")
def debug_page(url: str):
    """Sayfanın HTML yapısını analiz eder. Hangi class'ların kullanıldığını gösterir."""
    try:
        with httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.text

        soup = BeautifulSoup(html, "html.parser")
        info = {
            "title_h1": soup.find("h1").get_text(strip=True) if soup.find("h1") else None,
            "has_json_ld": bool(soup.find("script", type="application/ld+json")),
            "json_ld_types": [],
            "classes_with_ingredient": [],
            "classes_with_malzeme": [],
            "classes_with_step": [],
            "classes_with_yapilis": [],
            "total_li": len(soup.find_all("li")),
            "ol_li": len(soup.select("ol li")),
        }

        for el in soup.find_all(True):
            for c in el.get("class", []):
                cl = c.lower()
                if "ingredient" in cl and c not in info["classes_with_ingredient"]:
                    info["classes_with_ingredient"].append(c)
                if "malzeme" in cl and c not in info["classes_with_malzeme"]:
                    info["classes_with_malzeme"].append(c)
                if "step" in cl and c not in info["classes_with_step"]:
                    info["classes_with_step"].append(c)
                if any(k in cl for k in ["yapilis", "adim", "instruction", "direction"]):
                    if c not in info["classes_with_yapilis"]:
                        info["classes_with_yapilis"].append(c)

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            info["json_ld_types"].append(item.get("@type"))
                elif isinstance(data, dict):
                    if "@graph" in data:
                        info["json_ld_types"] = [i.get("@type") for i in data["@graph"] if isinstance(i, dict)]
                    else:
                        info["json_ld_types"].append(data.get("@type"))
            except Exception:
                pass

        return info
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Ana tarif endpoint ────────────────────────────────────────────────────────
@app.get("/tarif-getir")
def get_recipe(url: str):

    # 1. recipe-scrapers ile dene
    try:
        scraper = scrape_me(url, wild_mode=True)
        t, i, ins = scraper.title(), scraper.ingredients(), scraper.instructions()
        if t and i and ins:
            return {"baslik": t, "malzemeler": i, "yapis_adimlari": ins,
                    "resim_url": scraper.image(), "toplam_sure": scraper.total_time(), "site": scraper.host()}
    except Exception:
        pass

    # 2. HTML indir
    try:
        with httpx.Client(headers=HEADERS, timeout=25, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.text
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=400, detail=f"Sayfa indirilemedi: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bağlantı hatası: {str(e)}")

    # 3. scrape_html ile dene
    try:
        scraper = scrape_html(html, org_url=url)
        t, i, ins = scraper.title(), scraper.ingredients(), scraper.instructions()
        if t and i and ins:
            return {"baslik": t, "malzemeler": i, "yapis_adimlari": ins,
                    "resim_url": scraper.image(), "toplam_sure": scraper.total_time(), "site": scraper.host()}
    except Exception:
        pass

    # 4. BeautifulSoup manuel scraping
    return manual_scrape(html, url)


def extract_from_json_ld(soup):
    ingredients, instructions = [], ""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0]
            if not isinstance(data, dict):
                continue
            # @graph içinde Recipe ara
            if "@graph" in data:
                for item in data["@graph"]:
                    if isinstance(item, dict) and item.get("@type") == "Recipe":
                        data = item
                        break
            recipe_type = data.get("@type", "")
            if recipe_type not in ("Recipe", ["Recipe"]):
                continue

            raw_ing = data.get("recipeIngredient", [])
            if isinstance(raw_ing, list):
                ingredients = [str(i).strip() for i in raw_ing if str(i).strip()]

            raw_ins = data.get("recipeInstructions", "")
            if isinstance(raw_ins, list):
                steps = []
                for step in raw_ins:
                    if isinstance(step, dict):
                        text = step.get("text", step.get("name", "")).strip()
                    else:
                        text = str(step).strip()
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


def try_list_selectors(soup, selectors):
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


def try_step_selectors(soup, selectors):
    for sel in selectors:
        try:
            items = soup.select(sel)
            if len(items) >= 1:
                result = [el.get_text(strip=True) for el in items if len(el.get_text(strip=True)) > 10]
                if result:
                    return "\n".join(result)
        except Exception:
            continue
    return ""


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

    # JSON-LD
    ingredients, instructions = extract_from_json_ld(soup)

    # Malzeme selectors — nefisyemektarifleri dahil
    if not ingredients:
        ingredients = try_list_selectors(soup, [
            # nefisyemektarifleri.com
            ".recipe-ingre-list li",
            ".ingre-list li",
            ".ingredients-list li",
            ".recipe-ingredients li",
            ".recipe-ingredients-header ~ ul li",
            "[class*='ingredient'] li",
            "[class*='Ingredient'] li",
            "[class*='malzeme'] li",
            "[class*='Malzeme'] li",
            "[id*='ingredient'] li",
            "[id*='malzeme'] li",
            "[itemprop='recipeIngredient']",
            ".ingredients li",
            ".ingredient-list li",
            # Son çare: 3+ maddeli herhangi bir ul
        ])

    # Yapılış selectors
    if not instructions:
        instructions = try_step_selectors(soup, [
            # nefisyemektarifleri.com
            ".recipe-inst-list li",
            ".inst-list li",
            ".directions-list li",
            ".recipe-instructions li",
            ".recipe-instructions p",
            "[class*='instruction'] li",
            "[class*='instruction'] p",
            "[class*='Instruction'] li",
            "[class*='direction'] li",
            "[class*='Direction'] li",
            "[class*='step'] li",
            "[class*='step'] p",
            "[class*='Step'] li",
            "[class*='yapilis'] li",
            "[class*='yapilis'] p",
            "[class*='adim'] li",
            "[id*='instruction'] li",
            "[itemprop='recipeInstructions']",
            "ol li",
        ])

    if not title and not ingredients and not instructions:
        raise HTTPException(
            status_code=422,
            detail=f"Bu sayfadan tarif çıkarılamadı. Analiz için: /debug?url={url}"
        )

    return {
        "baslik":         title or "İsimsiz Tarif",
        "malzemeler":     ingredients,
        "yapis_adimlari": instructions,
        "resim_url":      image,
        "toplam_sure":    None,
        "site":           host,
    }
