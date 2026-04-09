from fastapi import FastAPI, HTTPException
from bs4 import BeautifulSoup
from recipe_scrapers import scrape_me, scrape_html
import re
import json
import cloudscraper
import httpx

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


def make_amp_urls(url: str) -> list:
    """Verilen URL için olası AMP versiyonlarını döndürür."""
    url = url.rstrip("/")
    return [
        url + "?amp=1",
        url + "/amp/",
        url + "?amp",
        url.replace("www.", "amp."),
    ]


def fetch_amp(url: str) -> str | None:
    """AMP versiyonunu dene, başarısız olursa None döndür."""
    for amp_url in make_amp_urls(url):
        try:
            resp = scraper_client.get(amp_url, timeout=20)
            if resp.status_code == 200 and len(resp.text) > 1000:
                return resp.text
        except Exception:
            continue
        try:
            with httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True) as client:
                resp = client.get(amp_url)
                if resp.status_code == 200 and len(resp.text) > 1000:
                    return resp.text
        except Exception:
            continue
    return None


@app.get("/tarif-getir")
def get_recipe(url: str):
    host = re.sub(r"https?://(www\.)?", "", url).split("/")[0]

    # 1. recipe-scrapers direkt
    try:
        sc = scrape_me(url, wild_mode=True)
        t, i, ins = sc.title(), sc.ingredients(), sc.instructions()
        if t and i and ins:
            return out(t, i, ins, sc.image(), sc.total_time(), sc.host())
    except Exception:
        pass

    # 2. AMP versiyonunu dene (özellikle nefisyemektarifleri için)
    amp_html = fetch_amp(url)
    if amp_html:
        result = parse_html(amp_html, url)
        # Yapılış adımları AMP'den geldiyse kullan
        if result.get("yapis_adimlari"):
            return result

    # 3. Normal HTML indir
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
    result = parse_html(html, url)

    # 6. Hâlâ adım yoksa AMP'den malzemeleri normal HTML'den aldık,
    #    AMP'den adımları deneyelim (karışık)
    if not result.get("yapis_adimlari") and amp_html:
        amp_result = parse_html(amp_html, url)
        if amp_result.get("yapis_adimlari"):
            result["yapis_adimlari"] = amp_result["yapis_adimlari"]

    return result


@app.get("/debug")
def debug_page(url: str):
    """Sayfa yapısını analiz eder."""
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    info = {
        "title": soup.find("h1").get_text(strip=True) if soup.find("h1") else None,
        "has_json_ld": bool(soup.find("script", type="application/ld+json")),
        "json_ld_types": [],
        "classes_ingredient": [],
        "classes_step": [],
        "total_li": len(soup.find_all("li")),
        "amp_urls_tried": make_amp_urls(url),
    }
    for el in soup.find_all(True):
        for c in el.get("class", []):
            cl = c.lower()
            if any(k in cl for k in ["ingredient", "malzeme", "ingre"]) and c not in info["classes_ingredient"]:
                info["classes_ingredient"].append(c)
            if any(k in cl for k in ["step", "direction", "instruction", "yapilis", "adim"]) and c not in info["classes_step"]:
                info["classes_step"].append(c)
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

    # AMP deneme sonucu
    amp_html = fetch_amp(url)
    info["amp_found"] = amp_html is not None
    if amp_html:
        amp_soup = BeautifulSoup(amp_html, "html.parser")
        info["amp_has_json_ld"] = bool(amp_soup.find("script", type="application/ld+json"))
        info["amp_li_count"] = len(amp_soup.find_all("li"))
        info["amp_classes_step"] = []
        for el in amp_soup.find_all(True):
            for c in el.get("class", []):
                cl = c.lower()
                if any(k in cl for k in ["step", "direction", "instruction", "yapilis", "adim"]) and c not in info["amp_classes_step"]:
                    info["amp_classes_step"].append(c)

    return info


def parse_html(html: str, url: str) -> dict:
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
            "ul.recipe-materials li",
            "[itemprop='recipeIngredient']",
            "[class*='ingredient'] li",
            "[class*='malzeme'] li",
            "[class*='ingre'] li",
            "[id*='ingredient'] li",
            "[id*='malzeme'] li",
            ".ingredients li",
            ".recipe-ingredients li",
        ])

    # Yapılış selectors
    if not instructions:
        instructions = try_steps(soup, [
            ".recipe-directions li",
            ".recipe-directions p",
            ".directions li",
            ".directions p",
            "ol.steps li",
            ".recipe-steps li",
            ".recipe-steps p",
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
        raise HTTPException(status_code=422, detail=f"Tarif çıkarılamadı: {url}")

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


@app.get("/amp-jsonld")
def amp_jsonld(url: str):
    """AMP sayfasındaki tüm JSON-LD içeriğini döndürür."""
    amp_html = fetch_amp(url)
    if not amp_html:
        return {"error": "AMP sayfası bulunamadı"}
    soup = BeautifulSoup(amp_html, "html.parser")
    result = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            result.append(data)
        except Exception as e:
            result.append({"parse_error": str(e)})
    return {"json_ld_blocks": result}
