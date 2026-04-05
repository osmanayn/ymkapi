from fastapi import FastAPI, HTTPException
from bs4 import BeautifulSoup
from recipe_scrapers import scrape_me, scrape_html
import re
import json
import cloudscraper
import httpx

app = FastAPI()

# cloudscraper — Cloudflare ve bot korumasını aşar
scraper_client = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'mobile': False,
    }
)

# Normal httpx headers (cloudscraper başarısız olursa yedek)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Referer": "https://www.google.com/",
    "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
}


@app.get("/")
def home():
    return {"mesaj": "Tarif API çalışıyor!"}


def fetch_html(url: str) -> str:
    """Önce cloudscraper, başarısız olursa httpx ile dene."""
    # 1. cloudscraper
    try:
        resp = scraper_client.get(url, timeout=25)
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass

    # 2. httpx yedek
    try:
        with httpx.Client(headers=HEADERS, timeout=25, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Sayfa indirilemedi: {str(e)}")


@app.get("/debug")
def debug_page(url: str):
    html = fetch_html(url)
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
        "ol_li_sample": [el.get_text(strip=True)[:60] for el in soup.select("ol li")][:5],
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


@app.get("/tarif-getir")
def get_recipe(url: str):

    # 1. recipe-scrapers direkt
    try:
        sc = scrape_me(url, wild_mode=True)
        t, i, ins = sc.title(), sc.ingredients(), sc.instructions()
        if t and i and ins:
            return out(t, i, ins, sc.image(), sc.total_time(), sc.host())
    except Exception:
        pass

    # 2. HTML indir (cloudscraper ile)
    html = fetch_html(url)

    # 3. scrape_html
    try:
        sc = scrape_html(html, org_url=url)
        t, i, ins = sc.title(), sc.ingredients(), sc.instructions()
        if t and i and ins:
            return out(t, i, ins, sc.image(), sc.total_time(), sc.host())
    except Exception:
        pass

    # 4. Manuel BeautifulSoup
    return manual_scrape(html, url)


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
    ingredients, instructions = extract_json_ld(soup)

    # Malzeme selectors
    if not ingredients:
        ingredients = try_list(soup, [
            "[itemprop='recipeIngredient']",
            "[class*='ingredient'] li",
            "[class*='Ingredient'] li",
            "[class*='malzeme'] li",
            "[class*='ingre'] li",
            "[id*='ingredient'] li",
            "[id*='malzeme'] li",
            ".ingredients li",
            ".ingredient-list li",
            ".recipe-ingredients li",
        ])

    # Yapılış selectors
    if not instructions:
        instructions = try_steps(soup, [
            "[itemprop='recipeInstructions']",
            "[class*='instruction'] li",
            "[class*='instruction'] p",
            "[class*='direction'] li",
            "[class*='step'] li",
            "[class*='step'] p",
            "[class*='yapilis'] li",
            "[class*='yapilis'] p",
            "[class*='adim'] li",
            "[id*='instruction'] li",
            ".recipe-instructions li",
            ".recipe-instructions p",
            "ol li",
        ])

    if not title and not ingredients and not instructions:
        raise HTTPException(
            status_code=422,
            detail=f"Tarif çıkarılamadı. Debug için: /debug?url={url}"
        )

    return out(title or "İsimsiz Tarif", ingredients, instructions, image, None, host)
