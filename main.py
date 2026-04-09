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

JS_SITES = ["nefisyemektarifleri.com"]


@app.get("/")
def home():
    return {"mesaj": "Tarif API çalışıyor!"}


def needs_js(url: str) -> bool:
    return any(s in url for s in JS_SITES)


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


def fetch_html_selenium(url: str) -> str:
    """Selenium ile JavaScript render edilmiş HTML döndürür."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from webdriver_manager.chrome import ChromeDriverManager
    import time

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )
    try:
        driver.get(url)
        # Sayfa tamamen yüklensin bekle
        time.sleep(4)
        # Tarif içeriği yüklensin
        try:
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "article.recipe-inner"))
            )
        except Exception:
            pass
        time.sleep(2)
        return driver.page_source
    finally:
        driver.quit()


@app.get("/tarif-getir")
def get_recipe(url: str):
    # 1. recipe-scrapers
    try:
        sc = scrape_me(url, wild_mode=True)
        t, i, ins = sc.title(), sc.ingredients(), sc.instructions()
        if t and i and ins:
            return out(t, i, ins, sc.image(), sc.total_time(), sc.host())
    except Exception:
        pass

    # 2. JS gerektiren siteler → Selenium
    if needs_js(url):
        try:
            html = fetch_html_selenium(url)
            result = parse_html(html, url)
            if result.get("yapis_adimlari"):
                return result
        except Exception as e:
            pass  # Selenium başarısız, normal yönteme geç

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

    # 5. Manuel
    return parse_html(html, url)


@app.get("/debug")
def debug_page(url: str):
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    info = {
        "title": soup.find("h1").get_text(strip=True) if soup.find("h1") else None,
        "has_json_ld": bool(soup.find("script", type="application/ld+json")),
        "classes_ingredient": [],
        "classes_step": [],
        "total_li": len(soup.find_all("li")),
    }
    for el in soup.find_all(True):
        for c in el.get("class", []):
            cl = c.lower()
            if any(k in cl for k in ["ingredient", "malzeme", "ingre"]) and c not in info["classes_ingredient"]:
                info["classes_ingredient"].append(c)
            if any(k in cl for k in ["step", "direction", "instruction", "yapilis", "adim"]) and c not in info["classes_step"]:
                info["classes_step"].append(c)
    return info


def parse_html(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    host = re.sub(r"https?://(www\.)?", "", url).split("/")[0]

    title = ""
    og = soup.find("meta", property="og:title")
    if og:
        title = og.get("content", "").strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)

    image = ""
    og_img = soup.find("meta", property="og:image")
    if og_img:
        image = og_img.get("content", "")

    ingredients, instructions = extract_json_ld(soup)

    if not ingredients:
        ingredients = try_list(soup, [
            "ul.recipe-materials li",
            "[itemprop='recipeIngredient']",
            "[class*='ingredient'] li",
            "[class*='malzeme'] li",
            ".ingredients li",
            ".recipe-ingredients li",
        ])

    if not instructions:
        instructions = try_steps(soup, [
            # nefisyemektarifleri JS sonrası
            ".recipe-directions li",
            ".recipe-directions p",
            ".directions li",
            "ol.steps li",
            ".recipe-steps li",
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
