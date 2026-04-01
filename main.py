from fastapi import FastAPI, HTTPException
from recipe_scrapers import scrape_me

app = FastAPI()

@app.get("/")
def home():
    return {"mesaj": "Tarif API çalışıyor!"}

@app.get("/tarif-getir")
def get_recipe(url: str):
    try:
        # wild_mode=True diyerek desteklenmeyen sitelerde bile şansını denemesini sağlıyoruz
        scraper = scrape_me(url, wild_mode=True) 
        
        return {
            "baslik": scraper.title(),
            "malzemeler": scraper.ingredients(),
            "yapis_adimlari": scraper.instructions(),
            "resim_url": scraper.image(),
            "toplam_sure": scraper.total_time(),
            "site": scraper.host()
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Tarif çekilemedi: {str(e)}")