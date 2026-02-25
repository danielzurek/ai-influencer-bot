import asyncio
from datetime import datetime, timedelta
from app.database.session import AsyncSessionLocal
from app.database.models import User
from sqlalchemy.orm.attributes import flag_modified

async def make_me_expired_vip():
    # TU WPISZ SWÓJ TELEGRAM ID (musisz mieć już wysłaną jakąś wiadomość do bota)
    MY_TELEGRAM_ID = 8396876807  

    async with AsyncSessionLocal() as db:
        user = await db.get(User, MY_TELEGRAM_ID)
        
        if not user:
            print(f"❌ Nie znaleziono użytkownika o ID {MY_TELEGRAM_ID}. Napisz najpierw 'Hej' do bota!")
            return
            
        # 1. Cofamy datę wygaśnięcia o np. 5 dni do tyłu
        past_date = datetime.utcnow() - timedelta(days=5)
        user.subscription_expires_at = past_date
        
        # 2. Dodajemy flagę "vip_kicked", żeby auto-kicker z main.py nie próbował 
        #    Cię teraz w kółko wyrzucać z kanału podczas Twoich testów panelu
        user_info = dict(user.info) if user.info else {}
        user_info["vip_kicked"] = True
        user.info = user_info
        flag_modified(user, "info")
        
        await db.commit()
        print(f"✅ Sukces! Użytkownik {user.username or MY_TELEGRAM_ID} wygasł 5 dni temu.")
        print("Wejdź w zakładkę 'Expired VIPs' w panelu admina i przetestuj wysyłanie!")

if __name__ == "__main__":
    asyncio.run(make_me_expired_vip())