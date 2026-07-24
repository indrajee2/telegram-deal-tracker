from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    DateTime
)
from sqlalchemy.sql import func
from sqlalchemy import ForeignKey
from sqlalchemy import UniqueConstraint
from Product_loader.database import Base


class Product(Base):
    __tablename__ = "products"
   
    __table_args__ = (
        UniqueConstraint(
            "platform",
            "external_id",
            name="uq_platform_external",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)

    # Product Identity
    platform = Column(String(20), nullable=False, index=True)
    external_id = Column(String(100), nullable=False, index=True)

    # Product Details
    brand = Column(String(255))
    product_name = Column(String, nullable=False)

    # Pricing
    current_price = Column(Float, nullable=False)
    lowest_price = Column(Float, nullable=False)
    mrp = Column(Float)
    discount_percent = Column(Float)

    # Rating
    rating = Column(Float)

    # Offers
    bank_offer = Column(String)
    coupon_discount = Column(Float, default=0)

    # URLs
    image_url = Column(String)
    product_url = Column(String)
    affiliate_url = Column(String)

    # Availability
    available = Column(Boolean, default=True)

    # Currency
    currency = Column(String(10), default="INR")

    # Timestamps
    first_seen_at = Column(DateTime(timezone=True), server_default=func.now())
    last_checked_at = Column(DateTime(timezone=True), server_default=func.now())
    last_updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now()
    )



class PriceHistory(Base):
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, index=True)

    product_id = Column(
        Integer,
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    price = Column(Float, nullable=False)
    mrp = Column(Float)
    discount_percent = Column(Float)

    recorded_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class DealsQueue(Base):
    __tablename__ = "deals_queue"

    id = Column(Integer, primary_key=True)

    product_id = Column(Integer, nullable=False, index=True)

    reason = Column(String(100))

    price = Column(Float)
    old_price = Column(Float)
    discount_percent = Column(Float)

    sent = Column(Boolean, default=False)

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )