from enum import StrEnum


class Capability(StrEnum):
    SEARCH = "search"
    DETAILS = "details"
    BARCODE = "barcode"
    ANALOGS = "analogs"
    CATEGORIES = "categories"
    PROMOTIONS = "promotions"
    PICKUP_POINTS = "pickup_points"
    SHARE_LINK = "share_link"
    ORDERS = "orders"
