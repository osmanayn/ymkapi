from fastapi import FastAPI, HTTPException
from recipe_scrapers import scrape_me

app = FastAPI(title="Yemek Tarifi Çekme API")

@app.get("/")
def ana_sayfa():
    return {"durum": "Başarılı", "mesaj": "API sorunsuz bir şekilde ayakta!"}

@app.get("/api/tarif")
def tarif_getir(url: str):
    try:
        # wild_mode=False yaparak Render'daki yetki (Playwright) hatasını önlüyoruz
        scraper = scrape_me(url, wild_mode=False) 
        
        return {
            "baslik": scraper.title(),
            "malzemeler": scraper.ingredients(),
            "yapis_adimlari": scraper.instructions(),
            "resim_url": scraper.image(),
            "porsiyon": scraper.yields(),
            "toplam_sure": scraper.total_time(),
            "site": scraper.host()
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Tarif çekilemedi veya site engelledi: {str(e)}")
