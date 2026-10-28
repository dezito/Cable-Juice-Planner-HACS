"""Price levels and their colours."""
from __future__ import annotations


COLOR_THRESHOLDS = [
    ("price1", "#0064FF"),
    ("price2", "#0096FF"),
    ("price3", "#00B496"),
    ("price4", "#00FA50"),
    ("price5", "#00FF32"),
    ("price6", "#00FF00"),
    ("price7", "#FFFF00"),
    ("price8", "#FFA500"),
    ("price9", "#FF5000"),
    ("price10", "#C81919"),
    ("price11", "#C800C8"),
]


class PriceLevels:
    """Price levels and their colours."""

    @staticmethod
    def calculate_price_levels(prices):
        """ Calculates price levels based on the provided prices. """
        lowest_price = min(prices)
        highest_price = max(prices)
        mean_price = sum(prices) / len(prices)
        step_under = (mean_price - lowest_price) / 5
        step_over = (highest_price - mean_price) / 5

        return {
            "price1": lowest_price,
            "price2": mean_price - step_under * 4,
            "price3": mean_price - step_under * 3,
            "price4": mean_price - step_under * 2,
            "price5": mean_price - step_under * 1,
            "price6": mean_price,
            "price7": mean_price + step_over * 1,
            "price8": mean_price + step_over * 2,
            "price9": mean_price + step_over * 3,
            "price10": mean_price + step_over * 4,
            "price11": highest_price,
        }

    @staticmethod
    def get_color(price, price_levels):
        """ Gets the color based on the price and predefined thresholds. """
        for key, color in reversed(COLOR_THRESHOLDS):
            if price >= price_levels[key]:
                return color
        return "#FFFFFF"
