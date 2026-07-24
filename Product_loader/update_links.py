import os
from dotenv import load_dotenv
from Product_loader.ekaro import EKaro

load_dotenv()

token = os.getenv("EKARO_API_TOKEN")

ekaro = EKaro(token) if token else None


def get_affiliate_url(url: str) -> str:

    try:
        return ekaro.convert_url(url)
    except Exception:
        return url

