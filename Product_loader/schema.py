from pydantic import BaseModel


class ProductCreate(BaseModel):
    platform: str
    brand: str
    product_name: str
    selling_price: float
    mrp: float
    discount: int
    wow_price: float
    rating: float

    bank_offer: str | None = None
    coupon_discount: float = 0

    affiliate_url: str
    product_url: str

    stock: bool
    currency: str = "INR"


class ProductResponse(ProductCreate):
    id: int

    class Config:
        from_attributes = True