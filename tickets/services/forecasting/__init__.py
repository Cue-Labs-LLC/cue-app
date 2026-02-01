"""
Ticket sales forecasting module.

This module provides tools for projecting ticket sales for upcoming events
based on historical sales patterns.
"""
from .sales_curve import SalesCurveCalculator
from .historical_aggregator import HistoricalAggregator
from .forecast_generator import ForecastGenerator, Projection, ProjectionPoint
from .models import SalesCurve, DailySales, BaselineCurveData, PaceMetrics
from .preview import generate_forecast_preview

__all__ = [
    'SalesCurveCalculator',
    'HistoricalAggregator',
    'ForecastGenerator',
    'Projection',
    'ProjectionPoint',
    'SalesCurve',
    'DailySales',
    'BaselineCurveData',
    'PaceMetrics',
    'generate_forecast_preview',
]
